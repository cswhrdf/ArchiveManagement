r"""平台官方工具(不是游戏)的默认排除清单.

平台目录里除了游戏, 还躺着运行库、录制工具这类官方软件: 它们没有存档, 混在探测
结果里只会干扰用户。命中清单的候选在扫描时**默认标记为已忽略**(界面上的"已隐藏"),
用户仍可在"已忽略"筛选里把它恢复成待处理。

清单是随程序发布的**只读数据文件** ``resources/excluded-games.json``: 由开发人员
手工维护, 程序既不往里写, 也不会因为用户手动忽略了某个程序就自动追加条目。文件与
译名缓存一样写成**单行紧凑 JSON**(不写缩进、换行与分隔空格), 便于当作"固定清单"逐条
比对; 用编辑器改完**不必手工转换** —— 提交钩子(`scripts/compact_json.py`)会就地压
回单行, 仓库里的 `tests/unit/test_exclusions.py` 再守住提交后的状态。

匹配规则(平台与名称都忽略大小写): 同一条目下 AppID 命中、名称全等或名称前缀命中
任意一条即算命中; ``platform`` 为空表示不限来源平台(监控目录发现的也算)。

清单只作用于"新发现的候选"与"仍待处理的候选": 用户已经导入或忽略过的决定不会被
下一次扫描覆盖。文件缺席或结构损坏只会让这份清单变空, 不影响扫描本身。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files

logger = logging.getLogger(__name__)

# 排除清单的文件名(随程序发布, 位于包内 resources 目录).
EXCLUDED_FILENAME = "excluded-games.json"
_RESOURCE_PACKAGE = "archive_management"
_RESOURCE_PATH = ("resources", EXCLUDED_FILENAME)


def _text(value: object) -> str:
    """把 JSON 里的值收成去空白的字符串(非字符串一律当空串)."""
    return value.strip() if isinstance(value, str) else ""


def _text_list(value: object) -> list[str]:
    """把 JSON 里的值收成"非空且已去空白"的字符串列表."""
    if not isinstance(value, list):
        return []
    texts: list[str] = []
    for item in value:
        text = _text(item)
        if text:
            texts.append(text)
    return texts


@dataclass(frozen=True)
class ExcludedProgram:
    """一条"这个程序不是游戏"的规则(字段全部按已归一的形态保存)."""

    platform: str = ""  # 空 = 不限来源平台
    app_ids: frozenset[str] = frozenset()
    names: frozenset[str] = frozenset()  # 已 casefold 的完整名称
    name_prefixes: tuple[str, ...] = ()  # 已 casefold 的名称前缀
    reason: str = ""  # 规则代码(如 redistributable/tool), 只用于日志

    def matches(self, *, platform: str, app_id: str | int | None, name: str) -> bool:
        """这条探测结果是否命中本规则(字段为空表示"不作要求").

        ``app_id`` 允许是文本或数字: 清单里存的是文本, 而领域层给的是 ``int``。
        """
        if self.platform and self.platform.casefold() != platform.casefold():
            return False
        if app_id is not None and str(app_id) in self.app_ids:
            return True
        folded = name.strip().casefold()
        if folded in self.names:
            return True
        return any(folded.startswith(prefix) for prefix in self.name_prefixes)


class Exclusions:
    """一套排除清单: 判断探测结果是不是平台自带的工具."""

    def __init__(self, programs: Sequence[ExcludedProgram] = ()) -> None:
        """保存规则序列(空序列表示不排除任何程序)."""
        self._programs = tuple(programs)

    def __len__(self) -> int:
        """规则条数(界面与日志用它说明清单规模)."""
        return len(self._programs)

    def match(
        self, *, platform: str, app_id: str | int | None, name: str
    ) -> ExcludedProgram | None:
        """返回命中的规则; 没命中任何规则时返回 ``None``."""
        for program in self._programs:
            if program.matches(platform=platform, app_id=app_id, name=name):
                return program
        return None


def parse_exclusions(payload: object) -> tuple[ExcludedProgram, ...]:
    """解析清单内容; 结构不符的条目直接跳过(坏文件不能把扫描卡死)."""
    if not isinstance(payload, dict):
        return ()
    programs = payload.get("programs")
    if not isinstance(programs, list):
        return ()
    parsed: list[ExcludedProgram] = []
    for entry in programs:
        program = _parse_program(entry)
        if program is not None:
            parsed.append(program)
    return tuple(parsed)


def _parse_program(entry: object) -> ExcludedProgram | None:
    """解析一条规则; 一个匹配条件都没有(AppID 与名称都空)的条目视为无效."""
    if not isinstance(entry, dict):
        return None
    app_ids = frozenset(_text_list(entry.get("app_ids")))
    names = frozenset(text.casefold() for text in _text_list(entry.get("names")))
    prefixes = tuple(text.casefold() for text in _text_list(entry.get("name_prefixes")))
    if not app_ids and not names and not prefixes:
        return None
    return ExcludedProgram(
        platform=_text(entry.get("platform")),
        app_ids=app_ids,
        names=names,
        name_prefixes=prefixes,
        reason=_text(entry.get("reason")),
    )


def load_exclusions() -> Exclusions:
    """读取随程序发布的排除清单(开发人员手工维护的那份).

    这是唯一的来源: 程序不会把它复制到配置目录, 也不会在运行期改写它。读不到或
    结构不符时返回空清单 —— 坏数据不能把扫描卡死。
    """
    return Exclusions(_read_programs())


def _read_programs() -> tuple[ExcludedProgram, ...]:
    """读内置清单并解析成规则; 资源缺失或不是 JSON 时视为没有规则."""
    resource = files(_RESOURCE_PACKAGE).joinpath(*_RESOURCE_PATH)
    try:
        payload: object = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # pragma: no cover - 资源缺失才会走到
        logger.debug("读取排除清单失败: %s", exc)
        return ()
    return parse_exclusions(payload)
