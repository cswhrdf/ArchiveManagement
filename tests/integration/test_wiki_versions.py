"""用户手册版本标注脚本(`scripts/stamp_wiki_versions.py`)的守卫用例.

为什么值得单测: 这一处**没有任何一步会因此变红** —— 标错了版本, 工作流照样全绿, Wiki 上只是
多一句不对的话。而"对不对"完全由 git 历史决定, 所以这里用真仓库(真 tag、真提交)量, 不喂假数据。

判据全部围绕一个口径: **页面最后一次改动是哪个 tag 头一次带上来的**; 语义最小的 tag 是基线,
基线那一版就有的内容一律不标版本(2026-10-31 定: 现有文档不追溯)。
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
    pytest.mark.feature("文档与发布"),
    pytest.mark.story("使用手册同步到 Wiki"),
    pytest.mark.layer("integration"),
]

# 造仓库时把作者与换行符都钉死: 让结果只取决于我们写的那几行(否则 Windows 的 autocrlf
# 与全局 user.name 会渗进来)。
_GIT_OPTIONS = (
    "-c",
    "core.autocrlf=false",
    "-c",
    "user.name=tester",
    "-c",
    "user.email=tester@example.com",
)


def _git(repo: Path, *args: str) -> str:
    """在临时仓库里跑一条 git 命令(失败就把 stderr 带出来).

    输出按 UTF-8 解: 手册文件名是中文, 而 Windows 上 ``text=True`` 默认用本地代码页(cp936)
    —— 解不开的字节会把整次输出丢在 reader 线程里(2026-10-31 实测, 只报一条警告, 断言却拿到空串)。
    """
    git = shutil.which("git")
    assert git is not None, "guard 要真跑 git: 找不到可执行文件就无法验证版本标注"
    # 命令与参数都是本模块拼出来的固定值, 不经 shell 执行。
    result = subprocess.run(  # noqa: S603
        [git, *_GIT_OPTIONS, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, f"git {args} 失败: {result.stderr}"
    return result.stdout


def _write(repo: Path, name: str, text: str) -> None:
    """写一页手册并提交(提交是判据的一部分: 版本标注只认 commit —— tag 区间)."""
    path = repo / "wiki" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")


def _commit(repo: Path, message: str) -> None:
    """提交当前暂存区(git 1.x 之后的 `commit -m` 在空暂存区上会失败, 由调用方保证有改动)."""
    _git(repo, "commit", "-m", message)


def _tag(repo: Path, name: str) -> None:
    """打一个轻量 tag(发布流程用的就是轻量 tag)."""
    _git(repo, "tag", name)


def _repo(tmp_path: Path) -> Path:
    """初始化一个带 main 分支的空仓库."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    return repo


def test_stamp_goes_right_under_the_first_heading() -> None:
    """标注插在一级标题下, 正文与标题本身都不动(章节编号另有用例盯着)."""
    script = _load_script("stamp_wiki_versions")

    stamped = script.stamp("# 01 安装与首次运行\n\n正文。\n", "0.0.2")

    assert stamped == (
        "# 01 安装与首次运行\n\n> 本页内容随 **v0.0.2** 更新。\n\n正文。\n"
    ), stamped


def test_stamping_twice_changes_nothing() -> None:
    """幂等: 重跑先抹掉自己上一轮写的那行 —— 否则每同步一次就多一行."""
    script = _load_script("stamp_wiki_versions")

    once = script.stamp("# 01 安装与首次运行\n\n正文。\n", "0.0.2")
    twice = script.stamp(once, "0.0.2")

    assert twice == once, "同一版本重复标注不该改变正文"


def test_a_later_version_replaces_the_earlier_stamp() -> None:
    """换版本时是**替换**而不是叠加(旧标注先被抹掉)."""
    script = _load_script("stamp_wiki_versions")

    older = script.stamp("# 01 安装与首次运行\n\n正文。\n", "0.0.2")
    newer = script.stamp(older, "0.0.5")

    assert "v0.0.5" in newer
    assert "v0.0.2" not in newer, "旧版本号要连同那一行一起去掉"


def test_a_page_without_a_heading_is_left_alone() -> None:
    """首行不是一级标题就原样返回: 那种页面不该被这里改写(章节标题形态另有守卫)."""
    script = _load_script("stamp_wiki_versions")

    assert script.stamp("没有标题的一页\n", "0.0.2") == "没有标题的一页\n"


def test_a_page_with_only_a_heading_still_gets_the_stamp() -> None:
    """只有标题的页面也要能标(否则正文为空的分支没人走过)."""
    script = _load_script("stamp_wiki_versions")

    assert script.stamp("# 11 导入与导出\n", "0.0.3") == (
        "# 11 导入与导出\n\n> 本页内容随 **v0.0.3** 更新。\n"
    )


def test_only_three_part_version_tags_are_recognized() -> None:
    """别的形态的 tag(v1 / 预发布段 / 别人的 release-x)一律看不见."""
    script = _load_script("stamp_wiki_versions")

    assert script.version_key("v0.0.10") == (0, 0, 10)
    assert script.version_key("v1") is None
    assert script.version_key("v0.0.2-rc1") is None
    assert script.version_key("release-2026") is None


def test_each_page_reports_the_version_that_first_brought_it(tmp_path: Path) -> None:
    """端到端: 基线那版就有的页不标; 之后改过的页标**首个带上它的版本**."""
    script = _load_script("stamp_wiki_versions")
    repo = _repo(tmp_path)

    _write(repo, "01 安装与首次运行.md", "# 01 安装与首次运行\n\n从未改过。\n")
    _write(repo, "Home.md", "# 手册\n\n目录。\n")
    _commit(repo, "首版手册")
    _tag(repo, "v0.0.1")

    # 第二版改了第一章。
    _write(repo, "01 安装与首次运行.md", "# 01 安装与首次运行\n\n第二版改过。\n")
    _commit(repo, "改第一章")
    _tag(repo, "v0.0.2")

    # 第三版新增一章(它有独立的首次改动), 第一章不再动。
    _write(repo, "11 导入与导出.md", "# 11 导入与导出\n\n新增。\n")
    _commit(repo, "新增第十一章")
    _tag(repo, "v0.0.3")

    out = tmp_path / "out"
    assert script.main(["--repo", str(repo), "--out", str(out)]) == 0

    assert "v0.0.2" in (out / "01 安装与首次运行.md").read_text(encoding="utf-8"), (
        "这一页是在 v0.0.2 改过的, 之后没再动, 所以要一直标 v0.0.2"
    )
    assert "v0.0.3" in (out / "11 导入与导出.md").read_text(encoding="utf-8")
    assert ">" not in (out / "Home.md").read_text(encoding="utf-8"), (
        "导航页不标版本(它跟着章节增删走, 标了只是噪音)"
    )


def test_a_page_untouched_since_the_baseline_is_never_stamped(tmp_path: Path) -> None:
    """基线(语义最小的 tag)那一版就有的内容不标版本 —— 现有文档不追溯."""
    script = _load_script("stamp_wiki_versions")
    repo = _repo(tmp_path)

    _write(repo, "02 游戏与存档位置.md", "# 02 游戏与存档位置\n\n从没改过。\n")
    _write(repo, "03 备份-分支与时间线.md", "# 03 备份-分支与时间线\n\n第二版改过。\n")
    _commit(repo, "首版手册")
    _tag(repo, "v0.0.1")

    _write(repo, "03 备份-分支与时间线.md", "# 03 备份-分支与时间线\n\n改过了。\n")
    _commit(repo, "改第三章")
    _tag(repo, "v0.0.2")

    out = tmp_path / "out"
    script.main(["--repo", str(repo), "--out", str(out)])

    untouched = (out / "02 游戏与存档位置.md").read_text(encoding="utf-8")
    assert untouched == "# 02 游戏与存档位置\n\n从没改过。\n", untouched


def test_a_repository_without_tags_stamps_nothing(tmp_path: Path) -> None:
    """还没发过版(一个 tag 都没有): 照常复制, 但一个版本号都标不出来."""
    script = _load_script("stamp_wiki_versions")
    repo = _repo(tmp_path)

    _write(repo, "01 安装与首次运行.md", "# 01 安装与首次运行\n\n正文。\n")
    _commit(repo, "首版手册")

    out = tmp_path / "out"
    assert script.main(["--repo", str(repo), "--out", str(out)]) == 0
    assert (out / "01 安装与首次运行.md").read_text(encoding="utf-8") == (
        "# 01 安装与首次运行\n\n正文。\n"
    )


def test_a_missing_manual_is_an_error(tmp_path: Path) -> None:
    """手册目录不存在/为空要当场判错: 静默同步一份空白出去比失败更糟."""
    script = _load_script("stamp_wiki_versions")
    repo = _repo(tmp_path)

    assert script.main(["--repo", str(repo), "--out", str(tmp_path / "out")]) == 1
