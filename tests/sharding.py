"""把用例分片执行的纯逻辑.

CI 会把同一平台的用例拆进多个作业并行跑, 最后把各片的 Allure 结果与覆盖率数据合并
成一份报告 —— 这样套件再长, 墙钟时间也只取决于最慢的那一片。分片要同时满足三件事:

- **确定**: 同一份用例集每次分到同一片(结果可复现, 也能拿两次运行对照);
- **不重不漏**: 各片合起来正好是全集(靠 ``tests/unit/test_sharding.py`` 的守卫锁住);
- **尽量均匀**: 套件耗时几乎都在 GUI 用例上, 只按用例**条数**平分会把慢的全堆在一片。

因此这里按**目录的经验单价**估权重, 再用"最慢的优先"贪心装箱(LPT)。单价是量出来的
经验值, 不需要精确: 只要量级对, 装箱结果就能把慢用例摊开; 新目录按
:data:`DEFAULT_COST` 处理, 不会因为"没见过"就被当成免费。
"""

from __future__ import annotations

from collections.abc import Sequence

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


def cost_of(node_id: str) -> float:
    """估算一个用例的耗时(秒); 目录不认识时返回 :data:`DEFAULT_COST`."""
    normalized = node_id.replace("\\", "/")
    for prefix, cost in _COST_BY_PREFIX:
        if normalized.startswith(prefix):
            return cost
    return DEFAULT_COST


def load_of(node_ids: Sequence[str]) -> float:
    """一片用例的预计总耗时(秒), 用于日志与分片均匀度检查."""
    return sum(cost_of(node_id) for node_id in node_ids)


def shard_plan(node_ids: Sequence[str], *, count: int) -> list[list[str]]:
    """把用例分成 ``count`` 片, 返回每片的 nodeid(片内按 nodeid 升序).

    "最慢的优先"贪心装箱: 用例按(权重降序, nodeid 升序)处理 —— 先安置各自最慢的,
    再依次放进当前**总权重最小**的一片。排序把并列情况也钉死了, 因此同样的输入
    总是得到同样的分片。
    """
    if count < 1:
        raise ValueError("分片数至少为 1")
    buckets: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for node_id in sorted(node_ids, key=lambda node: (-cost_of(node), node)):
        target = min(range(count), key=lambda index: (loads[index], index))
        buckets[target].append(node_id)
        loads[target] += cost_of(node_id)
    return [sorted(bucket) for bucket in buckets]
