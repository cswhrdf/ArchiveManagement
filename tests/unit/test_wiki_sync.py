"""使用手册与 Wiki 同步工作流的守卫用例.

为什么需要: 这套流程的失败模式**全是静默的** —— 少推一页、删掉的页面仍留在 Wiki 上、
占位符没被替换、章节链接写错, 都不会让任何一步变红(工作流照样绿, Wiki 上却少内容或
留下僵尸页面)。所以把不变式写成读文本的普通用例, 而不是靠下次 push 时肉眼发现。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("文档与发布"),
    pytest.mark.story("使用手册同步到 Wiki"),
    pytest.mark.layer("unit"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "wiki.yml"
_RELEASE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"
_WIKI = _REPO_ROOT / "wiki"
# 仓库链接占位符: 由工作流在推送前替换成真实地址(owner/repo 不能写死在文档里)。
_PLACEHOLDER = "{{REPO_URL}}"
# "多推一次"的守卫下限: 章节数少于此值说明手册被拆得太粗或被误删。
_MIN_CHAPTERS = 8
# 手册面向"不碰命令行的用户": 出现这些写法就说明内容跑偏了(该进 docs/ 或 README)。
_COMMAND_LINE_PATTERNS = (
    "uv run",
    "uv sync",
    "python -m",
    "pip install",
    "--root",
    "--verbose",
    "--smoke",
    "命令行",
    "子命令",
)


def _pages() -> list[Path]:
    """返回 wiki/ 下的页面文件(按名称排序)."""
    return sorted(_WIKI.glob("*.md"))


def _chapters() -> list[Path]:
    """返回带编号的章节页(`NN 标题.md`)."""
    return [page for page in _pages() if re.match(r"^\d{2} ", page.name)]


def test_workflow_file_exists() -> None:
    """同步工作流必须在仓库里(否则改文档的人会以为"写进 wiki/ 就会自动上线")."""
    assert _WORKFLOW.is_file(), "缺少 .github/workflows/wiki.yml"


def test_manual_is_split_into_numbered_chapters() -> None:
    """使用说明按章节细分: 有 Home 与侧边栏, 且章节数不小于下限."""
    names = {page.name for page in _pages()}

    assert "Home.md" in names, "缺少 Home.md(目录页)"
    assert "_Sidebar.md" in names, "缺少 _Sidebar.md(侧边栏)"
    assert len(_chapters()) >= _MIN_CHAPTERS, f"章节太少: {sorted(names)}"


def test_every_chapter_starts_with_a_heading_numbered_like_its_file() -> None:
    """章节首行的一级标题要与文件名编号一致(编号错位会让手册顺序乱掉)."""
    for page in _chapters():
        first_line = page.read_text(encoding="utf-8").splitlines()[0]
        number = page.name[:2]

        assert first_line.startswith(f"# {number} "), (
            f"{page.name} 首行不是 `# {number} …`"
        )


def test_home_and_sidebar_link_every_chapter() -> None:
    """目录页与侧边栏都要覆盖全部章节 —— 少一页就等于那页在 Wiki 上找不到入口."""
    home = (_WIKI / "Home.md").read_text(encoding="utf-8")
    sidebar = (_WIKI / "_Sidebar.md").read_text(encoding="utf-8")

    for page in _chapters():
        title = page.stem
        hint = f"{page.name} 没有在 {{Home,_Sidebar}} 里给出 `[[{title}]]` 链接"

        assert f"[[{title}]]" in home, hint
        assert f"[[{title}]]" in sidebar, hint


def test_pages_do_not_use_relative_links_to_the_repository() -> None:
    """Wiki 页面与仓库文件不在同一棵目录树里: 指向仓库的链接必须是绝对地址.

    相对链接(`./docs/…`、`docs/…`)在 Wiki 上会变成"不存在的页面", 而 GitHub 不会报错。
    """
    for page in _pages():
        text = page.read_text(encoding="utf-8")

        for bad in ("./docs/", "(docs/"):
            assert bad not in text, f"{page.name} 用了仓库相对链接: {bad}"


def test_pages_stay_free_of_the_command_line() -> None:
    """手册只讲窗口里的操作: 软件全平台编译发布, 用户拿到的是可执行包.

    在手册里写 `uv run` / `python -m` / `--root` 这类内容, 会让用户以为"想用这个
    软件得先装 Python、开命令行" —— 那是开发者的使用方式(属于 `docs/` 与 README)。
    这条与"章节编号""侧边栏链接"一样是静默失效的: 没有任何一步会因此变红。
    """
    for page in _pages():
        text = page.read_text(encoding="utf-8")

        for bad in _COMMAND_LINE_PATTERNS:
            assert bad not in text, f"{page.name} 里出现了命令行内容: {bad}"


def test_repository_links_use_the_placeholder() -> None:
    """指向仓库的链接一律用占位符, 由工作流替换(避免把 owner/repo 写死)."""
    sidebar = (_WIKI / "_Sidebar.md").read_text(encoding="utf-8")

    assert _PLACEHOLDER in sidebar, "侧边栏没有用占位符引用仓库文档"
    assert "github.com/" not in sidebar.replace(_PLACEHOLDER, ""), (
        "侧边栏里还有写死的 github.com 链接"
    )


def test_workflow_mirrors_pages_instead_of_appending() -> None:
    """镜像语义: 先删旧页面再整体复制, 否则改名/删除会在 Wiki 上留下僵尸页面."""
    text = _WORKFLOW.read_text(encoding="utf-8")

    assert "find wiki-target -maxdepth 1 -name '*.md' -delete" in text, (
        "缺少『先清空旧页面』这一步"
    )
    assert "cp wiki-stamped/*.md wiki-target/" in text, (
        "要复制打好版本标注的副本(wiki-stamped/), 不是仓库里那份原样"
    )
    assert "python3 scripts/stamp_wiki_versions.py" in text, (
        "版本标注要在复制之前现算(标注是派生物, 不写回 wiki/)"
    )


def test_workflow_replaces_the_repository_placeholder() -> None:
    """占位符必须真的被替换掉(不替换就等着 Wiki 上显示 `{{REPO_URL}}`)."""
    text = _WORKFLOW.read_text(encoding="utf-8")

    assert f"s|{_PLACEHOLDER}|" in text or f'"s|{_PLACEHOLDER}|' in text, (
        "工作流没有替换仓库链接占位符"
    )
    assert "REPO_URL:" in text, "没有定义 REPO_URL 变量"


def test_workflow_can_write_to_the_wiki_and_cancels_superseded_runs() -> None:
    """推送 Wiki 需要 contents: write; 连续 push 要取消被取代的那一轮."""
    text = _WORKFLOW.read_text(encoding="utf-8")

    assert "contents: write" in text, "没有给工作流推送 Wiki 的权限"
    assert "cancel-in-progress: true" in text, "连续 push 会白跑一轮同步"


def test_workflow_runs_only_for_a_release_instead_of_every_push() -> None:
    """手册要标“这页是哪个版本带上的”: 版本号只有发完版才知道, 所以它跟发版走, 不跟 push 走.

    ① 触发是 `workflow_call`(由 release.yml 的发布作业调)与手动档 —— **不再监听 push**,
    于是 dev 上改的手册不会先上线(那时谁也不知道它会进哪个版本); ② 两个入口都必须自带
    `tag`(否则“同步的是哪一版”说不清); ③ 不在 `main` 历史里的 tag 要拒绘同步(手动从 dev
    发出去的那一档) —— 给手册标一个正式版根本没有的版本号, 比不标更糟。
    """
    text = _WORKFLOW.read_text(encoding="utf-8")
    head = text.split("\njobs:", 1)[0]

    assert "workflow_call:" in head, "要能被 release.yml 的发布作业直接调"
    assert "push:" not in head, "不再跟着 push 跑: dev 上改手册时还没有版本号"
    assert "workflow_dispatch:" in head, "保留手动档(补跑某一版)"
    assert head.count("required: true") == 2, "两个入口都必须给 tag"
    assert "merge-base --is-ancestor" in text, "要拒绝不在 main 历史里的 tag"
    assert "ref: ${{ inputs.tag }}" in text, (
        "checkout 的必须是那个 tag(标注靠它的历史算)"
    )
    assert "fetch-depth: 0" in text, "标注要沿完整历史找“最后改动它的版本”"


def test_the_release_workflow_syncs_the_manual_after_it_tags_a_version() -> None:
    """发布作业之后才同步手册: tag 是定版之后才有的, 而手册要标的就是它.

    为什么不是“监听 release 事件”: 那一步的 `gh release create` 用的是 GITHUB_TOKEN, 它触发的
    release 事件**不会**再生成工作流运行(GitHub 的防环规则), 监听那条路根本没人叫得动 ——
    只能由发布作业直接调。
    """
    text = _RELEASE_WORKFLOW.read_text(encoding="utf-8")
    body = text.split("\njobs:", 1)[1]

    assert "uses: ./.github/workflows/wiki.yml" in body, "发布之后要调 wiki 那份工作流"
    assert "tag: v${{ needs.build.outputs.version }}" in body, (
        "同步的 tag 就是这一轮发出去的版本"
    )
    assert "needs.publish.result == 'success'" in body, (
        "手动档没开 publish 时没有 tag 可同步, 不该跑"
    )
