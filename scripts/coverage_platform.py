"""把"平台专属"的代码从**别的平台**的覆盖率统计里排除出去(生成覆盖率配置用).

为什么需要: 这个项目在三平台跑同一套用例, 而有一小撮代码**只可能**在自己的平台上执行
(注册表探测、``fcntl``、``pmset``…)。按平台判覆盖率时, 它们在别的平台上永远是"未覆盖"
⇒ 单平台 100% 从算术上就不可能。用 ``# pragma: no cover`` 把它们整块删掉又太粗暴:
**它自己那个平台上的统计也一起没了**(而那里正是它最该被量到的地方)。

做法: 源码里写平台标记 ``# platform: windows - 原因``(可以列多个平台, 空格分隔), 本模块按
**当前平台**生成一份覆盖率配置 —— 把 ``pyproject.toml`` 的 ``[tool.coverage.*]`` 原样派生
过来, 再追加"排除掉打给别的平台的标记行"的正则。于是:

- 在**自己**的平台上: 该行照常统计(这个平台的数字是真实的);
- 在**别的**平台上: 该行被排除(不会永远记成缺口)。

两种口径各归各位: ``coverage combine`` / ``report`` 那几个步骤不导入测试的 conftest, 用的是
``pyproject.toml`` 里的基线配置(**不做**平台排除) —— 合并口径因此仍然是"三平台并集"的完整
含义; 而每个平台自己判门槛时用的是本模块生成的配置。

为什么生成而不是写死三份 rc: ``exclude_also`` 这类配置在 coverage 里是**替换**而不是合并,
写死就会漏掉 ``branch`` / ``source`` / ``relative_files`` / ``fail_under`` —— 本模块从
``pyproject.toml`` 派生, 所以两处永远不会漂移。

规则只有一份: 标记的写法、平台名与原因下限都在这里; 守卫
(``tests/unit/test_coverage_platform.py``、``tests/unit/test_coverage_pragmas.py``)与报告
首页的豁免清单附件(``scripts/create_allure_summary.py``)都复用本模块 —— 与 ``pragma`` 那条
"解析只留一份"的纪律一致。

用法::

    python scripts/coverage_platform.py write [输出路径] [--platform 名字]   # 写平台配置
    python scripts/coverage_platform.py write --platform Windows            # 报告作业用
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SOURCE_ROOT = REPO_ROOT / "src"

#: 生成文件的默认落点。**必须避开 ``.coverage.*``**: coverage 的 ``parallel = true`` 会把那个
#: 通配符当作并行数据文件去 combine, 一个 rc 混进去就让合并报 "file is not a database" ——
#: 而 CI 的判定步骤正是"合并之后跑 `coverage report`"(2026-10-06 实测踩到)。
DEFAULT_OUTPUT = REPO_ROOT / ".coverage-platform.rc"

#: 项目支持的三平台(与源码里 ``PlatformFamily`` 的取值一致)。
KNOWN_PLATFORMS = ("linux", "macos", "windows")

#: 平台标记: ``# platform: windows - 原因``(多个平台用空格分隔, 原因在 `` - `` 之后)。
MARKER = re.compile(
    r"#\s*platform:\s*(?P<platforms>(?:[a-z]+\s+)*[a-z]+)\s*-\s*(?P<reason>.*)$"
)

#: 标记**候选**行: ``#`` 后紧跟 ``platform``。用它把"提及 platform 的普通注释"排除掉
#: (实测: ``platform: str = ""  # 空 = 不限来源平台`` 那种字段注释就撞过一次), 而写法不规范
#: 的标记(例如漏了冒号)仍然落在候选里 —— 守卫要能点名它们。
CANDIDATE = re.compile(r"#\s*platform\b")

#: 原因下限与占位词(与 ``pragma`` 守卫同一条口径: 不许写"todo"这种等于没写的话)。
MIN_REASON = 5
PLACEHOLDER_REASONS = frozenset({"todo", "fixme", "无", "暂无", "略", "tbd", "待补"})


def current_platform() -> str:
    """当前平台的三选一名字(``linux`` / ``macos`` / ``windows``).

    这里**不**导入 ``archive_management``: 报告作业与汇总作业不装项目依赖, 本模块必须只用
    标准库(与 ``scripts/create_allure_summary.py`` 同一条理由)。
    """
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def normalize_platform(name: str) -> str:
    """把常见的写法收敛到三选一(``win`` -> ``windows`` 之类)."""
    lowered = name.strip().lower()
    aliases = {
        "win": "windows",
        "win32": "windows",
        "windows": "windows",
        "mac": "macos",
        "macos": "macos",
        "darwin": "macos",
        "osx": "macos",
        "linux": "linux",
        "posix": "linux",
    }
    return aliases.get(lowered, lowered)


def parse_marker(line: str) -> tuple[tuple[str, ...], str] | None:
    """解析一行平台标记, 返回 (平台元组, 原因); 不是标记或写法不对时返回 ``None``.

    写法: ``# platform: windows - 原因``; 平台名必须是已知的三者之一(逗号也容忍), 原因不许
    是占位词。返回 ``None`` 的两种情况由守卫区分(见 ``marker_problems``)。
    """
    match = MARKER.search(line)
    if match is None:
        return None
    platforms = tuple(
        normalize_platform(part)
        for part in re.split(r"[,\s]+", match.group("platforms"))
        if part.strip()
    )
    return platforms, match.group("reason").strip()


def marker_problems(line: str) -> list[str]:
    """一行平台标记的问题清单(空清单 = 合规)."""
    if CANDIDATE.search(line) is None:
        return []
    match = MARKER.search(line)
    if match is None:
        return ["写法不是 `# platform: <平台...> - 原因`"]
    problems: list[str] = []
    platforms, reason = parse_marker(line) or ((), "")
    unknown = [name for name in platforms if name not in KNOWN_PLATFORMS]
    if unknown or not platforms:
        problems.append(f"平台名不认识: {unknown or '(空)'}")
    if len(reason) < MIN_REASON or reason.strip().lower() in PLACEHOLDER_REASONS:
        problems.append(f"原因太短或是占位词: {reason!r}")
    return problems


def marker_lines(root: Path | None = None) -> list[tuple[Path, int, str]]:
    """源码树里全部平台标记(文件, 行号, 整行) —— 豁免清单附件与守卫共用.

    ``root`` 默认是仓库的 ``src/``; 报告脚本会对别的根渲染(用例里造临时仓库), 所以留出口。
    """
    source = root if root is not None else SOURCE_ROOT
    found: list[tuple[Path, int, str]] = []
    for module in sorted(source.rglob("*.py")):
        text = module.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(text, start=1):
            if CANDIDATE.search(line):
                found.append((module, number, line))
    return found


def exclusion_pattern(platform: str) -> str:
    r"""排除"打给别的平台"的标记行的正则(``search`` 口径, 不加锚点).

    开头不写 ``#`` 是有原因的, 而且两条都踩过:

    - 这条规则会作为一行写进 INI 的 ``exclude_also``, 而行首是 ``#`` 的续行会被 configparser
      当注释**整行丢掉** —— 规则就只是"写在文件里很好看", 一条也不生效;
    - 规则前面带 ``.*`` 是为了兼容 coverage 的匹配口径(从行首匹配也能命中标记行)。
    """
    others = [name for name in KNOWN_PLATFORMS if name != platform]
    return "|".join(
        rf".*#\s*platform:\s*(?:[a-z]+\s+)*{other}(?:\s+[a-z]+)*\s*-\s*"
        for other in others
    )


def coverage_sections() -> dict[str, dict[str, Any]]:
    """``pyproject.toml`` 里的 ``[tool.coverage.*]`` 各节(读不到时给空)."""
    with PYPROJECT.open("rb") as handle:
        document = tomllib.load(handle)
    return dict(document.get("tool", {}).get("coverage", {}))


def _render_value(value: bool | int | float | str | list[str]) -> str:
    """把 rc 里的取值写成 INI 形式(列表逐行缩进, 字符串按原样)."""
    if isinstance(value, list):
        return "".join(f"\n    {item}" for item in value)
    if isinstance(value, bool):
        return f" {'true' if value else 'false'}"
    return f" {value}"


def render_config(
    platform: str, *, sections: dict[str, dict[str, Any]] | None = None
) -> str:
    """生成 ``platform`` 上用的覆盖率配置文本(从 ``pyproject.toml`` 派生)."""
    data = sections if sections is not None else coverage_sections()
    lines = [
        "# 本文件由 scripts/coverage_platform.py 生成, 不要手工编辑。",
        f"# 平台: {platform}; 排除的是打给**别的平台**的 `# platform: ...` 标记行;",
        "# 其余取值全部派生自 pyproject.toml 的 [tool.coverage.*]。",
    ]
    for section in ("run", "report"):
        values = data.get(section)
        if not isinstance(values, dict):
            continue
        lines.append(f"[{section}]")
        for key, value in values.items():
            if key == "exclude_also" and isinstance(value, list):
                value = [*value, exclusion_pattern(platform)]
            lines.append(f"{key} ={_render_value(value)}")
        lines.append("")
    return "\n".join(lines)


def write_platform_config(
    output: Path | None = None, *, platform: str | None = None
) -> Path:
    """把本平台的覆盖率配置写到 ``output``, 返回落点."""
    target = output if output is not None else DEFAULT_OUTPUT
    name = platform if platform is not None else current_platform()
    target.write_text(render_config(name), encoding="utf-8")
    return target


def setup_environment() -> Path | None:
    """给本次 pytest 会话装好平台配置(供 ``tests/conftest.py`` 在导入期调用).

    - 已经设了 ``COVERAGE_RCFILE`` 就不动(开发机/CI 的手工覆盖优先);
    - 生成失败时**不动**环境并打印原因: 退回 ``pyproject.toml`` 的基线配置(与今天一致),
      绝不因为"装配置"这件事把整个会话弄挂。
    """
    if os.environ.get("COVERAGE_RCFILE"):
        return None
    try:
        target = write_platform_config()
    except Exception as exc:  # pragma: no cover - 只有磁盘/权限异常才会走到
        print(
            f"平台覆盖率配置生成失败, 退回 pyproject 基线配置: {exc}", file=sys.stderr
        )
        return None
    os.environ["COVERAGE_RCFILE"] = str(target)
    return target


def main(arguments: list[str]) -> int:
    """命令行入口: ``write [输出路径] [--platform 名字]``.

    ``--platform`` 是给**报告作业**用的: 那台机器是 Ubuntu, 而它合并/判定的是某个平台的数据
    (矩阵里的展示名, 如 ``Windows``), 按宿主平台取配置会拿错那一边。
    """
    rest = [item for item in arguments[1:] if not item.startswith("--")]
    command = (
        arguments[0] if arguments and not arguments[0].startswith("--") else "write"
    )
    if command != "write":
        print(f"未知子命令: {command}(只有 write)", file=sys.stderr)
        return 2
    name: str | None = None
    for index, item in enumerate(arguments):
        if item == "--platform" and index + 1 < len(arguments):
            name = normalize_platform(arguments[index + 1])
    if name is not None and name not in KNOWN_PLATFORMS:
        print(
            f"平台名不认识: {name}(可选 {'/'.join(KNOWN_PLATFORMS)})", file=sys.stderr
        )
        return 2
    target = Path(rest[0]) if rest else None
    written = write_platform_config(target, platform=name)
    print(f"已写入 {written}(平台 {name or current_platform()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
