"""算 dev 预发布快照的四段版本号(出快照前由 `.github/workflows/dev-release.yml` 调).

口径(2026-10-31 定): ``版本 = <基础版本>.<第 4 位>``

* **基础版本** = ``packaging.py`` 的三段版本与"最近发过的三段 tag"里**语义较大**的那个。
  为什么不是直接读 ``packaging.py``: ``dev`` 上的那份可能还停在上一版(它是在某次 ``main``
  推进时重建出来的, 之后版本号由合入的 PR 推进), 照它算会得到"比已发布版本还旧"的号;
* **第 4 位** = 距最近一个三段 tag 的**提交数**(没有 tag 时数全仓)。它是派生量:
  - 同一个提交永远算得出同一个号(可复现, 重跑不会漂);
  - 天然单调 —— 这正是"两份 dev 快照谁新"要的比较依据;
  - 0 记 1(HEAD 正好停在已发布的 tag 上时, 快照仍然是"第 1 个")。
* 第 4 位必须 **< 65536**: Windows 的版本资源每段是 16 位字段(J-1 的
  ``packaging/version-info.txt`` 同理), 超出会被构建期拒掉。

四段号只用于**预发布**: 正式版本的单一真相仍是 ``packaging.py`` 的三段号, 而
``scripts/release_bump.py`` 只认三段 tag —— 所以四段 tag 不会打乱定版。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

# 只认本流程发过的版本 tag: v 前缀 + 三段数字(与 scripts/release_bump.py 同口径)。
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# 版本号在 packaging.py 里的唯一形态; 出现两处就当场报错(与定版脚本同样的防范)。
_VERSION = re.compile(r'__version__\s*=\s*"([^"]+)"')
# Windows 版本资源每段是 16 位字段, 第 4 位不能越界。
_FIELD_LIMIT = 65536


def _as_tuple(version: str) -> tuple[int, ...]:
    """把三段版本号翻成可比较的整数元组(逐段比, 不受字符串字典序影响)."""
    return tuple(int(part) for part in version.split("."))


def parse_version(text: str) -> str | None:
    """整串必须是三段数字版本号; 否则返回 ``None``."""
    stripped = text.strip()
    return stripped if re.fullmatch(r"\d+\.\d+\.\d+", stripped) else None


def latest_released(tags: Sequence[str]) -> str | None:
    """从全部 tag 里挑"最近发过的三段版本": v 前缀且形如三段数字的里语义最大的一个.

    四段 tag(dev 预发布)与别的形态(v1 / v0.0.2-rc1)一律看不见 —— 预发布不该改变正式版
    的定版基准。
    """
    versions = [
        version
        for tag in tags
        if (version := parse_version(tag.removeprefix("v"))) is not None
        and _TAG.match(tag.strip())
    ]
    return max(versions, key=_as_tuple) if versions else None


def read_current_version(path: Path) -> str:
    """从 packaging.py 读出三段版本号; 形态不对或出现不止一处时当场报错."""
    matches = _VERSION.findall(path.read_text(encoding="utf-8"))
    if len(matches) != 1:
        raise ValueError(
            f"{path} 里应有恰好一处 __version__, 实际找到 {len(matches)} 处"
        )
    version = parse_version(matches[0])
    if version is None:
        raise ValueError(f"{path} 的版本号不是三段数字: {matches[0]}")
    return version


def snapshot_version(current: str, released: str | None, commits: int) -> str:
    """拼出四段快照版本号(纯函数, 判据全在这里)."""
    base = max((current, released), key=lambda item: _as_tuple(item or current))
    build = max(commits, 1)
    if build >= _FIELD_LIMIT:
        raise ValueError(
            f"第 4 位 {build} 超过 Windows 版本资源的 16 位上限({_FIELD_LIMIT});"
            " 先给 dev 打一次正式版把计数拉回起点"
        )
    return f"{base}.{build}"


def count_commits_since(repo: Path, revision: str | None) -> int:
    """数 ``revision..HEAD`` 的提交数; ``revision`` 为 None 时数全仓."""
    git = shutil.which("git")
    if git is None:  # pragma: no cover - CI 与开发机都有 git
        raise RuntimeError("找不到 git 可执行文件")
    span = f"{revision}..HEAD" if revision else "HEAD"
    # 命令与参数都是本模块拼出来的固定值, 不经 shell 执行。
    result = subprocess.run(  # noqa: S603
        [git, "rev-list", "--count", span],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git rev-list 失败: {result.stderr.strip()}")
    return int(result.stdout.strip())


def all_tags(repo: Path) -> list[str]:
    """列出仓库里的全部 tag(四段 tag 与别的形态由 :func:`latest_released` 自己滤掉)."""
    git = shutil.which("git")
    if git is None:  # pragma: no cover - CI 与开发机都有 git
        raise RuntimeError("找不到 git 可执行文件")
    result = subprocess.run(  # noqa: S603
        [git, "tag", "--list"],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git tag 失败: {result.stderr.strip()}")
    return result.stdout.split()


def main(argv: Sequence[str] | None = None) -> int:
    """算出版本号并以"键=值"行打到 stdout(整段会被工作流收进 ``$GITHUB_OUTPUT``)."""
    parser = argparse.ArgumentParser(description="算 dev 预发布快照的四段版本号.")
    parser.add_argument(
        "--repo", type=Path, default=Path(), help="git 仓库根(默认当前目录)"
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=Path("src/archive_management/packaging.py"),
        help="版本号单一真相(相对仓库根)",
    )
    args = parser.parse_args(argv)

    try:
        current = read_current_version(args.repo / args.file)
        released = latest_released(all_tags(args.repo))
        commits = count_commits_since(args.repo, f"v{released}" if released else None)
        version = snapshot_version(current, released, commits)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"::error::算快照版本号失败: {exc}", file=sys.stderr)
        return 1

    print(f"base={version.rsplit('.', 1)[0]}")
    print(f"build={version.rsplit('.', 1)[1]}")
    print(f"version={version}")
    print(f"previous=v{released}" if released else "previous=")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
