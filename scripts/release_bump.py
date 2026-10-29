r"""合入 main 之后决定"这一次发什么版本号", 默认把第 3 位加 1.

发布口径是"手动预改版优先 + 自动 patch 兜底"(取舍见 docs/development.md「发布」节):

- 版本号的**单一真相**仍是 ``src/archive_management/packaging.py`` 的 ``__version__``
  (三段语义版本), 发布 tag 清一色 ``v<版本号>``;
- 要发 0.1.0 / 1.0.0 这类"动前两位"的版本, 由人在 PR 里手改 ``packaging.py`` —— 那份
  diff 经过评审; **故意不让脚本从改动量里猜前两位**: semver 段位是对用户的沟通契约
  (5 行可以是破坏性的 schema 变更, 5000 行重构可以用户无感), 任何按行数/文件数的启发式
  都会两头误判, 误判的代价由用户承担;
- PR 里没改版本号时, 合入 main 之后由 release.yml 的定版作业(classify)调本脚本, 在
  第 3 位上加 1(如 0.0.1 -> 0.0.2), 把这个 bump 提交推回 main, 再构建并发布。

三条判定(输入 = 当前 ``__version__`` 与"最近发过的版本", 即 ``v`` 前缀 tag 里语义
版本最大的一个):

- 从没发过(仓库还没有 ``v*.*.*`` 形态的 tag): 首版直发当前版本号, 不递增;
- 最近发过的 != 当前: 人在 PR 里改过版本号, 按人写的发;
- 最近发过的 == 当前: 自动 patch 递增(``--apply`` 时把 ``packaging.py`` 就地改掉)。

用法(release.yml 的 classify 作业; 本脚本只用标准库, runner 自带的 python3 就够,
不装 uv 环境)::

    tags=$(git tag -l)
    python3 scripts/release_bump.py \
        --file src/archive_management/packaging.py --tags $tags --apply

stdout 只写"键=值"行(整段被工作流收进 ``$GITHUB_OUTPUT``): ``bump``(none|patch)、
``version``(本轮要发布的版本号)、``previous``、``reason``(first-release|manual|patch);
给人看的说明走 stderr 的 ``::notice::``/``::error::``, 不会混进输出参数。
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# 与 release-build.yml 里 PowerShell 那处抽取用同一形态的正规式: 两边必须读出同一个值
# (tests/unit/test_release_bump.py 有守卫钉住这一点), 改形态要两处一起改。
VERSION_PATTERN = re.compile(r'__version__\s*=\s*"([^"]+)"')
# 只认三段纯数字: 带前缀/预发布段/四段的都当"不是发布 tag"忽略。
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class Decision:
    """定版结论: bump 说明"要不要递增", version 是本轮要发布的版本号."""

    bump: str
    """none(按当前版本号发) | patch(第 3 位加 1)."""

    version: str
    """本轮要发布的版本号(发布 tag 用它)."""

    previous: str
    """做决定时的当前版本号(bump 提交的说明文字用它)."""

    reason: str
    """first-release | manual(人在 PR 里改过) | patch(自动兜底)."""


def parse_version(text: str) -> str | None:
    """整串必须是三段数字版本号; 不是(前缀/预发布段/四段)返回 None."""
    stripped = text.strip()
    return stripped if _SEMVER.match(stripped) else None


def _as_tuple(version: str) -> tuple[int, ...]:
    """把三段版本号翻成可比较的整数元组(逐段比, 不受字符串字典序影响)."""
    return tuple(int(part) for part in version.split("."))


def latest_released(tags: Sequence[str]) -> str | None:
    """从全部 tag 里挑"最近发过的版本": v 前缀且形如三段数字的里语义最大的一个.

    其他形态的 tag(``v1`` / ``v0.0.2-rc1`` / ``release-x`` 之类)不是本流程发的,
    一律看不见 —— 排序用整数元组, 所以 v0.0.10 大于 v0.0.9(字符串序会排反)。
    """
    versions = [
        version
        for tag in tags
        if (version := parse_version(tag.removeprefix("v"))) is not None
    ]
    return max(versions, key=_as_tuple) if versions else None


def bump_patch(version: str) -> str:
    """第 3 位加 1(0.0.9 -> 0.0.10; 第 3 段不向上进位, 这是语义版本的规矩)."""
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def read_current_version(path: Path) -> str:
    """从 packaging.py 读出当前版本号; 形态不对时当场报错而不是带病往下走.

    build 那一步(exe 属性页、产物名)读的也是这一行, 所以这里多判一道"恰好一处",
    免得文件里出现第二处 ``__version__`` 时两边悄悄读出不同的值。
    """
    text = path.read_text(encoding="utf-8")
    matches = VERSION_PATTERN.findall(text)
    if len(matches) != 1:
        raise ValueError(
            f"{path} 里应有恰好一处 __version__, 实际找到 {len(matches)} 处"
        )
    version = parse_version(matches[0])
    if version is None:
        raise ValueError(f"{path} 的版本号不是三段数字: {matches[0]}")
    return version


def decide(current: str, latest: str | None) -> Decision:
    """按"首版直发 / 人已定档 / 自动 patch 兜底"三档给出定版结论."""
    if latest is None:
        return Decision("none", current, current, "first-release")
    if latest != current:
        return Decision("none", current, current, "manual")
    return Decision("patch", bump_patch(current), current, "patch")


def apply_bump(path: Path, previous: str, next_version: str) -> None:
    """把 packaging.py 里的版本号就地替换成 next_version(只动那一行引号里的值).

    三道前置检查都在写盘**之前**: 文件里恰好一处 ``__version__``、当前值与
    ``previous`` 一致、新值确实更大 —— 写错了应当红在这一步, 而不是构建期。
    """
    if _as_tuple(next_version) <= _as_tuple(previous):
        raise ValueError(f"新版本 {next_version} 必须大于 {previous}, 拒绝平级/降级")
    text = path.read_text(encoding="utf-8")
    matches = VERSION_PATTERN.findall(text)
    if len(matches) != 1:
        raise ValueError(
            f"{path} 里应有恰好一处 __version__, 实际找到 {len(matches)} 处"
        )
    if matches[0] != previous:
        raise ValueError(f"文件里的版本是 {matches[0]}, 与要替换的 {previous} 不一致")
    path.write_text(
        VERSION_PATTERN.sub(f'__version__ = "{next_version}"', text, count=1),
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """定版入口: 判定, 按需写回, 把结论以"键=值"行打到 stdout."""
    parser = argparse.ArgumentParser(
        description="决定本轮发布的版本号(默认第 3 位加 1)."
    )
    parser.add_argument(
        "--file",
        type=Path,
        required=True,
        help="packaging.py 的路径(版本号的单一真相)",
    )
    parser.add_argument(
        "--tags",
        nargs="*",
        default=(),
        help="git tag -l 的全部输出(从中挑 v*.*.* 里语义最大的)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="判定为 patch 时把递增结果就地写回 --file",
    )
    args = parser.parse_args(argv)

    try:
        current = read_current_version(args.file)
        decision = decide(current, latest_released(args.tags))
        if args.apply and decision.bump == "patch":
            apply_bump(args.file, decision.previous, decision.version)
            print(
                f"::notice::版本号 {decision.previous} -> {decision.version}"
                "(自动 patch 递增).",
                file=sys.stderr,
            )
    except (OSError, ValueError) as exc:
        print(f"::error::定版失败: {exc}", file=sys.stderr)
        return 1

    # stdout 整段会被工作流收进 $GITHUB_OUTPUT: 只许键=值, 别的都走 stderr。
    print(f"bump={decision.bump}")
    print(f"version={decision.version}")
    print(f"previous={decision.previous}")
    print(f"reason={decision.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
