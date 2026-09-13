"""SQLite schema 与增量迁移定义.

迁移以 ``(版本号, 有序 SQL 语句)`` 形式集中声明;应用启动时按版本号在事务
内应用尚未执行的迁移(PLAN 5、阶段 A).

表结构与 PLAN 第 5 节对齐: games、save_locations、backup_nodes、
backup_files、scheduled_jobs, 阶段 E-1 增加的 monitored_directories 与
game_candidates, 以及阶段 E-2 增加的主页分类字段与 home_state. 操作日志不落库
(见版本 3), 改为写入日志文件。
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

# 版本 2(阶段 D 迭代): 备份命名与描述、每游戏的"当前节点"指针、自动备份保留份数.
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

# 版本 3(阶段 E 迭代): 安全点标记与操作日志出库.
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

# 版本 4(阶段 E 迭代): 备份目录按名称命名所需的两个字段.
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

# 版本 5(阶段 E-1 迭代): 本地游戏探测所需的监控目录与候选表.
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

# 版本 6(阶段 E-2 迭代): 统一游戏主页分类与筛选所需的状态.
#
# - ``games.origin``: 游戏来源平台(steam/epic/gog/battle_net/monitored/manual),
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

# 版本 7(阶段 E-2 迭代): 主页的展示偏好.
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

# 版本 8(阶段 E-2 迭代): 无限滚动模式被去掉, 主页只保留分页翻页.
#
# 展示方式(列表/海报)与每页条数仍然持久化; ``paging`` 已无任何读取方, 留着
# 会让人以为还存在两种翻页模式, 因此直接删列(该列不在索引或约束里, 可安全删除)。
_V8_STATEMENTS: Sequence[str] = (
    """
    ALTER TABLE home_state DROP COLUMN paging
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
)


def meta_table_sql() -> str:
    """返回幂等创建元数据表所需的 SQL."""
    return _META_TABLE_SQL
