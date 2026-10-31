"""Steam 云端同步清单(``remotecache.vdf``)解析与 root 映射.

Steam 给每个启用云同步的游戏在 ``userdata/<account>/<appid>/remotecache.vdf``
留一份清单: 按文件记录该游戏同步出去的内容, 每条带一个 **root 编号**
(Steam SDK 里的 ``ERemoteStorageFileRoot``)说明文件在本机**逻辑上**的位置
(游戏安装目录 / 我的文档 / AppData 各层 ...)。这份清单由 Steam 自己维护, 属于
"平台云端同步清单"这类可信来源, 据此可以产出存档路径候选。

设计原则:

- **只读**: 只解析清单, 不改动 ``userdata`` 与任何用户数据;
- **不推断**: root 编号到本机目录只有一张显式映射表(见 :func:`cloud_root_path`);
  未知编号、只在别的平台有效的编号、以及 Steam 自己的云端镜像目录(root 0)一律
  返回 None 并记 DEBUG 日志, 不猜路径;
- **缺什么就少一条**: 账号目录不存在、清单缺失或损坏、条目没有 root 都只让结果
  变少(空列表), 不抛异常, 也不阻断手动添加游戏与存档位置;
- **候选粒度**: 同一 root 下的文件取**公共父目录**作为一条目录候选; 文件直接落在
  root 下(公共父目录为空)时按单个文件出候选 —— 既不会把"我的文档"整个当存档
  目录, 也不会漏掉就写在根下的存档;
- **不越界**: 条目里的绝对路径与 ``..`` 一律拒绝, 候选必须落在 root 目录之内
  (清单损坏或被改写时, 不能让候选跑到用户主目录或磁盘根目录去)。

root 编号 → 本地目录的对应关系来自 Steam SDK 枚举 ``ERemoteStorageFileRoot``
(OpenSteamworks 镜像中的枚举定义), 并用本机真实清单核对过: ``root 1`` 指向
``steamapps/common/<installdir>``、``root 2`` 指向用户的"文档"、``root 3`` 指向
``%LOCALAPPDATA%``, 与下表一致。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from archive_management.domain import PathKind, SavePathCandidate
from archive_management.services.pathcheck import is_within, normalize_path
from archive_management.services.platform_scan import (
    ScanRoots,
    parse_vdf_pairs,
    read_steam_installs,
    read_text,
    steam_roots,
)

logger = logging.getLogger(__name__)

# 远端清单的文件名与它所在的目录层级(userdata/<account>/<appid>/remotecache.vdf)。
REMOTE_CACHE_FILENAME = "remotecache.vdf"
USERDATA_DIRECTORY = "userdata"
# 候选的规则代码: 界面据此解释"这条来自 Steam 云端同步清单"。
REASON_STEAM_REMOTECACHE = "steam_remotecache"

# root 编号 → 语义名称(Steam SDK 的 ERemoteStorageFileRoot 枚举). 名称用于日志与
# 候选说明, 因此保留官方拼写, 不做翻译。
CLOUD_ROOT_NAMES: dict[int, str] = {
    0: "Default",
    1: "GameInstall",
    2: "WinMyDocuments",
    3: "WinAppDataLocal",
    4: "WinAppDataRoaming",
    5: "SteamUserBaseStorage",
    6: "MacHome",
    7: "MacAppSupport",
    8: "MacDocuments",
    9: "WinSavedGames",
    10: "WinProgramData",
    11: "SteamCloudDocuments",
    12: "WinAppDataLocalLow",
    13: "MacCaches",
    14: "LinuxHome",
    15: "LinuxXdgDataHome",
    16: "LinuxXdgConfigHome",
    17: "AndroidSteamPackageRoot",
    18: "WindowsHome",
}

# 清单条目的形状: ``"<相对路径>" { <扁平键值对> }``. 条目体内没有嵌套块(全是
# root/size/sha 之类的标量), 因此"键 + 花括号 + 不含花括号的键值体"足以安全切分;
# 头部字段(ChangeNumber/OSType)因为体内还有嵌套结构而不会被当成文件条目。
_REMOTE_CACHE_ENTRY = re.compile(r'"((?:[^"\\]|\\.)*)"\s*\{([^{}]*)\}')
# 相对路径的分隔符: VDF 里是正斜杠, 但被手工改过的清单可能用反斜杠。
_PATH_SEPARATORS = re.compile(r"[\\/]+")
# 绝对路径的开头(盘符或根斜杠): 这类条目直接拒绝, 不做任何"去掉前缀"的补救。
_ABSOLUTE_PREFIX = re.compile(r"^(?:[A-Za-z]:|[\\/])")
# userdata 下的账号目录名: Steam 用纯数字(SteamID), 其它内容一律不当账号。
_ACCOUNT_DIRECTORY = re.compile(r"\d+")


@dataclass(frozen=True)
class SteamCloudFile:
    """``remotecache.vdf`` 里的一条文件记录."""

    relative_path: str
    root_id: int
    size: int | None = None
    sha: str = ""


def cloud_root_name(root_id: int) -> str:
    """返回 root 编号的语义名称; 未登记的编号给出可读的占位名."""
    return CLOUD_ROOT_NAMES.get(root_id, f"root {root_id}")


def cloud_root_path(
    root_id: int, roots: ScanRoots, *, install_dir: Path | None = None
) -> Path | None:
    """把 root 编号解析成本机目录.

    返回 None 表示**无法定位**, 调用方应当记一条 DEBUG 日志并跳过这条记录:

    - 未登记的编号(含 ``SteamUserBaseStorage``/``SteamCloudDocuments``/
      ``AndroidSteamPackageRoot`` 这些本机无从映射的类型);
    - 只在别的平台有效的编号(例如 Windows 上的 ``MacDocuments``);
    - ``Default``(root 0): 它是 Steam 自己维护的云端镜像目录
      (``userdata/<account>/<appid>/remote``), 不是游戏真正写存档的地方;
    - ``GameInstall``(root 1): 没拿到该游戏的安装目录时(游戏已卸载等)。
    """
    if root_id == 1:
        return install_dir
    if roots.platform == "windows":
        home = roots.user_profile
        mapping: dict[int, Path] = {
            2: home / "Documents",
            3: roots.local_app_data,
            4: home / "AppData" / "Roaming",
            9: home / "Saved Games",
            10: roots.program_data,
            12: home / "AppData" / "LocalLow",
            18: home,
        }
    elif roots.platform == "macos":
        home = roots.user_profile
        mapping = {
            6: home,
            7: roots.local_app_data,
            8: home / "Documents",
            13: home / "Library" / "Caches",
        }
    else:
        home = roots.user_profile
        mapping = {
            14: home,
            15: roots.local_app_data,
            16: home / ".config",
        }
    return mapping.get(root_id)


def parse_remotecache(text: str) -> list[SteamCloudFile]:
    """解析一份远端清单, 按出现顺序返回其中的文件记录.

    没有 ``root`` 或 ``root`` 不是数字的条目会被跳过: 无法定位的记录留在结果里
    只会让上层去猜路径。
    """
    files: list[SteamCloudFile] = []
    for match in _REMOTE_CACHE_ENTRY.finditer(text):
        fields = {
            key.casefold(): value for key, value in parse_vdf_pairs(match.group(2))
        }
        raw_root = fields.get("root", "").strip()
        if not raw_root.isdigit():
            continue
        files.append(
            SteamCloudFile(
                relative_path=_unescape(match.group(1)),
                root_id=int(raw_root),
                size=_as_int(fields.get("size")),
                sha=fields.get("sha", "").strip(),
            )
        )
    return files


def _unescape(value: str) -> str:
    r"""还原 VDF 里的 ``\\`` 与 ``\"`` 转义."""
    return value.replace("\\\\", "\\").replace('\\"', '"')


def _as_int(raw: str | None) -> int | None:
    """把清单里的数字字段转成 int; 缺失或非法时为 None."""
    if raw is None:
        return None
    text = raw.strip()
    return int(text) if text.lstrip("-").isdigit() else None


def _relative_parts(relative: str) -> tuple[str, ...] | None:
    """把清单里的相对路径切成片段; 绝对路径或含 ``..`` 时返回 None(拒绝)."""
    if _ABSOLUTE_PREFIX.match(relative.strip()):
        return None
    parts = tuple(
        part for part in _PATH_SEPARATORS.split(relative) if part not in ("", ".")
    )
    if not parts or any(part == ".." for part in parts):
        return None
    return parts


def _common_directory(paths: Sequence[tuple[str, ...]]) -> tuple[str, ...]:
    """返回一组相对路径的公共父目录(片段形式); 没有公共父目录时为空元组."""
    common: list[str] = []
    for index, parts in enumerate(paths):
        directory = parts[:-1]
        if index == 0:
            common = list(directory)
            continue
        limit = min(len(common), len(directory))
        keep = 0
        while keep < limit and common[keep].casefold() == directory[keep].casefold():
            keep += 1
        common = common[:keep]
    return tuple(common)


class SteamCloudSource:
    """从 Steam 云端同步清单产出存档路径候选.

    使用哪几个 root 由本机平台决定: 清单是账号级记录, 里面可能仍有 Windows 专属
    的条目, 在 macOS/Linux 上这些会被跳过(见 :func:`cloud_root_path`)。
    """

    def __init__(self, roots: ScanRoots) -> None:
        """绑定探测环境(平台与根目录, 与本地探测共用同一套)."""
        self._roots = roots

    def accounts(self) -> list[str]:
        """返回本机 ``userdata`` 下的账号目录名(只认纯数字目录, 其余跳过)."""
        found: list[str] = []
        for root in steam_roots(self._roots):
            userdata = root / USERDATA_DIRECTORY
            try:
                children = sorted(userdata.iterdir(), key=lambda item: item.name)
            except OSError:
                logger.debug("无法列出 userdata 目录: %s", userdata)
                continue
            found.extend(
                child.name
                for child in children
                if child.is_dir() and _ACCOUNT_DIRECTORY.fullmatch(child.name)
            )
        return found

    def manifests(self, app_id: str) -> list[Path]:
        """返回该 AppID 的远端清单路径(每个账号/库目录各一份, 按路径排序)."""
        found: list[Path] = []
        for root in steam_roots(self._roots):
            userdata = root / USERDATA_DIRECTORY
            try:
                accounts = sorted(userdata.glob(f"*/{app_id}/{REMOTE_CACHE_FILENAME}"))
            except OSError:  # pragma: no cover - glob 失败极少见, 但仍要降级
                logger.debug("无法在 %s 下查找远端清单", userdata)
                continue
            found.extend(
                path
                for path in accounts
                if _ACCOUNT_DIRECTORY.fullmatch(path.parent.parent.name)
            )
        return found

    def candidates(
        self, app_id: str, *, install_dir: Path | None = None
    ) -> list[SavePathCandidate]:
        """返回该 AppID 的存档路径候选(没有清单或全部无法定位时为空).

        ``install_dir`` 是 ``root 1`` 的解析依据; 不传时按本机应用清单查找(游戏
        已卸载则查不到, 此时 root 1 的条目会被跳过, 其余 root 仍照常产出候选)。
        """
        wanted = app_id.strip()
        if not wanted:
            logger.debug("缺少 AppID, 无法查找云端同步清单")
            return []
        manifests = self.manifests(wanted)
        if not manifests:
            logger.debug(
                "没有找到 AppID %s 的云端同步清单(未启用云同步或账号目录缺失)", wanted
            )
            return []
        resolved = install_dir if install_dir is not None else self._install_dir(wanted)
        found: list[SavePathCandidate] = []
        for manifest in manifests:
            text = read_text(manifest)
            if text is None:
                logger.debug("远端清单不可读, 已跳过: %s", manifest)
                continue
            found.extend(self._grouped_candidates(text, install_dir=resolved))
        return _dedupe(found)

    def _install_dir(self, app_id: str) -> Path | None:
        """按本机应用清单查找该 AppID 的安装目录(root 1 的解析依据)."""
        for install in read_steam_installs(self._roots):
            if install.app_id == app_id:
                return install.install_dir
        logger.debug("本机应用清单里没有 AppID %s(游戏可能已卸载)", app_id)
        return None

    def _grouped_candidates(
        self, text: str, *, install_dir: Path | None
    ) -> list[SavePathCandidate]:
        """按 root 分组后产出候选(每个 root 独立解析, 互不影响)."""
        grouped: dict[int, list[SteamCloudFile]] = {}
        for entry in parse_remotecache(text):
            grouped.setdefault(entry.root_id, []).append(entry)
        if not grouped:
            logger.debug("云端同步清单里没有可用记录(文件可能损坏或不完整)")
            return []
        found: list[SavePathCandidate] = []
        for root_id, entries in sorted(grouped.items()):
            base = cloud_root_path(root_id, self._roots, install_dir=install_dir)
            if base is None:
                logger.debug(
                    "跳过 %d 条无法定位的云端记录: root %d(%s)",
                    len(entries),
                    root_id,
                    cloud_root_name(root_id),
                )
                continue
            found.extend(self._root_candidates(base, root_id, entries))
        return found

    def _root_candidates(
        self, base: Path, root_id: int, entries: Sequence[SteamCloudFile]
    ) -> list[SavePathCandidate]:
        """把一个 root 下的文件记录折成候选(公共父目录优先, 否则逐个文件)."""
        parsed: list[tuple[str, ...]] = []
        for entry in entries:
            parts = _relative_parts(entry.relative_path)
            if parts is None:
                logger.debug(
                    "跳过越界或非相对的云端记录(root %d): %s",
                    root_id,
                    entry.relative_path,
                )
                continue
            parsed.append(parts)
        if not parsed:
            return []
        common = _common_directory(parsed)
        if common:
            candidate = _build_candidate(
                base, common, root_id=root_id, kind="directory"
            )
            return [candidate] if candidate is not None else []
        found: list[SavePathCandidate] = []
        for parts in parsed:
            candidate = _build_candidate(base, parts, root_id=root_id, kind="file")
            if candidate is not None:
                found.append(candidate)
        return found


def _build_candidate(
    base: Path, parts: tuple[str, ...], *, root_id: int, kind: PathKind
) -> SavePathCandidate | None:
    """构造一条候选; 越出 root 之外时返回 None(不产出可疑候选)."""
    target = base.joinpath(*parts)
    normalized = normalize_path(str(target))
    if not is_within(Path(normalized), Path(normalize_path(str(base)))):
        logger.debug("候选越出 root %d 的范围, 已跳过: %s", root_id, normalized)
        return None
    return SavePathCandidate(
        path=normalized,
        reason_code=REASON_STEAM_REMOTECACHE,
        detail=cloud_root_name(root_id),
        confidence="high",
        path_kind=kind,
        relative_path="/".join(parts),
    )


def _dedupe(candidates: Iterable[SavePathCandidate]) -> list[SavePathCandidate]:
    """按规范化路径去重并排序(目录在前, 同一路径只留最先出现的一条)."""
    best: dict[str, SavePathCandidate] = {}
    for candidate in candidates:
        best.setdefault(candidate.path.casefold(), candidate)
    return sorted(
        best.values(), key=lambda item: (item.path_kind, item.path.casefold())
    )
