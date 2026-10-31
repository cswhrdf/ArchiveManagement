"""平台工具排除清单的单元测试(解析、匹配与"固定单行文件"约束).

清单决定"哪些探测结果不算游戏": 它是随程序发布的只读数据文件, 由开发人员手工维护。
这里覆盖解析与匹配规则, 以及真实清单能认出哪些工具、文件必须保持单行紧凑格式。
"""

from __future__ import annotations

import json
from dataclasses import replace
from importlib.resources import files

import pytest

from archive_management.services.exclusions import (
    EXCLUDED_FILENAME,
    ExcludedProgram,
    Exclusions,
    load_exclusions,
    parse_exclusions,
)

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("本地游戏探测"),
    pytest.mark.story("默认隐藏平台官方工具"),
    pytest.mark.layer("unit"),
]


def _programs(*entries: object) -> tuple[ExcludedProgram, ...]:
    """把 JSON 形状的条目解析成规则(便于表格化断言)."""
    return parse_exclusions({"programs": list(entries)})


def test_parse_reads_every_matching_field_and_folds_case() -> None:
    """解析: AppID/名称/前缀都能用, 平台与名称大小写不敏感."""
    parsed = _programs(
        {
            "platform": "Steam",
            "app_ids": ["228980", 0],
            "names": [" OBS Studio "],
            "name_prefixes": ["Steam Linux Runtime"],
            "reason": "tool",
        }
    )

    assert len(parsed) == 1
    program = parsed[0]
    assert program.app_ids == frozenset({"228980"})
    assert program.names == frozenset({"obs studio"})
    assert program.name_prefixes == ("steam linux runtime",)
    assert program.matches(platform="steam", app_id="228980", name="任意") is True
    assert program.matches(platform="steam", app_id=None, name="obs studio") is True
    assert (
        program.matches(platform="steam", app_id=None, name="Steam Linux Runtime 3.0")
        is True
    )
    assert program.matches(platform="steam", app_id=None, name="Hades") is False
    assert program.matches(platform="epic", app_id="228980", name="任意") is False


def test_parse_skips_entries_without_any_matching_field() -> None:
    """没有 AppID 也没有名称的条目视为无效: 有匹配条件才算规则."""
    assert _programs({"platform": "steam", "reason": "tool"}, "不是对象", {}) == ()
    assert parse_exclusions({"programs": "不是列表"}) == ()
    assert parse_exclusions([]) == ()


def test_empty_program_list_matches_nothing() -> None:
    """一条规则都没有时不会把所有候选都排除掉."""
    exclusions = Exclusions()

    assert len(exclusions) == 0
    assert exclusions.match(platform="steam", app_id="1", name="Hades") is None


def test_match_returns_the_first_matching_rule() -> None:
    """命中多条时返回第一条(便于日志里说明"为什么被隐藏")."""
    exclusions = Exclusions(
        [
            ExcludedProgram(reason="first", names=frozenset({"tool"})),
            ExcludedProgram(reason="second", names=frozenset({"tool"})),
        ]
    )

    matched = exclusions.match(platform="steam", app_id=None, name="Tool")

    assert matched is not None
    assert matched.reason == "first"


def test_builtin_list_covers_the_shipped_platform_tools() -> None:
    """内置清单: 平台工具被认出, 而同库的真实游戏不会被误伤."""
    exclusions = load_exclusions()

    assert len(exclusions) >= 4
    shared = exclusions.match(
        platform="steam",
        app_id="228980",
        name="Steamworks Common Redistributables",
    )
    assert shared is not None
    assert shared.reason == "redistributable"
    obs = exclusions.match(platform="steam", app_id=None, name="OBS Studio")
    assert obs is not None
    assert obs.reason == "tool"
    # 监控目录发现的平台自带工具(不限来源平台, 按名称前缀命中).
    assert (
        exclusions.match(
            platform="monitored",
            app_id=None,
            name="Steam Controller Configs",
        )
        is not None
    )
    assert (
        exclusions.match(
            platform="steam",
            app_id=None,
            name="Steam Linux Runtime 3.0 (sniper)",
        )
        is not None
    )
    for name in ("Hades", "Deep Rock Galactic", "Satisfactory"):
        assert exclusions.match(platform="steam", app_id=None, name=name) is None


def test_shipped_file_is_one_compact_line_without_a_description() -> None:
    """清单是固定的手工数据: 单行紧凑 JSON, 不带描述字段, 也不写排版空白.

    提交钩子(`compact-json`)会自动把文件压成这个形状, 这里守住提交后的状态:
    `--no-verify` 提交、钩子被摘掉、或用编辑器格式化后忘了提交, 都会在这里报错。
    """
    raw = (
        files("archive_management")
        .joinpath("resources", EXCLUDED_FILENAME)
        .read_text(encoding="utf-8")
    )
    payload = json.loads(raw)
    hint = (
        "清单应保持单行紧凑 JSON: 运行 "
        "`uv run python scripts/compact_json.py "
        f"src/archive_management/resources/{EXCLUDED_FILENAME}` 转换, 或直接重新提交"
        "(提交钩子会自动转换)。"
    )

    assert set(payload) == {"version", "programs"}, hint
    assert "\n" not in raw, hint
    assert ": " not in raw, hint
    assert raw == json.dumps(payload, ensure_ascii=False, separators=(",", ":")), hint
    assert len(load_exclusions()) == len(payload["programs"]), hint


def test_program_defaults_match_nothing() -> None:
    """规则字段全空时不匹配任何东西(空字段表示不作要求, 不是通配)."""
    empty = ExcludedProgram()

    assert empty.platform == ""
    assert empty.matches(platform="steam", app_id="1", name="Hades") is False
    assert (
        replace(empty, reason="仅用于日志").matches(
            platform="steam", app_id="1", name="Hades"
        )
        is False
    )
