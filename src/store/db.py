# -*- coding: utf-8 -*-
"""SQLite 数据访问底座：连接、建表、meta 读写。

表结构定稿于计划文档 §6.5（9 张表 + 索引 + ``meta.schema_version``）：

bans / players / aliases / replays / battle_players / source_snapshots / meta /
battle_stats / player_stats

约定
----
- 所有时间字段统一存 ISO 8601 字符串（``2026-11-18T15:30:00+08:00``，秒级）。
- ``meta`` 表的值统一 JSON 编码：字符串 / 数字 / 列表 / 字典都可直接存取。
- 去重键 ``(batch_day, nickname, reason, duration)`` 由唯一索引保证；
  SQLite 唯一索引把 NULL 视为互不相等，写入方需先清洗空值。
- 数据库文件默认 ``<项目>/data/wotban.db``（路径来自 Config），运行产物不入库。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

try:
    from ..config import load_config
except ImportError:  # 直接以脚本方式运行（python src/store/db.py）时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.config import load_config

# 当前 schema 版本；结构变更时 +1 并在 init_schema 里补迁移。
SCHEMA_VERSION = 1
META_SCHEMA_VERSION = "schema_version"

EXPECTED_TABLES = (
    "aliases",
    "bans",
    "battle_players",
    "battle_stats",
    "meta",
    "player_stats",
    "players",
    "replays",
    "source_snapshots",
)

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS bans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_day   TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'csv',
    source_url  TEXT,
    nickname    TEXT NOT NULL,
    reason      TEXT,
    duration    TEXT,
    ban_times   INTEGER NOT NULL DEFAULT 1,
    wgid        INTEGER,
    raw_sheet   TEXT,
    raw_nickname TEXT,
    fetched_at  TEXT,
    credibility TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_bans_dedup
    ON bans(batch_day, nickname, reason, duration);
CREATE INDEX IF NOT EXISTS ix_bans_wgid ON bans(wgid);
CREATE INDEX IF NOT EXISTS ix_bans_nickname ON bans(nickname);
CREATE INDEX IF NOT EXISTS ix_bans_batch_day ON bans(batch_day);

CREATE TABLE IF NOT EXISTS players (
    account_id INTEGER PRIMARY KEY,
    first_seen TEXT,
    last_seen  TEXT,
    notes      TEXT
);

CREATE TABLE IF NOT EXISTS aliases (
    account_id INTEGER NOT NULL,
    nickname   TEXT NOT NULL,
    first_seen TEXT,
    last_seen  TEXT,
    source     TEXT,
    PRIMARY KEY (account_id, nickname)
);
CREATE INDEX IF NOT EXISTS ix_aliases_nickname ON aliases(nickname);

CREATE TABLE IF NOT EXISTS replays (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    path           TEXT NOT NULL UNIQUE,
    size           INTEGER,
    mtime          REAL,
    battle_time    TEXT,
    map            TEXT,
    client_version TEXT,
    block_count    INTEGER,
    parse_status   TEXT NOT NULL DEFAULT 'ok',
    player_count   INTEGER,
    parsed_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_replays_battle_time ON replays(battle_time);
CREATE INDEX IF NOT EXISTS ix_replays_mtime ON replays(mtime);

CREATE TABLE IF NOT EXISTS battle_players (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    replay_id       INTEGER NOT NULL REFERENCES replays(id) ON DELETE CASCADE,
    dbid            INTEGER,
    nickname        TEXT,
    clan_abbrev     TEXT,
    clan_id         INTEGER,
    team            INTEGER,
    tank            TEXT,
    name_resolution TEXT
);
CREATE INDEX IF NOT EXISTS ix_battle_players_replay ON battle_players(replay_id);
CREATE INDEX IF NOT EXISTS ix_battle_players_dbid ON battle_players(dbid);
CREATE INDEX IF NOT EXISTS ix_battle_players_nickname ON battle_players(nickname);

CREATE TABLE IF NOT EXISTS source_snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL,
    kind       TEXT,
    status     INTEGER,
    bytes      INTEGER,
    fetched_at TEXT,
    batch_day  TEXT
);
CREATE INDEX IF NOT EXISTS ix_source_snapshots_batch ON source_snapshots(batch_day);

CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS battle_stats (
    arena_id   TEXT PRIMARY KEY,
    replay_id  INTEGER,
    fetched_at TEXT,
    source     TEXT,
    status     TEXT,
    raw_json   TEXT
);

CREATE TABLE IF NOT EXISTS player_stats (
    account_id   INTEGER PRIMARY KEY,
    source       TEXT,
    fetched_at   TEXT,
    update_time  TEXT,
    combat_power REAL,
    win_rate     REAL,
    avg_damage   REAL,
    battles_hint INTEGER,
    raw_json     TEXT
);
CREATE INDEX IF NOT EXISTS ix_player_stats_source ON player_stats(source);
"""


def now_iso() -> str:
    """当前本地时间（ISO 8601，秒级，带时区）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _resolve_db_path(db_path: Optional[Union[str, Path]]) -> Path:
    """未显式给路径时使用 Config.db_path；``:memory:`` 原样返回。"""
    if db_path is None:
        return Path(load_config().db_path)
    if str(db_path) == ":memory:":
        return Path(":memory:")
    return Path(db_path)


def connect(db_path: Optional[Union[str, Path]] = None, ensure_schema: bool = True) -> sqlite3.Connection:
    """打开数据库连接（必要时建库文件、建表）。

    参数
    ----
    db_path: 数据库路径；默认取 ``Config.db_path``；``:memory:`` 用于测试。
    ensure_schema: 为 True（默认）时自动执行 init_schema，保证表已就绪。
    """
    path = _resolve_db_path(db_path)
    if str(path) == ":memory:":
        conn = sqlite3.connect(":memory:", timeout=15.0)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    if ensure_schema:
        init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> int:
    """建表（幂等），写入/升级 ``meta.schema_version``，返回当前 schema 版本。"""
    conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    current = get_meta(conn, META_SCHEMA_VERSION)
    if isinstance(current, int) and current > SCHEMA_VERSION:
        raise RuntimeError(
            "数据库 schema_version=%s 高于本程序支持的 %s，请升级程序后再打开"
            % (current, SCHEMA_VERSION)
        )
    if current != SCHEMA_VERSION:
        set_meta(conn, META_SCHEMA_VERSION, SCHEMA_VERSION)
    conn.commit()
    return SCHEMA_VERSION


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """读取 meta 值（按 JSON 解码；不存在时返回 default）。"""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    raw = row["value"]
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """写入 meta 值（JSON 编码，``None`` 也合法），立即提交。"""
    payload = json.dumps(value, ensure_ascii=False)
    conn.execute(
        "INSERT INTO meta(key, value, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, payload, now_iso()),
    )
    conn.commit()


def list_tables(conn: sqlite3.Connection) -> list:
    """列出用户表名（排除 sqlite 内部表），用于自检/对账。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


def main(argv: list) -> int:
    """自检：在临时内存库建表并打印结构信息（独立运行入口）。"""
    import time

    start = time.time()
    base_path = "."
    print("[1/2] 建表自检（内存库，base_path=%r）" % base_path)
    conn = connect(":memory:")
    tables = list_tables(conn)
    missing = [name for name in EXPECTED_TABLES if name not in tables]
    version = get_meta(conn, META_SCHEMA_VERSION)
    set_meta(conn, "self_check", {"ok": True, "at": now_iso()})
    roundtrip = get_meta(conn, "self_check")
    print("      表数量=%d，schema_version=%s，缺失=%s" % (len(tables), version, missing or "无"))
    print("      meta 往返=%s" % (roundtrip,))
    print("[2/2] 自检完成，耗时 %.2f 秒" % (time.time() - start))
    return 0 if not missing else 1


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
