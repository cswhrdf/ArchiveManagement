r"""算出"这一次该发什么版本号", 并告诉调用方版本号是否已经动过.

发布口径是"人在 PR 里定档"(2026-11-01 起, 取舍见 docs/development.md「发布」节):

- 版本号的**单一真相**仍是 ``src/archive_management/packaging.py`` 的 ``__version__``
  (三段语义版本), 发布 tag 清一色 ``v<版本号>``;
- 要发 0.1.0 / 1.0.0 这类"动前两位"的版本, 由人在 PR 里手改 ``packaging.py`` —— 那份
  diff 经过评审; **故意不让脚本从改动量里猜前两位**: semver 段位是对用户的沟通契约
  (5 行可以是破坏性的 schema 变更, 5000 行重构可以用户无感), 任何按行数/文件数的启发式
  都会两头误判, 误判的代价由用户承担;
- **本脚本不写文件**(2026-11-01): `main` 开了分支保护, 任何"合入之后再改 main"的做法都会
  被拒, 于是定版前移到 **PR 阶段** —— 本脚本只算"建议的下一版号", 由 CI 在 PR 上提醒人改;
  合入之后 release.yml 只读地再复查一遍。

三条判定(输入 = 当前 ``__version__`` 与"最近发过的版本", 即 ``v`` 前缀 tag 里语义
版本最大的一个):

- 从没发过(仓库还没有 ``v*.*.*`` 形态的 tag): 首版直发当前版本号, 不递增;
- 最近发过的 != 当前: 人在 PR 里改过版本号, 按人写的发;
- 最近发过的 == 当前: **版本号还没动** —— 给出建议的下一版号(第 3 位加 1),
  ``reason=needs-bump``, 由人在 PR 里改。

用法(PR 的提示作业; 本脚本只用标准库, runner 自带的 python3 就够, 不装 uv 环境)::

    tags=$(git tag -l)
    python3 scripts/release_bump.py \
        --file src/archive_management/packaging.py --tags $tags

stdout 只写"键=值"行(整段被工作流收进 ``$GITHUB_OUTPUT``): ``bump``(none|needs-bump)、
``version``(建议的下一版号)、``previous``、``reason``(first-release|manual|needs-bump);
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
    """none(按当前版本号发) | needs-bump(版本号还没动, 下面的 version 是建议值)."""

    version: str
    """本轮要发布的版本号(发布 tag 用它)."""

    previous: str
    """做决定时的当前版本号(bump 提交的说明文字用它)."""

    reason: str
    """first-release | manual(人在 PR 里改过) | needs-bump(需要人在 PR 里定档)."""


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
    """按"首版直发 / 人已定档 / 还没动版本号"三档给出结论(不写文件)."""
    if latest is None:
        return Decision("none", current, current, "first-release")
    if latest != current:
        return Decision("none", current, current, "manual")
    return Decision("needs-bump", bump_patch(current), current, "needs-bump")


def main(argv: Sequence[str] | None = None) -> int:
    """入口: 判定并以"键=值"行把结论打到 stdout(**不写任何文件**)."""
    parser = argparse.ArgumentParser(
        description="算出本轮该发的版本号, 并说明版本号是否已经动过(不写文件)."
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
    args = parser.parse_args(argv)

    try:
        current = read_current_version(args.file)
        decision = decide(current, latest_released(args.tags))
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
