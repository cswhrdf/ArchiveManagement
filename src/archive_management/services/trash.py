"""回收站删除后端(阶段 E 第 5 条).

PLAN 要求"删除原始存档位置"默认进入系统回收站而不是永久删除. 回收站实现
在受限权限、网络盘或未安装依赖的环境下可能不可用, 因此这里把它封装成可
注入的调用后端: 默认实现惰性导入 ``send2trash``, 失败时抛出
:class:`~archive_management.exceptions.StorageError`, 由用例转成用户可理解的
错误状态; 测试可注入替身, 验证"确实走回收站"而不触碰真实回收站。
"""

from __future__ import annotations

from collections.abc import Callable

from archive_management.exceptions import StorageError

# 回收站后端: 接收绝对路径, 失败时抛出异常.
TrashBackend = Callable[[str], None]


def send_to_trash(path: str) -> None:
    """把路径移入系统回收站(默认后端).

    依赖缺失或系统不支持回收站时抛出 :class:`StorageError`, 调用方必须把
    它当作"未删除"处理, 绝不能回退到永久删除。
    """
    try:
        from send2trash import send2trash
    except ImportError as exc:  # pragma: no cover - 取决于运行环境
        raise StorageError("当前环境缺少回收站支持, 未执行删除") from exc
    try:
        send2trash(str(path))
    except OSError as exc:
        raise StorageError(f"移入回收站失败: {exc}") from exc
