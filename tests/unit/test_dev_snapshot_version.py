"""dev 预发布快照版本号(`scripts/dev_snapshot_version.py`)的守卫用例.

为什么值得盯: 第 4 位是**派生量**, 算错了不会有任何一步变红 —— 只会让两份快照的号撞在
一起(后发的覆盖前面的), 或者让号**倒退**(用户按号比新旧就比不出来)。所以判据拆成两层:
纯函数(基础版本取谁 / 第 4 位怎么算 / 16 位上限)与真仓库(距 tag 的提交数)。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from report_support import _load_script

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("发布流程"),
    pytest.mark.story("dev 预发布快照"),
    pytest.mark.layer("unit"),
]

# 造仓库时把作者与换行符钉死: 结果只取决于我们写的那几个提交。
_GIT_OPTIONS = (
    "-c",
    "core.autocrlf=false",
    "-c",
    "user.name=tester",
    "-c",
    "user.email=tester@example.com",
)
_PACKAGING = Path("src") / "archive_management" / "packaging.py"


def _git(repo: Path, *args: str) -> None:
    """在临时仓库里跑一条 git 命令(失败就把 stderr 带出来)."""
    git = shutil.which("git")
    assert git is not None, "guard 要真跑 git: 找不到可执行文件就没法验证第 4 位"
    result = subprocess.run(  # noqa: S603 - 命令与参数都是本用例拼出来的固定值
        [git, *_GIT_OPTIONS, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, f"git {args} 失败: {result.stderr}"


def _repo(tmp_path: Path, *, version: str = "0.0.1") -> Path:
    """初始化一个带 main 分支的最小仓库(里面有一份真的 packaging.py)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    return repo


def _write_version(repo: Path, version: str) -> None:
    """写一份最小但形态真实的 packaging.py(判据只读 __version__ 那一行)."""
    path = repo / _PACKAGING
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'APP_NAME = "ArchiveManagement"\n__version__ = "{version}"\n')


def _commit(repo: Path, message: str) -> None:
    """提交当前全部改动(判据的一部分: 第 4 位数的是提交)."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _outputs(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    """把脚本打到 stdout 的 ``键=值`` 行拆成字典."""
    captured = capsys.readouterr()
    return dict(line.split("=", 1) for line in captured.out.splitlines() if "=" in line)


def test_the_snapshot_number_counts_commits_past_the_release() -> None:
    """基础版本就是最近一次正式发布的版本时, 第 4 位就是距它的提交数."""
    script = _load_script("dev_snapshot_version")

    assert script.snapshot_version("0.0.1", "0.0.1", 97) == "0.0.1.97"


def test_a_newer_release_tag_wins_over_a_stale_packaging_version() -> None:
    """bump 提交只落在 main 上: dev 的 packaging.py 常常还停在上一版, 不能照它算."""
    script = _load_script("dev_snapshot_version")

    assert script.snapshot_version("0.0.1", "0.0.2", 5) == "0.0.2.5", (
        "照 packaging.py 会算出 0.0.1.x —— 比已发布的 0.0.2 还旧"
    )


def test_a_hand_bumped_packaging_version_wins_over_the_last_tag() -> None:
    """人在 dev 上把版本号改成 0.1.0(准备发大版本)时, 快照要跟着用 0.1.0."""
    script = _load_script("dev_snapshot_version")

    assert script.snapshot_version("0.1.0", "0.0.2", 5) == "0.1.0.5"


def test_a_head_sitting_on_the_release_still_gets_field_one() -> None:
    """HEAD 正好停在已发布的 tag 上: 第 4 位记 1(0 没有意义, 也没法再发一份)."""
    script = _load_script("dev_snapshot_version")

    assert script.snapshot_version("0.0.1", "0.0.1", 0) == "0.0.1.1"


def test_the_fourth_field_must_fit_windows_version_resources() -> None:
    """第 4 位是 16 位字段: 越界要在算号这一步就红, 而不是等构建期报一句看不懂的错."""
    script = _load_script("dev_snapshot_version")

    with pytest.raises(ValueError, match="16 位"):
        script.snapshot_version("0.0.1", "0.0.1", 65536)


def test_only_three_part_tags_count_as_releases() -> None:
    """四段 tag(dev 快照)与别的形态一律看不见: 预发布不该改变正式版的定版基准."""
    script = _load_script("dev_snapshot_version")

    released = script.latest_released(
        ["v0.0.9", "v0.0.2.97", "v1", "v0.0.2-rc1", "v0.0.10", "release-2026"]
    )

    assert released == "0.0.10", "拿字符串序会挑到 0.0.9"


def test_the_two_version_scripts_agree_on_the_latest_release() -> None:
    """预发布与定版必须认同一批 tag —— 两处口径漂移会让 dev 快照的基准跳来跳去."""
    snapshot = _load_script("dev_snapshot_version")
    bump = _load_script("release_bump")
    tags = ("v0.0.1", "v0.0.10", "v0.0.9", "v0.0.2.97", "v1", "v0.0.3-rc1")

    assert snapshot.latest_released(tags) == bump.latest_released(tags)


def test_reading_the_version_rejects_ambiguous_or_wrong_shapes(tmp_path: Path) -> None:
    """两处 `__version__`、四段号、文件里没有它: 一律当场报错, 不带病往下算."""
    script = _load_script("dev_snapshot_version")
    path = tmp_path / "packaging.py"

    path.write_text('__version__ = "0.0.1"\n__version__ = "0.0.2"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="恰好一处"):
        script.read_current_version(path)

    path.write_text('__version__ = "0.0.1.7"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="三段"):
        script.read_current_version(path)

    path.write_text('APP_NAME = "ArchiveManagement"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="恰好一处"):
        script.read_current_version(path)


def test_main_counts_the_commits_after_the_last_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """端到端: 发过 v0.0.1 之后又提了两个提交 → 0.0.1.2."""
    script = _load_script("dev_snapshot_version")
    repo = _repo(tmp_path)
    _write_version(repo, "0.0.1")
    _commit(repo, "首版")
    _git(repo, "tag", "v0.0.1")
    _write_version(repo, "0.0.1")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(repo, "第一个改动")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _commit(repo, "第二个改动")

    assert script.main(["--repo", str(repo)]) == 0

    outputs = _outputs(capsys)
    assert outputs["version"] == "0.0.1.2"
    assert outputs["base"] == "0.0.1"
    assert outputs["build"] == "2"
    assert outputs["previous"] == "v0.0.1"


def test_main_counts_every_commit_when_nothing_was_ever_released(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """还没发过版(一个 tag 都没有): 数全仓提交, 基线取 packaging.py 的版本."""
    script = _load_script("dev_snapshot_version")
    repo = _repo(tmp_path)
    _write_version(repo, "0.0.1")
    _commit(repo, "第一个提交")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(repo, "第二个提交")

    assert script.main(["--repo", str(repo)]) == 0

    outputs = _outputs(capsys)
    assert outputs["version"] == "0.0.1.2"
    assert outputs["previous"] == "", (
        "没有正式版时 previous 留空(发布正文据此不写基准 tag)"
    )


def test_main_fails_loudly_when_the_manual_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """连 packaging.py 都找不到时要非 0 退出, 而不是打一个空版本号给工作流."""
    script = _load_script("dev_snapshot_version")
    repo = _repo(tmp_path)

    assert script.main(["--repo", str(repo)]) == 1
    assert "::error::" in capsys.readouterr().err
