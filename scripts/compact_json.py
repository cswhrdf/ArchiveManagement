"""把仓库里的固定数据清单压成单行紧凑 JSON, 并把它并入本次提交(提交钩子用).

平台工具排除清单这类文件与运行时缓存一样**只给程序读**, 因此统一写成单行紧凑 JSON:
不写缩进、换行与分隔空格(键的顺序保持原样)。用编辑器改这类文件时通常会被格式化
(自动缩进 + 折行), 手工转回去既繁琐又容易漏, 所以交给提交钩子 —— 本脚本接收
pre-commit 传进来的文件路径, 逐个压成单行写回, 再 ``git add`` 回暂存区。

为什么要自己暂存: pre-commit 只要发现"钩子改过文件"(工作区与索引不一致)就会拦下
这次提交。改写完立刻加回索引, 工作区与索引一起变成单行, 钩子就不算"弄脏文件",
这次提交直接带上转换好的内容 —— 不需要手工再 ``git add`` 一次。没有改写时也同样
加一次索引: 索引里可能还留着上一次提交前的多行版本。加不进索引(没有 git、权限
问题)时, 行为退回成"就地改写并拦下提交", 绝不会让未压缩的版本进仓库。

用法:  ``uv run python scripts/compact_json.py <文件> [<文件> ...]``

``pre-commit run --all-files`` 手动运行时同样会把结果加进暂存区, 这是刻意的。不是
合法 JSON 或读不到的文件只报错跳过, 不会被清空或写坏。
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def compact(path: Path) -> bool:
    """把单个文件压成单行紧凑 JSON, 返回是否发生了改写.

    文件不是合法 JSON 时抛出异常, 由调用方报错跳过 —— 坏文件不会被覆盖。
    """
    raw = path.read_text(encoding="utf-8")
    payload: object = json.loads(raw)
    compacted = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if compacted == raw:
        return False
    path.write_text(compacted, encoding="utf-8")
    return True


def stage(path: Path) -> bool:
    """把文件加回暂存区, 返回是否成功(没有 git 时返回 ``False``).

    没有改写时也要加一次: 工作区可能已经是单行, 而索引里还留着上一次提交前的多行
    版本。加不进去不是致命错误 —— 工作区与索引不一致时 pre-commit 会照旧拦下提交,
    交给用户手工 ``git add``。
    """
    git = shutil.which("git")
    if git is None:  # pragma: no cover - 正常开发环境都有 git
        return False
    # 命令与参数都是本模块拼出来的固定值, 不经 shell 执行。
    completed = subprocess.run(  # noqa: S603
        [git, "add", "--", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.returncode == 0


def use_utf8_output() -> None:
    """把标准输出与错误输出固定为 UTF-8.

    git/pre-commit 会把钩子的输出原样转给界面(按 UTF-8 解码), 而 Windows 上管道默认
    用 ANSI 代码页(cp936 等), 中文会显示成乱码。``reconfigure`` 只存在于真实文件流
    上(标准库把它标成 ``TextIO``), 因此这里显式忽略联合属性的类型检查。
    """
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def main(arguments: list[str]) -> int:
    """处理命令行给出的文件, 返回退出码(只有出错才非 0)."""
    compacted: list[str] = []
    unstaged: list[str] = []
    skipped: list[str] = []
    for argument in arguments:
        path = Path(argument)
        try:
            changed = compact(path)
        except (OSError, ValueError) as exc:
            skipped.append(f"{argument} ({exc})")
            continue
        if not stage(path):
            unstaged.append(argument)
        elif changed:
            compacted.append(argument)
    for item in compacted:
        print(f"已压成单行并加入暂存区: {item}")
    for item in unstaged:
        print(f"未能加入暂存区(请手动 git add): {item}", file=sys.stderr)
    for item in skipped:
        print(f"跳过(不是合法 JSON 或无法读写): {item}", file=sys.stderr)
    return 1 if unstaged or skipped else 0


if __name__ == "__main__":
    use_utf8_output()
    raise SystemExit(main(sys.argv[1:]))
