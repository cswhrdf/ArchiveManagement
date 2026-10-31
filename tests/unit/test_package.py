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
    """产物名是一处改、三处跟: spec 的 name= → 冒烟脚本默认路径 → 发布作业拷的文件."""
    exe_name, _ = _build_names()
    smoke = (_ROOT / "scripts" / "smoke_packaged.py").read_text(encoding="utf-8")
    workflow = (_ROOT / ".github" / "workflows" / "release-build.yml").read_text(
        encoding="utf-8"
    )
    hint = "改了 spec 的产物名就要同步冒烟脚本与 release-build.yml 的路径"
    assert f"dist/{exe_name}.exe" in smoke, hint
    assert f"release/{exe_name}.exe" in workflow, hint
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
    assert env["TMP"] == str(temp_dir), "TMP 也要指过去(两套变量都得盖住)"
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
