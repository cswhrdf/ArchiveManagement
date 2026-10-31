"""超时留证(``tests/timeout_guard.py``)的单元测试.

要守住的是两条互相拉扯的契约: **退出码说了算**(超时必须以 1 结束, 任何留证动作都不许
把它变成"挂在那里"), 以及**留证不许反过来打断留证**(某一步失败只是结论里的一行说明)。
所以这里的用例大多在验证"坏掉的时候依然按约定走"。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import crash_capture
import timeout_guard

pytestmark = [
    pytest.mark.critical,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("超时留证"),
    pytest.mark.layer("unit"),
]


class _FakeController:
    """假装是 pytest-cov 的 ``CovController``(只需要 ``finish`` 与数据文件路径)."""

    def __init__(self, *, explode: bool = False) -> None:
        self.finished = 0
        self._explode = explode
        self.cov = SimpleNamespace(
            config=SimpleNamespace(data_file=".coverage.shard-2")
        )

    def finish(self) -> None:
        self.finished += 1
        if self._explode:
            raise RuntimeError("磁盘满了")


class _FakeConfig:
    """够 ``timeout_guard`` 用的假配置: 两个 dump 参数 + alluredir + cov 插件.

    选项名与插件名都按**真实**的那些写(pytest-cov 把持有 ``cov_controller`` 的插件注册在
    私有名 ``_cov`` 上; allure-pytest 把 ``--alluredir`` 的 ``dest`` 定成
    ``allure_report_dir``)—— 假配置写得比现实宽松就会盖住这两个坑。
    """

    def __init__(
        self, root: Path, controller: Any = None, *, plugin_name: str = "_cov"
    ) -> None:
        self._root = root
        self._plugins = (
            {}
            if controller is None
            else {plugin_name: SimpleNamespace(cov_controller=controller)}
        )
        self.pluginmanager = SimpleNamespace(
            hasplugin=lambda name: name in self._plugins,
            getplugin=lambda name: self._plugins.get(name),
            get_plugins=lambda: list(self._plugins.values()),
        )

    def getoption(self, name: str, default: Any = None) -> Any:
        return {
            "--crash-dump-dir": self._root / "dumps",
            "--crash-dump-depth": 5,
            "allure_report_dir": self._root / "allure-results",
        }.get(name, default)


class _BrokenConfig:
    """什么都拿不到的配置: 留证每一步都会失败, 正好验证"失败也只是说明"."""

    def getoption(self, name: str, default: Any = None) -> Any:
        raise RuntimeError(f"没有这个参数: {name}")


def _item(
    root: Path, controller: Any = None, *, broken: bool = False, name: str = "_cov"
) -> Any:
    config = (
        _BrokenConfig() if broken else _FakeConfig(root, controller, plugin_name=name)
    )
    return SimpleNamespace(nodeid="tests/unit/test_x.py::test_y", config=config)


def _fake_dump(monkeypatch: pytest.MonkeyPatch, root: Path) -> dict[str, Any]:
    """把 ``crash_capture.write_dump`` 换成假实现, 顺便记下它收到的参数.

    真去跑 coredumpy 要 dump 整个 pytest 栈, 而且 coredumpy 自己有 20s 的兜底超时 ——
    单元测试不该为此变慢、更不该变得不确定。
    """
    seen: dict[str, Any] = {}

    def write_dump(**kwargs: Any) -> tuple[Path, str]:
        seen.update(kwargs)
        target = root / "dumps" / "sample.dump"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not really a dump")
        return target, "已生成(19 B)"

    monkeypatch.setattr(crash_capture, "write_dump", write_dump)
    return seen


def test_the_timer_is_left_alone_in_signal_mode() -> None:
    """``signal`` 模式不接管: 它抛异常, 收尾路径本来就完整(覆盖率与留证都不缺)."""
    settings = SimpleNamespace(method="signal", timeout=60)

    assert timeout_guard.set_timer(_item(Path()), settings) is None


def _guard_timer(item: Any) -> list[threading.Thread]:
    """按名字找那条计时器线程.

    **必须按名字找**: 当前这个用例自己头上也挂着一条(conftest 里的钩子会给每个用例
    都装一条), 名字里带 nodeid 才能把两者分开。
    """
    name = f"timeout_guard {item.nodeid}"
    return [t for t in threading.enumerate() if t.name == name]


def test_the_timer_takes_over_thread_mode_and_can_be_cancelled() -> None:
    """``thread`` 模式接管, 且留下的取消钩子真能撤销计时器(否则用例结束后还会炸).

    这是与 pytest-timeout 的接口契约: 它撤计时器时只调 ``item.cancel_timeout``。
    """
    item = _item(Path())
    settings = SimpleNamespace(
        method="thread", timeout=60, disable_debugger_detection=True
    )

    assert timeout_guard.set_timer(item, settings) is True
    assert len(_guard_timer(item)) == 1

    assert timeout_guard.cancel_timer(item) is True
    assert not [t for t in _guard_timer(item) if t.is_alive()]


def test_cancelling_without_a_timer_is_harmless() -> None:
    """没装计时器时取消也不许炸(比如被 ``--timeout=0`` 关掉的那一轮)."""
    assert timeout_guard.cancel_timer(_item(Path())) is True


def _block_here(entered: threading.Event, release: threading.Event) -> None:
    """主线程在这行“卡住”: 给下面那个用例当一个看得见的现场."""
    entered.set()
    release.wait(5)


def _call_chain(frame: Any) -> list[str]:
    """从最内层帧往外走, 列出函数名 —— coredumpy 拿到的就是这条链."""
    names: list[str] = []
    while frame is not None:
        names.append(frame.f_code.co_name)
        frame = frame.f_back
    return names


def test_the_dump_captures_the_blocked_main_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """在别的线程里抓 dump 时, 取的是**主线程**那条卡住的调用链.

    超时留证线程干的正是这件事: 真正卡住的是主线程(等锁/等 IO/死循环都在那里), 抓
    抓取者自己的那一帧毫无意义。这里验证的是链上有“卡住的那个函数”, 没有“抓取函数”。
    """
    seen = _fake_dump(monkeypatch, tmp_path)
    entered, release = threading.Event(), threading.Event()

    def grab() -> None:
        entered.wait(5)
        timeout_guard.capture_dump(_item(tmp_path), "head")
        release.set()

    grabber = threading.Thread(target=grab, name="dump-grabber")
    grabber.start()
    _block_here(entered, release)
    grabber.join()

    chain = _call_chain(seen["frame"])
    assert "_block_here" in chain
    assert "grab" not in chain


def test_a_failing_dump_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """抓 dump 炸了也不能往外抛: 排在它后面的步骤(写 Allure 结论)还得继续跑."""

    def boom(**_: Any) -> tuple[Path | None, str]:
        raise RuntimeError("coredumpy 挂了")

    monkeypatch.setattr(crash_capture, "write_dump", boom)

    dump, note = timeout_guard.capture_dump(_item(tmp_path), "head")

    assert dump is None
    assert "生成失败" in note
    assert "RuntimeError" in note


def test_coverage_is_saved_before_the_process_is_killed() -> None:
    """覆盖率要**提前**存: ``os._exit`` 跳过 atexit, 那一片的数据否则会整个消失."""
    controller = _FakeController()

    note = timeout_guard.save_coverage(_item(Path(), controller))

    assert controller.finished == 1
    assert "已保存" in note
    assert ".coverage.shard-2" in note


def test_coverage_is_found_even_if_the_plugin_is_renamed() -> None:
    """插件名是私有的 ``_cov``: 万一以后改了名, 兜底查找也得能把它找出来."""
    controller = _FakeController()

    note = timeout_guard.save_coverage(_item(Path(), controller, name="cov-renamed"))

    assert controller.finished == 1
    assert "已保存" in note


def test_a_missing_coverage_plugin_is_reported_not_raised() -> None:
    """没开覆盖率(pytest-cov 不在)时给一句说明, 不是异常."""
    assert "未启用" in timeout_guard.save_coverage(_item(Path()))


def test_a_failing_coverage_save_is_reported_not_raised() -> None:
    """存覆盖率失败也要说清楚原因 —— 否则报告里"缺片"又变成一个谜."""
    controller = _FakeController(explode=True)

    note = timeout_guard.save_coverage(_item(Path(), controller))

    assert controller.finished == 1
    assert "保存失败" in note
    assert "磁盘满了" in note


class _FakeCaptureManager:
    """假装是 pytest 的 capturemanager: 只关心"有没有先挂起捕获再读走内容"."""

    def __init__(self, out: str, err: str) -> None:
        self.stdout = out
        self.stderr = err
        self.suspended = False

    def suspend_global_capture(self, _item: Any) -> None:
        self.suspended = True

    def read_global_capture(self) -> tuple[str, str]:
        return self.stdout, self.stderr


def _with_capture_manager(item: Any, capman: Any) -> Any:
    """把假捕获管理器挂上去(其余插件名一律返回 None)."""
    item.config.pluginmanager.getplugin = lambda name: (
        capman if name == "capturemanager" else None
    )
    return item


def test_captured_output_is_dumped_before_exiting(tmp_path: Path) -> None:
    """卡死之前程序自己打的东西也要倒出来: 它本来躺在 pytest 捕获里, 而 ``os._exit`` 让收尾不跑."""
    capman = _FakeCaptureManager("测试自己打的 stdout", "这里是一段错误输出")
    item = _with_capture_manager(_item(tmp_path), capman)
    lines: list[str] = []

    timeout_guard.dump_captured_output(item, lines.append)

    text = "\n".join(lines)
    assert capman.suspended is True
    assert "测试自己打的 stdout" in text
    assert "这里是一段错误输出" in text


def test_a_broken_capture_read_is_reported_not_raised(tmp_path: Path) -> None:
    """读捕获失败也只是结论里的一行说明 —— 后面还有覆盖率与 dump 要写."""

    class _Boom(_FakeCaptureManager):
        def read_global_capture(self) -> tuple[str, str]:
            raise RuntimeError("捕获坏了")

    item = _with_capture_manager(_item(tmp_path), _Boom("", ""))
    lines: list[str] = []

    timeout_guard.dump_captured_output(item, lines.append)

    assert any("倒出捕获输出失败" in line for line in lines)


def test_without_a_capture_manager_nothing_is_said(tmp_path: Path) -> None:
    """没启用捕获时不该在日志里刷一句废话."""
    lines: list[str] = []

    timeout_guard.dump_captured_output(_item(tmp_path), lines.append)

    assert lines == []


def test_thread_stacks_lists_every_live_thread() -> None:
    """附件里的线程栈要认得出是谁: 每条栈前面得写清线程名与 id."""
    text = timeout_guard.thread_stacks()

    assert threading.main_thread().name in text
    assert "thread_stacks" in text  # 自己的那一帧也在里面


def test_preserve_scene_writes_the_evidence_and_a_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """完整一次留证: 覆盖率存盘 + dump 落盘 + 一条 ``broken`` 结论(带 3 个附件)."""
    _fake_dump(monkeypatch, tmp_path)
    controller = _FakeController()
    item = _item(tmp_path, controller)

    timeout_guard.preserve_scene(item, SimpleNamespace(timeout=60))

    assert controller.finished == 1
    results = sorted((tmp_path / "allure-results").glob("*-result.json"))
    assert len(results) == 1
    payload = json.loads(results[0].read_text(encoding="utf-8"))
    assert payload["status"] == "broken"
    assert "超过 60s" in payload["statusDetails"]["message"]
    labels = {label["name"]: label["value"] for label in payload["labels"]}
    assert labels["feature"] == timeout_guard.FEATURE
    assert labels["story"] == item.nodeid
    assert labels["severity"] == "critical"
    assert labels["env"] in {"Windows", "Linux", "macOS"}
    # 分类标签不能乱写: 那条"质量门禁必须全绿"的规则只认 pytest 的结果
    assert "testCategory" not in labels
    assert "framework" not in labels
    names = [attachment["name"] for attachment in payload["attachments"]]
    assert "threads.txt" in names
    assert "notes.txt" in names
    dumps = [item for item in payload["attachments"] if item["name"].endswith(".dump")]
    assert len(dumps) == 1
    assert dumps[0]["type"] == crash_capture.DUMP_MEDIA_TYPE
    assert "coredumpy load" in payload["description"]


def test_a_huge_dump_is_kept_out_of_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """过大的 dump 只留落点不挂附件: 报告是要上传下载的产物, 不能塞进上兆字节."""
    monkeypatch.setattr(timeout_guard, "MAX_ATTACHMENT_BYTES", 8)
    seen = _fake_dump(monkeypatch, tmp_path)

    timeout_guard.preserve_scene(_item(tmp_path), SimpleNamespace(timeout=60))

    assert seen  # dump 确实生成了, 只是没进报告
    result = next((tmp_path / "allure-results").glob("*-result.json"))
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert [attachment["name"] for attachment in payload["attachments"]] == [
        "threads.txt",
        "notes.txt",
    ]


def test_preserve_scene_survives_a_completely_broken_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """环境彻底坏掉时留证仍不许抛异常 —— 它是在"已经出错"的路上跑的."""
    seen = _fake_dump(monkeypatch, tmp_path)

    timeout_guard.preserve_scene(
        _item(tmp_path, broken=True), SimpleNamespace(timeout=60)
    )

    assert seen == {}  # 连 dump 都没走到, 但函数本身正常返回了


def test_on_timeout_always_exits_with_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """契约: 留证完照样以 1 退出 —— 超时意味着这一片已经不可信."""
    exits: list[int] = []
    monkeypatch.setattr(timeout_guard, "os", SimpleNamespace(_exit=exits.append))
    item = _item(tmp_path)

    timeout_guard.on_timeout(item, SimpleNamespace(timeout=60))

    assert exits == [1]


def test_on_timeout_exits_even_when_preserving_explodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最坏情况: 留证自己炸了 —— 更得退出, 绝不能挂在那里不动."""
    exits: list[int] = []
    monkeypatch.setattr(timeout_guard, "os", SimpleNamespace(_exit=exits.append))

    def boom(*_: Any) -> None:
        raise MemoryError("连打印都打不出来了")

    monkeypatch.setattr(timeout_guard, "preserve_scene", boom)

    timeout_guard.on_timeout(_item(Path()), SimpleNamespace(timeout=60))

    assert exits == [1]


def test_the_hooks_are_wired_into_conftest() -> None:
    """钩子必须真挂在 conftest 上 —— 否则这个模块只是个摆设."""
    source = (Path(__file__).parents[1] / "conftest.py").read_text(encoding="utf-8")

    assert "pytest_timeout_set_timer" in source
    assert "pytest_timeout_cancel_timer" in source
    assert "timeout_guard.set_timer" in source
    assert "timeout_guard.cancel_timer" in source


def test_tests_keep_running_with_the_thread_timeout_method() -> None:
    """整套留证只在 ``thread`` 模式下生效, 而跑测试的方式必须还是它.

    ``signal`` 模式的超时是抛异常, 走正常收尾(覆盖率与 ``pytest_runtest_makereport``
    都在), 用不着接管。但只要方式被换成 ``signal``, 这里的 ``set_timer`` 就会一直返回
    ``None``: "超时后什么都没有"的老问题会**静默**回来 —— 所以钉住它。
    """
    text = (Path(__file__).parents[2] / "pyproject.toml").read_text(encoding="utf-8")

    assert '"--timeout-method=thread"' in text
