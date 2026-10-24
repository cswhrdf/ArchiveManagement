"""包结构冒烟测试: 所有子包均可导入且版本号可解析."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

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
