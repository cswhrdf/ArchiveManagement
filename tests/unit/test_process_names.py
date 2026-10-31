"""进程名归一化与匹配(`domain/process_names.py`)的直测.

为什么要单开一个模块: 这三个函数以前只经 `probe_game_process` / `build_name_ownership`
被间接走到, 于是"哪些写法算同一个名字"这条规则从来没有被**逐条**钉住 —— 后缀表里只有
`.exe` 被真实用例覆盖, `.app` / `.bin` / `.run` 与"反向包含"那半边一直是空的, 而它们
恰恰是跨平台判断的依据(macOS 上是 `.app`, Linux 上常见 `.bin` / `.run`)。

判据按模块自己的承诺写: 归一化后只留小写字母与数字; 短于 `MIN_NAME_LENGTH` 的名字一律
不参与匹配(拿不准宁可不判断); 匹配是**双向包含**。
"""

from __future__ import annotations

import pytest

from archive_management.domain.process_names import (
    MIN_NAME_LENGTH,
    name_needles,
    names_match,
    normalize_name,
)

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("进程探测"),
    pytest.mark.story("进程名归一化与匹配"),
    pytest.mark.layer("unit"),
]


@pytest.mark.parametrize("suffix", [".exe", ".app", ".bin", ".run"])
def test_every_executable_suffix_is_stripped(suffix: str) -> None:
    """四个后缀都要认: 少一个就有一整类平台写法永远匹配不上."""
    assert normalize_name(f"Game{suffix}") == "game"


def test_the_suffix_check_ignores_case() -> None:
    """Windows 上大写后缀很常见(: 归一化先转小写, 后缀判断必须跟着它走)."""
    assert normalize_name("OuterWilds.EXE") == "outerwilds"


def test_spellings_with_separators_meet_in_the_middle() -> None:
    """`Outer Wilds` 与 `OuterWilds.exe` 要归一到同一个针(这是这个模块存在的理由)."""
    assert normalize_name("Outer Wilds") == normalize_name("OuterWilds.exe")


def test_a_name_that_merely_ends_with_those_letters_is_kept() -> None:
    """后缀判定看的是**分隔点**, 不是结尾那几个字母: `notexe` 不该被削成 `not`."""
    assert normalize_name("notexe") == "notexe"


def test_non_ascii_names_normalize_away_on_purpose() -> None:
    """中文名归一化后为空 —— 这是刻意的: 认不出名字就别拿它去匹配."""
    assert normalize_name("游戏") == ""


def test_a_candidate_shorter_than_the_floor_never_matches() -> None:
    """太短的进程名一律不参战(三两个字母的进程太容易误报)."""
    short = "a" * (MIN_NAME_LENGTH - 1)
    assert not names_match("a", short)


def test_matching_is_containment_in_both_directions() -> None:
    """双向包含: 游戏名比进程名长时也要匹配(带版本号的进程名就是这样)."""
    assert names_match("outerwilds", "outerwilds")
    assert names_match("outerwilds", "outerwildsgame")
    assert names_match("outerwildsgame", "outerwilds")


def test_needles_drop_unusable_names_and_keep_the_first_spelling() -> None:
    """去重(保留先出现的那个)、丢掉过短的、丢掉归一化后为空的, 其余顺序不变."""
    needles = name_needles(["Game 1", "game1.exe", "ab", "游戏", "Ok Name"])

    assert needles == ("game1", "okname")


def test_no_candidates_yield_no_needles() -> None:
    """一个名字都不给时返回空元组(调用方据此跳过枚举进程表)."""
    assert name_needles([]) == ()
