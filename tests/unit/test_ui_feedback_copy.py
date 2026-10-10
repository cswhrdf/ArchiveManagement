"""反馈文案的守卫(进行中可辨识 / 结果有去处 / 失败给下一步).

三条判据都落在**文案本身**上, 因为这三件事没有别的可测量处:

A. **失败文案必须告诉用户接下来怎么办** —— 只报错原文(甚至只报 OS 的 reason)
   等于把问题丢回给用户。量法: ``error.*`` 每一句(两套语言)都要含一个"下一步"
   信号词(请 / 重试 / 先 / 打开 / 刷新 ...).
B. **结果文案必须带去处与计数** —— "已删除该备份"没说删了哪一份, "导出完成"没说
   导到哪个文件。量法: 登记表里的每条 ``result.*`` 都必须带指定的占位符.
C. **兜底** —— ``result.*`` 里没进登记表的必须写进豁免清单(并说明为什么), 新加一条
   结果文案却没人判它时会直接变红.

量的是 i18n 文件(两套语言都量), 纯数据, 不需要 GUI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

I18N = Path(__file__).resolve().parents[2] / "src/archive_management/resources/i18n"
LOCALES = ("zh-CN", "en")

pytestmark = [
    pytest.mark.i18n,
    pytest.mark.trivial,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("反馈与文案"),
    pytest.mark.story("失败要说清下一步"),
    pytest.mark.layer("unit"),
]

# "下一步"信号词: 至少命中一个才算给了出路(两套语言各一组).
_NEXT_STEP = {
    "zh-CN": (
        "请",
        "可以",
        "无需",
        "换一个",
        "换成",
        "改成",
        "重新",
        "刷新",
        "重试",
        "输入",
        "填写",
        "选择",
        "关闭",
        "查看",
        "删减",
        "在游戏库",
    ),
    "en": (
        "please",
        "try again",
        "again",
        "first",
        "before",
        "refresh",
        "check",
        "instead",
        "no need",
        "enter",
        "pick",
        "choose",
        "find it",
        "close the game",
        "change it",
        "shorten",
        "by hand",
        "run the scan",
    ),
}

# 结果文案 -> 必须出现的占位符(去处: {file}/{path}; 计数: {count}/{files}/{size}...).
_RESULT_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "result.backup_done": ("{name}",),
    "result.branch_done": ("{branch_name}",),
    "result.delete_cascade": ("{count}",),
    "result.delete_single": ("{title}",),
    "result.delete_shift": ("{title}",),
    "result.export_batch_done": ("{file}", "{games}", "{size}"),
    "result.export_done": ("{file}", "{files}", "{size}"),
    "result.game_added": ("{name}",),
    "result.game_added_disabled": ("{name}",),
    "result.game_added_with_paths": ("{name}", "{count}"),
    "result.import_batch_done": ("{games}", "{skipped}", "{skipped_games}"),
    "result.import_done": ("{name}", "{nodes}", "{size}"),
    "result.import_done_skipped": ("{name}", "{skipped}"),
    "result.import_skipped": ("{name}", "{skipped}"),
    "result.location_deleted": ("{path}", "{count}"),
    "result.renamed": ("{title}",),
    "result.restore_written": ("{name}", "{title}", "{files}"),
    "result.schedule_saved": ("{interval}",),
}

# 不进登记表的 ``result.*`` 及理由(兜底断言要求每一条都被归过类).
_RESULT_EXEMPT: dict[str, str] = {
    "result.backup_canceled": '取消类: 结果就是"什么都没写", 没有去处与计数可给',
    "result.delete_export_simulated": '演示后端: 去处由文案里的"不会写出文件"说明',
    "result.export_batch_simulated": "演示后端: 同上(计数在文案里)",
    "result.export_simulated": "演示后端: 同上",
    "result.import_batch_all_skipped": '全跳过: 只说"什么都没有导入", 计数在 {games}',
    "result.import_batch_canceled": "取消类: 说明已导入的留下、其余不再导入",
    "result.import_batch_simulated": "演示后端: 同上",
    "result.import_simulated": "演示后端: 同上",
    "result.restore_canceled": '取消类: 脚本里已写明"原始存档未被修改"',
    "result.schedule_cleared": '状态类: 只有"已取消定时备份"这一件事可说',
}

_ZH = json.loads((I18N / "zh-CN.json").read_text(encoding="utf-8"))


def _catalogue(locale: str) -> dict[str, str]:
    """读一份文案表(键 -> 文案)."""
    data: dict[str, str] = json.loads(
        (I18N / f"{locale}.json").read_text(encoding="utf-8")
    )
    return data


def _keys(prefix: str) -> list[str]:
    return sorted(key for key in _ZH if key.startswith(prefix))


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("key", _keys("error."))
def test_error_messages_tell_the_user_what_to_do_next(locale: str, key: str) -> None:
    """每条 ``error.*`` 都要给出下一步(不能只报错原文)."""
    text = _catalogue(locale)[key]
    hit = [word for word in _NEXT_STEP[locale] if word in text.lower()]
    assert hit, f"{locale}: {key} 没有给出下一步: {text!r}"


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("key", sorted(_RESULT_PLACEHOLDERS))
def test_result_messages_carry_where_it_went_and_how_many(
    locale: str, key: str
) -> None:
    """结果文案必须带登记的占位符(去处 / 计数), 少一个都算"没说清"."""
    text = _catalogue(locale)[key]
    for placeholder in _RESULT_PLACEHOLDERS[key]:
        assert placeholder in text, f"{locale}: {key} 缺 {placeholder}: {text!r}"


def test_every_result_key_is_registered_or_explained() -> None:
    """兜底: 每条 ``result.*`` 要么进登记表, 要么在豁免清单里写明理由."""
    judged = set(_RESULT_PLACEHOLDERS) | set(_RESULT_EXEMPT)
    assert set(_keys("result.")) <= judged, (
        f"没有判过的结果文案: {sorted(set(_keys('result.')) - judged)}"
    )


def test_the_registries_are_not_empty() -> None:
    """兜底: 两个清单都不能退化成空表(否则上面两条会静默空转)."""
    assert len(_keys("error.")) >= 30
    assert len(_RESULT_PLACEHOLDERS) >= 15


def test_both_locales_define_the_same_keys() -> None:
    """两套语言的键必须一一对应(少了键会在运行时抛 KeyError)."""
    assert set(_catalogue("zh-CN")) == set(_catalogue("en"))


def test_next_step_words_actually_match_some_messages() -> None:
    """兜底: 信号词表本身要真的能匹配上(表写错了会让 A 类判据变成空转)."""
    for locale in LOCALES:
        catalogue = _catalogue(locale)
        matched = sum(
            1
            for key in _keys("error.")
            if any(word in catalogue[key].lower() for word in _NEXT_STEP[locale])
        )
        assert matched == len(_keys("error.")), f"{locale}: 只有 {matched} 条命中信号词"
