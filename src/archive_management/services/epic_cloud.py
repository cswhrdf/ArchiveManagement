r"""Epic 启动器云存档缓存(``CloudCache``)解析与宏路径映射.

Epic 给开启云存档的游戏在
``%LOCALAPPDATA%\\EpicGamesLauncher\\Saved\\Saves\\<账号GUID>\\<游戏AppName>\\CloudCache\\``
留下证据: 每次同步写一个 ``<时间戳>.manifest`` 二进制容器(魔数 ``0C C0 BE 44``,
非标准压缩格式), 里面内嵌一条明文 ``CloudSaveFolder${AppData}/厂商/游戏/SaveGames/``
宏路径 —— 云存档的**根目录由启动器自己记录**, 因此属于"平台云端同步清单"这类
可信来源(2026-11-05 本机实测, 与 Steam 云清单同一条判据: 本机文件 + 平台客户端
维护 + 指向真实存档路径), 据此可以产出存档路径候选。

与 :mod:`archive_management.services.steam_cloud` 同一套纪律:

- **只读**: 只解析缓存, 不改动启动器数据;
- **不猜路径**: 宏到本机目录只有一张显式映射表(见 :func:`macro_roots`),
  未登记的宏、非宏开头的段、绝对路径与 ``..`` 一律丢弃;
- **目录存在性验证**: 宏展开后的目录必须**真实存在**才算候选(实测 ``${AppData}``
  落在 ``AppData\\Local`` 而不是直觉上的 Roaming, 但不排除个别游戏映射不同,
  存在性检查就是兜底 —— 展开后不存在的目录直接丢, 不产出可疑候选);
- **缺什么就少一条**: 启动器没装、没登录、没开云存档、缓存损坏, 都只让结果
  变少(空列表)并记 DEBUG 日志, 不抛异常, 不阻断手动添加存档位置;
- **宁缺勿错**: ``.manifest`` 是未公开的二进制容器, 这里只做保守的明文扫描
  (定位 ``CloudSaveFolder`` 前缀、取后续可打印段), 格式漂移即放弃该清单;
  ``IncludeList.txt`` 只有文件名没有路径, 不能单独当位置证据, 干脆不读;
- **仅 Windows**: macOS 侧缓存位置未实测(2026-11-05), Linux 无官方启动器,
  这两个平台显式返回空并记日志, 缺口交给"手动添加"。

游戏身份映射: CloudCache 的目录名是 AppName, 与安装清单(``*.item``)的文件名/
``AppName`` 字段一致 —— 游戏标识已知时直接定位; 缺失时按安装目录回查安装清单
(:func:`archive_management.services.platform_scan.read_epic_items`)。游戏已卸载、
安装清单也不在了, 就查不到(孤儿缓存宁可不用), 不做"按路径段猜游戏"的启发。

候选置信度是 ``medium``(低于 Steam 云清单的 ``high``): 格式无公开契约、覆盖面
只到"开过云存档的游戏"、AppName 映射依赖安装清单 —— 证据本身可信, 但判定的
确定性不如 Steam, 由用户确认(危险目标另有一层校验)兜住。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from pathlib import Path

from archive_management.domain import SavePathCandidate
from archive_management.services.pathcheck import normalize_path
from archive_management.services.platform_scan import ScanRoots, read_epic_items

logger = logging.getLogger(__name__)

# 启动器云存档缓存的目录层级(Saves 下按 <账号>/<游戏AppName>/CloudCache 组织)。
LAUNCHER_SAVES_PARTS = ("EpicGamesLauncher", "Saved", "Saves")
CLOUD_CACHE_DIRECTORY = "CloudCache"
# 候选的规则代码: 界面据此解释"这条来自 Epic 云存档缓存"。
REASON_EPIC_CLOUDCACHE = "epic_cloudcache"

# manifest 容器里明文宏路径的前缀; 之后紧跟一段可打印 ASCII(到第一个结构字节止)。
_CLOUD_SAVE_PREFIX = b"CloudSaveFolder"
# 可打印段取 0x21..0x7e 且不含空格: 容器里明文串之后紧跟二进制结构, 放宽到空格
# 会把垃圾并进路径段。带空格的存档目录因此认不出, 属"宁缺勿错"的已知代价。
_PRINTABLE_BYTES = frozenset(range(0x21, 0x7F))
# 宏路径的形状: ${宏名}/相对段(斜杠或反斜杠都接受, 与实测样本一致)。
_MACRO_HEAD = re.compile(r"^\$\{([A-Za-z]+)\}[/\\](.*)$", re.DOTALL)
_PATH_SEPARATORS = re.compile(r"[/\\]+")
# 绝对路径的开头(盘符或根斜杠): 这类段直接拒绝, 不做"去掉前缀"的补救。
_ABSOLUTE_PREFIX = re.compile(r"^(?:[A-Za-z]:|[\\/])")


def parse_cloud_save_folders(data: bytes) -> list[str]:
    """从一份 manifest 容器里提取 CloudSaveFolder 宏路径(按出现顺序去重).

    只认 ``CloudSaveFolder`` 后面紧跟可打印段的形态; 提不出来(格式漂移)就返回
    空列表, 由调用方记 DEBUG 日志。
    """
    found: list[str] = []
    seen: set[str] = set()
    start = 0
    while True:
        index = data.find(_CLOUD_SAVE_PREFIX, start)
        if index < 0:
            return found
        segment = _printable_run(data, index + len(_CLOUD_SAVE_PREFIX))
        start = index + 1
        if segment and segment not in seen:
            seen.add(segment)
            found.append(segment)


def _printable_run(data: bytes, begin: int) -> str:
    """返回 ``begin`` 起的可打印 ASCII 段(到第一个非路径字节止)."""
    end = begin
    while end < len(data) and data[end] in _PRINTABLE_BYTES:
        end += 1
    return data[begin:end].decode("ascii", errors="replace")


def macro_roots(
    macro: str, roots: ScanRoots, *, install_dir: Path | None
) -> list[Path]:
    """宏名 → 候选本机根目录(未登记的宏返回空列表).

    ``${AppData}`` 的两个候选根按实测顺序排列(Local 优先, Roaming 次之),
    由调用方的目录存在性验证挑出真实存在的那一个。``${InstalledDir}`` 只在
    调用方给了安装目录时可用。
    """
    key = macro.casefold()
    home = roots.user_profile
    if key in ("appdata", "localappdata"):
        # 2026-11-05 本机实测 ${AppData} 落在 AppData\Local(Roaming 下没有那个
        # 目录); 把 Roaming 留作次选, 存在性验证兜住个别游戏的差异。
        return [roots.local_app_data, home / "AppData" / "Roaming"]
    if key in ("mydocuments", "personal"):
        return [home / "Documents"]
    if key == "userprofile":
        return [home]
    if key == "installeddir":
        return [] if install_dir is None else [install_dir]
    logger.debug("未登记的 Epic 宏 ${%s}, 已跳过", macro)
    return []


def _relative_parts(rest: str) -> tuple[str, ...] | None:
    """把宏后面的相对路径切成安全段; 越界/未展开/绝对路径一律返回 None.

    候选必须落在宏对应的根目录之内: 清单损坏或被改写时, 不能让候选跑到用户
    主目录或磁盘根目录去。
    """
    if _ABSOLUTE_PREFIX.match(rest):
        return None
    parts = tuple(
        part for part in _PATH_SEPARATORS.split(rest) if part not in ("", ".")
    )
    if any(part == ".." or ":" in part or "${" in part for part in parts):
        return None
    return parts


def _folder_target(
    raw: str, roots: ScanRoots, *, install_dir: Path | None
) -> Path | None:
    """把一条宏路径解析成本机目录; 解析不了或目录不存在时返回 None."""
    matched = _MACRO_HEAD.match(raw.strip())
    if matched is None:
        logger.debug("CloudSaveFolder 段不是宏路径, 已跳过: %s", raw)
        return None
    macro, rest = matched.group(1), matched.group(2).strip().rstrip("/\\")
    parts = _relative_parts(rest)
    if not parts:
        logger.debug("CloudSaveFolder 段的子路径不安全或为空, 已跳过: %s", raw)
        return None
    for base in macro_roots(macro, roots, install_dir=install_dir):
        target = base.joinpath(*parts)
        if target.is_dir():
            return target
    logger.debug("宏展开后的目录不存在, 已跳过: %s", raw)
    return None


class EpicCloudSource:
    """从 Epic 云存档缓存产出存档路径候选."""

    def __init__(self, roots: ScanRoots) -> None:
        """绑定探测环境(平台与根目录, 与本地探测共用同一套)."""
        self._roots = roots

    def app_name_for(self, install_dir: Path) -> str:
        """按安装目录回查游戏标识(AppName; 找不到时为空串).

        ``Game`` 记录不带 Epic 标识, 导入流程只能给安装目录; 安装清单
        (``*.item``)还在, 就能按 ``InstallLocation`` 对上 AppName。
        """
        wanted = normalize_path(str(install_dir)).casefold()
        for item in read_epic_items(self._roots):
            if normalize_path(str(item.install_dir)).casefold() == wanted:
                return item.app_name
        logger.debug("Epic 安装清单里找不到安装目录 %s(游戏可能已卸载)", install_dir)
        return ""

    def cloud_caches(self, app_name: str) -> list[Path]:
        """返回该游戏的 CloudCache 目录(每个账号各一份, 按账号目录名排序).

        目录名比对不区分大小写, 但**拼接用的是磁盘上真实的目录名**: 拿
        ``casefold()`` 后的名字去拼路径, 在大小写敏感的卷上会一条都找不到
        (Windows 的卷恰好不敏感, 但这不该成为正确性的前提)。
        """
        wanted = app_name.strip().casefold()
        if not wanted:
            return []
        saves = self._saves_root()
        try:
            accounts = sorted(child for child in saves.iterdir() if child.is_dir())
        except OSError as exc:
            logger.debug("无法列出 Epic 云存档目录 %s: %s", saves, exc)
            return []
        caches: list[Path] = []
        for account in accounts:
            caches.extend(_account_caches(account, wanted))
        return caches

    def candidates(
        self, app_id: str, *, install_dir: Path | None = None
    ) -> list[SavePathCandidate]:
        """返回该游戏的存档路径候选(没有缓存或全部无法定位时为空).

        ``app_id`` 是 Epic 的 AppName(探测结果的 ``.item`` 文件名); 缺失时按
        ``install_dir`` 回查安装清单。两样都没有就查不了, 返回空列表。
        """
        if self._roots.platform != "windows":
            logger.debug("Epic 云存档缓存仅接入 Windows(其余平台位置未实测), 已跳过")
            return []
        caches = self.cloud_caches(app_id)
        if not caches and install_dir is not None:
            caches = self.cloud_caches(self.app_name_for(install_dir))
        if not caches:
            logger.debug(
                "没有找到 %s 的 Epic 云存档缓存(未开云存档或启动器没装)", app_id
            )
            return []
        found: list[SavePathCandidate] = []
        for cache in caches:
            found.extend(self._cache_candidates(cache, install_dir=install_dir))
        return _dedupe(found)

    def _saves_root(self) -> Path:
        """返回启动器的云存档根目录(Saves)."""
        return self._roots.local_app_data.joinpath(*LAUNCHER_SAVES_PARTS)

    def _cache_candidates(
        self, cache: Path, *, install_dir: Path | None
    ) -> list[SavePathCandidate]:
        """读一个 CloudCache 目录下的全部 manifest, 汇总候选."""
        found: list[SavePathCandidate] = []
        for manifest in self._manifests(cache):
            data = _read_bytes(manifest)
            if data is None:
                continue
            for raw in parse_cloud_save_folders(data):
                target = _folder_target(raw, self._roots, install_dir=install_dir)
                if target is None:
                    continue
                found.append(
                    SavePathCandidate(
                        path=normalize_path(str(target)),
                        reason_code=REASON_EPIC_CLOUDCACHE,
                        detail=manifest.name,
                        confidence="medium",
                        path_kind="directory",
                        relative_path=raw,
                    )
                )
        return found

    @staticmethod
    def _manifests(cache: Path) -> list[Path]:
        """返回缓存目录下的 manifest 文件(按文件名排序, 时间戳即字典序)."""
        try:
            return sorted(cache.glob("*.manifest"))
        except OSError as exc:  # pragma: no cover - glob 失败极少见, 但仍要降级
            logger.debug("无法列出云存档缓存 %s: %s", cache, exc)
            return []


def _account_caches(account: Path, wanted: str) -> list[Path]:
    """返回一个账号目录下该游戏的 CloudCache(目录名不敏感比对)."""
    try:
        games = sorted(child for child in account.iterdir() if child.is_dir())
    except OSError as exc:
        logger.debug("无法列出 Epic 账号目录 %s: %s", account, exc)
        return []
    return [
        game / CLOUD_CACHE_DIRECTORY
        for game in games
        if game.name.casefold() == wanted and (game / CLOUD_CACHE_DIRECTORY).is_dir()
    ]


def _read_bytes(path: Path) -> bytes | None:
    """读取二进制文件; 不存在或不可读时返回 None."""
    try:
        return path.read_bytes()
    except OSError as exc:
        logger.debug("云存档缓存清单不可读, 已跳过: %s(%s)", path, exc)
        return None


def _dedupe(candidates: Iterable[SavePathCandidate]) -> list[SavePathCandidate]:
    """按规范化路径去重并排序(同一缓存的多份时间戳会重复出现)."""
    best: dict[str, SavePathCandidate] = {}
    for candidate in candidates:
        best.setdefault(candidate.path.casefold(), candidate)
    return sorted(best.values(), key=lambda item: item.path.casefold())
