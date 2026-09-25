"""游戏状态与可用动作的规则单元测试.

锁住"归档只保留四项功能"这条边界: 规则表是界面与后端共用的唯一来源, 因此这里
遍历动作取值的全集来断言, 而不是逐个列举调用点 —— 新增动作时如果忘了决定它在
归档下是否可用, 这个用例会失败。
"""

from __future__ import annotations

from typing import get_args

import pytest

from archive_management.domain import (
    ARCHIVED_ACTIONS,
    Game,
    GameAction,
    action_allowed,
    is_active,
)
from archive_management.ui.models import HomeGameItem, ScheduleItem

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("游戏状态规则"),
    pytest.mark.story("归档与启用的动作边界"),
    pytest.mark.layer("unit"),
]

# 需求写明的四项: 删除游戏、导出游戏、取消归档、打开详情.
# 另加 ``manage``: 管理窗口是归档游戏删除自己的唯一入口, 因此仍然可以打开
# (窗口内部除删除外的按钮同样被规则置灰)。
_DOCUMENTED = {"detail", "delete", "export", "archive", "manage"}


def _home_item(*, archived: bool, enabled: bool) -> HomeGameItem:
    """构造一个主页列表项(只关心与状态相关的字段)."""
    return HomeGameItem(
        game_id="1",
        name="Demo",
        origin="steam",
        location_count=1,
        backup_count=0,
        last_backup_label="",
        activity_label="",
        risk=False,
        archived=archived,
        enabled=enabled,
    )


def _schedule_item(
    *, enabled: bool, game_enabled: bool, archived: bool = False
) -> ScheduleItem:
    """构造一个定时任务条目(只关心与状态相关的字段)."""
    return ScheduleItem(
        game_id="1",
        game_name="Demo",
        interval_text="30m",
        enabled=enabled,
        keep_auto=3,
        next_run_label="—",
        auto_count=0,
        game_enabled=game_enabled,
        archived=archived,
    )


def test_archived_games_only_allow_the_documented_actions() -> None:
    allowed = {
        action
        for action in get_args(GameAction)
        if action_allowed(action, archived=True)
    }
    assert allowed == set(ARCHIVED_ACTIONS)
    assert allowed == _DOCUMENTED


def test_every_action_is_allowed_while_the_game_is_not_archived() -> None:
    assert not [
        action
        for action in get_args(GameAction)
        if not action_allowed(action, archived=False)
    ]


def test_is_active_requires_enabled_and_not_archived() -> None:
    assert is_active(Game(name="Demo", enabled=True)) is True
    # 新建游戏默认停用: 不会响应快捷键与定时备份.
    assert is_active(Game(name="Demo")) is False
    assert is_active(Game(name="Demo", enabled=True, archived=True)) is False


def test_home_item_uses_the_same_rule_for_its_actions() -> None:
    """展示模型沿用同一套规则: 归档后只有四个动作可用."""
    archived = _home_item(archived=True, enabled=False)
    allowed = [action for action in get_args(GameAction) if archived.allow(action)]
    assert set(allowed) == _DOCUMENTED
    assert archived.backup_enabled is False
    assert archived.state_label == "已归档"
    assert _home_item(archived=False, enabled=False).state_label == "未启用"
    assert _home_item(archived=False, enabled=True).state_label == ""


def test_schedule_item_blocks_enabling_while_the_game_is_not_active() -> None:
    """停用或归档的游戏不能启用定时任务; 归档后连编辑也不允许."""
    paused = _schedule_item(enabled=False, game_enabled=False)
    assert paused.can_enable is False
    assert paused.can_toggle is False
    assert paused.state_label == "已暂停"
    assert _schedule_item(enabled=True, game_enabled=False).can_toggle is True
    archived = _schedule_item(enabled=False, game_enabled=False, archived=True)
    assert archived.editable is False
    assert archived.can_schedule is False
    assert archived.state_label == "已归档"
