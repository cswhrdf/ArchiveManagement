"""生成"这个 PR 还没定版本号"的提示评论(2026-11-01 起 `main` 只收 PR).

为什么要有这条评论: 定版从"合入之后由 bot 改 main"前移到了 **PR 阶段**(分支保护禁止一切
直接推送, bot 也一样)。人不会记得每次合入都改版本号, 而"没改"的表现是**安静地不发新版** ——
Release 页什么都不多, 谁也不知道为什么。所以 CI 在 PR 上留一句, 说清"当前号是多少、
想发版就改成哪个、不改会怎样"。

评论正文里带一个固定 marker(``MARKER``), 工作流靠它找到自己上一次的评论并原地更新 ——
否则每次 push 都会新加一条, 一个 PR 攒出七八条同样的提示。

只用标准库(与 ``scripts/release_bump.py`` 同一条理由: 这个作业不装项目依赖)。

用法::

    python3 scripts/release_hint.py --current 0.0.1 --next 0.0.2 --out hint.md
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

#: 评论的身份标记: 工作流按它去重(`gh api ... --jq 'select(.body | contains("release-hint"))'`)。
MARKER = "<!-- release-hint -->"

#: 版本号所在的那一行(提示里指名道姓, 免得人去找)。
VERSION_FILE = "src/archive_management/packaging.py"


def render(current: str, next_version: str) -> str:
    """提示评论的正文(当前号 / 建议号 / 不改会怎样, 三句话)."""
    return f"""{MARKER}
### 这一版还没有定版本号

`{VERSION_FILE}` 里的 `__version__` 仍是 **{current}**, 与最近发过的 `v{current}` 相同。

2026-11-01 起 `main` 只收 PR, 合入之后没有任何工作流能再写 `main` —— 版本号**只能在本 PR 里改**:

- **想发版**: 把 `__version__` 改成 **{next_version}**(或者你想要的 `0.1.0` / `1.0.0`)再推上来;
- **不想发版**: 忽略这条, 合入后只会构建、不会新建 Release。
"""


def main(argv: Sequence[str] | None = None) -> int:
    """入口: 把提示正文写进 ``--out``(交给 ``gh api --input`` 发出去)."""
    parser = argparse.ArgumentParser(
        description="生成'这个 PR 还没定版本号'的提示评论正文"
    )
    parser.add_argument("--current", required=True, help="packaging.py 里的当前版本号")
    parser.add_argument("--next", required=True, help="建议的下一版号(第 3 位加 1)")
    parser.add_argument("--out", type=Path, required=True, help="写出的 markdown 文件")
    args = parser.parse_args(argv)

    args.out.write_text(render(args.current, args.next), encoding="utf-8")
    print(f"提示正文已写入 {args.out}({args.current} -> {args.next})")
    return 0


if __name__ == "__main__":  # pragma: no cover - 脚本入口
    sys.exit(main())
