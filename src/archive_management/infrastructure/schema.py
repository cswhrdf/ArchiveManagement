"""SQLite schema 与增量迁移定义.

迁移以 ``(版本号, 有序 SQL 语句)`` 形式集中声明;应用启动时按版本号在事务
内应用尚未执行的迁移(PLAN 5、阶段 A).

表结构与 PLAN 第 5 节对齐: games、save_locations、backup_nodes、
backup_files、scheduled_jobs、operations.
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
    """
    CREATE TABLE IF NOT EXISTS operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        op_kind TEXT NOT NULL
            CHECK (op_kind IN ('backup', 'restore', 'import', 'export',
                               'delete', 'create_branch')),
        game_id INTEGER REFERENCES games(id) ON DELETE SET NULL,
        status TEXT NOT NULL DEFAULT 'started'
            CHECK (status IN ('started', 'succeeded', 'failed', 'cancelled')),
        message TEXT,
        started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        finished_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_operations_started ON operations (started_at)
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

SCHEMA_MIGRATIONS: Sequence[tuple[int, Sequence[str]]] = (
    (1, _V1_STATEMENTS),
    (2, _V2_STATEMENTS),
)


def meta_table_sql() -> str:
    """返回幂等创建元数据表所需的 SQL."""
    return _META_TABLE_SQL
