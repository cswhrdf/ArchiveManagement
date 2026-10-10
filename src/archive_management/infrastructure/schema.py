"""SQLite schema 与增量迁移定义.

迁移以 ``(版本号, 有序 SQL 语句)`` 形式集中声明; 应用启动时按版本号在事务内
应用尚未执行的迁移, 使旧数据库可以就地升级。

业务表: games、save_locations、backup_nodes、backup_files、scheduled_jobs,
游戏发现用的 monitored_directories、game_candidates, 以及主页状态表
home_state。操作日志不落库(见版本 3), 改为写入日志文件。
"""

from __future__ import annotations

from collections.abc import Sequence

MIGRATION_TABLE = "schema_migrations"

# 元数据表需在任何业务迁移之前存在, 单独维护并幂等创建.
_META_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""

# 版本 1: 初始业务表.
_V1_STATEMENTS: Sequence[str] = (
    """
    CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        steam_app_id INTEGER,
        platform TEXT NOT NULL DEFAULT 'windows',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS save_locations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        path TEXT NOT NULL,
        path_kind TEXT NOT NULL DEFAULT 'directory'
            CHECK (path_kind IN ('file', 'directory')),
        source TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('steam', 'manual')),
        is_primary INTEGER NOT NULL DEFAULT 0,
        last_checked_at TEXT,
        last_check_status TEXT,
        UNIQUE (game_id, path)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_save_locations_game
        ON save_locations (game_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        parent_id INTEGER REFERENCES backup_nodes(id) ON DELETE SET NULL,
        node_kind TEXT NOT NULL DEFAULT 'manual'
            CHECK (node_kind IN ('manual', 'branch', 'auto')),
        branch_name TEXT,
        note TEXT NOT NULL DEFAULT '',
        content_hash TEXT,
        storage_relpath TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_backup_nodes_game ON backup_nodes (game_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_backup_nodes_parent ON backup_nodes (parent_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        backup_id INTEGER NOT NULL REFERENCES backup_nodes(id) ON DELETE CASCADE,
        relative_path TEXT NOT NULL,
        size INTEGER NOT NULL DEFAULT 0,
        sha256 TEXT NOT NULL,
        file_kind TEXT NOT NULL DEFAULT 'file'
            CHECK (file_kind IN ('file', 'symlink', 'directory')),
        UNIQUE (backup_id, relative_path)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_backup_files_backup ON backup_files (backup_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduled_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER REFERENCES games(id) ON DELETE CASCADE,
        schedule TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_run_at TEXT,
        next_run_at TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
)

# 版本 2: 备份命名与描述、每游戏的"当前节点"指针、自动备份保留份数.
# 说明: SQLite 允许 ADD COLUMN 带 REFERENCES, 但该列默认值必须为 NULL,
# 因此 current_backup_id 在重建节点时由用例显式维护.
_V2_STATEMENTS: Sequence[str] = (
    # 名称(可编辑)与描述(备注)分离: title 为空时回退到分支名或类型默认名.
    """
    ALTER TABLE backup_nodes ADD COLUMN title TEXT NOT NULL DEFAULT ''
    """,
    # "恢复到此节点"= 把当前节点指针移到这里, 之后的备份/分支都从该节点继续.
    """
    ALTER TABLE games ADD COLUMN current_backup_id INTEGER
        REFERENCES backup_nodes(id) ON DELETE SET NULL
    """,
    # 自动备份是特殊备份: 只保留最近 N 份(默认 3), 允许用户自定义.
    """
    ALTER TABLE scheduled_jobs ADD COLUMN keep_auto INTEGER NOT NULL DEFAULT 3
    """,
)

# 版本 3: 安全点标记与操作日志出库.
#
# - ``is_safety``: "恢复前安全点"是特殊的手动备份, 只在时间线展示, 不参与
#   分支树的线路关系, 因此用一个独立标记而不是新增 node_kind(避免为了改
#   CHECK 约束而重建整张表, 那会连带影响外键与既有数据).
# - ``operations``: 操作日志改为写入日志文件(见 services/audit.py), 不再落库.
_V3_STATEMENTS: Sequence[str] = (
    """
    ALTER TABLE backup_nodes ADD COLUMN is_safety INTEGER NOT NULL DEFAULT 0
    """,
    "DROP TABLE IF EXISTS operations",
    "DROP INDEX IF EXISTS idx_operations_started",
)

# 版本 4: 备份目录按名称命名所需的两个字段.
#
# - ``original_name``: 录入游戏时识别到的名称(重命名不会改写它), 仅用于界面
#   展示"原始名称", 让用户知道磁盘上的目录来自哪个名字;
# - ``storage_key``: 备份根目录下该游戏实际使用的目录名(``<slug>-<token>``),
#   第一次备份时写入后不再变化 —— 改名或增删存档位置都不会搬动已有备份。
_V4_STATEMENTS: Sequence[str] = (
    """
    ALTER TABLE games ADD COLUMN original_name TEXT NOT NULL DEFAULT ''
    """,
    """
    ALTER TABLE games ADD COLUMN storage_key TEXT NOT NULL DEFAULT ''
    """,
)

# 版本 5: 本地游戏探测所需的监控目录与候选表.
#
# - ``monitored_directories``: 用户自行添加的监控目录(启用状态、备注、上次
#   扫描时间与结果); 路径唯一, 重复添加在用例层直接拒绝.
# - ``game_candidates``: 探测得到的候选游戏。候选是"待用户确认"的对象, 与
#   ``games`` 分开存放: 用户的导入/忽略决定(status)不会因为下次扫描被覆盖,
#   自动识别为"已纳入库"的候选通过 ``game_id`` 关联到游戏记录。
_V5_STATEMENTS: Sequence[str] = (
    """
    CREATE TABLE IF NOT EXISTS monitored_directories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        last_scan_at TEXT,
        last_scan_status TEXT,
        UNIQUE (path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS game_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        install_dir TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual',
        confidence TEXT NOT NULL DEFAULT 'medium'
            CHECK (confidence IN ('high', 'medium', 'low')),
        reason_code TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        found_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        status TEXT NOT NULL DEFAULT 'new'
            CHECK (status IN ('new', 'imported', 'ignored')),
        health TEXT NOT NULL DEFAULT 'ok',
        game_id INTEGER REFERENCES games(id) ON DELETE SET NULL,
        UNIQUE (install_dir)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_game_candidates_status
        ON game_candidates (status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_game_candidates_game
        ON game_candidates (game_id)
    """,
)

# 版本 6: 统一游戏主页分类与筛选所需的状态.
#
# - ``games.origin``: 游戏来源平台(steam/epic/gog/ubisoft/monitored/manual),
#   用于主页的"平台"分类; 与既有的 ``platform``(操作系统)无关。
# - ``games.tags``: 用户自定义标签, 逗号拼接保存(标签内不允许逗号)。
# - ``games.archived``: 归档标记 = 从主页收起来(记录与备份都保留, 可随时取消)。
# - ``games.last_activity_at``: 最近一次备份/恢复/修改的时间, 主页按它排序。
# - ``home_state``: 单行表, 保存主页当前的视图/平台/分类/搜索词。筛选条件属于
#   "用户偏好"而不是业务数据, 单独存一张表可以让迁移与清理互不影响; ``version``
#   用于最小版本控制, 格式变化时旧记录会被用例层忽略。
_V6_STATEMENTS: Sequence[str] = (
    """
    ALTER TABLE games ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'
    """,
    """
    ALTER TABLE games ADD COLUMN tags TEXT NOT NULL DEFAULT ''
    """,
    """
    ALTER TABLE games ADD COLUMN archived INTEGER NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE games ADD COLUMN last_activity_at TEXT
    """,
    """
    UPDATE games SET last_activity_at = created_at WHERE last_activity_at IS NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_games_archived ON games (archived)
    """,
    """
    CREATE TABLE IF NOT EXISTS home_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        view TEXT NOT NULL DEFAULT 'all',
        origin TEXT NOT NULL DEFAULT '',
        category TEXT NOT NULL DEFAULT '',
        search TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )
    """,
)

# 版本 7: 主页的展示偏好.
#
# 游戏发现合并进主窗口后, 主页同时承担"游戏库总览"的职责, 因此把展示方式
# (列表/海报)、翻页方式(分页/无限滚动)与每页条数一起保存到 home_state: 下次打开
# 软件仍是上次看到的样子。默认列表模式 + 分页模式 + 每页 30 条。
_V7_STATEMENTS: Sequence[str] = (
    """
    ALTER TABLE home_state ADD COLUMN layout TEXT NOT NULL DEFAULT 'list'
    """,
    """
    ALTER TABLE home_state ADD COLUMN paging TEXT NOT NULL DEFAULT 'page'
    """,
    """
    ALTER TABLE home_state ADD COLUMN page_size INTEGER NOT NULL DEFAULT 30
    """,
)

# 版本 8: 去掉无限滚动模式, 主页只保留分页翻页.
#
# 展示方式(列表/海报)与每页条数仍然持久化; ``paging`` 已无任何读取方, 留着
# 会让人以为还存在两种翻页模式, 因此直接删列(该列不在索引或约束里, 可安全删除)。
_V8_STATEMENTS: Sequence[str] = (
    """
    ALTER TABLE home_state DROP COLUMN paging
    """,
)

# 版本 9: 平台探测出的"存档路径候选".
#
# 平台清单(Steam 的 remotecache.vdf)只能给出"疑似存档"的路径, 不能直接当成
# 存档位置入库: 探测结果可能指向主目录、盘符根或游戏安装目录, 静默写入就会把
# 整盘内容卷进备份。因此候选单独放表, 只有用户确认后才写进 save_locations。
#
# risk_reason 非空表示该候选被判定为危险目标(见 services.pathcheck), 只展示
# 不采用; status 是处理进度, decided_at 记录用户做出决定的时间。
_V9_STATEMENTS: Sequence[str] = (
    """
    CREATE TABLE IF NOT EXISTS save_path_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        platform TEXT NOT NULL DEFAULT '',
        platform_game_id TEXT NOT NULL DEFAULT '',
        path TEXT NOT NULL,
        path_kind TEXT NOT NULL DEFAULT 'directory'
            CHECK (path_kind IN ('file', 'directory')),
        reason_code TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        relative_path TEXT NOT NULL DEFAULT '',
        confidence TEXT NOT NULL DEFAULT 'high'
            CHECK (confidence IN ('high', 'medium', 'low')),
        health TEXT NOT NULL DEFAULT 'ok',
        risk_reason TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'suggested'
            CHECK (status IN ('suggested', 'confirmed', 'ignored')),
        found_at TEXT NOT NULL,
        decided_at TEXT,
        UNIQUE (game_id, path)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_save_candidates_game
        ON save_path_candidates (game_id, status)
    """,
)

# 版本 10: 同一时刻只允许一个游戏处于启用状态.
#
# 启用态的语义从"这款游戏我还管着"变成"当前正在玩的就是这款" —— 快捷键与定时
# 备份只对它生效。旧库里可能同时存在多个 enabled=1, 无法判断哪个才是用户当前在玩
# 的, 因此迁移时全部停用, 由用户自己启用一个; 之后新建的游戏默认也是停用状态
# (见 domain.entities.Game)。顺带给 enabled 建索引: "现在启用的是哪款"会被反复查询。
_V10_STATEMENTS: Sequence[str] = (
    """
    UPDATE games SET enabled = 0
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_games_enabled ON games (enabled)
    """,
)

# 版本 11: 自动启停的状态.
#
# 自动启停要回答"现在该不该把启用态收回去", 判断依据是"被跟踪的那一款有没有运行过"
# (armed)与"用户最后手动停用的是哪一款"(suppressed)。两件事都必须落库: 重启软件后
# 丢掉 armed 会把"游戏还没启动"误当成"已经退出"。单行表, 与 home_state 同一套做法;
# 跟踪对象随游戏删除由外键置空, version 不符时旧记录按"没有状态"处理。
_V11_STATEMENTS: Sequence[str] = (
    """
    CREATE TABLE IF NOT EXISTS activation_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        tracked_game_id INTEGER REFERENCES games(id) ON DELETE SET NULL,
        armed INTEGER NOT NULL DEFAULT 0,
        suppressed_game_id INTEGER REFERENCES games(id) ON DELETE SET NULL,
        version INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )
    """,
)

# 版本 12: 全库监控与"启动顺序队列".
#
# 监控范围从"当前启用的那一款"扩到"当前导入的全部游戏", 因此
# 多出两样东西:
#
# - ``activation_runs``: 按"先后被观察到启动"的顺序登记正在运行的游戏 —— 顺序
#   就是回落依据, 必须跨重启保留; ``suppressed`` 记录"用户手动停用过它", 只要它
#   还在运行就不会被自动接管, 该行随它退出一起消失(抑制随之解除)。
# - ``activation_state`` 换成 ``monitor_game_id`` / ``armed`` / ``paused``: 监控
#   对象不能再从"哪一款启用"推出来(手动启用一款还没运行的游戏时, 队列里没有任何
#   可接的项, 得先把它记下来)。v11 的两列已无任何读取方, 直接重建该表; 旧记录
#   随之丢失 —— 与"版本不符按没有状态处理"一致。
_V12_STATEMENTS: Sequence[str] = (
    """
    DROP TABLE IF EXISTS activation_state
    """,
    """
    CREATE TABLE IF NOT EXISTS activation_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        monitor_game_id INTEGER REFERENCES games(id) ON DELETE SET NULL,
        armed INTEGER NOT NULL DEFAULT 0,
        paused INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 2,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS activation_runs (
        game_id INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        suppressed INTEGER NOT NULL DEFAULT 0,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_activation_runs_position
        ON activation_runs (position)
    """,
)

# 版本 13: 记住"程序自己写上去的译名".
#
# 译名会直接改写 ``games.name``, 而"用户改过名"原本只能靠 ``name != original_name``
# 判断 —— 那会把自己刚写进去的译名当成用户改的名, 于是切换语言后名字再也跟不上
# (中英来回切只有第一次生效)。因此多记一列"上一次由程序写入的译名": 名字等于它时
# 才允许被新译名覆盖, 用户改名时由 ``GameRepository.update`` 一并清空。
#
# 迁移是**保守方向**: 旧库里已经被改写过的名字(``name != original_name``)在升级后
# 一律按"用户改的名"看待 —— 它们分不出是谁写的, 宁可少改一次, 也不要把用户起的
# 名字覆盖掉。
_V13_STATEMENTS: Sequence[str] = (
    """
    ALTER TABLE games ADD COLUMN localized_name TEXT NOT NULL DEFAULT ''
    """,
)

# 版本 14: "按名字记住的忽略".
#
# 重新扫描时会把**这次没扫到、且不是已导入**的候选清掉(磁盘上没有的东西不该继续占着
# 列表, 用户 2026-10-03 的要求), 而"用户忽略过某些游戏"这个决定不能跟着记录一起没 ——
# 否则同一款游戏重新装上/重新扫到就会又冒回待处理。所以忽略改记在这张**与路径无关**的
# 表上: ``key`` 是规范化后的名字(见 ``domain.discovery.normalize_game_name``), ``name``
# 保留最后一次的原拼写供界面展示。
#
# 已知代价(写在这里免得以后当成 bug): 同名不同安装会一起被忽略; 名字变了(译名/版本
# 后缀/平台改名)就匹配不上, 那条已忽略的游戏会回到待处理。
_V14_STATEMENTS: Sequence[str] = (
    """
    CREATE TABLE IF NOT EXISTS ignored_candidates (
        key TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
)

# 版本 15: 记住"这份备份是用哪种校验方式记录的".
#
# 校验方式可以随时切换(见 ``config.VerificationSettings``): ``sha256`` 逐文件比对内容
# 哈希, ``name`` 只核对名称。名称模式下创建的备份**先不算哈希**(清单里的 sha256 留空,
# 备份因此很快), 由后台协程随后补齐 —— 于是需要一列记下"它当初是怎么记的": 补齐前只能
# 按名称校验, 还原预检与界面据此给出提示(见 ``application.backup.BackupService``)。
#
# 迁移是**空默认值**: 升级前创建的老备份里逐文件 sha256 一应俱全(那时只有一种校验
# 方式), 因此空串读出来按 sha256 看待(见 ``domain.entities.normalize_verification_mode``)
# —— "老存档没有当前校验的数据时一律回落到 sha256", 而不是降级成不校。将来新增校验
# 方式时沿用同一条规则: 新列先给空默认值, 读出来缺数据就回落到 sha256。
_V15_STATEMENTS: Sequence[str] = (
    """
    ALTER TABLE backup_nodes ADD COLUMN verify_mode TEXT NOT NULL DEFAULT ''
    """,
)

SCHEMA_MIGRATIONS: Sequence[tuple[int, Sequence[str]]] = (
    (1, _V1_STATEMENTS),
    (2, _V2_STATEMENTS),
    (3, _V3_STATEMENTS),
    (4, _V4_STATEMENTS),
    (5, _V5_STATEMENTS),
    (6, _V6_STATEMENTS),
    (7, _V7_STATEMENTS),
    (8, _V8_STATEMENTS),
    (9, _V9_STATEMENTS),
    (10, _V10_STATEMENTS),
    (11, _V11_STATEMENTS),
    (12, _V12_STATEMENTS),
    (13, _V13_STATEMENTS),
    (14, _V14_STATEMENTS),
    (15, _V15_STATEMENTS),
)


def meta_table_sql() -> str:
    """返回幂等创建元数据表所需的 SQL."""
    return _META_TABLE_SQL
