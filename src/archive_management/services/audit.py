"""用户操作审计日志(阶段 E 迭代).

所有用户操作都通过 ``archive_management.audit`` 日志器记录:

- 备份、分支、恢复、删除原始目录、删除备份、修改计划等**高风险操作**用
  INFO 级别, 默认既落盘也打印到控制台;
- 主题/视图切换、选择、打开对话框等**基础操作**用 DEBUG 级别, 默认只落盘,
  控制台不打印(需要时用 ``--verbose`` 打开)。

日志中不写入 API Key、凭据、文件内容或完整敏感路径(PLAN 第 7 节): 路径字段
请用 :func:`redacted_path` 脱敏成"父目录名/条目名"后再传入。
"""

from __future__ import annotations

import logging

AUDIT_LOGGER_NAME = "archive_management.audit"
# 高风险操作: 默认打印并落盘.
LEVEL_OPERATION = logging.INFO
# 基础操作: 默认只落盘(不打印).
LEVEL_BASIC = logging.DEBUG

_MAX_FIELD_LENGTH = 200
# 命中这些关键字的字段只记录"是否提供", 不记录取值.
_SENSITIVE_MARKERS = ("key", "token", "password", "secret", "credential")

logger = logging.getLogger(AUDIT_LOGGER_NAME)


def log_action(action: str, *, basic: bool = False, **fields: object) -> None:
    """记录一次用户操作.

    ``basic=True`` 表示基础操作(用 DEBUG 输出); 其余按高风险操作处理
    (用 INFO 输出). ``fields`` 会被压成单行 ``key=value`` 文本, 空值跳过,
    敏感字段只记录是否提供。
    """
    level = LEVEL_BASIC if basic else LEVEL_OPERATION
    if not logger.isEnabledFor(level):
        return
    logger.log(level, "%s %s", action, _render(fields))


def log_failure(action: str, **fields: object) -> None:
    """记录一次失败的操作(ERROR 级别: 默认打印并落盘)."""
    if not logger.isEnabledFor(logging.ERROR):
        return
    logger.error("%s %s", f"{action}.failed", _render(fields))


def redacted_path(value: str) -> str:
    r"""返回脱敏后的路径文本(只保留最后两级片段).

    ``D:\Games\OuterWilds\save`` -> ``OuterWilds/save``: 足以定位对象,
    又不把用户完整路径写进日志。
    """
    parts = [part for part in str(value).replace("\\", "/").split("/") if part]
    if not parts:
        return ""
    return "/".join(parts[-2:])


def _render(fields: dict[str, object]) -> str:
    """把字段渲染为单行 key=value 文本(字段名排序保证可读且稳定)."""
    parts: list[str] = []
    for key in sorted(fields):
        value = fields[key]
        if value is None or value == "":
            continue
        if any(marker in key.lower() for marker in _SENSITIVE_MARKERS):
            parts.append(f"{key}=<hidden>")
            continue
        parts.append(f"{key}={_text(value)}")
    return " ".join(parts)


def _text(value: object) -> str:
    """把字段值转成单行文本, 过长时截断."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if len(text) > _MAX_FIELD_LENGTH:
        text = f"{text[: _MAX_FIELD_LENGTH - 3]}..."
    return f'"{text}"' if " " in text else text
