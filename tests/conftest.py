"""测试严重等级与 Allure 元数据(含四层标签).

严重等级使用 Allure 官方分级: blocker/critical/normal/minor/trivial/no_severity,
**语义是"用例失败的影响面", 与测试层次无关**(层次用 ``layer`` 表达):

- ``blocker``: 安全与数据完整性底线, 以及**会让软件崩溃/卡死的缺陷** —— 路径越界/危险目标
  必须被拒、快照或清单被篡改必须拒绝恢复、游戏运行中不得静默覆盖存档、界面不得因事件递归
  而崩溃或抽死(如按需滚动条引发的 `maximum recursion depth exceeded`, 见
  ``.github/instructions/ctk-scrollbar-twitch.instructions.md``);
- ``critical``: 核心业务不可用或结果不正确 —— 备份/快照/恢复/删除计划、仓储事务、
  数据库迁移、调度、GUI 真实后端、全链路流水线;
- ``normal``: 常规功能与交互 —— 配置、平台探测、主页聚合、对话框、CLI、热键、
  审计、存档位置与命名、展示模型;
- ``minor``: 展示与辅助 —— 调色板/控件样式/渲染修正、i18n 文案、打包元数据、
  演示后端、性能基准;
- ``trivial``: 极低影响 —— 色值、以及脚本生成的报告汇总项(覆盖率/性能/安全摘要).

解析优先级: 用例或模块上显式标注的等级优先(如 ``@pytest.mark.blocker`` 可覆盖模块级
``critical``), 否则按目录兜底(unit/integration=normal, performance=minor,
security=critical)。**每个测试模块都必须显式声明等级**(守卫见
``tests/unit/test_test_config.py``), 兜底只是防止新目录在报告里冒出 no_severity 桶。

本地 CI(pre-commit)通过 ``--min-severity=critical`` 只保留 blocker+critical, 即
"数据安全 + 核心逻辑"子集; 其余等级交给 GitHub Actions 的全量执行。调整等级请按
影响面判断, 不要按目录/层次照搬 —— 这个选择同时决定了本地钩子能拦到哪些回归。

Allure 标签语义(与 Allure 3 报告控件一一对应):

- ``epic``: 产品级模块, 取值限定在 ``_EPICS``(基础工程/数据持久化/界面框架/
  游戏与存档位置/备份与分支/工程与发布);
- ``feature``: 功能模块, 与生产模块一一对应(如 快照服务、数据仓储);
- ``story``: 具体用户场景(如 删除备份节点、创建备份与分支);
- ``layer``: 测试层次, ``unit`` / ``integration`` / ``e2e`` / ``performance`` /
  ``security``, 供 Allure 的"测试金字塔"与"按层耗时"控件使用。判定标准是**用例实际
  接了什么**而不是它放在哪个目录: ``unit`` = 单个组件 + 替身/内存数据;
  ``integration`` = 真实数据库/文件系统/领域服务之间的协作(例如真 SQLite 的仓储与
  迁移、真实快照与恢复); ``e2e`` = 从真实入口(窗口/命令行)走完整用户流程。
  因此 ``tests/unit`` 下真实读写 SQLite 与文件系统的模块声明为 ``integration``。

测试模块用 ``pytestmark`` 声明默认标签, 单个用例可用同名标记覆盖
(``@pytest.mark.story("...")``)。``layer`` 未声明时按 ``integration`` 标记或目录推断,
保证直方图不会缺数据; 声明的 ``epic``/``layer`` 必须属于闭集, 拼错在收集期直接报错,
以避免报告里冒出只有一个用例的畸形分类。

另外两个跨平台相关的约定:

- **平台参与用例身份**: CI 会把三个平台的结果合并成一份报告, 同一个用例会在
  Windows/Linux/macOS 各跑一次。平台写成**参数**(并同步写 suite 层级与 ``os`` 标签),
  因为 Allure 的 ``historyId`` 由"用例全名 + 非 excluded 参数"算出 —— 不加参数时
  三份结果会被当成"同一个用例重试了多次", 报告里只看得到重复执行、看不出是哪台机器;
- **标题还原转义**: pytest 会把非 ASCII 的参数 id 转义成 Unicode 转义序列, 直接用会
  变成一串编码, 因此展示前先还原成可读文字; 标题要通过 ``__allure_display_name__``
  (即 ``@allure.title`` 的属性)交给 allure-pytest, 因为 ``allure.dynamic.title`` 在
  fixture 之后会被它用 ``item.name`` 覆盖。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import allure
import pytest

import crash_capture
import sharding
import tk_guard
from archive_management.i18n import DEFAULT_LOCALE, set_locale
from archive_management.services.audit import AUDIT_LOGGER_NAME
from archive_management.services.platforms import current_platform, platform_label

_SEVERITY_LEVELS = ("blocker", "critical", "normal", "minor", "trivial")
_SEVERITY_RANK = {
    "blocker": 5,
    "critical": 4,
    "normal": 3,
    "minor": 2,
    "trivial": 1,
    "no_severity": 0,
}
_ALLURE_LEVELS = {
    "blocker": allure.severity_level.BLOCKER,
    "critical": allure.severity_level.CRITICAL,
    "normal": allure.severity_level.NORMAL,
    "minor": allure.severity_level.MINOR,
    "trivial": allure.severity_level.TRIVIAL,
}

# 测试层次: 闭集, 供 Allure "按层耗时" 直方图分组使用.
# performance/security 是只在 CI 执行的两类测试, 单独成层
# 可以一眼看出它们占用的时间, 不会和普通单元/集成测试混在一起。
_LAYERS = ("unit", "integration", "e2e", "performance", "security")
# 层次在 suite 视图里的展示名(Allure 3 部分控件只认 suite 标签, 双保险).
_LAYER_SUITES = {
    "unit": "单元测试 unit",
    "integration": "集成测试 integration",
    "e2e": "端到端测试 e2e",
    "performance": "性能测试 performance",
    "security": "安全测试 security",
}
# 目录名到默认层次的映射(未显式声明 layer 时的兜底).
_DIRECTORY_LAYERS = {
    "integration": "integration",
    "performance": "performance",
    "security": "security",
}
# 产品级模块: 闭集, 新增产品级模块时在此登记.
_EPICS = (
    "基础工程",
    "数据持久化",
    "界面框架",
    "游戏与存档位置",
    "备份与分支",
    "工程与发布",
)
# 标签取值约束: None 表示自由文本(功能模块与场景随功能增长).
_LABEL_ALLOWED: dict[str, tuple[str, ...] | None] = {
    "epic": _EPICS,
    "feature": None,
    "story": None,
    "layer": _LAYERS,
}
_METADATA_MARKERS = frozenset(_LABEL_ALLOWED)
# 元数据与等级标记不再重复当作 tag(其余标记如 backend/ui 保留为标签).
_TAG_EXCLUDED = _METADATA_MARKERS | set(_SEVERITY_LEVELS) | {"integration"}
_UNCLASSIFIED = "未分类"
_FALLBACK_DESCRIPTION = "ArchiveManagement 测试用例"
# 本钩子写进函数对象的标题(键为 id(对象))。参数化用例共用同一个函数对象, 必须靠
# 这张表区分"我们自己写的"(每个用例都要刷新)与"@allure.title 写的"(不覆盖)。
_OUR_TITLES: dict[int, str] = {}
# allure.dynamic 的函数没有类型标注, 统一按 Any 调用, 避免满屏 no-untyped-call 忽略.
_ALLURE_DYNAMIC: Any = allure.dynamic

# pytest 把非 ASCII 的参数 id 用 ``unicode_escape`` 转义(中文变成 ``\u5e03\u5c14``,
# 反斜杠变成 ``\\``), 报告标题要用可读文字, 所以展示前按同样的规则解回去。


def _readable(value: str) -> str:
    """把参数化 id 里的 ASCII 转义还原成可读文字(``\u5e03\u5c14`` -> ``布尔``).

    只对纯 ASCII 取值解码: 非 ASCII 文字会被 ``unicode_escape`` 按 latin-1 重新解读
    而变成乱码, 而本来就含中文的取值已经不需要还原。
    """
    if not value.isascii():
        return value
    try:
        return value.encode("ascii").decode("unicode_escape")
    except UnicodeDecodeError:  # 截断或未知的转义: 宁可不还原, 也不能报错
        return value


def _current_platform_label() -> str:
    """返回当前平台的可读名称(Windows/Linux/macOS), 供报告区分多平台结果."""
    return platform_label(current_platform())


def _title_of(item: pytest.Item) -> str:
    """用例标题: 去掉 ``test_`` 前缀、下划线转空格、首字母大写, 并还原参数化 id 的转义."""
    return _readable(item.name.removeprefix("test_").replace("_", " ").capitalize())


def _description_of(item: pytest.Item) -> str:
    """用例描述: 模块文档字符串 + 该用例验证的行为."""
    module = getattr(item, "module", None)
    return f"{inspect.getdoc(module) or _FALLBACK_DESCRIPTION}\n\n验证行为: {_title_of(item)}。"


def _apply_display_name(item: pytest.Item, title: str) -> None:
    """把可读标题写到用例函数上, 让 allure-pytest 自己用它命名.

    ``allure.dynamic.title`` 存不住: allure-pytest 的 ``pytest_runtest_setup`` 在所有
    fixture 跑完后会用 ``item.name`` 重新赋值(见 ``allure_pytest.utils.allure_name``),
    所以标题必须落到它读取的属性 ``__allure_display_name__`` 上 —— 也就是
    ``@allure.title`` 装饰器写的同一个属性。
    """
    obj = getattr(item, "obj", None)
    if obj is None:
        _ALLURE_DYNAMIC.title(title)
        return
    existing = getattr(obj, "__allure_display_name__", None)
    if existing is not None and _OUR_TITLES.get(id(obj)) != existing:
        return  # 用例自己用 @allure.title 指定了标题, 不覆盖
    # allure-pytest 会把标题当格式化模板处理, 花括号需要转义才能原样显示.
    template = title.replace("{", "{{").replace("}", "}}")
    obj.__allure_display_name__ = template
    _OUR_TITLES[id(obj)] = template


def _restore_description(item: pytest.Item) -> None:
    """函数没有文档字符串时补回描述。

    allure-pytest 同样在 ``pytest_runtest_setup`` 收尾把描述换成
    ``item.function.__doc__``(或 ``@allure.description`` 标记), 没有文档字符串就变成
    空; 此时在用例收尾阶段把生成的描述补回去。
    """
    if getattr(getattr(item, "function", None), "__doc__", None):
        return
    if item.get_closest_marker("allure_description"):
        return
    _ALLURE_DYNAMIC.description(_description_of(item))


def pytest_addoption(parser: pytest.Parser) -> None:
    """注册 --min-severity、分片参数与失败留证参数."""
    parser.addoption(
        "--min-severity",
        action="store",
        default=None,
        choices=list(_SEVERITY_LEVELS),
        help="只保留严重等级不低于该级别的测试(本地 CI 用 critical)",
    )
    # 分片: CI 把同一平台的用例拆到多个作业并行跑, 最后合并结果(见 tests/sharding.py)。
    # 默认 1 片 = 与以前完全一样, 本地不需要关心。
    parser.addoption(
        "--shard-count",
        type=int,
        default=1,
        metavar="N",
        help="把用例均分成 N 片(默认 1 = 不分片)",
    )
    parser.addoption(
        "--shard-index",
        type=int,
        default=0,
        metavar="K",
        help="本作业只跑第 K 片(0 基, 需与 --shard-count 一起用)",
    )
    # 失败现场留证(见 tests/crash_capture.py): 默认开启 —— 难以复现的问题只能在失败那一刻
    # 留下来的东西里找; 失败本来就少见, 这点开销可忽略。
    parser.addoption(
        "--crash-dump-dir",
        type=Path,
        default=crash_capture.DUMP_DIRECTORY,
        metavar="DIR",
        help=f"失败现场 dump 的落盘目录(默认 {crash_capture.DUMP_DIRECTORY}); "
        "dump 会作为附件挂进失败用例的 Allure 结果",
    )
    parser.addoption(
        "--crash-dump-depth",
        type=int,
        default=crash_capture.DEFAULT_DEPTH,
        metavar="N",
        help=f"失败现场 dump 的递归深度(默认 {crash_capture.DEFAULT_DEPTH}; 0 = 不生成 dump)",
    )


def _resolve_severity(item: pytest.Item) -> str:
    """解析用例严重等级: 显式标记优先, 否则按目录兜底."""
    for level in _SEVERITY_LEVELS:
        if item.get_closest_marker(level):
            return level
    parts = Path(str(item.fspath)).parts
    if "security" in parts:
        return "critical"
    if "performance" in parts:
        return "minor"
    # 单元与集成测试兜底 normal: 核心模块必须自己声明 critical/blocker, 否则新模块
    # 会静默落进(或错过)pre-commit 子集 —— 守卫用例保证每个模块都显式声明了等级。
    return "normal"


def _where(item: pytest.Item) -> str:
    """定位串, 用于拼写错误提示."""
    return f"{item.fspath}::{item.name}"


def _label_value(
    item: pytest.Item, name: str, allowed: tuple[str, ...] | None = None
) -> str | None:
    """取最近一层同名标记的取值(用例级覆盖模块级), 并校验闭集取值."""
    marker = item.get_closest_marker(name)
    if marker is None:
        return None
    if not marker.args:
        raise pytest.UsageError(
            f'{_where(item)}: {name} 标记缺少取值, 请写成 pytest.mark.{name}("...")'
        )
    value = str(marker.args[0])
    if allowed is not None and value not in allowed:
        raise pytest.UsageError(
            f"{_where(item)}: 未知的 {name} {value!r}, 可用取值: {' / '.join(allowed)}"
        )
    return value


def _default_layer(item: pytest.Item) -> str:
    """未显式声明 layer 时的兜底: 目录名, 否则按 integration 标记, 再兜底 unit."""
    parts = Path(str(item.fspath)).parts
    for directory, layer in _DIRECTORY_LAYERS.items():
        if directory in parts:
            return layer
    if item.get_closest_marker("integration"):
        return "integration"
    return "unit"


def _validate_metadata(item: pytest.Item) -> None:
    """收集期校验标签取值, 拼错时立即失败而不是静默多出分类."""
    for name, allowed in _LABEL_ALLOWED.items():
        _label_value(item, name, allowed)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """附加严重等级, 校验 Allure 标签, 按 --min-severity 过滤, 再按分片取本片."""
    minimum = config.getoption("--min-severity")
    min_rank = _SEVERITY_RANK[minimum] if minimum is not None else None
    kept: list[pytest.Item] = []
    for item in items:
        _validate_metadata(item)
        severity = _resolve_severity(item)
        item.add_marker(getattr(pytest.mark, severity))

        if min_rank is not None and _SEVERITY_RANK[severity] < min_rank:
            continue
        kept.append(item)

    if min_rank is not None:
        deselected = [item for item in items if item not in kept]
        items[:] = kept
        if deselected:
            config.hook.pytest_deselected(items=deselected)

    _apply_shard(config, items)


def _apply_shard(config: pytest.Config, items: list[pytest.Item]) -> None:
    """按 ``--shard-count`` / ``--shard-index`` 只保留本片要跑的用例.

    分片在**严重等级过滤之后**做: 本地 ``--min-severity=critical`` 选出来的子集也
    能均分成几片跑, 与 CI 的全量分片互不影响。
    """
    count = int(config.getoption("--shard-count"))
    index = int(config.getoption("--shard-index"))
    if count < 1:
        raise pytest.UsageError(f"--shard-count 至少为 1(当前 {count})")
    if not 0 <= index < count:
        raise pytest.UsageError(
            f"--shard-index 必须在 0..{count - 1} 之间(当前 {index})"
        )
    if count == 1:
        return
    buckets = sharding.shard_plan([item.nodeid for item in items], count=count)
    mine = set(buckets[index])
    total = sharding.load_of([item.nodeid for item in items])
    planned = sharding.load_of(buckets[index])
    print(
        f"[shard {index + 1}/{count}] 本片 {len(mine)} 个用例, "
        f"预计 {planned:.0f}s / 全量 {total:.0f}s"
    )
    deselected = [item for item in items if item.nodeid not in mine]
    items[:] = [item for item in items if item.nodeid in mine]
    if deselected:
        config.hook.pytest_deselected(items=deselected)


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[Any]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """用例失败时把现场留进它的 Allure 结果(见 ``tests/crash_capture.py``).

    - 钩子跑在**阶段报告生成时**, 也就是任何 teardown 夹具之前: 那时界面窗口还活着,
      截图才拍得到现场(``tests/gui_support.py`` 的登记表由夹具在用例结束后清空);
    - 只处理失败的报告; 每个用例只留一次现场(失败后 teardown 常跟着报第二次错);
    - 留证自身出错也只是附件里的一句话, 绝不影响用例结果与退出码。
    """
    report = yield
    if report.failed:
        from gui_support import live_apps

        crash_capture.attach_failure_evidence(
            item,
            call,
            directory=item.config.getoption("--crash-dump-dir"),
            depth=int(item.config.getoption("--crash-dump-depth")),
            apps=live_apps(),
        )
    elif report.skipped:
        # "跳过了"不等于"没事": GUI 守卫把建窗口期的任何 TclError 都写成跳过, 于是
        # 一条永远不跑的用例在报告里只是一次 skip。只有已知环境问题才允许跳过, 其它
        # 一律改成失败(见 tests/tk_guard.py)。
        message = tk_guard.unknown_tk_skip_message(
            tk_guard.skip_reason(report.longrepr)
        )
        if message is not None:
            report.outcome = "failed"
            report.longrepr = message
    return report


def _configure_allure(item: pytest.Item) -> None:
    """把名称、描述、四层标签、平台、分类与严重等级写入 Allure 元数据."""
    title = _title_of(item)

    epic = _label_value(item, "epic", _EPICS) or _UNCLASSIFIED
    feature = _label_value(item, "feature") or _UNCLASSIFIED
    story = _label_value(item, "story") or title
    layer = _label_value(item, "layer", _LAYERS) or _default_layer(item)
    platform = _current_platform_label()

    _apply_display_name(item, title)
    _ALLURE_DYNAMIC.description(_description_of(item))
    # epic/feature/story 驱动 "按产品级模块/功能模块/用户场景的稳定性分布" 控件.
    _ALLURE_DYNAMIC.epic(epic)
    _ALLURE_DYNAMIC.feature(feature)
    _ALLURE_DYNAMIC.story(story)
    # allure-pytest 没有 layer 装饰器, 只能写原始标签(Allure 3 "按层耗时" 直方图读它).
    _ALLURE_DYNAMIC.label("layer", layer)
    # 平台写成**参数**而不只是标签: Allure 用"用例全名 + 非 excluded 参数 + 标签"算重试
    # 身份(retryHash), 少了参数, 三个平台的同名结果容易被并成"同一用例重试了多次";
    # os 标签同时作为筛选维度。
    _ALLURE_DYNAMIC.parameter("平台", platform)
    _ALLURE_DYNAMIC.label("os", platform)
    # env 标签把平台升级成 Allure 3 的一等维度"环境": 仓库根的 ``allurerc.mjs`` 用它把结果
    # 归到 windows/macos/linux 三个环境, 于是合并报告里能按环境筛选, 每个用例的"环境"分页
    # 会列出它在三台机器上的结果(而不是只能从参数/套件名里认平台)。
    # 参数与套件名都保留: 它们是"配置没被生成端读到"时的兜底(那时环境会退化成 default),
    # 自检脚本 ``scripts/verify_allure_report.py`` 会把这种退化报成失败。
    _ALLURE_DYNAMIC.label("env", platform)
    # suite 三层与 layer/epic/feature 对齐, 作为只认 suite 标签的控件兜底;
    # 平台加在 parentSuite 末尾, 合并报告的套件树里能一眼看出用例跑在哪台机器上.
    _ALLURE_DYNAMIC.parent_suite(f"{_LAYER_SUITES.get(layer, layer)} · {platform}")
    _ALLURE_DYNAMIC.suite(epic)
    _ALLURE_DYNAMIC.sub_suite(feature)

    tags = sorted({marker.name for marker in item.iter_markers()} - _TAG_EXCLUDED)
    if tags:
        _ALLURE_DYNAMIC.tag(*tags)

    severity = _resolve_severity(item)
    if severity != "no_severity":
        _ALLURE_DYNAMIC.severity(_ALLURE_LEVELS[severity])


@pytest.fixture(autouse=True)
def _close_gui_apps() -> Iterator[None]:
    """用例结束后销毁它创建的 GUI 窗口(``tests/gui_support.gui_app`` 建的).

    放在根 conftest 而不是 ``tests/integration/conftest.py``: 两个同名 conftest 会让
    mypy 报 `Duplicate module named "conftest"`(两个目录都不是包)。因此这个夹具对所有
    用例都生效, 但单元测试不建窗口, 登记表始终是空的, 收尾是一次空循环。

    时机与原来的 ``try/finally: app.destroy()`` 完全一致(通过/失败/报错都会走到),
    于是三个 GUI 模块里 69 处收尾样板可以删掉, 销毁也变成报告里的夹具步骤。
    """
    yield
    from gui_support import close_gui_apps

    close_gui_apps()


@pytest.fixture(autouse=True)
def _allure_metadata(request: pytest.FixtureRequest) -> Iterator[None]:
    """在每个测试开始后写入 Allure 元数据, 结束时补回被 allure-pytest 清掉的描述。"""
    _configure_allure(request.node)
    yield
    _restore_description(request.node)


@pytest.fixture(autouse=True)
def _default_locale() -> Iterator[None]:
    """每个用例都从默认语言开始.

    界面语言是 i18n 模块里的全局状态, 而语言设置会真的调用 ``set_locale``; 用例之间
    必须隔离, 否则先跑语言用例的随机顺序会把后续用例的文案全变成英文。
    """
    set_locale(DEFAULT_LOCALE)
    yield
    set_locale(DEFAULT_LOCALE)


class _RecordingHandler(logging.Handler):
    """把审计日志采样到内存列表, 供用例断言。"""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def audit_log() -> Iterator[list[str]]:
    """捕获 ``archive_management.audit`` 的日志行.

    用户操作只写日志文件与内存, 不落数据库, 因此用例通过日志行断言
    "操作是否被记录"; 夹具临时把审计日志器降到 DEBUG 并捕获全部行。
    """
    logger = logging.getLogger(AUDIT_LOGGER_NAME)
    handler = _RecordingHandler()
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
