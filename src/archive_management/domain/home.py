"""统一游戏主页的分类与筛选规则.

游戏数量变多以后, "现在哪些游戏需要处理"比"某个游戏有哪些备份"更常用。主页把
每个游戏收敛为一组**事实**(平台来源、存档位置数量、备份数量、最近备份时间、
最近活动时间、路径是否有风险、是否来自探测流程、是否归档、自定义标签), 再由这
些事实推导分类、视图与筛选结果。

"待处理"只包含真正卡住不能用的游戏: 还没有配置存档位置, 或者存档路径已经失效。
配置了可用位置但还没备份过的游戏不算待处理(它随时可以备份), 这类状态由"未备份"
与"长期未更新"分类表达。

这里只放纯逻辑: 不查数据库、不碰界面。数据库负责把事实取出来、把用户的选择存
回去, 界面负责渲染; 三者分开后, 分类规则可以脱离 GUI 单独测试, 换界面也不会
改变筛选语义。筛选条件按 :data:`HOME_STATE_VERSION` 做最小版本控制: 格式升级
时旧记录会被忽略并回落到默认视图, 而不是让界面读到无法解释的取值。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from archive_management.i18n import tr

# 持久化筛选条件的格式版本(最小值 1): 不匹配的旧记录视为无效.
HOME_STATE_VERSION = 1
# "最近活跃"的判定窗口(天): 在该窗口内有过备份或变更才算活跃.
RECENT_DAYS = 14
# "长期未更新"的判定窗口(天): 从未备份, 或距上次备份超过该天数.
STALE_DAYS = 30
# 自定义标签的数量与长度上限, 避免主页被标签撑满.
MAX_TAGS = 6
MAX_TAG_LENGTH = 16
# 列表分页的可选每页条数与默认值(默认 30 条一页).
DEFAULT_PAGE_SIZE = 30
PAGE_SIZES = (30, 60, 100, 200)


class HomeLayout(StrEnum):
    """游戏库的展示方式(成员顺序即切换按钮顺序)."""

    LIST = "list"
    POSTER = "poster"

    @property
    def label(self) -> str:
        """返回切换按钮文案."""
        return tr(f"home.layout_{self.value}")


class HomeView(StrEnum):
    """主页顶部的统一视图(成员顺序即页签顺序)."""

    ALL = "all"
    RECENT = "recent"
    PENDING = "pending"
    ARCHIVED = "archived"

    @property
    def label(self) -> str:
        """返回页签文案."""
        return tr(f"home.view_{self.value}")


class CategoryKind(StrEnum):
    """一级分类: 平台、备份、监控、活跃度、风险与用户自定义标签."""

    ORIGIN = "origin"
    BACKUP = "backup"
    ACTIVITY = "activity"
    RISK = "risk"
    TAG = "tag"


@dataclass(frozen=True)
class GameCategory:
    """一个可筛选的分类项: 一级分类(kind) + 取值(key)."""

    kind: CategoryKind
    key: str

    @property
    def value(self) -> str:
        """下拉框与数据库中使用的字符串形式(``kind:key``)."""
        return f"{self.kind.value}:{self.key}"

    @property
    def label(self) -> str:
        """展示文案: 自定义标签直接用标签名, 内置分类走 i18n."""
        if self.kind is CategoryKind.TAG:
            return self.key
        return tr(f"home.cat_{self.kind.value}_{self.key}")


def parse_category(raw: str) -> GameCategory | None:
    """把持久化/下拉框里的取值解析为分类; 非法取值返回 None."""
    kind, _, key = raw.partition(":")
    if not key:
        return None
    try:
        parsed = CategoryKind(kind)
    except ValueError:
        return None
    return GameCategory(kind=parsed, key=key)


# 标签里不允许出现的字符: 数据库按逗号拼接保存, 允许逗号会让一个标签在往返之后
# 变成两个。中英文逗号一视同仁 —— 全角逗号同样会被剔除, 否则同一个标签用哪种逗号
# 写会得到两种结果。
TAG_SEPARATORS: tuple[str, ...] = (",", "\uff0c")


def normalize_tags(tags: Iterable[str]) -> tuple[str, ...]:
    """清理用户输入的标签: 去空白、去逗号、去重, 并限制数量与单个长度.

    标签里不允许出现逗号(见 :data:`TAG_SEPARATORS`, 中英文都算): 数据库按逗号
    拼接保存, 允许逗号会让一个标签在往返之后变成两个。
    """
    cleaned: list[str] = []
    for raw in tags:
        tag = raw.strip()
        for separator in TAG_SEPARATORS:
            tag = tag.replace(separator, "")
        if not tag or tag in cleaned:
            continue
        cleaned.append(tag[:MAX_TAG_LENGTH])
        if len(cleaned) >= MAX_TAGS:
            break
    return tuple(cleaned)


@dataclass(frozen=True)
class GameFacts:
    """主页渲染与筛选所需的全部事实(由数据库查询与一次路径判定补齐)."""

    game_id: str
    name: str
    origin: str = "manual"
    created_at: datetime | None = None
    location_count: int = 0
    backup_count: int = 0
    last_backup_at: datetime | None = None
    last_activity_at: datetime | None = None
    risk: bool = False
    archived: bool = False
    enabled: bool = True
    tags: tuple[str, ...] = ()

    @property
    def activity_at(self) -> datetime | None:
        """最近活动时间: 备份时间、活动时间与录入时间中最晚的一个."""
        moments = [
            moment
            for moment in (self.last_backup_at, self.last_activity_at, self.created_at)
            if moment is not None
        ]
        return max(moments) if moments else None

    @property
    def pending(self) -> bool:
        """是否需要用户处理: 还没有存档位置, 或者存档路径已失效.

        配置了可用存档位置但从未备份过的游戏**不算待处理**: 它已经可以正常备份,
        只是还没轮到; "从未备份"由"未备份"分类与"长期未更新"分类单独表达, 不再
        把大量刚录入的游戏塞进待处理列表。
        """
        return self.location_count == 0 or self.risk

    def is_recent(self, now: datetime) -> bool:
        """最近活动窗口内是否有过动作."""
        moment = self.activity_at
        return moment is not None and now - moment <= timedelta(days=RECENT_DAYS)

    def is_stale(self, now: datetime) -> bool:
        """是否长期未更新: 从未备份, 或距上次备份超过窗口."""
        moment = self.last_backup_at
        return moment is None or now - moment > timedelta(days=STALE_DAYS)

    def categories(self, now: datetime) -> tuple[GameCategory, ...]:
        """返回该游戏命中的全部分类(平台/备份/活跃度/风险/标签)."""
        items = [
            GameCategory(CategoryKind.ORIGIN, self.origin),
            GameCategory(CategoryKind.BACKUP, "done" if self.backup_count else "none"),
            GameCategory(
                CategoryKind.ACTIVITY, "stale" if self.is_stale(now) else "recent"
            ),
            GameCategory(CategoryKind.RISK, "yes" if self.risk else "no"),
        ]
        items.extend(GameCategory(CategoryKind.TAG, tag) for tag in self.tags)
        return tuple(items)

    def in_view(self, view: HomeView, *, now: datetime) -> bool:
        """判断该游戏是否属于某个视图(归档的游戏只在"已归档"里出现)."""
        if view is HomeView.ARCHIVED:
            return self.archived
        if self.archived:
            return False
        if view is HomeView.RECENT:
            return self.is_recent(now)
        if view is HomeView.PENDING:
            return self.pending
        return True


@dataclass(frozen=True)
class HomeFilter:
    """主页当前的筛选条件与展示偏好(视图 + 平台 + 分类 + 搜索词 + 列表形态).

    两部分语义不同, **跨启动的待遇也不同**(见 ``application.home.load_home``):

    * **展示偏好**(``layout`` / ``page_size``)—— "我想怎么摆", 启动时继承;
    * **筛选条件**(``view`` / ``origin`` / ``category`` / ``search``)—— "我上次在看
      什么", 启动时一律回到默认(用户 2026-10-02: 关在"待处理"页签上, 下次不该还在
      那一页)。

    两者仍然存在**同一行**(``home_state``): 它们同属"用户上次看到的样子", 分开存反而
    要额外维护两套版本号; 落库写整行, 由读取端决定继承哪几项。
    """

    view: HomeView = HomeView.ALL
    origin: str = ""
    category: str = ""
    search: str = ""
    layout: HomeLayout = HomeLayout.LIST
    page_size: int = DEFAULT_PAGE_SIZE
    version: int = HOME_STATE_VERSION

    def normalized(self) -> HomeFilter:
        """规范化为可持久化且可解释的取值(非法分类直接回落为"全部类型")."""
        category = self.category if parse_category(self.category) else ""
        return replace(
            self,
            view=self.view if self.view in set(HomeView) else HomeView.ALL,
            origin=self.origin.strip(),
            category=category,
            search=self.search.strip(),
            layout=(self.layout if self.layout in set(HomeLayout) else HomeLayout.LIST),
            page_size=(
                self.page_size if self.page_size in PAGE_SIZES else DEFAULT_PAGE_SIZE
            ),
            version=HOME_STATE_VERSION,
        )

    @property
    def category_item(self) -> GameCategory | None:
        """当前选中的分类(未选择或取值非法时返回 None)."""
        return parse_category(self.category)

    def matches(self, facts: GameFacts, *, now: datetime) -> bool:
        """判断一个游戏是否满足全部筛选条件."""
        if not facts.in_view(self.view, now=now):
            return False
        if self.origin and facts.origin != self.origin:
            return False
        wanted = self.category_item
        if wanted is not None and wanted not in facts.categories(now):
            return False
        return not (self.search and self.search.casefold() not in facts.name.casefold())


def _moment(value: datetime | None) -> float:
    """把可选时间转为可比较的时间戳(None 视为最久远)."""
    return value.timestamp() if value is not None else 0.0


def home_sort_key(facts: GameFacts) -> tuple[int, float, str]:
    """主页排序: 待处理最前, 其余按最近活动倒序, 最后按名称兜底.

    已归档的游戏只会出现在"已归档"视图里(见 :meth:`GameFacts.in_view`), 因此
    排序不需要再单独考虑归档标记。
    """
    return (
        0 if facts.pending else 1,
        -_moment(facts.activity_at),
        facts.name.casefold(),
    )


def filter_games(
    games: Sequence[GameFacts], active: HomeFilter, *, now: datetime
) -> list[GameFacts]:
    """按筛选条件过滤并排序(主页列表的唯一入口)."""
    clean = active.normalized()
    visible = [game for game in games if clean.matches(game, now=now)]
    return sorted(visible, key=home_sort_key)


@dataclass(frozen=True)
class HomeStats:
    """主页概览计数(视图页签与底部摘要共用)."""

    total: int = 0
    recent: int = 0
    pending: int = 0
    archived: int = 0
    risky: int = 0
    backed_up: int = 0

    def count_for(self, view: HomeView) -> int:
        """返回某个视图下的游戏数量."""
        counts = {
            HomeView.ALL: self.total,
            HomeView.RECENT: self.recent,
            HomeView.PENDING: self.pending,
            HomeView.ARCHIVED: self.archived,
        }
        return counts[view]


def home_stats(games: Sequence[GameFacts], *, now: datetime) -> HomeStats:
    """统计各视图与关键分类的数量(已归档的游戏不进入"全部"计数).

    一次遍历里把所有标记累加起来: 分类项都是"是否"判定, 多遍扫描只是重复读同一
    列数据。已归档的游戏单独计数(它们不算"全部", 但仍占归档视图的数字)。
    """
    active = [game for game in games if not game.archived]
    recent = pending = risky = backed_up = 0
    for game in active:
        recent += int(game.is_recent(now))
        pending += int(game.pending)
        risky += int(game.risk)
        backed_up += int(game.backup_count > 0)
    return HomeStats(
        total=len(active),
        recent=recent,
        pending=pending,
        archived=sum(1 for game in games if game.archived),
        risky=risky,
        backed_up=backed_up,
    )


def category_counts(
    games: Sequence[GameFacts], active: HomeFilter, *, now: datetime
) -> list[tuple[GameCategory, int]]:
    """统计"当前视图/平台/搜索"下各分类的数量(分类自身不参与筛选).

    这样切换视图时下拉框里的数字始终与列表一致, 不会出现数字非零却点进去为空
    的分类。
    """
    base = replace(active.normalized(), category="")
    counts: dict[GameCategory, int] = {}
    for game in games:
        if not base.matches(game, now=now):
            continue
        for category in game.categories(now):
            counts[category] = counts.get(category, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0].label))


def origin_counts(
    games: Sequence[GameFacts], active: HomeFilter, *, now: datetime
) -> list[tuple[str, int]]:
    """统计各来源平台在当前视图下的游戏数量(平台自身不参与筛选)."""
    base = replace(active.normalized(), origin="")
    counts: dict[str, int] = {}
    for game in games:
        if base.matches(game, now=now):
            counts[game.origin] = counts.get(game.origin, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))
