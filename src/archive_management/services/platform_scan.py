"""本地游戏探测服务(阶段 E-1).

从主流平台的安装清单、Windows 注册表与本机常规安装目录中发现"已安装游戏",
并把结果表达成 :class:`~archive_management.domain.GameCandidate`; 同时支持扫描
用户自行添加的"监控目录", 覆盖平台客户端未安装、未登录或路径自定义的情况
(PLAN 阶段 E-1 第 1~4 条)。

设计要点:

- **可解释**: 每个候选都带来源平台、得出它的规则代码(``reason_code``)与
  附加说明, 界面据此告诉用户"这条是怎么来的";
- **可注入**: 环境根目录与注册表读取器全部通过 :class:`ScanRoots` 注入,
  测试可以在临时目录里造出 Steam/Epic/GOG/Battle.net 的目录结构, 不需要
  真实安装平台客户端;
- **只读**: 探测阶段只读清单与注册表, 不修改任何用户数据目录;
- **不静默信任**: 路径缺失、不可读或属于高风险位置(盘符根目录、用户主目录)
  时给出明确的健康状态, 由用户决定是否导入(PLAN 阶段 C 第 5 条)。
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
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

logger = logging.getLogger(__name__)

# 注册表根键: 用字符串而不是 winreg 常量, 保证非 Windows 上也能导入本模块.
HKCU = "HKCU"
HKLM = "HKLM"

# 探测来源代码(与 domain.discovery.DiscoverySource 的取值对应).
SOURCE_STEAM = "steam"
SOURCE_EPIC = "epic"
SOURCE_GOG = "gog"
SOURCE_BATTLE_NET = "battle_net"
SOURCE_MONITORED = "monitored"

# 规则代码: 界面据此解释候选是怎么被发现的.
REASON_STEAM_MANIFEST = "steam_manifest"
REASON_EPIC_MANIFEST = "epic_manifest"
REASON_GOG_REGISTRY = "gog_registry"
REASON_BNET_REGISTRY = "battlenet_registry"
REASON_MONITORED_CHILD = "monitored_child"
REASON_MONITORED_ROOT = "monitored_root"

# Valve VDF 里的 ``"key" "value"`` 键值对(嵌套结构由调用方按需解释).
_VDF_PAIR = re.compile(r'"((?:[^"\\]|\\.)*)"\s+"((?:[^"\\]|\\.)*)"')
_STEAM_LIBRARY_FILES = ("libraryfolders.vdf",)
# 注册表根键名 → winreg 常量名.
_HIVE_NAMES: dict[str, str] = {
    "HKCU": "HKEY_CURRENT_USER",
    "HKLM": "HKEY_LOCAL_MACHINE",
}


@runtime_checkable
class RegistryReader(Protocol):
    """只读访问 Windows 注册表的能力(非 Windows 上返回空结果)."""

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
    """基于 ``winreg`` 的注册表读取器; 非 Windows 或缺少权限时静默降级."""

    @staticmethod
    def _module() -> WinRegModule | None:
        """惰性导入 ``winreg``(非 Windows 返回 None)."""
        try:
            module = importlib.import_module("winreg")
        except ImportError:  # pragma: no cover - 仅非 Windows 触发
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
    """探测使用的环境根目录与注册表读取器(全部可注入)."""

    program_data: Path
    program_files: Path
    program_files_x86: Path
    local_app_data: Path
    user_profile: Path
    registry: RegistryReader


def default_roots(registry: RegistryReader | None = None) -> ScanRoots:
    """从环境变量推导默认探测根目录(注册表缺省使用 :class:`WinRegistry`)."""
    env = os.environ
    home = Path.home()
    return ScanRoots(
        program_data=Path(env.get("PROGRAMDATA", r"C:\ProgramData")),
        program_files=Path(env.get("PROGRAMFILES", r"C:\Program Files")),
        program_files_x86=Path(env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")),
        local_app_data=Path(env.get("LOCALAPPDATA", str(home / "AppData" / "Local"))),
        user_profile=Path(env.get("USERPROFILE", str(home))),
        registry=WinRegistry() if registry is None else registry,
    )


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


class LocalGameScanner:
    """从平台清单、注册表与监控目录中发现已安装游戏."""

    def __init__(self, roots: ScanRoots) -> None:
        """绑定探测环境(根目录与注册表读取器)."""
        self._roots = roots

    def scan(self, *, monitored: Sequence[str] = ()) -> list[GameCandidate]:
        """返回本次探测的全部候选(已按路径去重并排序).

        单个来源出错不会影响其它来源: 平台清单损坏、权限不足都只记录一条
        DEBUG 日志并跳过该项(PLAN 阶段 E-1 验收: 探测失败不阻断手动管理).
        """
        found: list[GameCandidate] = []
        probes: tuple[Callable[[], list[GameCandidate]], ...] = (
            self._steam,
            self._epic,
            self._gog,
            self._battle_net,
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
        """读取文本文件; 不存在或不可读时返回 None."""
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    # ------------------------------------------------------------------- Steam

    def _steam(self) -> list[GameCandidate]:
        """从 Steam 库清单(appmanifest_*.acf)读取已安装游戏."""
        steam_root = self._steam_root()
        if steam_root is None:
            return []
        candidates: list[GameCandidate] = []
        for library in self._steam_libraries(steam_root):
            steamapps = library / "steamapps"
            for manifest in sorted(steamapps.glob("appmanifest_*.acf")):
                text = self._read_text(manifest)
                if text is None:
                    continue
                pairs = parse_vdf_pairs(text)
                name = vdf_first(pairs, "name")
                installdir = vdf_first(pairs, "installdir")
                if not name or not installdir:
                    continue
                candidates.append(
                    self._candidate(
                        name,
                        steamapps / "common" / installdir,
                        source=SOURCE_STEAM,
                        confidence="high",
                        reason_code=REASON_STEAM_MANIFEST,
                        detail=manifest.name,
                    )
                )
        return candidates

    def _steam_root(self) -> Path | None:
        """返回 Steam 安装目录(来自注册表); 未安装时返回 None."""
        values = self._roots.registry.values(HKCU, r"Software\Valve\Steam")
        for key in ("SteamPath", "InstallPath"):
            raw = values.get(key)
            if raw and raw.strip():
                return Path(normalize_path(raw))
        return None

    def _steam_libraries(self, steam_root: Path) -> list[Path]:
        """返回 Steam 的库目录列表(主目录 + libraryfolders.vdf 中登记的库)."""
        libraries = [steam_root]
        steamapps = steam_root / "steamapps"
        for filename in _STEAM_LIBRARY_FILES:
            text = self._read_text(steamapps / filename)
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

    # -------------------------------------------------------------------- Epic

    def _epic(self) -> list[GameCandidate]:
        """从 Epic Games 启动器的清单目录读取已安装游戏."""
        manifest_dir = (
            self._roots.program_data
            / "Epic"
            / "EpicGamesLauncher"
            / "Data"
            / "Manifests"
        )
        if not manifest_dir.is_dir():
            return []
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
        """从 GOG 注册表项读取已安装游戏(32 位与 64 位视图都尝试)."""
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

    # --------------------------------------------------------------- Battle.net

    def _battle_net(self) -> list[GameCandidate]:
        """从 Blizzard Entertainment 注册表项读取已安装游戏."""
        candidates: list[GameCandidate] = []
        for subkey in (
            r"SOFTWARE\WOW6432Node\Blizzard Entertainment",
            r"SOFTWARE\Blizzard Entertainment",
        ):
            for game in self._roots.registry.subkeys(HKLM, subkey):
                values = self._roots.registry.values(HKLM, f"{subkey}\\{game}")
                location = str(values.get("InstallLocation") or "").strip()
                if not location:
                    continue
                candidates.append(
                    self._candidate(
                        game,
                        Path(location),
                        source=SOURCE_BATTLE_NET,
                        confidence="high",
                        reason_code=REASON_BNET_REGISTRY,
                        detail=game,
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
