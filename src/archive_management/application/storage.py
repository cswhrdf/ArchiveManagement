"""备份根目录的修改校验与存储位置总览.

设置页「存储位置」分区背后的用例层: 校验一个新备份根候选是否可用, 以及
组装"当前生效 / 下次启动"两个口径的总览。**只允许改备份根**(P2b 的范围
红线), 数据库/配置/日志/封面一律留在内置位置。
"""

from __future__ import annotations

from pathlib import Path

from archive_management.services.pathcheck import (
    dangerous_target_reason,
    is_writable_target,
)

# 拒绝原因代码(界面映射为文案; 其余代码来自 pathcheck 的公共清单:
# not_absolute / drive_root / user_home / protected / contains_protected)。
REASON_NOT_WRITABLE = "not_writable"
# 与某款游戏的原始存档位置相同: 那会让"备份绝不写进原始目录"的保护失效。
REASON_SAVE_PATH_CLASH = "save_path_clash"


def backup_root_error(
    raw: str,
    *,
    protected: tuple[Path, ...],
    save_paths: tuple[str, ...] = (),
) -> str | None:
    """校验新的备份根候选, 返回拒绝原因代码; 合法(含空输入 = 恢复内置)返回 None.

    危险目标复用 :func:`dangerous_target_reason`, 保护名单由调用方给: 内置
    ``data_dir`` 与当前生效的备份根 —— 不许把备份指进数据目录或备份自身里面。
    这里只补它不管的两条: 与任何游戏的原始存档位置相同, 以及目标(或其最近
    已存在的父目录)不可写。
    """
    text = raw.strip()
    if not text:
        return None  # 清空输入 = 恢复内置位置, 合法
    reason = dangerous_target_reason(
        text, protected=tuple(str(item) for item in protected)
    )
    if reason is not None:
        return reason
    target = Path(text).expanduser().resolve()
    for save_path in save_paths:
        if target == Path(save_path).expanduser().resolve():
            return REASON_SAVE_PATH_CLASH
    if not is_writable_target(str(target)):
        return REASON_NOT_WRITABLE
    return None


def normalize_backup_root(raw: str) -> str:
    """把合法候选规范化为存进配置的文本(绝对路径; 空输入原样返回空串)."""
    text = raw.strip()
    if not text:
        return ""
    return str(Path(text).expanduser().resolve())
