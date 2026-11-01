"""包结构冒烟测试: 所有子包均可导入且版本号可解析."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import io
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [
    pytest.mark.package,
    pytest.mark.smoke,
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("打包与版本信息"),
    pytest.mark.story("包结构与版本"),
    pytest.mark.layer("unit"),
]

_SUBPACKAGES = (
    "archive_management.application",
    "archive_management.domain",
    "archive_management.infrastructure",
    "archive_management.services",
    "archive_management.ui",
)


def test_all_subpackages_importable() -> None:
    for name in _SUBPACKAGES:
        assert importlib.import_module(name) is not None


def test_package_version_available() -> None:
    from archive_management import __version__

    assert isinstance(__version__, str)
    assert len(__version__) > 0


def test_display_name_available() -> None:
    from archive_management import APP_DISPLAY_NAME

    assert APP_DISPLAY_NAME


def test_package_version_matches_source_version() -> None:
    import archive_management.packaging as pkg

    assert pkg.package_version() == pkg.__version__


_SPEC_PATH = (
    Path(__file__).resolve().parents[2] / "packaging" / "archive-management.spec"
)


def _upx_keywords() -> list[ast.keyword]:
    """取出 spec 里所有 `upx=` 关键字参数(单文件产物的那块 EXE)."""
    tree = ast.parse(_SPEC_PATH.read_text(encoding="utf-8"), filename=str(_SPEC_PATH))
    return [
        keyword
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "upx"
    ]


def test_the_spec_asks_for_upx_explicitly_off() -> None:
    """J-6 定案不压: 没装 UPX 时 `upx=True` 会被静默跳过, 看上去压了其实一次没压."""
    hint = "spec 的 upx= 必须显式 False: 没装 UPX 时 True 会被静默跳过(J-6 定案不压)"
    enabled = [
        keyword
        for keyword in _upx_keywords()
        if not (
            isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        )
    ]
    assert enabled == [], hint


def test_every_build_target_declares_its_upx_choice() -> None:
    hint = "单文件产物只有一块 EXE upx=: 新增构建目标时一起决定压不压"
    assert len(_upx_keywords()) == 1, hint


_ROOT = Path(__file__).resolve().parents[2]


def _build_names() -> tuple[str, int]:
    """解析 spec: 单文件产物的名字 / EXE 块的总数.

    产物是 `dist/<name>.exe`(onefile): 那块 `EXE` 把 binaries 与 datas 一起收进来。EXE 总数
    给"只产一份单文件"的守卫用(2026-10-31 起发布产物回到 onefile)。
    """
    tree = ast.parse(_SPEC_PATH.read_text(encoding="utf-8"), filename=str(_SPEC_PATH))
    exe_name = ""
    exe_calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "EXE":
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        name = keywords.get("name")
        hint = f"spec 的 {node.func.id} 必须写字面量 name="
        assert isinstance(name, ast.Constant), hint
        assert isinstance(name.value, str), hint
        exe_calls += 1
        exe_name = name.value
    assert exe_name, "spec 里没找到 EXE 的 name="
    return exe_name, exe_calls


def test_the_spec_builds_a_single_file_exe() -> None:
    """发布产物是**一份单文件 exe**(2026-10-31 定, 见 spec 末尾的说明).

    onefile 的判据不是"代码里写了 EXE": 而是"那块 EXE 把 binaries/datas 收进来、没有
    `exclude_binaries`", 并且 spec 里**没有** COLLECT(那是 onedir 的目录目标)。反过来,
    COLLECT 一回来就说明有人只改了 spec —— 冒烟清单、发布文件与文档都还指着单文件。
    """
    _, exe_calls = _build_names()
    assert exe_calls == 1, "发布产物是单文件: spec 里只该有一块 EXE"
    # 按 AST 判而不是搜原文: 注释里就写着「不带 `exclude_binaries`」, 拿原文子串会把自己喂红。
    tree = ast.parse(_SPEC_PATH.read_text(encoding="utf-8"), filename=str(_SPEC_PATH))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    exes = [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "EXE"
    ]
    assert exes, "spec 里没找到 EXE"
    hint = "单文件 EXE 不能带 exclude_binaries —— 那会退化成 onedir 的「目录+独立 exe」"
    assert all(keyword.arg != "exclude_binaries" for keyword in exes[0].keywords), hint
    assert [
        node
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "COLLECT"
    ] == [], "发布产物是单文件: spec 里不该再有 COLLECT"


def test_the_smoke_script_and_the_workflow_follow_the_spec_names() -> None:
    """产物名是一处改、多处跟: spec 的 name= → 冒烟脚本的产物名表 → 两个构建作业拷的文件."""
    exe_name, _ = _build_names()
    smoke = _load_smoke_module()
    # 表驱动(2026-11-01 起两平台): Windows 带后缀、Linux 不带, 键就是平台闸门放行的清单。
    expected_products = {"win32": f"{exe_name}.exe", "linux": exe_name}
    hint = (
        "spec 的 name= 与冒烟脚本的产物名表必须一致(改了 spec 就同步 PRODUCT_FILENAMES)"
    )
    assert expected_products == smoke.PRODUCT_FILENAMES, hint
    hint = "报告平台显示名与产物名表共用同一份平台清单, 两边键集合必须一致"
    assert set(smoke.REPORT_PLATFORMS) == set(smoke.PRODUCT_FILENAMES), hint
    workflow = (_ROOT / ".github" / "workflows" / "release-build.yml").read_text(
        encoding="utf-8"
    )
    assert f"release/{exe_name}.exe" in workflow, hint
    assert f"-C dist {exe_name}" in workflow, (
        "Linux 作业打进 tar 的就是 spec 的那个产物名"
    )
    assert f"dist/{exe_name}/" not in workflow, "发布作业不再收 onedir 目录"


def _smoke_source() -> str:
    return (_ROOT / "scripts" / "smoke_packaged.py").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    """取 `scripts/smoke_packaged.py` 里某个函数的源码(到下一个顶层 def 为止)."""
    return _smoke_source().split(f"def {name}", 1)[-1].split("\ndef ", 1)[0]


def _load_smoke_module() -> ModuleType:
    """按路径加载 ``scripts/smoke_packaged.py``(scripts 不是包, 与别的报道脚本同一手法)."""
    path = _ROOT / "scripts" / "smoke_packaged.py"
    spec = importlib.util.spec_from_file_location("smoke_packaged", path)
    assert spec is not None, f"加载不到 {path}"
    assert spec.loader is not None, f"{path} 没有 loader"
    module = importlib.util.module_from_spec(spec)
    sys.modules["smoke_packaged"] = module
    spec.loader.exec_module(module)
    return module


def test_the_artifact_has_a_cold_start_bound() -> None:
    """J-7①: 给了上限才能拦住"又往包里塞大件"(单文件产物要留出解包到 %TEMP% 的开销)."""
    limits = _load_smoke_module()._COLD_START_LIMITS_SECONDS
    hint = "产物要有冷启动上限(Popen → '界面启动' 落地)"
    assert set(limits) == {"onefile"}, hint
    assert all(limit > 0 for limit in limits.values()), "上限必须是正数"


def test_exactly_at_the_bound_is_not_a_failure() -> None:
    smoke = _load_smoke_module()
    limits = smoke._COLD_START_LIMITS_SECONDS
    for label, limit in limits.items():
        assert smoke.cold_start_problem(label, limit) is None, (
            f"{label} 恰好等于上限不判超"
        )
        reason = smoke.cold_start_problem(label, limit + 0.1)
        assert reason is not None, f"{label} 超限要给原因"
        assert "超过上限" in reason, f"{label}: {reason}"


def test_the_no_args_check_really_applies_the_bound() -> None:
    """只在常量里写个数不等于有闸门: 上限必须真接进无参数启动那条判据, 超了判红."""
    body = _function_body("check_no_args")
    assert "cold_start_problem(" in body, (
        "无参数启动那条判据必须调用 cold_start_problem"
    )
    after = body.split("if slow is not None:", 1)
    assert len(after) == 2, "上限没接进判据(只算了耗时, 没有比较)"
    tail = after[1][:160]
    assert "return False" in tail, f"超限要判红(return False), 不能只打印: {tail!r}"


def test_the_checks_that_kill_a_process_keep_its_temp_writes_local(
    tmp_path: Path,
) -> None:
    """起产物进程的判据都要把子进程的 TEMP/TMP 指进工作目录(J-7② 的纪律).

    这条纪律落下的原因是 onefile 的解包残渣: 杀进程树会把 `_MEI` 目录留在 %TEMP%
    (实测每份 ~75 MB; 2026-10-26 又咬到一次 ``_MEI000004cc2``)。onefile 已于 2026-10-28
    停产, 但"冒烟起过的进程, 临时文件归冒烟自己收"仍然成立 —— ① 无参数启动与 ⑤ 单实例
    闸门自己杀, ②③④ 限时到了由 ``_run_timed`` 杀, **每一条起产物的判据**都得走这里,
    否则一次冒烟就往用户的 %TEMP% 里丢临时文件。
    """
    smoke = _load_smoke_module()
    temp_dir = tmp_path / "temp"
    env = smoke._temp_env({"KEEP": "1"}, temp_dir)
    assert env["KEEP"] == "1", "别把父进程环境整份丢掉"
    assert env["TEMP"] == str(temp_dir), "TEMP 必须指进工作目录"
    assert env["TMP"] == str(temp_dir), "TMP 也要指过去(Windows 认两套拼写)"
    assert env["TMPDIR"] == str(temp_dir), (
        "TMPDIR 也要指过去(Linux 上 onefile 解到 $TMPDIR 下的 _MEI 目录)"
    )
    assert temp_dir.is_dir(), "指过去的目录要先建出来"
    for name in (
        "check_no_args",
        "check_version",
        "check_doctor",
        "check_gui_smoke",
        "check_single_instance",
    ):
        assert "_temp_env(" in _function_body(name), (
            f"{name} 会起产物进程(限时到了就杀), 必须走 _temp_env"
        )


def test_a_timed_out_command_reports_where_it_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时要自带现场: 子进程已经打出来的东西与应用日志末尾都要进失败消息.

    窗口化产物没有控制台, 杀掉之后什么都留不下 —— 没有这两行, 下一次 CI 红还得靠猜
    (2026-10-26 runner 上 doctor 卡 120s 那次就是这样)。
    """
    smoke = _load_smoke_module()
    killed: list[int] = []

    class FakeProcess:
        pid = 4321

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(cmd="doctor", timeout=timeout or 0)

    def fake_popen(args: list[str], **kwargs: object) -> FakeProcess:
        out = kwargs["stdout"]
        err = kwargs["stderr"]
        assert isinstance(out, io.BufferedWriter), "限时命令的 stdout 要落成文件"
        assert isinstance(err, io.BufferedWriter), "限时命令的 stderr 要落成文件"
        out.write("备份根  : X\n".encode())  # 模拟子进程已经打到这里
        out.flush()  # 真进程走 fd, 不用 flush; 替身写的是 Python 缓冲, 得自己冲
        err.write(b"Traceback: boom\n")
        err.flush()
        return FakeProcess()

    monkeypatch.setattr(smoke.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(smoke, "_kill_tree", killed.append)
    log = tmp_path / "root" / "logs" / "archive-management.log"
    log.parent.mkdir(parents=True)
    log.write_text("应用   : 存档管理工具\n", encoding="utf-8")

    with pytest.raises(TimeoutError) as caught:
        smoke._run_timed(
            tmp_path / "fake.exe",
            ["doctor"],
            timeout=5,
            evidence=tmp_path / "evidence",
            log=log,
        )

    message = str(caught.value)
    assert killed == [4321], f"超时了要杀整棵树, 实际: {killed}"
    assert "超过 5s 未退出" in message, message
    assert "备份根" in message, f"子进程输出要进消息: {message}"
    assert "Traceback: boom" in message, f"stderr 也要进消息: {message}"
    assert "应用   : 存档管理工具" in message, f"应用日志末尾也要进消息: {message}"


def test_the_sandbox_redirection_covers_both_platforms() -> None:
    """无参数启动的沙箱重定向按平台给: 两边各自"盖住 platformdirs 解析的全部入口".

    Windows 用 platformdirs 自带的 ``WIN_PD_OVERRIDE_*``; Linux 没有 testing 覆盖, 改盖
    XDG 约定本身 —— platformdirs 在 Linux 上把 config/data/cache/state 四个目录全按
    ``XDG_*_HOME`` 解析, 少盖一个, 无参数启动就会把日志/数据库写进真实用户目录(冒烟在
    CI 的 runner 上跑, 那等于污染构建机; 本地跑等于污染开发者自己的家目录)。
    """
    smoke = _load_smoke_module()
    overrides = smoke._SANDBOX_OVERRIDES
    assert set(overrides) == {"win32", "linux"}, (
        "沙箱重定向要按平台给(两平台的打包产物都进 CI), 键与 PRODUCT_FILENAMES 一致"
    )
    assert overrides["win32"] == (
        "WIN_PD_OVERRIDE_APPDATA",
        "WIN_PD_OVERRIDE_LOCAL_APPDATA",
    ), "Windows: 两个系统目录覆盖都要盖住(只盖一个会漏一半)"
    assert overrides["linux"] == (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ), "Linux: platformdirs 走的四个 XDG 入口一个不能少(日志在 state 里)"


def test_the_kill_tree_uses_the_platform_native_mechanism() -> None:
    """杀进程树按平台用各自的机制: Windows 的 taskkill 到不了 Linux, killpg 也要有人起会话.

    POSIX 侧是一对: 子进程由 ``start_new_session`` 起进**独立会话**(组 id = 自己的 pid),
    ``_kill_tree`` 才能 ``killpg`` 一次带走整组 —— 少了会话那一半, killpg 要么杀错组(把
    冒烟自己连同 runner 的进程一起带走), 要么 ProcessLookupError 空转。
    """
    source = _smoke_source()
    body = _function_body("_kill_tree")
    assert "taskkill" in body, "Windows 侧按进程树杀靠 taskkill /T /F"
    assert "killpg" in body, "POSIX 侧杀整组靠 killpg(单杀父进程会留子进程活口)"
    # 会话那一半: 起进程的两个入口都要带 start_new_session=_NEW_SESSION(常量在 POSIX 上开)。
    assert '_NEW_SESSION = sys.platform != "win32"' in source, (
        "POSIX 上要把子进程起进独立会话, 常量按平台翻转"
    )
    for name in ("_launch", "_run_timed"):
        assert "start_new_session=_NEW_SESSION" in _function_body(name), (
            f"{name} 起产物进程必须带 start_new_session(killpg 的前提)"
        )


def test_the_platform_gate_and_the_display_warning_are_wired() -> None:
    """平台闸门放行 PRODUCT_FILENAMES 里的平台; Linux 无显示时先提醒再照跑.

    闸门按表放行而不是写死 ``win32``: 将来加 macOS 打包时只改表, 闸门跟着走。无显示的
    提醒只该在 Linux 侧出现, 且 DISPLAY 与 WAYLAND_DISPLAY 任一在就算"有显示"(Wayland
    会话里 Tk 走 XWayland, 只有 DISPLAY 时同样能跑)。
    """
    body = _function_body("main")
    assert "sys.platform not in PRODUCT_FILENAMES" in body, (
        "平台闸门要按产物名表放行(不在表里的平台没有产物可冒)"
    )
    assert "_warn_about_missing_display()" in body, "闸门之后要接无显示的提醒"
    warning = _function_body("_warn_about_missing_display")
    assert 'sys.platform != "linux"' in warning, (
        "提醒只属于 Linux(Windows/macOS 不适用)"
    )
    assert "DISPLAY" in warning, "X11 会话认 DISPLAY"
    assert "WAYLAND_DISPLAY" in warning, "Wayland 会话常只有 WAYLAND_DISPLAY 在"
