"""备份存储目录命名.

备份目录不再用数字 id 命名, 而是"游戏名称 + 由名称与存档路径推导的短哈希":

``<slug>-<token>`` 例如 ``OuterWilds-3f9a2c1b``

- ``slug``: 由游戏名称规范化而来(保留中日韩字符, 替换文件系统非法字符);
- ``token``: 游戏名称与全部存档路径拼接后的 SHA-256 前 8 位十六进制, 用来在
  同名游戏之间区分(例如同一个游戏的不同存档方案)。

名称可以被用户修改, 因此目录名在**第一次备份时确定并持久化**在 ``games``
表中(见 :attr:`archive_management.domain.Game.storage_key`): 改名或增删存档
位置都不会搬动已有备份, 磁盘上的目录始终与既有备份保持一致。命名的取值来源
(当时的游戏名称)另外记录在 :attr:`archive_management.domain.Game.original_name`,
供界面作为额外信息展示。

本模块是纯函数, 不做任何文件系统或数据库访问, 便于单独测试。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

#: 名称规范化后为空(或全是非法字符)时的兜底名称.
DEFAULT_SLUG = "game"
#: 目录名中名称部分的长度上限, 避免路径过长导致 Windows 创建失败.
MAX_SLUG_LENGTH = 40
#: 哈希部分保留的十六进制位数.
TOKEN_LENGTH = 8

# Windows/POSIX 都不允许出现在文件名中的字符, 以及控制字符.
_INVALID = re.compile(r"[\x00-\x1f\x7f<>:\"/\\|?*]+")
# 空白与下划线合并, 让名称保持可读又不产生连续分隔符.
_SEPARATORS = re.compile(r"[\s_]+")
# 仅在两端出现的点/空格/下划线同样不允许(Windows 会静默丢弃点与空格).
_EDGES = re.compile(r"^[.\s_]+|[.\s_]+$")


def game_slug(name: str) -> str:
    """把游戏名称规范化为可用作目录名的片段.

    非法字符替换为下划线, 连续空白/下划线合并为一个, 两端的点号、空格与
    下划线去除; 结果为空或过长时分别回退到 :data:`DEFAULT_SLUG` 与截断到
    :data:`MAX_SLUG_LENGTH`。
    """
    cleaned = _SEPARATORS.sub("_", _INVALID.sub("_", str(name).strip()))
    cleaned = _EDGES.sub("", cleaned[: MAX_SLUG_LENGTH + 1])
    return cleaned[:MAX_SLUG_LENGTH] or DEFAULT_SLUG


def storage_token(name: str, paths: Iterable[str]) -> str:
    """返回"名称 + 存档路径"的短哈希(小写十六进制).

    路径顺序不影响结果(排序后再拼接), 大小写与路径分隔符差异也一并归一,
    因此同一套名称与路径总是得到同一个 token。
    """
    parts = [str(name).strip().casefold()]
    parts.extend(sorted(_normalized_path(path) for path in paths))
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return digest[:TOKEN_LENGTH]


def _normalized_path(path: str) -> str:
    """归一化路径文本(统一分隔符与大小写), 仅用于计算哈希."""
    return str(path).strip().replace("\\", "/").casefold()


def game_folder(name: str, paths: Iterable[str]) -> str:
    """返回备份根目录下该游戏使用的目录名(``<slug>-<token>``)."""
    return f"{game_slug(name)}-{storage_token(name, paths)}"


def backup_relpath(folder: str, stamp: str, unique: str) -> str:
    """返回一次备份相对于备份根目录的路径(时间前缀便于人工排查)."""
    return f"{folder}/{stamp}-{unique}"
