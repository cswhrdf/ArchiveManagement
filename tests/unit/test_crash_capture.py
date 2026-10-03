"""失败现场留证(coredumpy dump + 界面截图)的守卫用例.

留证的价值全在"出问题时真的留下了东西": 文件名写错、dump 没生成、截图悄悄跳过、附件
没挂上当前用例 —— 这些都不会让测试变红, 只会让人在需要现场时发现两手空空。所以这里把
四件事钉死:

- **文件名**: 稳定、可读、跨平台安全(不能带上路径分隔符与冒号)、过长要截断且互不覆盖;
- **dump**: 深度为 0 时关闭、成功时写出可解析的文件、失败时只记一句说明而不改变用例结果;
- **截图**: 只碰登记过的窗口、未显示/未布局时给出原因、抓不到时降级成一句说明;
- **接线**: conftest 真的注册了参数与钩子, 且每个用例只留一次现场。
"""

from __future__ import annotations

import faulthandler
import gzip
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import allure
import pytest
from PIL import Image

import crash_capture

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("失败现场留证"),
    pytest.mark.layer("unit"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFTEST = _REPO_ROOT / "tests" / "conftest.py"
_GUI_SUPPORT = _REPO_ROOT / "tests" / "gui_support.py"
_GITIGNORE = _REPO_ROOT / ".gitignore"


class _Window:
    """窗口替身: 只实现留证用到的几何/标题接口(不建真窗口)."""

    def __init__(
        self,
        *,
        mapped: bool = True,
        size: tuple[int, int] = (320, 200),
        position: tuple[int, int] = (120, 120),
        title: str = "探针窗口",
    ) -> None:
        self._mapped = mapped
        self._size = size
        self._position = position
        self._title = title

    def winfo_ismapped(self) -> int:
        """是否已映射到屏幕."""
        return int(self._mapped)

    def winfo_width(self) -> int:
        """窗口宽(像素)."""
        return self._size[0]

    def winfo_height(self) -> int:
        """窗口高(像素)."""
        return self._size[1]

    def winfo_rootx(self) -> int:
        """窗口左上角横坐标."""
        return self._position[0]

    def winfo_rooty(self) -> int:
        """窗口左上角纵坐标."""
        return self._position[1]

    def title(self) -> str:
        """窗口标题."""
        return self._title


class _DeadWindow:
    """已销毁的窗口: 任何几何调用都抛 TclError(与真窗口一致)."""

    def __getattr__(self, name: str) -> Any:
        def boom(*_args: Any, **_kwargs: Any) -> Any:
            from tkinter import TclError

            raise TclError(f"窗口已销毁: {name}")

        return boom


class _BrokenWindow:
    """状态怪异的窗口: 抛的不是 TclError(例如驱动/封装层自己报错)."""

    def __getattr__(self, name: str) -> Any:
        if name == "title":
            return lambda: "怪异窗口"

        def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(f"窗口状态异常: {name}")

        return boom


class _Item:
    """用例替身: 留证只用到 nodeid 与 stash."""

    def __init__(self, node_id: str = "tests/unit/test_x.py::test_y") -> None:
        self.nodeid = node_id
        self.stash = pytest.Stash()


class _Call:
    """阶段替身: 留证只用到 when 与 excinfo."""

    def __init__(self, excinfo: Any, when: str = "call") -> None:
        self.when = when
        self.excinfo = excinfo


def _excinfo_for(exc: BaseException) -> Any:
    """造一个与 pytest 传给钩子的东西同形的 excinfo 替身(value + 真 traceback)."""
    return SimpleNamespace(value=exc, tb=exc.__traceback__)


def _failed_exception() -> BaseException:
    """在真实栈上抛一个异常并把它捕获出来(留证用的是真实的 traceback)."""

    def inner() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError) as captured:
        inner()
    return captured.value


def _summary_text(evidence: list[crash_capture.Evidence]) -> str:
    """取出摘要附件里的文本(它必须是第一条证据)."""
    assert evidence[0].body is not None
    return evidence[0].body.decode("utf-8")


# ------------------------------------------------------------------ 文件名


def test_dump_target_is_readable_and_path_safe(tmp_path: Path) -> None:
    """文件名能把用例认出来, 且不含路径分隔符/冒号(Windows 上会出事)."""
    target = crash_capture.dump_target(
        "tests/integration/test_gui.py::test_x[param/data:C]", directory=tmp_path
    )

    assert target.parent == tmp_path
    assert target.suffix == crash_capture.DUMP_EXTENSION
    assert "/" not in target.name
    assert "\\" not in target.name
    assert ":" not in target.name
    assert "test_gui.py" in target.name


def test_dump_target_truncates_long_ids_into_unique_names(tmp_path: Path) -> None:
    """过长的参数化 id 要截断(Windows 路径长度)且两条用例不能撞成同一个文件."""
    long_suffix = "x" * 400
    first = crash_capture.dump_target(
        f"tests/unit/test_x.py::test_a[{long_suffix}0]", directory=tmp_path
    )
    second = crash_capture.dump_target(
        f"tests/unit/test_x.py::test_a[{long_suffix}1]", directory=tmp_path
    )

    assert len(first.name) <= crash_capture.MAX_FILE_NAME + len(
        crash_capture.DUMP_EXTENSION
    )
    assert first != second


# ---------------------------------------------------------------------- dump


def test_deepest_frame_points_at_the_raise_site() -> None:
    """栈帧取最深一层: 异常真正抛出的位置才是现场."""
    exc = _failed_exception()

    frame = crash_capture.deepest_frame(exc.__traceback__)

    assert frame is not None
    assert frame.f_code.co_name == "inner"
    assert crash_capture.deepest_frame(None) is None


def test_exception_text_is_one_readable_line() -> None:
    """异常摘要只有一行, 不把整段 traceback 糊进描述里."""
    assert crash_capture.exception_text(_excinfo_for(ValueError("boom"))) == (
        "ValueError: boom"
    )
    assert crash_capture.exception_text(object()) == ""


def test_sqlite_error_details_name_the_failure_class(tmp_path: Path) -> None:
    """SQLite 的失败要连**错误分类**一起留下: 光有 OperationalError 分不清是锁、缺表还是文件不对.

    同一条消息文本会随平台/版本变(``file is not a database`` 与 ``unsupported file format``
    其实是同一类 NOTADB), 只有 errorname/errorcode 稳定 —— 2026-09-30 那次偶发就是靠它定类的。
    """
    junk = tmp_path / "junk.db"
    junk.write_text("这不是数据库", encoding="utf-8")
    connection = sqlite3.connect(junk)
    try:
        with pytest.raises(sqlite3.Error) as not_a_db:
            connection.execute("SELECT 1").fetchone()
    finally:
        connection.close()

    assert crash_capture.sqlite_error_details(_excinfo_for(not_a_db.value)) == (
        "SQLITE_NOTADB (26)"
    )
    # 不是 SQLite 异常时不留这一行(摘要里不该多出一个空字段)。
    assert crash_capture.sqlite_error_details(_excinfo_for(ValueError("boom"))) == ""
    assert crash_capture.sqlite_error_details(object()) == ""


def test_write_dump_is_off_when_the_depth_is_zero(tmp_path: Path) -> None:
    """--crash-dump-depth=0 等于关掉 dump: 不写文件, 只留一句说明."""
    path, note = crash_capture.write_dump(
        node_id="tests/unit/test_x.py::test_y",
        frame=None,
        description="",
        directory=tmp_path,
        depth=0,
    )

    assert path is None
    assert "已关闭" in note
    assert not list(tmp_path.iterdir())


def test_write_dump_writes_a_compressed_dump(tmp_path: Path) -> None:
    """dump 真的写出来了, 且是 coredumpy 的压缩格式(不是空文件/普通文本)."""
    exc = _failed_exception()
    frame = crash_capture.deepest_frame(exc.__traceback__)

    path, note = crash_capture.write_dump(
        node_id="tests/unit/test_x.py::test_y",
        frame=frame,
        description="探针",
        directory=tmp_path,
        depth=3,
    )

    assert path is not None
    assert path.is_file()
    assert "已生成" in note
    # coredumpy 把 dump 写成压缩流(gzip 魔数), 所以要交给 coredumpy load/peek 读;
    # 这里只确认它不是空文件、也不是被写成了文本。
    assert path.read_bytes()[:2] == b"\x1f\x8b"


def test_write_dump_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """留证失败只记一句说明: 它绝不能把原本的失败原因盖掉, 更不能让用例变红."""
    import coredumpy

    def boom(**_kwargs: Any) -> str:
        raise RuntimeError("dump 炸了")

    monkeypatch.setattr(coredumpy, "dump", boom)

    path, note = crash_capture.write_dump(
        node_id="tests/unit/test_x.py::test_y",
        frame=None,
        description="探针",
        directory=tmp_path,
        depth=3,
    )

    assert path is None
    assert "生成失败: RuntimeError: dump 炸了" in note


# -------------------------------------------------------------------- 截图


def test_window_box_reads_the_live_geometry() -> None:
    """窗口范围 = 左上角 + 尺寸(Tk 的 rootx/rooty 是客户区左上角)."""
    window = _Window(size=(320, 200), position=(120, 130))

    assert crash_capture.window_box(window) == (120, 130, 440, 330)


@pytest.mark.parametrize(
    "window",
    [
        _Window(mapped=False),
        _Window(size=(1, 1)),
        _Window(size=(320, 1)),
        _DeadWindow(),
    ],
)
def test_window_box_rejects_windows_without_a_frame(window: Any) -> None:
    """未映射/未布局/已销毁的窗口没有可截的画面, 必须报"没有"."""
    assert crash_capture.window_box(window) is None


def test_screenshots_capture_the_window_pixels() -> None:
    """注入替身抓屏器: 返回的 PNG 就是抓到的画面(尺寸与图一致)."""
    boxes: list[tuple[int, int, int, int]] = []

    def grabber(_window: Any, box: tuple[int, int, int, int]) -> Any:
        boxes.append(box)
        return Image.new("RGB", (box[2] - box[0], box[3] - box[1]), "red")

    shots = crash_capture.screenshots([_Window()], grabber=grabber)

    assert len(shots) == 1
    assert shots[0].title == "探针窗口"
    assert shots[0].png is not None
    assert boxes == [(120, 120, 440, 320)]
    assert "已抓取 320x200" in shots[0].note


def test_screenshots_explain_a_window_without_a_frame() -> None:
    """窗口没显示时不静默丢: 摘要里要写清为什么没有截图."""
    shots = crash_capture.screenshots([_Window(mapped=False)])

    assert [shot.png for shot in shots] == [None]
    assert "窗口未显示或已销毁" in shots[0].note


def test_screenshots_keep_the_reason_when_grabbing_fails() -> None:
    """抓屏失败(Linux 没 DISPLAY、macOS 没屏幕录制权限)只记原因, 不抛异常."""

    def grabber(_window: Any, _box: tuple[int, int, int, int]) -> Any:
        raise OSError("没有可用的显示")

    shots = crash_capture.screenshots([_Window()], grabber=grabber)

    assert shots[0].png is None
    assert "截图失败: OSError: 没有可用的显示" in shots[0].note


def test_screenshots_are_capped() -> None:
    """一个用例建了多个窗口时只截前几张(附件与报告体积都要有上限)."""
    apps = [
        _Window(title=f"窗口{index}")
        for index in range(crash_capture.MAX_SCREENSHOTS + 2)
    ]

    shots = crash_capture.screenshots(
        apps, grabber=lambda _w, box: Image.new("RGB", (4, 4), "blue")
    )

    assert len(shots) == crash_capture.MAX_SCREENSHOTS


# ------------------------------------------------------------------ 组装与接线


def test_failure_evidence_attaches_dump_and_screenshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次失败至少要有三样: 摘要、dump 文件、界面截图(名与类型都要稳定)."""
    exc = _failed_exception()
    monkeypatch.setattr(
        crash_capture, "_grab_screen", lambda _w, box: Image.new("RGB", (8, 6), "blue")
    )

    evidence = crash_capture.failure_evidence(
        node_id="tests/unit/test_x.py::test_y",
        phase="call",
        excinfo=_excinfo_for(exc),
        directory=tmp_path,
        depth=3,
        apps=[_Window()],
    )

    names = [item.name for item in evidence]
    assert names[0] == "失败现场摘要"
    assert any(name.startswith("崩溃现场 dump · ") for name in names)
    assert any(name.startswith("失败时界面截图 · 探针窗口") for name in names)
    dump = next(item for item in evidence if item.path is not None)
    assert dump.path is not None
    assert dump.path.parent == tmp_path
    assert dump.path.is_file()
    # dump 的媒体类型必须是 Allure **不认识**的那一类: 认识 text/* 时它会把整份
    # 上兆字节的 JSON 读进预览区渲染, 打开报告就卡死(用户实测反馈)。
    assert dump.attachment_type == crash_capture.DUMP_MEDIA_TYPE
    assert not str(dump.attachment_type).startswith(("text/", "image/"))
    summary = _summary_text(evidence)
    assert "tests/unit/test_x.py::test_y" in summary
    assert "ValueError: boom" in summary


def test_failure_evidence_records_the_sqlite_error_class(tmp_path: Path) -> None:
    """分类信息同时进**摘要附件**与**dump 描述** —— 本地不带 ``--alluredir`` 时后者是唯一的现场."""
    junk = tmp_path / "junk.db"
    junk.write_text("这不是数据库", encoding="utf-8")
    connection = sqlite3.connect(junk)
    try:
        with pytest.raises(sqlite3.Error) as not_a_db:
            connection.execute("SELECT 1").fetchone()
    finally:
        connection.close()

    evidence = crash_capture.failure_evidence(
        node_id="tests/unit/test_x.py::test_y",
        phase="call",
        excinfo=_excinfo_for(not_a_db.value),
        directory=tmp_path,
        depth=3,
        apps=[],
    )

    assert "SQLITE_NOTADB (26)" in _summary_text(evidence)
    dump = next(item for item in evidence if item.path is not None)
    assert dump.path is not None
    # dump 是压缩流: 解开后是 JSON, 描述字段里有这一行(用 gzip 而不是 coredumpy.load ——
    # 后者会直接进 pdb)。
    with gzip.open(dump.path, "rt", encoding="utf-8") as stream:
        assert "SQLITE_NOTADB (26)" in stream.read()


def test_failure_evidence_says_when_there_is_no_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有界面窗口的用例(单元测试)要明说"无现场可截", 而不是让人猜."""
    exc = _failed_exception()

    evidence = crash_capture.failure_evidence(
        node_id="tests/unit/test_x.py::test_y",
        phase="call",
        excinfo=_excinfo_for(exc),
        directory=tmp_path,
        depth=0,
        apps=[],
    )

    summary = _summary_text(evidence)
    assert "本用例没有创建界面窗口" in summary
    assert "未生成(已关闭" in summary
    assert [item.attachment_type for item in evidence] == [allure.attachment_type.TEXT]


def test_attach_sends_files_by_path_and_bodies_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """大文件(dump)按路径交给 allure, 小内容(截图)直接给字节, 都要带上名字与类型.

    按路径的那条还要带真实后缀(``dump``): allure 只在附件类型是枚举时才从枚举里推后缀,
    给字符串媒体类型时必须自己带, 否则存成 ``.attach``, 下载下来得先改名才能 ``coredumpy
    load``。
    """
    calls: list[tuple[str, str, Any]] = []

    class _Allure:
        """allure 替身: 只记录被调用的两种附件入口."""

        attachment_type = allure.attachment_type

        class _Attach:
            """``allure.attach`` 与它的 ``.file`` 变体."""

            @staticmethod
            def __call__(
                body: Any, name: str = "", attachment_type: Any = None
            ) -> None:
                calls.append(("body", name, (body, attachment_type)))

            @staticmethod
            def file(
                source: str,
                name: str = "",
                attachment_type: Any = None,
                extension: str | None = None,
            ) -> None:
                calls.append(("file", name, (source, attachment_type, extension)))

        attach = _Attach()

    monkeypatch.setattr(crash_capture, "allure", _Allure)

    crash_capture.attach(
        [
            crash_capture.Evidence(
                name="摘要",
                attachment_type=allure.attachment_type.TEXT,
                body=b"hello",
            ),
            crash_capture.Evidence(
                name="dump",
                attachment_type=crash_capture.DUMP_MEDIA_TYPE,
                path=Path("crash-dumps/x.dump"),
            ),
        ]
    )

    assert calls == [
        ("body", "摘要", (b"hello", allure.attachment_type.TEXT)),
        (
            "file",
            "dump",
            (str(Path("crash-dumps/x.dump")), crash_capture.DUMP_MEDIA_TYPE, "dump"),
        ),
    ]


def test_attach_failure_evidence_keeps_one_shot_per_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败后 teardown 常跟着再报一次错: 同一个用例只留一次现场."""
    attached: list[int] = []
    monkeypatch.setattr(
        crash_capture, "attach", lambda evidence: attached.append(len(evidence))
    )
    item = _Item()
    call = _Call(_failed_exception())

    first = crash_capture.attach_failure_evidence(
        cast(pytest.Item, item),
        cast(pytest.CallInfo[Any], call),
        directory=tmp_path,
        depth=0,
    )
    second = crash_capture.attach_failure_evidence(
        cast(pytest.Item, item),
        cast(pytest.CallInfo[Any], call),
        directory=tmp_path,
        depth=0,
    )

    assert first
    assert second == []
    assert attached == [len(first)]


def test_attach_failure_evidence_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """留证自己出错时只留一行日志: 原本的失败原因必须原样保留."""

    def boom(**_kwargs: Any) -> list[crash_capture.Evidence]:
        raise RuntimeError("留证炸了")

    monkeypatch.setattr(crash_capture, "failure_evidence", boom)

    evidence = crash_capture.attach_failure_evidence(
        cast(pytest.Item, _Item()),
        cast(pytest.CallInfo[Any], _Call(_failed_exception())),
        directory=tmp_path,
        depth=0,
    )

    assert evidence == []
    assert "留证失败, 已跳过: RuntimeError: 留证炸了" in capsys.readouterr().out


# -------------------------------------------------------------- 硬崩溃留证


def test_hard_crash_log_opens_a_real_file_for_faulthandler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """进程级崩溃的栈要落到 ``crash-dumps`` 下的文件里(CI 靠它留证).

    用替身而不是真调 ``faulthandler.enable``: 后者是**全局**开关, 一个用例翻完, 后面所有
    用例崩溃时就不再有那条栈了。

    ``fileno()`` 是这条的关键: faulthandler 只接受有真实文件描述符的对象(它直接往 fd 上
    写)。想把输出顺手抄给 stderr 的包装对象会在 ``enable`` 时 AttributeError —— 实测那个
    异常发生在 ``pytest_configure`` 里, 整个会话直接 INTERNALERROR, 一个用例都跑不了。
    """
    calls: list[dict[str, Any]] = []

    def fake_enable(*, file: Any, all_threads: bool = False) -> None:
        calls.append({"file": file, "all_threads": all_threads})

    monkeypatch.setattr(faulthandler, "enable", fake_enable)

    path = crash_capture.enable_hard_crash_log(tmp_path)

    assert path == tmp_path / crash_capture.CRASH_LOG_NAME
    assert path is not None
    assert path.is_file(), "日志文件要当场建出来, 否则 artifact 里什么都没有"
    assert calls[0]["all_threads"] is True, "段错误可能发生在任何一个线程里"
    handle = calls[0]["file"]
    assert isinstance(handle.fileno(), int), "faulthandler 要求句柄有 fileno"
    handle.write("探针\n")
    handle.flush()
    assert "探针" in path.read_text(encoding="utf-8"), "写入要真的到文件(行缓冲)"


def test_hard_crash_log_never_breaks_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """留证失败只打一行日志: 它绝不能把会话变成 INTERNALERROR."""

    def boom(*, file: Any, all_threads: bool = False) -> None:
        raise RuntimeError("faulthandler 用不了")

    monkeypatch.setattr(faulthandler, "enable", boom)

    assert crash_capture.enable_hard_crash_log(tmp_path) is None
    assert "[crash] 无法准备崩溃日志: RuntimeError: faulthandler 用不了" in (
        capsys.readouterr().out
    )


def test_conftest_registers_the_crash_options_and_hook() -> None:
    """接线守卫: 参数与钩子被摘掉时, 留证会静默消失(用例不会因此变红)."""
    text = _CONFTEST.read_text(encoding="utf-8")

    assert "--crash-dump-dir" in text
    assert "--crash-dump-depth" in text
    assert "def pytest_runtest_makereport" in text
    assert "crash_capture.attach_failure_evidence(" in text
    assert "wrapper=True" in text
    # 硬崩溃(SIGSEGV/SIGABRT) 走的是另一条路: coredumpy 等不到那一刻, 只能靠会话一开始就
    # 接上的 faulthandler 把栈写进文件。
    assert "def pytest_configure" in text
    assert "crash_capture.enable_hard_crash_log(" in text


def test_gui_support_exposes_the_live_windows() -> None:
    """截图取自窗口登记表: 这个入口没了, 界面截图就永远为空."""
    text = _GUI_SUPPORT.read_text(encoding="utf-8")

    assert "def live_apps()" in text
    assert "live_apps()" in _CONFTEST.read_text(encoding="utf-8")


def test_crash_dumps_are_not_committed() -> None:
    """现场文件是本地产物: 必须被忽略, 免得哪天被顺手提交上去."""
    assert "crash-dumps/" in _GITIGNORE.read_text(encoding="utf-8")
