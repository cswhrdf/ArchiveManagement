"""把 pre-commit 传进来的 Python 文件就地排版, 并把排版结果并入本次提交(提交钩子用).

为什么不再用 ``ruff format --check``: 它只报"哪个文件会被改", 于是流程变成"提交 -> 被拦下 ->
手工 ``uv run ruff format <文件>`` -> ``git add`` -> 再提交一次"。这个脚本把后面三步并进钩子里:
排版完立刻把文件加回索引, 工作区与索引一起变成排版后的样子。

为什么要自己暂存(与 ``scripts/compact_json.py`` 同一套判据): pre-commit 只要发现"钩子改过文件"
(工作区与索引不一致)就会拦下这次提交。加回索引之后就不算"弄脏文件"了, **本次提交直接带上排版好的
内容** —— 不需要手工再 ``git add`` 一次, 也不需要重新触发一次提交。没有改写时也同样加一次索引:
索引里可能还留着上一次提交前的旧版本。加不进索引(没有 git、索引被别的进程占着、权限问题)时
**先重试几次**(见 :func:`stage`), 仍然失败才**返回非 0**: 这时 pre-commit 会拦下提交, 并把 git
的原始报错打出来 —— 既不会让没排版的内容进仓库, 也不让一条看不到原因的失败把提交卡住。

用法:  ``uv run python scripts/ruff_format_and_stage.py <文件> [<文件> ...]``
(``pre-commit run --all-files`` 手动运行时同样会把结果加进暂存区, 这是刻意的。)

命令行参数全部原样交给 ``ruff``(它会自己读 ``pyproject.toml`` 里的排版配置), 不经 shell 执行。
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import subprocess
import sys
import time
from pathlib import Path


def digest(path: Path) -> str:
    """文件内容的指纹(读不到时给空串): 用来判断 ruff 到底改没改这个文件."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:  # pragma: no cover - pre-commit 传进来的文件都在, 这里只是不炸
        return ""


#: ``git add`` 抢不到索引锁时的重试次数与间隔(缘由见 :func:`stage`)。
STAGE_ATTEMPTS = 5
STAGE_RETRY_SECONDS = 0.2


def run_git(git: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """跑一条 git 子命令(命令与参数都由本模块拼出, 不经 shell)."""
    return subprocess.run(  # noqa: S603
        [git, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def index_matches_worktree(git: str, paths: list[Path]) -> bool:
    """索引里是否已经是工作区那一份(``git diff --quiet`` 退出 0 = 没有差异)."""
    completed = run_git(git, "diff", "--quiet", "--", *(str(path) for path in paths))
    return completed.returncode == 0


def stage(paths: list[Path]) -> list[Path]:
    """把文件加回暂存区, 返回**没能加进去**的那些(全部成功时空列表).

    没有改写时也要加一次: 工作区可能已经是排版后的样子, 而索引里还留着上一次提交前的版本。

    为什么要重试(2026-10-05 实测): ``git add`` 要写索引, 而 VS Code 的 Git 集成会周期性刷新索引
    —— 那一下会短暂拿住 ``.git/index.lock``。撞上就得到 ``fatal: Unable to create
    '.../index.lock': File exists.``(实测: 同一批文件在 pre-commit 之外跑全是 ``rc=0``, 在
    pre-commit 里则**每次失败的文件都不一样**)。拿它当"提交被拦下"太吃亏 —— 用户看到的只有一句
    "未能加入暂存区", 连原因都看不到。所以: ① 整批只 ``git add`` 一次(原来是逐文件一次, 25 个
    文件就多 25 次撞锁机会); ② 失败先等一小会儿重试; ③ 重试期间只要索引已经等于工作区那一份就
    算成功(没东西可加, 例如别的进程刚替我们加过); ④ 仍然失败才返回失败, 并把 git 的原始报错
    打出来。
    """
    git = shutil.which("git")
    if git is None:  # pragma: no cover - 正常开发环境都有 git
        return list(paths)
    arguments = ["add", "--", *(str(path) for path in paths)]
    for attempt in range(STAGE_ATTEMPTS):
        completed = run_git(git, *arguments)
        if completed.returncode == 0 or index_matches_worktree(git, paths):
            return []
        if attempt + 1 < STAGE_ATTEMPTS:
            time.sleep(STAGE_RETRY_SECONDS)
            continue
        detail = (completed.stderr or completed.stdout).strip()
        print(f"git add 失败(已重试 {STAGE_ATTEMPTS} 次): {detail}", file=sys.stderr)
    return list(paths)


def ruff_command() -> list[str]:
    """找到 ruff: 优先 PATH 上的可执行文件, 退回到 ``python -m ruff``."""
    found = shutil.which("ruff")
    if found is not None:
        return [found]
    return [
        sys.executable,
        "-m",
        "ruff",
    ]  # pragma: no cover - uv 环境里 ruff 就在 PATH 上


def format_files(files: list[Path]) -> str:
    """跑一次 ``ruff format``, 返回错误说明(成功时给空串).

    一次把全部文件交给 ruff: 它自己会并发处理, 比逐个起进程快; 参数用 ``--`` 隔开, 免得
    文件名被当成选项。
    """
    command = [*ruff_command(), "format", "--", *(str(path) for path in files)]
    # 命令与参数都是本模块拼出来的固定值, 不经 shell 执行。
    completed = subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return (
            f"ruff format 失败(退出码 {completed.returncode}):\n"
            f"{completed.stdout}{completed.stderr}"
        )
    return ""


def use_utf8_output() -> None:
    """把标准输出与错误输出固定为 UTF-8.

    与 ``scripts/compact_json.py`` 里那份同源(脚本不是包, 只能各留一份): git/pre-commit 会把
    钩子的输出原样转给界面(按 UTF-8 解码), 而 Windows 上管道默认用 ANSI 代码页(cp936 等),
    中文会显示成乱码。``reconfigure`` 只存在于真实文件流上(标准库把它标成 ``TextIO``),
    因此这里显式忽略联合属性的类型检查。
    """
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def main(arguments: list[str]) -> int:
    """排版给定的文件并重新暂存, 返回退出码(只有出错或暂存失败才非 0)."""
    files = [Path(argument) for argument in arguments]
    if not files:
        return 0
    before = {path: digest(path) for path in files}
    failure = format_files(files)
    if failure:
        print(failure, file=sys.stderr)
        return 1
    changed = [path for path in files if digest(path) != before[path]]
    unstaged = stage(files)
    for path in changed:
        print(f"已排版并重新暂存: {path}")
    if not changed:
        print("排版无需改动(仍会刷新一次索引, 免得索引里留着旧版本)")
    for path in unstaged:
        print(f"未能加入暂存区(请手动 git add): {path}", file=sys.stderr)
    return 1 if unstaged else 0


if __name__ == "__main__":
    use_utf8_output()
    raise SystemExit(main(sys.argv[1:]))
