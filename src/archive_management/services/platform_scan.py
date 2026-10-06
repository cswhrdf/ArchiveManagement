"""本地游戏探测服务.

从主流平台的安装清单、Windows 注册表与本机常规安装目录中发现"已安装游戏",
并把结果表达成 :class:`~archive_management.domain.GameCandidate`; 同时支持扫描
用户自行添加的"监控目录", 覆盖平台客户端未安装、未登录或路径自定义的情况。

设计要点:

- **可解释**: 每个候选都带来源平台、得出它的规则代码(``reason_code``)与
  附加说明, 界面据此告诉用户"这条是怎么来的";
- **可注入**: 平台、环境根目录与注册表读取器全部通过 :class:`ScanRoots`
  注入, 测试可以在临时目录里造出各平台的目录结构, 不需要真实安装平台客户端,
  也不需要运行在对应平台上;
- **平台适配**: 注册表只存在于 Windows, GOG/Ubisoft 的安装位置也只写在
  注册表里, 因此这类探测被显式限制在 Windows(见
  :meth:`LocalGameScanner._registry_source_available`), 其它平台返回空结果
  并把缺口交回"监控目录"; Steam/Epic 则按各平台自己的清单目录探测。
- **只读**: 探测时只读清单与注册表, 不修改任何用户数据目录;
- **不静默信任**: 路径缺失、不可读或属于高风险位置(盘符根目录、用户主目录)
  时给出明确的健康状态, 由用户决定是否导入;
- **单一解析入口**: Steam 主目录/库目录/清单的解析以模块级函数暴露(见
  :func:`steam_roots` / :func:`steam_libraries` / :func:`read_steam_installs`),
  平台适配器与后续的云端清单解析复用同一份实现, 不再写第二遍 VDF 解析。
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from archive_management.domain import (
    GameCandidate,
    PathHealth,
    dedupe_candidates,
)
from archive_management.services.pathcheck import (
    dangerous_target_reason,
    normalize_path,
)
from archive_management.services.platforms import (
    PlatformFamily,
    current_platform,
    platform_label,
)

logger = logging.getLogger(__name__)

# 注册表根键: 用字符串而不是 winreg 常量, 保证非 Windows 上也能导入本模块.
HKCU = "HKCU"
HKLM = "HKLM"

# 探测来源代码(与 domain.discovery.DiscoverySource 的取值对应).
SOURCE_STEAM = "steam"
SOURCE_EPIC = "epic"
SOURCE_GOG = "gog"
SOURCE_UBISOFT = "ubisoft"
SOURCE_MONITORED = "monitored"

# 规则代码: 界面据此解释候选是怎么被发现的.
REASON_STEAM_MANIFEST = "steam_manifest"
REASON_EPIC_MANIFEST = "epic_manifest"
REASON_GOG_REGISTRY = "gog_registry"
REASON_UBISOFT_REGISTRY = "ubisoft_registry"
REASON_MONITORED_CHILD = "monitored_child"
REASON_MONITORED_ROOT = "monitored_root"

# Valve VDF 里的 ``"key" "value"`` 键值对(嵌套结构由调用方按需解释).
_VDF_PAIR = re.compile(r'"((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"')
_STEAM_LIBRARY_FILES = ("libraryfolders.vdf",)
# 应用清单文件名里带着 AppID(appmanifest_<appid>.acf): 这是本地最能确认
# "这个游戏在 Steam 里是谁"的依据, 平台适配器要用它去查云端同步清单。
_STEAM_MANIFEST = re.compile(r"appmanifest_(\d+)\.acf", re.IGNORECASE)
# 注册表根键名 → winreg 常量名.
_HIVE_NAMES: dict[str, str] = {
    "HKCU": "HKEY_CURRENT_USER",
    "HKLM": "HKEY_LOCAL_MACHINE",
}

# macOS 的跨用户共享目录: Epic 启动器的清单与安装记录写在这里.
_MACOS_SHARED_ROOT = Path("/Users/Shared")


@runtime_checkable
class RegistryReader(Protocol):
    """只读访问 Windows 注册表的能力.

    注册表是 Windows 独有的设施: 非 Windows 平台应当注入
    :class:`NullRegistry`, 并且依赖注册表的探测本身也会按平台短路
    (见 :meth:`LocalGameScanner._registry_source_available`)。
    """

    def values(self, hive: str, subkey: str) -> dict[str, str]:
        """返回指定键下的字符串值(键不存在时返回空字典)."""
        ...

    def subkeys(self, hive: str, subkey: str) -> list[str]:
        """返回指定键下的子键名(键不存在时返回空列表)."""
        ...


class NullRegistry:
    """空实现: 永远查不到任何东西, 让探测自然降级到目录规则."""

    def values(self, hive: str, subkey: str) -> dict[str, str]:
        """返回空字典."""
        del hive, subkey
        return {}

    def subkeys(self, hive: str, subkey: str) -> list[str]:
        """返回空列表."""
        del hive, subkey
        return []


class WinRegModule(Protocol):
    """``winreg`` 的最小接口(自定义协议, 避免在注解里出现 ``Any``).

    模块只在 Windows 上存在, 因此用 :func:`importlib.import_module` 惰性导入后
    ``cast`` 成这个协议: 非 Windows 平台不会因为导入失败而连模块都加载不了。
    """

    def OpenKey(self, key: object, sub_key: str) -> AbstractContextManager[object]:
        """打开注册表键, 返回可 ``with`` 使用的句柄."""
        ...

    def EnumValue(self, key: object, index: int) -> tuple[str, object, int]:
        """枚举键下的一个值(索引越界时抛 ``OSError``)."""
        ...

    def EnumKey(self, key: object, index: int) -> str:
        """枚举键下的一个子键(索引越界时抛 ``OSError``)."""
        ...


class WinRegistry:
    """基于 ``winreg`` 的注册表读取器; 非 Windows 或缺少权限时静默降级.

    ``winreg`` 只随 Windows 版 Python 提供, 因此本类在 macOS/Linux 上不应当被
    构造(:func:`default_roots` 在这两个平台改用 :class:`NullRegistry`); 即使
    被构造, 所有查询也只会返回空结果, 不会抛出异常。
    """

    @staticmethod
    def _module() -> WinRegModule | None:
        """惰性导入 ``winreg``(非 Windows 返回 None)."""
        try:
            module = importlib.import_module("winreg")
        except ImportError:  # 非 Windows 或精简过的解释器: 静默降级
            return None
        return cast(WinRegModule, module)

    @staticmethod
    def _root(hive: str, module: WinRegModule) -> int | None:
        """把 hive 字符串映射为 winreg 的根键常量."""
        name = _HIVE_NAMES.get(hive.upper())
        if name is None:
            return None
        root = getattr(module, name, None)
        return root if isinstance(root, int) else None

    def values(self, hive: str, subkey: str) -> dict[str, str]:
        """读取键下所有字符串值; 任何失败都视为"没有"."""
        module = self._module()
        if module is None:  # pragma: no cover - 仅非 Windows 触发
            return {}
        root = self._root(hive, module)
        if root is None:
            return {}
        result: dict[str, str] = {}
        try:
            with module.OpenKey(root, subkey) as key:
                index = 0
                while True:
                    try:
                        name, value, _kind = module.EnumValue(key, index)
                    except OSError:
                        break
                    if isinstance(name, str) and isinstance(value, str):
                        result[name] = value
                    index += 1
        except OSError:
            return {}
        return result

    def subkeys(self, hive: str, subkey: str) -> list[str]:
        """读取键下的子键名; 任何失败都视为"没有"."""
        module = self._module()
        if module is None:  # pragma: no cover - 仅非 Windows 触发
            return []
        root = self._root(hive, module)
        if root is None:
            return []
        names: list[str] = []
        try:
            with module.OpenKey(root, subkey) as key:
                index = 0
                while True:
                    try:
                        name = module.EnumKey(key, index)
                    except OSError:
                        break
                    if isinstance(name, str):
                        names.append(name)
                    index += 1
        except OSError:
            return []
        return names


@dataclass(frozen=True)
class ScanRoots:
    """探测使用的平台、环境根目录与注册表读取器(全部可注入).

    ``platform`` 决定使用哪套目录规则; ``user_profile`` 是用户主目录(macOS 的
    ``~/Library`` 与 Linux 的 ``~/.local/share`` 都从它推导), ``local_app_data``
    是"当前用户的应用数据目录"(Windows 的 ``%LOCALAPPDATA%``、macOS 的
    ``~/Library/Application Support``、Linux 的 ``$XDG_DATA_HOME``)。
    ``user_shared`` 只在 macOS 有意义(``/Users/Shared``)。
    """

    platform: PlatformFamily
    program_data: Path
    program_files: Path
    program_files_x86: Path
    local_app_data: Path
    user_profile: Path
    registry: RegistryReader = field(default_factory=NullRegistry)
    user_shared: Path | None = None

    @property
    def shared_root(self) -> Path:
        """跨用户共享目录(macOS 的 ``/Users/Shared``, 其它平台回落到 program_data)."""
        return self.program_data if self.user_shared is None else self.user_shared


def default_roots(
    registry: RegistryReader | None = None,
    *,
    platform: PlatformFamily | None = None,
    env: Mapping[str, str] | None = None,
) -> ScanRoots:
    """按平台推导默认探测根目录.

    Windows 从环境变量取系统目录, 并默认启用注册表读取器; macOS 与 Linux 用
    家目录下的 Library/XDG 目录, 缺省注入 :class:`NullRegistry`——这两个平台
    没有注册表, 构造真实读取器只会引入无意义的失败分支。

    ``platform``/``env`` 用于测试: 传入平台族与假的环境变量即可在任意平台上
    校验三套规则。
    """
    family = platform or current_platform()
    source = os.environ if env is None else env
    raw_home = source.get("HOME") or source.get("USERPROFILE")
    home = Path(raw_home) if raw_home else Path.home()
    if family == "windows":
        return ScanRoots(
            platform=family,
            program_data=Path(source.get("PROGRAMDATA", r"C:\ProgramData")),
            program_files=Path(source.get("PROGRAMFILES", r"C:\Program Files")),
            program_files_x86=Path(
                source.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
            ),
            local_app_data=Path(
                source.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
            ),
            user_profile=Path(source.get("USERPROFILE", str(home))),
            registry=WinRegistry() if registry is None else registry,
        )
    if family == "macos":
        return ScanRoots(
            platform=family,
            program_data=Path("/usr/local/share"),
            program_files=Path("/Applications"),
            program_files_x86=Path("/Applications"),
            local_app_data=Path(str(home / "Library" / "Application Support")),
            user_profile=home,
            registry=registry if registry is not None else NullRegistry(),
            user_shared=_MACOS_SHARED_ROOT,
        )
    return ScanRoots(
        platform=family,
        program_data=Path("/usr/share"),
        program_files=Path("/opt"),
        program_files_x86=Path("/opt"),
        local_app_data=Path(
            source.get("XDG_DATA_HOME", str(home / ".local" / "share"))
        ),
        user_profile=home,
        registry=registry if registry is not None else NullRegistry(),
    )


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    """按字符串形式去重并保持顺序(大小写不敏感), 避免重复扫描同一目录."""
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def parse_vdf_pairs(text: str) -> list[tuple[str, str]]:
    """解析 Valve VDF 文本中的 ``"key" "value"`` 键值对(按出现顺序).

    本项目的用途只需要读取扁平键值(库路径、``name``、``installdir``), 因此
    不做完整 VDF 解析: 嵌套结构由调用方结合上下文解释。
    """
    return [(match.group(1), match.group(2)) for match in _VDF_PAIR.finditer(text)]


def vdf_first(pairs: Iterable[tuple[str, str]], key: str) -> str | None:
    """返回第一个不区分大小写匹配 ``key`` 的取值."""
    wanted = key.casefold()
    for name, value in pairs:
        if name.casefold() == wanted:
            return value
    return None


def app_id_from_manifest(name: str) -> str:
    """从 Steam 应用清单文件名解析 AppID(``appmanifest_<appid>.acf``).

    解析不出时返回空字符串: 调用方据此判断"这条记录谈不谈得上 AppID", 而不是
    拿一个猜出来的值去查云端清单。
    """
    matched = _STEAM_MANIFEST.fullmatch(name)
    return "" if matched is None else matched.group(1)


def path_health(raw: str) -> PathHealth:
    """判定路径的健康状态: 高风险位置优先于"存在性"判断.

    危险路径(盘符根目录、用户主目录)排在前面, 这样即使用户把整个盘符添加成
    监控目录, 也会得到明确的 ``unsafe`` 而不是被当成可用目录去遍历。
    """
    normalized = normalize_path(raw)
    if dangerous_target_reason(normalized) is not None:
        return "unsafe"
    target = Path(normalized)
    if not target.exists():
        return "missing"
    if not target.is_dir():
        return "not_directory"
    if not os.access(target, os.R_OK):
        return "unreadable"
    return "ok"


@dataclass(frozen=True)
class SteamInstall:
    """一个已安装的 Steam 应用(应用清单文件的解析结果).

    保留 ``app_id``/``manifest``/``library`` 而不只是路径: 平台适配器要用 AppID
    去查云端同步清单与缓存资源, 界面要用清单文件名解释"这条是怎么来的"。
    ``app_id`` 在文件名不符合 ``appmanifest_<数字>.acf`` 时为空字符串(不改写
    清单内容, 也不因此丢掉整条记录)。
    """

    app_id: str
    name: str
    installdir: str
    install_dir: Path
    manifest: Path
    library: Path


def read_text(path: Path) -> str | None:
    """读取文本文件; 不存在或不可读时返回 None."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def registry_source_available(roots: ScanRoots, source: str) -> bool:
    """注册表探测只在 Windows 上有意义.

    macOS 与 Linux 没有注册表: 在这里显式短路, 比让查询静默返回空结果更
    可解释(日志能说明"这个来源在本平台不适用")。
    """
    if roots.platform == "windows":
        return True
    logger.debug("%s 的注册表探测仅 Windows 可用, 已跳过", source)
    return False


def registry_paths(
    roots: ScanRoots, hive: str, subkey: str, names: tuple[str, ...]
) -> list[Path]:
    """读取注册表键下几个候选值并规范化成路径(非 Windows 返回空)."""
    if not registry_source_available(roots, subkey):
        return []
    values = roots.registry.values(hive, subkey)
    paths: list[Path] = []
    for name in names:
        raw = values.get(name)
        if raw and raw.strip():
            paths.append(Path(normalize_path(raw)))
    return paths


def steam_roots(roots: ScanRoots) -> list[Path]:
    """返回本机存在的 Steam 主目录(按平台规则, 只保留含 steamapps 的).

    - Windows: 注册表登记的路径, 其次默认安装目录;
    - macOS: ``~/Library/Application Support/Steam``;
    - Linux: 原生安装目录、发行版包目录与 Flatpak/Snap 容器内的目录。

    过滤掉不存在 ``steamapps`` 的候选: Steam 未安装时不去猜路径, 也就不会
    产出一条注定不可用的候选。
    """
    if roots.platform == "windows":
        candidates = [
            *registry_paths(
                roots, HKCU, r"Software\Valve\Steam", ("SteamPath", "InstallPath")
            ),
            roots.program_files_x86 / "Steam",
            roots.program_files / "Steam",
        ]
    elif roots.platform == "macos":
        candidates = [roots.local_app_data / "Steam"]
    else:
        home = roots.user_profile
        candidates = [
            home / ".steam" / "steam",
            home / ".steam" / "root",
            roots.local_app_data / "Steam",
            home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam",
            home / "snap" / "steam" / "common" / ".local" / "share" / "Steam",
        ]
    return [root for root in _dedupe_paths(candidates) if (root / "steamapps").is_dir()]


def steam_libraries(steam_root: Path) -> list[Path]:
    """返回 Steam 的库目录列表(主目录 + libraryfolders.vdf 中登记的库)."""
    libraries = [steam_root]
    steamapps = steam_root / "steamapps"
    for filename in _STEAM_LIBRARY_FILES:
        text = read_text(steamapps / filename)
        if text is None:
            continue
        for key, value in parse_vdf_pairs(text):
            # 新版格式是 ``"path" "D:\\Steam"``, 旧版是 ``"1" "D:\\Steam"``.
            if key.casefold() != "path" and not key.isdigit():
                continue
            if not value.strip():
                continue
            library = Path(normalize_path(value))
            if library not in libraries:
                libraries.append(library)
    return libraries


def read_steam_installs(roots: ScanRoots) -> list[SteamInstall]:
    """读取本机所有库目录下的应用清单, 返回已安装游戏的记录.

    这是 Steam 本地清单的**唯一**解析入口: 本地游戏探测与平台适配器都从这里取。
    清单缺失、损坏或缺少 ``name``/``installdir`` 时跳过该条, 不影响其它游戏。
    """
    installs: list[SteamInstall] = []
    for root in steam_roots(roots):
        for library in steam_libraries(root):
            steamapps = library / "steamapps"
            for manifest in sorted(steamapps.glob("appmanifest_*.acf")):
                text = read_text(manifest)
                if text is None:
                    continue
                pairs = parse_vdf_pairs(text)
                name = vdf_first(pairs, "name")
                installdir = vdf_first(pairs, "installdir")
                if not name or not installdir:
                    continue
                installs.append(
                    SteamInstall(
                        app_id=app_id_from_manifest(manifest.name),
                        name=name,
                        installdir=installdir,
                        install_dir=steamapps / "common" / installdir,
                        manifest=manifest,
                        library=library,
                    )
                )
    return installs


class LocalGameScanner:
    """从平台清单、注册表与监控目录中发现已安装游戏.

    使用哪几个来源取决于 :attr:`ScanRoots.platform`: 注册表来源只在 Windows
    上执行, Steam/Epic 按各平台的目录规则查找, 监控目录在所有平台上都可用。
    """

    def __init__(self, roots: ScanRoots) -> None:
        """绑定探测环境(平台、根目录与注册表读取器)."""
        self._roots = roots

    @property
    def platform(self) -> PlatformFamily:
        """本次探测使用的平台规则."""
        return self._roots.platform

    def scan(self, *, monitored: Sequence[str] = ()) -> list[GameCandidate]:
        """返回本次探测的全部候选(已按路径去重并排序).

        单个来源出错不会影响其它来源: 平台清单损坏、权限不足都只记录一条
        DEBUG 日志并跳过该项(探测失败不阻断手动管理).
        """
        logger.debug("开始本地游戏探测(平台 %s)", platform_label(self._roots.platform))
        found: list[GameCandidate] = []
        probes: tuple[Callable[[], list[GameCandidate]], ...] = (
            self._steam,
            self._epic,
            self._gog,
            self._ubisoft,
        )
        for probe in probes:
            found.extend(self._guard(probe))
        found.extend(self._guard(lambda: self._monitored(monitored)))
        return dedupe_candidates(found)

    # ---------------------------------------------------------------- 内部工具

    def _guard(self, probe: Callable[[], list[GameCandidate]]) -> list[GameCandidate]:
        """执行一个探测步骤并吞掉意外错误(只留 DEBUG 日志)."""
        try:
            return probe()
        except Exception as exc:
            logger.debug("探测步骤失败: %s", exc)
            return []

    def _candidate(
        self,
        name: str,
        target: Path,
        *,
        source: str,
        confidence: str,
        reason_code: str,
        detail: str = "",
    ) -> GameCandidate:
        """构造候选, 顺便计算出路径健康状态."""
        normalized = normalize_path(str(target))
        return GameCandidate(
            name=name.strip() or Path(normalized).name,
            install_dir=normalized,
            source=source,  # type: ignore[arg-type]
            confidence=confidence,  # type: ignore[arg-type]
            reason_code=reason_code,
            detail=detail,
            health=path_health(normalized),
        )

    @staticmethod
    def _read_text(path: Path) -> str | None:
        """读取文本文件; 不存在或不可读时返回 None(见模块级 :func:`read_text`)."""
        return read_text(path)

    # ------------------------------------------------------------------- Steam

    def _steam(self) -> list[GameCandidate]:
        """从 Steam 库清单(appmanifest_*.acf)读取已安装游戏.

        Steam 在三个平台上的清单格式完全相同, 区别只在主目录怎么找; 解析本身在
        模块级的 :func:`read_steam_installs` 里, 平台适配器复用同一份实现, 这里只
        把记录翻译成本地探测的候选模型。
        """
        return [
            self._candidate(
                install.name,
                install.install_dir,
                source=SOURCE_STEAM,
                confidence="high",
                reason_code=REASON_STEAM_MANIFEST,
                detail=install.manifest.name,
            )
            for install in read_steam_installs(self._roots)
        ]

    def _registry_source_available(self, source: str) -> bool:
        """注册表探测是否可用(见模块级 :func:`registry_source_available`)."""
        return registry_source_available(self._roots, source)

    # -------------------------------------------------------------------- Epic

    def _epic(self) -> list[GameCandidate]:
        """从 Epic Games 启动器的清单目录读取已安装游戏(Windows/macOS)."""
        candidates: list[GameCandidate] = []
        for manifest_dir in self._epic_manifest_dirs():
            if manifest_dir.is_dir():
                candidates.extend(self._epic_manifests(manifest_dir))
        return candidates

    def _epic_manifest_dirs(self) -> list[Path]:
        """返回 Epic 的清单目录.

        Linux 上没有官方 Epic 启动器(该平台的 Epic 游戏通常来自 Heroic 等
        第三方客户端), 因此这个来源在 Linux 上明确为空, 缺口交给监控目录。
        """
        base = ("EpicGamesLauncher", "Data", "Manifests")
        if self._roots.platform == "windows":
            return [self._roots.program_data.joinpath("Epic", *base)]
        if self._roots.platform == "macos":
            return [
                self._roots.shared_root.joinpath("Epic Games", *base),
                self._roots.local_app_data.joinpath("Epic", *base),
            ]
        logger.debug("Epic 启动器没有 Linux 版本, 该来源请改用监控目录")
        return []

    def _epic_manifests(self, manifest_dir: Path) -> list[GameCandidate]:
        """解析一个清单目录下的 ``*.item`` 文件."""
        candidates: list[GameCandidate] = []
        for item in sorted(manifest_dir.glob("*.item")):
            data = self._read_json(item)
            if data is None:
                continue
            name = str(data.get("DisplayName") or "").strip()
            location = str(data.get("InstallLocation") or "").strip()
            if not name or not location:
                continue
            candidates.append(
                self._candidate(
                    name,
                    Path(location),
                    source=SOURCE_EPIC,
                    confidence="high",
                    reason_code=REASON_EPIC_MANIFEST,
                    detail=item.name,
                )
            )
        return candidates

    @staticmethod
    def _read_json(path: Path) -> dict[str, object] | None:
        """读取 JSON 对象; 缺失或损坏时返回 None."""
        text = LocalGameScanner._read_text(path)
        if text is None:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return {str(key): value for key, value in data.items()}

    # --------------------------------------------------------------------- GOG

    def _gog(self) -> list[GameCandidate]:
        """从 GOG 注册表项读取已安装游戏(仅 Windows, 32 位与 64 位视图都尝试)."""
        if not self._registry_source_available("GOG"):
            return []
        candidates: list[GameCandidate] = []
        for subkey in (
            r"SOFTWARE\WOW6432Node\GOG.com\Games",
            r"SOFTWARE\GOG.com\Games",
        ):
            for game_id in self._roots.registry.subkeys(HKLM, subkey):
                values = self._roots.registry.values(HKLM, f"{subkey}\\{game_id}")
                name = (values.get("gameName") or values.get("GameName") or "").strip()
                location = (values.get("path") or values.get("Path") or "").strip()
                if not name or not location:
                    continue
                candidates.append(
                    self._candidate(
                        name,
                        Path(location),
                        source=SOURCE_GOG,
                        confidence="high",
                        reason_code=REASON_GOG_REGISTRY,
                        detail=game_id,
                    )
                )
        return candidates

    # ----------------------------------------------------------------- Ubisoft

    def _ubisoft(self) -> list[GameCandidate]:
        """从 Ubisoft Connect 注册表项读取已安装游戏(仅 Windows, 64/32 位与旧版启动器都尝试).

        Ubisoft 用数字安装 id 当子键, 子键里只保证有 ``InstallDir``: 游戏名优先取
        ``GameName``/``DisplayName``, 两个都没有时退回安装目录的目录名(与监控目录
        的取名规则一致), 不因为缺一个可选值就丢掉整条候选。
        """
        if not self._registry_source_available("Ubisoft"):
            return []
        candidates: list[GameCandidate] = []
        for hive, subkey in (
            (HKLM, r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"),
            (HKLM, r"SOFTWARE\Ubisoft\Launcher\Installs"),
            (HKCU, r"SOFTWARE\Ubisoft\Ubisoft Game Launcher\Installs"),
        ):
            for install_id in self._roots.registry.subkeys(hive, subkey):
                values = self._roots.registry.values(hive, f"{subkey}\\{install_id}")
                location = str(values.get("InstallDir") or "").strip()
                if not location:
                    continue
                name = str(
                    values.get("GameName") or values.get("DisplayName") or ""
                ).strip()
                path = Path(location)
                candidates.append(
                    self._candidate(
                        name or path.name,
                        path,
                        source=SOURCE_UBISOFT,
                        confidence="high",
                        reason_code=REASON_UBISOFT_REGISTRY,
                        detail=install_id,
                    )
                )
        return candidates

    # ------------------------------------------------------------ 监控目录

    def _monitored(self, monitored: Sequence[str]) -> list[GameCandidate]:
        """把监控目录下的子目录当作候选游戏.

        监控目录通常是"游戏的上一层"目录(如 ``D:/Games``), 因此每个直接子目录
        都是一条候选; 若该目录下没有子目录, 说明用户添加的很可能是某一个游戏
        自己的根目录, 此时把目录本身作为候选。
        """
        candidates: list[GameCandidate] = []
        for raw in monitored:
            root = Path(normalize_path(raw))
            children = self._list_children(root)
            if children is None:
                continue
            if not children:
                candidates.append(
                    self._monitored_candidate(
                        root, root, reason_code=REASON_MONITORED_ROOT
                    )
                )
                continue
            candidates.extend(
                self._monitored_candidate(
                    child, root, reason_code=REASON_MONITORED_CHILD
                )
                for child in children
            )
        return candidates

    @staticmethod
    def _list_children(root: Path) -> list[Path] | None:
        """列出目录中的子目录(隐藏目录除外); 不可用时返回 None."""
        if not root.is_dir():
            return None
        try:
            return sorted(
                (
                    child
                    for child in root.iterdir()
                    if child.is_dir() and not child.name.startswith(".")
                ),
                key=lambda item: item.name.casefold(),
            )
        except OSError as exc:
            logger.debug("无法列出监控目录 %s: %s", root, exc)
            return None

    def _monitored_candidate(
        self, target: Path, root: Path, *, reason_code: str
    ) -> GameCandidate:
        """构造一条来自监控目录的候选."""
        return self._candidate(
            target.name,
            target,
            source=SOURCE_MONITORED,
            confidence="medium",
            reason_code=reason_code,
            detail=str(root),
        )
