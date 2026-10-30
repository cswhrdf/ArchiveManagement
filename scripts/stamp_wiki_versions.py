"""给用户手册的每一页标注「本页内容随哪个版本更新」(Wiki 同步前跑).

为什么要派生而不是让人手写: 版本号是**合并之后**才由 ``release.yml`` 的 ``classify`` 定出来的
(见 ``scripts/release_bump.py``), PR 阶段谁也不知道自己是 ``v0.0.几`` —— 手册作者不可能在合入
前把版本号写进去, 所以只能在发版那一刻按 git 历史补上。

判据: 这一页最后一次被改动, 是哪个 tag **头一次**带上来的 ——

    git log -1 --format=%H --follow -- <page>   改动这一页的最后一个提交
    git tag --contains <sha>                    含它的全部 tag, 取语义最小的那个

最小的那个 tag 就是「首个带上这次改动的版本」。比逐对 tag 做 diff 便宜, 也不用维护一张版本
排序表。语义最小的 tag 同时充当**基线**: 基线那一版就有的内容不标版本(2026-10-31 定: 现有
文档不追溯版本号), 只有基线之后改动过的页才标。

标注是**派生**的, 不写回 ``wiki/``: 仓库里的手册保持干净(评审时不看噪音), 每次同步现算,
因此"什么时候标、标哪一版"永远只由 git 历史决定。脚本对同一份输入是幂等的(重跑会先抹掉自己
上一次写的那行)。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

# 只认本流程发的 tag: v 前缀 + 三段数字(与 scripts/release_bump.py 的 parse_version 同口径)。
# 别的形态(v1 / v0.0.2-rc1 / release-x)一律看不见, 免得把别人的 tag 当成版本号。
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# 导航页不是"内容页": 它们跟着章节增删走, 标版本只会制造噪音。
_NAVIGATION = frozenset({"Home.md", "_Sidebar.md"})
# 页内标注的固定形状; 重跑时靠它认出自己上一次写的那行(幂等的前提)。
_STAMP_LINE = "> 本页内容随 **v{version}** 更新。"
_STAMP_PATTERN = re.compile(
    r"^> 本页内容随 \*\*v\d+\.\d+\.\d+\*\* 更新。$", re.MULTILINE
)


def version_key(tag: str) -> tuple[int, int, int] | None:
    """把 ``v1.2.3`` 翻成可比较的三元组; 不是本流程的 tag 返回 ``None``."""
    match = _TAG.match(tag.strip())
    if match is None:
        return None
    return (int(match[1]), int(match[2]), int(match[3]))


def _key(tag: str) -> tuple[int, int, int]:
    """排序键: 调用前已确认是版本 tag, 这里只负责把 ``None`` 分支收掉."""
    return version_key(tag) or (0, 0, 0)


def _git(repo: Path, *args: str) -> str:
    """跑一条只读的 git 命令; 失败时把 stderr 带进异常(CI 里要能看出是哪一步).

    输出按 UTF-8 解: 手册的文件名和提交信息都是中文, 而 Windows 上 ``text=True`` 会用本地代码页
    (cp936) 去解, 遇到解不开的字节直接把整次输出丢在 reader 线程里(2026-10-31 实测)。
    """
    git = shutil.which("git")
    if git is None:  # pragma: no cover - wiki 同步只在装了 git 的 CI/开发机上跑
        raise RuntimeError("找不到 git 可执行文件")
    # 命令与参数都是本模块拼出来的固定值, 不经 shell 执行。
    result = subprocess.run(  # noqa: S603
        [git, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {result.stderr.strip()}")
    return result.stdout


def baseline_tag(repo: Path) -> str | None:
    """语义最小的版本 tag 就是基线(它那一版就有的内容不标版本); 一个都没有时返回 ``None``."""
    tags = [tag for tag in _git(repo, "tag", "--list").split() if version_key(tag)]
    return min(tags, key=_key) if tags else None


def first_shipping_tag(repo: Path, path: str) -> str | None:
    """首个带上「最后一次改动 ``path`` 那个提交」的版本; 没有 tag 含它时返回 ``None``."""
    sha = _git(repo, "log", "-1", "--format=%H", "--follow", "--", path).strip()
    if not sha:
        return None
    tags = [
        tag for tag in _git(repo, "tag", "--contains", sha).split() if version_key(tag)
    ]
    return min(tags, key=_key) if tags else None


def stamp(text: str, version: str) -> str:
    """在一级标题下插入版本标注.

    幂等: 先抹掉已有的那一行再插, 所以同一页反复处理结果一致; 首行不是一级标题的页面原样返回
    (那种页面本来就不该由这里改写 —— 章节标题的形态另有守卫盯着)。
    """
    cleaned = re.sub(r"\n{3,}", "\n\n", _STAMP_PATTERN.sub("", text)).strip("\n")
    head, _, body = cleaned.partition("\n")
    if not head.startswith("# "):
        return text
    body = body.strip("\n")
    line = _STAMP_LINE.format(version=version)
    if not body:
        return f"{head}\n\n{line}\n"
    return f"{head}\n\n{line}\n\n{body}\n"


def main(argv: Sequence[str] | None = None) -> int:
    """把 ``--wiki`` 下的页面复制到 ``--out``, 该标版本的页顺手标上."""
    parser = argparse.ArgumentParser(description="给用户手册的页面标注版本来源.")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(),
        help="git 仓库根(默认当前目录); 必须已 checkout 到要同步的那个 tag",
    )
    parser.add_argument("--wiki", default="wiki", help="仓库里的手册目录(相对仓库根)")
    parser.add_argument(
        "--out", type=Path, required=True, help="打好标注的副本写到哪里"
    )
    args = parser.parse_args(argv)

    wiki = args.repo / args.wiki
    pages = sorted(wiki.glob("*.md"))
    if not pages:
        print(f"::error::{wiki} 里一个页面都没有", file=sys.stderr)
        return 1

    baseline = baseline_tag(args.repo)
    args.out.mkdir(parents=True, exist_ok=True)
    stamped: list[str] = []
    for page in pages:
        text = page.read_text(encoding="utf-8")
        if page.name not in _NAVIGATION:
            source = first_shipping_tag(args.repo, f"{args.wiki}/{page.name}")
            # 与基线相同 = 这一页从第一版起就是这内容, 按口径不标版本。
            if source is not None and source != baseline:
                text = stamp(text, source.removeprefix("v"))
                stamped.append(f"{page.name} -> {source}")
        (args.out / page.name).write_text(text, encoding="utf-8")

    for line in stamped:
        print(f"标注: {line}")
    print(f"写出 {len(pages)} 页到 {args.out}(其中 {len(stamped)} 页标了版本)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
