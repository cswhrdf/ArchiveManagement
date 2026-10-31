"""
发布定版脚本: 三档判定、tag 挑选与排序、"不写文件"这条纪律、main 的输出契约、与 release-build.yml 的版本抽取一致。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ci_workflow
from report_support import _REPO_ROOT, _load_script

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("发布版本自动化"),
    pytest.mark.layer("unit"),
]


def _packaging_py() -> Path:
    """真实 packaging.py 的路径(守卫要拿它对"两处版本抽取"做一致性核对)."""
    return _REPO_ROOT / "src" / "archive_management" / "packaging.py"


def _write_packaging(path: Path, version: str) -> None:
    """造一份最小但形态真实的 packaging.py 片段(判定只读 __version__ 那一行)."""
    path.write_text(
        f'APP_NAME = "ArchiveManagement"\n__version__ = "{version}"\n',
        encoding="utf-8",
    )


def test_the_first_release_ships_the_current_version() -> None:
    """仓库还没发过版(没有 v*.*.* 的 tag)时: 首版直发当前版本号, 不递增."""
    script = _load_script("release_bump")
    decision = script.decide("0.0.1", None)
    assert decision.bump == "none", "首版不需要 bump: v0.0.1 还没发过"
    assert decision.version == "0.0.1"
    assert decision.reason == "first-release"


def test_a_manually_bumped_version_wins() -> None:
    """最近发过的 != 当前: 人在 PR 里改过版本号(大版本的声明通道), 按人写的发."""
    script = _load_script("release_bump")
    decision = script.decide("0.1.0", "0.0.7")
    assert decision.bump == "none", "人已定档, 脚本不许再叠一个 patch"
    assert decision.version == "0.1.0"
    assert decision.reason == "manual"


def test_an_unchanged_version_needs_a_human_bump() -> None:
    """版本号没动时不自己动手: 给出建议的下一版号(0.0.9 -> 0.0.10, 不受字典序影响).

    2026-11-01 起 `main` 有分支保护, 合入之后谁都推不进去 —— 所以"自动加一位"改成了
    "在 PR 上提醒人加", 脚本只负责把建议号算出来。
    """
    script = _load_script("release_bump")
    decision = script.decide("0.0.9", "0.0.9")
    assert decision.bump == "needs-bump", "不再有 patch 档: 脚本不写文件"
    assert decision.version == "0.0.10", "version 是**建议**值, 由人写进 PR"
    assert decision.previous == "0.0.9"
    assert decision.reason == "needs-bump"


def test_the_latest_release_is_picked_numerically_among_v_tags() -> None:
    """挑"最近发过的版本": 只认 v 前缀三段数字, 逐段整数比较."""
    script = _load_script("release_bump")
    tags = ["v0.0.9", "v0.0.10", "v1", "v0.1", "v0.0.2-rc1", "release-2026"]
    assert script.latest_released(tags) == "0.0.10"
    # 只有杂形态 tag 时等于"从没发过"(首版直发), 不能被 rc 之类的骗走。
    assert script.latest_released(["v0.0.2-rc1", "v1", "release-x"]) is None
    assert script.latest_released([]) is None


def test_both_version_extractions_agree_on_the_real_file() -> None:
    """定版脚本与 release-build.yml 必须从 packaging.py 读出同一个版本号.

    两处各自用一份正规式(这里 Python, 那边 PowerShell), 形态一旦分叉就会出现
    "tag 说 0.0.2 / 产物说 0.0.1"。守卫把两侧的形态都钉住, 再拿真实文件对一遍。
    """
    script = _load_script("release_bump")
    workflow = ci_workflow.RELEASE_BUILD_WORKFLOW.read_text(encoding="utf-8")

    assert script.VERSION_PATTERN.pattern == r'__version__\s*=\s*"([^"]+)"'
    assert r"__version__\s*=\s*" in workflow
    assert r'"([^"]+)"' in workflow, (
        "release-build.yml 的抽取正规式变了形态, 要与 scripts/release_bump.py 一起改"
    )
    from archive_management.packaging import __version__ as declared

    assert script.read_current_version(_packaging_py()) == declared


def test_reading_current_version_rejects_bad_shapes(tmp_path: Path) -> None:
    """读版本号时多判一道形态: 恰好一处 + 三段数字, 不带病往下走."""
    script = _load_script("release_bump")
    path = tmp_path / "packaging.py"

    _write_packaging(path, "0.1")
    with pytest.raises(ValueError, match="三段数字"):
        script.read_current_version(path)

    path.write_text('a = "1.0.0"\nb = "2.0.0"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="0 处"):
        script.read_current_version(path)


def test_main_writes_the_outputs_and_never_touches_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main 的契约: stdout 只写键=值行; **任何情况下都不写文件**(定版在 PR 里做)."""
    script = _load_script("release_bump")
    path = tmp_path / "packaging.py"
    _write_packaging(path, "0.0.1")
    original = path.read_text(encoding="utf-8")

    assert script.main(["--file", str(path), "--tags", "v0.0.1"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "bump=needs-bump" in lines, "版本号没动: 输出建议档而不是自己加"
    assert "version=0.0.2" in lines, "建议的下一版号"
    assert "previous=0.0.1" in lines
    assert "reason=needs-bump" in lines
    assert path.read_text(encoding="utf-8") == original, (
        "这个脚本不许改文件: 合入之后改 main 会被分支保护拒掉"
    )


def test_main_reports_failures_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """读不出合法版本号: 退码 1 + ::error:: 说明(CI 日志直接能读), 不甩栈."""
    script = _load_script("release_bump")
    path = tmp_path / "packaging.py"
    path.write_text('APP_NAME = "ArchiveManagement"\n', encoding="utf-8")

    assert script.main(["--file", str(path), "--tags"]) == 1
    assert "::error::" in capsys.readouterr().err


def test_the_file_argument_is_required() -> None:
    """缺 --file 时 argparse 自己就会拦(退码 2), 不需要脚本项目内的处理."""
    script = _load_script("release_bump")
    with pytest.raises(SystemExit) as caught:
        script.main(["--tags", "v0.0.1"])
    assert caught.value.code == 2
