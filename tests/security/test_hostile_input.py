"""恶意配置、不可信数据文件与日志脱敏.

覆盖"不可信输入不得进入文件操作流程"的三条边界:

1. 应用配置: 未知字段、错误类型、越界数值、非法快捷键、版本不兼容;
2. 外部数据文件(Steam 数据): 字段类型错误、未知字段、空名称、版本不兼容;
3. 审计日志: 敏感字段不落明文、路径脱敏、单行截断。

全部输入都是测试内构造的假数据, 不含真实凭据; 日志断言只针对内存里捕获的记录。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from archive_management.config import (
    CONFIG_FILENAME,
    load_or_repair_config,
    parse_config,
)
from archive_management.domain.steam_data import SteamDataFile
from archive_management.exceptions import ConfigurationError, SteamIntegrationError
from archive_management.services.audit import log_action, redacted_path
from archive_management.services.steam import LocalSteamDataReader
from reporting import SecurityRecorder

pytestmark = [
    pytest.mark.security,
    pytest.mark.critical,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("不可信输入防护"),
    pytest.mark.story("拒绝恶意配置与脏数据"),
    pytest.mark.layer("security"),
    pytest.mark.timeout(120),
]

_CATEGORY = "hostile_input"

# 恶意配置载荷: 每条都应被 pydantic 严格校验拒绝(而不是被忽略后继续使用).
_HOSTILE_CONFIGS: list[tuple[str, dict[str, Any]]] = [
    ("未知顶层字段", {"version": 1, "evil": "/etc/passwd"}),
    ("未知子字段", {"version": 1, "logging": {"evil": 1}}),
    ("配置文件版本不兼容", {"version": 999}),
    ("主题取值非法", {"version": 1, "theme": "neon"}),
    ("日志上限为负", {"version": 1, "logging": {"max_bytes": -1}}),
    ("日志级别不在白名单", {"version": 1, "logging": {"level": "TRACE"}}),
    ("布尔字段取值非法", {"version": 1, "logging": {"console": "maybe"}}),
    ("快捷键为路径穿越字符串", {"version": 1, "hotkeys": {"save": "../../evil"}}),
    ("快捷键缺少修饰键", {"version": 1, "hotkeys": {"save": "s"}}),
    ("快捷键缺少字母键", {"version": 1, "hotkeys": {"branch": "<win>+<alt>+<shift>"}}),
]

# 恶意 Steam 数据载荷: 字段类型/取值/未知字段都必须被拒绝.
_HOSTILE_STEAM: list[tuple[str, dict[str, Any]]] = [
    ("app_id 不是整数", {"version": 1, "games": [{"app_id": "abc", "name": "x"}]}),
    ("app_id 非正数", {"version": 1, "games": [{"app_id": 0, "name": "x"}]}),
    ("名称为空白", {"version": 1, "games": [{"app_id": 1, "name": "   "}]}),
    (
        "游戏条目含未知字段",
        {"version": 1, "games": [{"app_id": 1, "name": "x", "evil": "y"}]},
    ),
    ("版本字段类型错误", {"version": "one", "games": []}),
    ("games 不是列表", {"version": 1, "games": {"app_id": 1}}),
]


@pytest.mark.parametrize(("label", "payload"), _HOSTILE_CONFIGS)
def test_malicious_config_payloads_are_rejected(
    label: str, payload: dict[str, Any], security_recorder: SecurityRecorder
) -> None:
    """恶意配置一律抛出 ConfigurationError, 不会静默取默认值继续运行."""
    rejected = False
    try:
        parse_config(payload)
    except ConfigurationError:
        rejected = True
    security_recorder.expect_blocked(
        category=_CATEGORY,
        scenario=f"配置载荷: {label}",
        input_summary=json.dumps(payload, ensure_ascii=False)[:120],
        expected="抛出 ConfigurationError",
        actual="已拒绝" if rejected else "未拒绝, 载荷被接受",
        blocked=rejected,
    )


def test_non_object_config_file_is_reset_not_used(tmp_path: Path) -> None:
    """顶层不是对象的配置文件不会被当成配置使用, 而是还原为默认值并保留原件."""
    config_path = tmp_path / "config" / CONFIG_FILENAME
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(["evil"]), encoding="utf-8")

    result = load_or_repair_config(config_path)

    assert result.reset is True
    assert result.backup is not None
    assert result.backup.is_file()
    assert result.config.hotkeys.save  # 回到合法默认值
    assert json.loads(config_path.read_text(encoding="utf-8"))["version"] == 1


@pytest.mark.parametrize(("label", "payload"), _HOSTILE_STEAM)
def test_hostile_steam_data_is_rejected(label: str, payload: dict[str, Any]) -> None:
    """Steam 数据模型的字段校验必须拒绝脏数据."""
    with pytest.raises(ValidationError):
        SteamDataFile.model_validate(payload)


def test_steam_reader_rejects_broken_and_future_files(tmp_path: Path) -> None:
    """损坏 JSON 与未来版本的 Steam 数据文件都抛 SteamIntegrationError."""
    reader = LocalSteamDataReader()
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(SteamIntegrationError):
        reader.read(broken)

    future = tmp_path / "future.json"
    future.write_text(json.dumps({"version": 99, "games": []}), encoding="utf-8")
    with pytest.raises(SteamIntegrationError):
        reader.read(future)

    with pytest.raises(SteamIntegrationError):
        reader.read(tmp_path / "missing.json")


def test_audit_log_hides_sensitive_fields(audit_log: list[str]) -> None:
    """敏感字段名只记录"是否提供", 取值不得出现在日志里."""
    # 用字典传递敏感字段: 既能覆盖全部关键字, 也不会把假凭据写成代码里的字面量.
    fake_secrets = {
        "api_key": "sk-live-000",
        "token": "tok-000",
        "password": "pw-000",
        "credential": "cred-000",
    }
    log_action(
        "cli.probe",
        api_key=fake_secrets["api_key"],
        token=fake_secrets["token"],
        password=fake_secrets["password"],
        credential=fake_secrets["credential"],
        game_id=7,
    )
    joined = " ".join(audit_log)

    assert "<hidden>" in joined
    for secret in fake_secrets.values():
        assert secret not in joined
    assert "game_id=7" in joined  # 非敏感字段照常记录


def test_audit_log_redacts_user_paths(audit_log: list[str]) -> None:
    """用户完整路径不落日志: 只保留最后两级片段."""
    full = Path.home() / "Games" / "OuterWilds" / "save"
    log_action("restore.start", path=redacted_path(str(full)))
    joined = " ".join(audit_log)

    assert str(Path.home()) not in joined
    assert "OuterWilds/save" in joined


def test_audit_values_are_single_line_and_truncated(audit_log: list[str]) -> None:
    """多行与超长取值会被压成单行并截断, 避免日志被注入换行或撑爆."""
    log_action("cli.probe", note="first\nsecond" + "x" * 500)
    line = audit_log[-1]

    assert "\n" not in line
    assert len(line) < 300
    assert line.startswith("cli.probe")
