"""包结构冒烟测试: 所有子包均可导入且版本号可解析."""

from __future__ import annotations

import ast
import importlib
import importlib.util
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
    """取出 spec 里所有 `upx=` 关键字参数(两块 EXE + COLLECT)."""
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
    hint = "两块 EXE 与 COLLECT 共三处 upx=: 新增构建目标时一起决定压不压"
    assert len(_upx_keywords()) == 3, hint


_ROOT = Path(__file__).resolve().parents[2]


def _build_names() -> tuple[str, str, str]:
    """从 spec 的 `name=` 读出三个名字: onedir 目录 / 目录里的 exe / onefile 的 exe.

    目录与文件可以同名(PyInstaller 产出 `dist/<目录>/` 与 `dist/<文件名>.exe`), 所以这里
    分开取: `COLLECT` 的是目录名, `exclude_binaries=True` 那块 `EXE` 是目录里的 exe, 另一块
    `EXE` 就是 onefile.
    """
    tree = ast.parse(_SPEC_PATH.read_text(encoding="utf-8"), filename=str(_SPEC_PATH))
    onedir_exe = folder = onefile = ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in {"EXE", "COLLECT"}:
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        name = keywords.get("name")
        hint = f"spec 的 {node.func.id} 必须写字面量 name="
        assert isinstance(name, ast.Constant), hint
        assert isinstance(name.value, str), hint
        excluded = keywords.get("exclude_binaries")
        if node.func.id == "COLLECT":
            folder = name.value
        elif isinstance(excluded, ast.Constant) and excluded.value is True:
            onedir_exe = name.value
        else:
            onefile = name.value
    missing = [
        label
        for label, value in (
            ("onedir", onedir_exe),
            ("目录", folder),
            ("onefile", onefile),
        )
        if not value
    ]
    assert missing == [], f"spec 里没找到这些构建目标: {missing}"
    return onedir_exe, folder, onefile


def test_the_smoke_script_and_the_workflow_follow_the_spec_names() -> None:
    """产物改名是一处改、三处跟: spec 的 name= → 冒烟脚本默认路径 → 发布作业的拷贝来源."""
    onedir_exe, folder, onefile = _build_names()
    smoke = (_ROOT / "scripts" / "smoke_packaged.py").read_text(encoding="utf-8")
    workflow = (_ROOT / ".github" / "workflows" / "release-build.yml").read_text(
        encoding="utf-8"
    )
    hint = "改了 spec 的产物名就要同步冒烟脚本与 release-build.yml 的路径"
    assert f"dist/{folder}/{onedir_exe}.exe" in smoke, hint
    assert f'"dist/{onefile}.exe"' in smoke, hint
    assert f'"dist/{onefile}.exe"' in workflow, hint


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


def test_both_artifacts_have_a_cold_start_bound() -> None:
    """J-7①: onefile 的慢是解包开销买的, 给了上限才能拦住"又往包里塞大件"."""
    limits = _load_smoke_module()._COLD_START_LIMITS_SECONDS
    hint = "两份产物都要有冷启动上限(Popen → '界面启动' 落地)"
    assert set(limits) == {"onedir", "onefile"}, hint
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
    assert limits["onefile"] > limits["onedir"], "onefile 要解包, 上限不该比 onedir 紧"


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


def test_the_checks_that_kill_a_process_contain_its_unpack_dir(
    tmp_path: Path,
) -> None:
    """J-7②: 杀进程树会把 onefile 的 `_MEI` 解包目录留在 %TEMP%(实测每份 ~75 MB).

    ① 无参数启动与 ⑤ 单实例闸门都会杀, 所以这两处的 TEMP/TMP 必须指进工作目录 ——
    否则一次冒烟就往用户的 %TEMP% 里丢 150 MB(实测 16 → 18 个、+150.7 MB)。
    """
    smoke = _load_smoke_module()
    temp_dir = tmp_path / "temp"
    env = smoke._temp_env({"KEEP": "1"}, temp_dir)
    assert env["KEEP"] == "1", "别把父进程环境整份丢掉"
    assert env["TEMP"] == str(temp_dir), "TEMP 必须指进工作目录"
    assert env["TMP"] == str(temp_dir), "TMP 也要指过去(两套变量都得盖住)"
    assert temp_dir.is_dir(), "指过去的目录要先建出来"
    for name in ("check_no_args", "check_single_instance"):
        assert "_temp_env(" in _function_body(name), (
            f"{name} 会杀进程, 必须走 _temp_env"
        )


def test_mei_dirs_only_picks_the_unpack_directories(tmp_path: Path) -> None:
    (tmp_path / "_MEI12345").mkdir()
    (tmp_path / "_MEIabc").mkdir()
    (tmp_path / "am-smoke-x").mkdir()
    (tmp_path / "_MEIfile").write_text("x", encoding="utf-8")
    found = {path.name for path in _load_smoke_module()._mei_dirs(tmp_path)}
    assert found == {"_MEI12345", "_MEIabc"}, f"只认 _MEI 目录, 多认了: {found}"
