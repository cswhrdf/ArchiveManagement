"""把 PR 里**手写**的「本次变更」一节取出来, 作为 Release 说明(给 release.yml 的 publish 用).

为什么要有这个脚本(2026-10-31 定): 版本信息由**发起者在 PR 里手写**(模板的「本次变更」),
CI 不再自动汇总 —— 自动摘要只能说"改了几个文件", 而发布页要说的是"用户这一版会看到什么"。
发布作业在合入 `main` 之后回头找那个 PR: 用**合入提交**(`github.sha`)去问
``repos/{owner}/{repo}/commits/{sha}/pulls``, **不能**用它要发布的 ``--target`` —— 自动定版
时那个是 bump 提交, 与 PR 没有关联(squash 合入只把合并提交挂在 PR 上)。

只用标准库: 发布作业不装项目依赖(与 ``scripts/create_allure_summary.py`` 同一条理由)。

取文口径: 从 ``## 本次变更`` 这个二级标题起, 到**下一个二级标题**为止; 段内的 HTML 注释
(模板里给发起者的引导话)与 marker 一律剥掉, 连续空行压成一行。取不到内容(没有 PR、或那一节
是空的)时回退成一句说明 —— 发布页宁可少一段, 也不要写上一份"看起来像变更说明"的空话。

用法::

    gh api "repos/$GITHUB_REPOSITORY/commits/$GITHUB_SHA/pulls" > pulls.json
    python3 scripts/release_notes.py --pulls pulls.json --out release-notes.md
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: 「本次变更」这一节的标题(模板里就是它; 允许后面带括号说明, 历史上写作「(自动生成)」)。
HEADING = re.compile(r"^##[ \t]+本次变更[^\n]*$", re.MULTILINE)

#: 下一个二级标题 —— 一节的终点(「## Squash 合并署名」「## 动机」…都在它后面)。
NEXT_HEADING = re.compile(r"^##[ \t]+\S", re.MULTILINE)

#: 段内的 HTML 注释(模板给发起者的引导话), 可跨行。
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

#: 没有任何内容时的回退说明(不写版本/提交号 —— 那些在 Release 标题与页面上已经有了)。
FALLBACK = "(本次合并没有在 PR 里填写「本次变更」, 详见提交记录。)"


def extract(body: str) -> str:
    """从 PR 正文里取出「本次变更」一节的纯文本(取不到时返回空串)."""
    heading = HEADING.search(body)
    if heading is None:
        return ""
    rest = body[heading.end() :]
    following = NEXT_HEADING.search(rest)
    section = rest[: following.start()] if following is not None else rest
    section = COMMENT.sub("", section)
    lines = [line.rstrip() for line in section.splitlines()]
    text = "\n".join(lines)
    # 连续空行压成一行: 手写的段落之间留空行是对的, 三层空行只是噪音。
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def pick_pull(pulls: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """从 ``commits/{sha}/pulls`` 的结果里挑一个 PR(优先已合入的、最近合入的)."""
    usable = [pull for pull in pulls if isinstance(pull, dict)]
    if not usable:
        return None
    merged = [pull for pull in usable if pull.get("merged_at")]
    pool = merged or usable
    return max(pool, key=lambda pull: str(pull.get("merged_at") or ""))


def notes_from_pulls(pulls: Sequence[dict[str, Any]]) -> str:
    """PR 列表 → Release 说明正文(永远有内容)."""
    pull = pick_pull(pulls)
    body = pull.get("body") if pull is not None else None
    text = extract(body) if isinstance(body, str) else ""
    return text or FALLBACK


def main(argv: Sequence[str] | None = None) -> int:
    """入口: 读 pulls JSON → 写 release-notes.md."""
    parser = argparse.ArgumentParser(
        description="把 PR 里手写的「本次变更」一节取出来当作 Release 说明"
    )
    parser.add_argument(
        "--pulls",
        type=Path,
        required=True,
        help="gh api repos/{owner}/{repo}/commits/{sha}/pulls 的输出(JSON 数组)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="写出的 markdown 文件(交给 gh release create --notes-file)",
    )
    args = parser.parse_args(argv)

    try:
        payload = json.loads(args.pulls.read_text(encoding="utf-8") or "[]")
    except (OSError, json.JSONDecodeError) as error:
        print(f"::warning::读不了 {args.pulls}({error}), 改用回退说明。")
        payload = []
    pulls = payload if isinstance(payload, list) else []

    pull = pick_pull(pulls)
    notes = notes_from_pulls(pulls)
    args.out.write_text(notes + "\n", encoding="utf-8")
    if pull is None:
        print("::notice::这个提交没有关联的 PR(手动构建), Release 说明用回退文案。")
    elif notes == FALLBACK:
        print(f"::notice::PR #{pull.get('number')} 的「本次变更」是空的, 用回退文案。")
    else:
        print(f"已取到 PR #{pull.get('number')} 的「本次变更」: {len(notes)} 字符。")
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    sys.exit(main())
