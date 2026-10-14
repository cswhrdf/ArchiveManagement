"""把用例分片执行的纯逻辑.

CI 会把同一平台的用例拆进多个作业并行跑, 最后把各片的 Allure 结果与覆盖率数据合并
成一份报告 —— 这样套件再长, 墙钟时间也只取决于最慢的那一片。分片要同时满足三件事:

- **确定**: 同一份用例集每次分到同一片(结果可复现, 也能拿两次运行对照);
- **不重不漏**: 各片合起来正好是全集(靠 ``tests/unit/test_sharding.py`` 的守卫锁住);
- **尽量均匀**: 套件耗时几乎都在 GUI 用例上, 只按用例**条数**平分会把慢的全堆在一片。

因此权重分三级取(见 :func:`cost_of`): **实测值**(``tests/durations.json`` 里每条用例
最近一次全量运行的真实耗时)优先; 没有实测的退回**目录经验单价**; 没见过的目录再按
:data:`DEFAULT_COST` 处理, 不会因为"没见过"就被当成免费。实测值是必须的: 同一模块里
单条用例的耗时能差 8 倍(2026-10-07 实测 test_gui_home.py: 1.5s~12.1s), 只按目录单价
装箱等于按条数装箱, 慢用例会堆在同一片把墙钟拖长。数据用全量跑一次刷新::

    uv run python scripts/run_tests_local.py -- --record-durations=tests/durations.json

(双片各记一份, 脚本自动合并; 记录/合并/加载的实现与约定见 ``tests/durations.py``。)

除了逐条装箱, 还支持**不可拆开的组**(``groups``): 共享一份 UI 会话的用例必须留在同一
片 —— 它们靠 ``tests/ui_sharing.py`` 的池复用同一扇窗口/同一个后端, 拆开后每片各自重建
一份, 复用的收益全部作废(而且重建本身比不复用更贵)。哪些模块共享哪一个池登记在
``tests/conftest.py`` 的 ``_SHARED_UI_SESSIONS`` 里。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import durations

# 各目录下一个用例的大致耗时(秒). 2026-09 实测(Windows 本机, 约 1060 个用例):
# tests/integration 约 180s / 128 个, tests/unit 约 39s / 931 个; 这里取偏整的数,
# 只要**相对**量级正确, 贪心装箱的结果就一样。
_COST_BY_PREFIX: tuple[tuple[str, float], ...] = (
    ("tests/integration/", 2.0),
    ("tests/security/", 0.6),
    ("tests/unit/", 0.05),
)
# 没见过的目录(以后新增测试目录)按这个估: 偏保守, 免得整片都是未知用例。
DEFAULT_COST = 0.3

# 每条用例的实测耗时(秒, ``tests/durations.json``, 见 ``tests/durations.py``): 有记录的
# 用例按真实耗时装箱, 没有的退回上面的目录单价。注意 0.0 也是合法的实测值, 所以下面
# 的判空必须用 ``is not None`` 而不是真值判断。
_DURATION_OVERRIDES: dict[str, float] = durations.load()


def cost_of(node_id: str) -> float:
    """估算一个用例的耗时(秒): 实测值优先, 其次目录单价, 再否则 :data:`DEFAULT_COST`."""
    # 参数化 id 里的 ``\uXXXX`` 转义自带反斜杠, 全串归一会破坏匹配 —— 只归一 ``::``
    # 之前的文件部分(那才是 Windows 风格分隔符会出现的地方), 参数部分保持原样
    # (实测键就是按 pytest 的 nodeid 原样记录的)。
    file_part, separator, rest = node_id.partition("::")
    normalized = file_part.replace("\\", "/") + separator + rest
    recorded = _DURATION_OVERRIDES.get(normalized)
    if recorded is not None:
        return recorded
    for prefix, cost in _COST_BY_PREFIX:
        if normalized.startswith(prefix):
            return cost
    return DEFAULT_COST


def load_of(node_ids: Sequence[str]) -> float:
    """一片用例的预计总耗时(秒), 用于日志与分片均匀度检查."""
    return sum(cost_of(node_id) for node_id in node_ids)


def shard_plan(
    node_ids: Sequence[str],
    *,
    count: int,
    groups: Mapping[str, str] | None = None,
) -> list[list[str]]:
    """把用例分成 ``count`` 片, 返回每片的 nodeid(片内按 nodeid 升序).

    "最慢的优先"贪心装箱: 用例按(权重降序, nodeid 升序)处理 —— 先安置各自最慢的,
    再依次放进当前**总权重最小**的一片。排序把并列情况也钉死了, 因此同样的输入
    总是得到同样的分片。

    ``groups`` 是"不可拆开的组"(nodeid → 组名): 同一组的用例当成**一个装箱单元**,
    不会被分到两片。共享一份 UI 会话的用例靠它留在同一片(见模块说明), 没登记在
    ``groups`` 里的用例各自成组 —— 组内只有一条时, 装箱顺序与结果都与不传 ``groups``
    逐条装箱时完全相同。
    """
    if count < 1:
        raise ValueError("分片数至少为 1")
    buckets: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for load, _first, members in _packing_units(node_ids, groups):
        target = min(range(count), key=lambda index: (loads[index], index))
        buckets[target].extend(members)
        loads[target] += load
    return [sorted(bucket) for bucket in buckets]


def cluster_groups(node_ids: Sequence[str], groups: Mapping[str, str]) -> list[str]:
    """把同组的用例**聚成一段**执行(片内执行顺序策略, 与 :func:`shard_plan` 配套).

    按文件名字母序收集时, 一个共享池的用例往往散在多个模块里, 中间隔着十几个无关
    模块 —— 池窗在这段空档里全程闲置挂着(2026-10 实测 demo-app 池: 开着约 258s,
    真正在用约 33s)。本函数把同组用例平移到**组内首条**出现的位置连成一块, 组内
    保持原相对顺序, 组外用例的相对顺序也不变(只被整体推后)。池窗由此只在它那一段
    里存在, 与"用完即关"配合后既不陪跑也不闲置。

    纯函数 + 稳定排序: 同样的输入总得到同样的顺序; 输入本身是"某一片的用例集"时,
    聚出的相对顺序与全量视角一致(池成员整组同片, 见 :func:`shard_plan` 的 ``groups``)。
    """
    position = {node_id: index for index, node_id in enumerate(node_ids)}
    anchors: dict[str, int] = {}
    for index, node_id in enumerate(node_ids):
        name = groups.get(node_id)
        if name is not None and name not in anchors:
            anchors[name] = index  # 每组锚定在自己首条出现的位置

    def key(node_id: str) -> int:
        name = groups.get(node_id)
        return position[node_id] if name is None else anchors[name]

    return sorted(node_ids, key=key)


def _packing_units(
    node_ids: Sequence[str], groups: Mapping[str, str] | None
) -> list[tuple[float, str, list[str]]]:
    """把用例合成装箱单元 ``(权重, 排序用的首条 nodeid, 成员)``, 并按装箱顺序排好.

    排序键取组内**最小**的 nodeid(而不是"随机某条"): 组的构成只由用例集决定,
    哪种收集顺序进来都得到同一个计划。
    """
    by_group: dict[str, list[str]] = {}
    for node_id in node_ids:
        by_group.setdefault(_group_key(node_id, groups), []).append(node_id)
    units = [
        (sum(cost_of(node) for node in members), min(members), sorted(members))
        for members in by_group.values()
    ]
    return sorted(units, key=lambda unit: (-unit[0], unit[1]))


def _group_key(node_id: str, groups: Mapping[str, str] | None) -> str:
    """用例所属的组名(没登记分组的用例自己成一组, 组名就是它的 nodeid)."""
    if groups is None:
        return node_id
    return groups.get(node_id, node_id)
