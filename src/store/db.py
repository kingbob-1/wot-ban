# -*- coding: utf-8 -*-
"""SQLite 数据访问底座：连接、建表、meta 读写。

表结构定稿于计划文档 §6.5（9 张表 + 索引 + ``meta.schema_version``）：

bans / players / aliases / replays / battle_players / source_snapshots / meta /
battle_stats / player_stats

v2 追加（幂等迁移，``CREATE TABLE IF NOT EXISTS``，不动既有表/数据）：

replay_arena_map —— partial 录像 → 盒子 arena_id 的匹配结果持久化
（key = 录像绝对路径；命中后二次打开零网络，见 ``BoxClient.get_partial_battle_stats``）。
battlelog_anchor —— 盒子战斗日志「页号 → 时间范围」锚点表（按昵称），
用于按 battle_time 估算页号 + 割线法校正，避免线性翻页（见 ``BoxClient``）。
battlelog_cap —— 战斗日志「封顶页」缓存（按昵称）：从该页起盒子返回完全重复的
同一份数据，记录 cap_page 与覆盖下界时间，后续同账号直接复用、不再探测。

约定
----
- 所有时间字段统一存 ISO 8601 字符串（``2026-11-18T15:30:00+08:00``，秒级）。
- ``meta`` 表的值统一 JSON 编码：字符串 / 数字 / 列表 / 字典都可直接存取。
- 去重键 ``(batch_day, nickname, reason, duration)`` 由唯一索引保证；
  SQLite 唯一索引把 NULL 视为互不相等，写入方需先清洗空值。
- 数据库文件默认 ``<项目>/data/wotban.db``（路径来自 Config），运行产物不入库。
- **schema 迁移只增不改**：``init_schema`` 可对旧库重复执行（幂等），只补新表/索引
  并升级 ``meta.schema_version``，绝不 DROP/重建既有表（保护 32 万名单与 1.2 万录像索引）。
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

# 当前 schema 版本；结构变更时 +1 并在 init_schema 里补迁移（只增不改）。
SCHEMA_VERSION = 2
META_SCHEMA_VERSION = "schema_version"

EXPECTED_TABLES = (
    "aliases",
    "bans",
    "battle_players",
    "battle_stats",
    "battlelog_anchor",
    "battlelog_cap",
    "meta",
    "player_stats",
    "players",
    "replay_arena_map",
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

-- v2：partial 录像 → 盒子 arena_id 映射（成功匹配后持久化，二次打开零网络）。
CREATE TABLE IF NOT EXISTS replay_arena_map (
    replay_path TEXT PRIMARY KEY,
    arena_id    TEXT NOT NULL,
    arena_win   INTEGER,
    tank_code   TEXT,
    battle_time TEXT,
    matched_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_replay_arena_map_arena ON replay_arena_map(arena_id);

-- v2：盒子战斗日志「页号 → 时间范围」锚点（按昵称），用于估算页号 + 跨进程复用。
CREATE TABLE IF NOT EXISTS battlelog_anchor (
    nickname    TEXT NOT NULL,
    page        INTEGER NOT NULL,
    newest_time TEXT,
    oldest_time TEXT,
    entry_count INTEGER,
    updated_at  TEXT,
    PRIMARY KEY (nickname, page)
);

-- v2：战斗日志封顶页缓存（从该页起盒子返回完全重复的数据；记录覆盖下界）。
CREATE TABLE IF NOT EXISTS battlelog_cap (
    nickname    TEXT PRIMARY KEY,
    cap_page    INTEGER,
    oldest_time TEXT,
    newest_time TEXT,
    updated_at  TEXT
);
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
    """建表（幂等）并升级 ``meta.schema_version``，返回当前 schema 版本。

    对旧库（如 schema_version=1 的 ``data/wotban.db``）重复执行是安全的：
    ``CREATE TABLE/INDEX IF NOT EXISTS`` 只补 v2 新表 ``replay_arena_map``，
    不触碰 bans / replays 等既有表与数据。
    """
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


# ---------------------------------------------------------------- replay→arena 映射
def get_replay_arena(conn: sqlite3.Connection, replay_path: Any) -> Optional[dict]:
    """读取 partial 录像 → 盒子 arena 映射（未匹配过 / 表缺失时返回 None）。"""
    if conn is None or not replay_path:
        return None
    try:
        row = conn.execute(
            "SELECT replay_path, arena_id, arena_win, tank_code, battle_time, matched_at "
            "FROM replay_arena_map WHERE replay_path = ?",
            (str(replay_path),),
        ).fetchone()
    except sqlite3.Error:  # 旧库未迁移/表缺失：视为未命中，不影响主流程
        return None
    if row is None:
        return None
    return {
        "replay_path": row["replay_path"],
        "arena_id": str(row["arena_id"]),
        "arena_win": row["arena_win"],
        "tank_code": row["tank_code"],
        "battle_time": row["battle_time"],
        "matched_at": row["matched_at"],
    }


def set_replay_arena(conn: sqlite3.Connection, replay_path: Any, arena_id: Any,
                     arena_win: Optional[int] = None, tank_code: Optional[str] = None,
                     battle_time: Optional[str] = None) -> None:
    """写入/更新 partial 录像 → arena 映射（按 ``replay_path`` upsert，幂等）。

    映射属于缓存增强：写失败只跳过持久化，绝不抛异常影响战绩主流程。
    """
    if conn is None or not replay_path or arena_id in (None, ""):
        return
    try:
        conn.execute(
            "INSERT INTO replay_arena_map(replay_path, arena_id, arena_win, tank_code, "
            "battle_time, matched_at) VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(replay_path) DO UPDATE SET arena_id = excluded.arena_id, "
            "arena_win = excluded.arena_win, tank_code = excluded.tank_code, "
            "battle_time = excluded.battle_time, matched_at = excluded.matched_at",
            (str(replay_path), str(arena_id), arena_win, tank_code, battle_time, now_iso()),
        )
        conn.commit()
    except sqlite3.Error:  # 缓存写失败不影响主流程
        pass


# ---------------------------------------------------------------- battleLog 页锚点
def get_battlelog_anchors(conn: sqlite3.Connection, nickname: Any) -> list:
    """读取某昵称的 battleLog 页→时间锚点（按页号升序；表缺失时返回空列表）。"""
    if conn is None or not nickname:
        return []
    try:
        rows = conn.execute(
            "SELECT nickname, page, newest_time, oldest_time, entry_count, updated_at "
            "FROM battlelog_anchor WHERE nickname = ? ORDER BY page ASC",
            (str(nickname),),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "nickname": row["nickname"],
            "page": int(row["page"]),
            "newest_time": row["newest_time"],
            "oldest_time": row["oldest_time"],
            "entry_count": row["entry_count"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def upsert_battlelog_anchor(conn: sqlite3.Connection, nickname: Any, page: Any,
                            newest_time: Optional[str] = None,
                            oldest_time: Optional[str] = None,
                            entry_count: Optional[int] = None) -> None:
    """写入/更新一条 battleLog 页锚点（按 (nickname, page) upsert，幂等）。"""
    if conn is None or not nickname or page in (None, ""):
        return
    try:
        conn.execute(
            "INSERT INTO battlelog_anchor(nickname, page, newest_time, oldest_time, "
            "entry_count, updated_at) VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(nickname, page) DO UPDATE SET newest_time = excluded.newest_time, "
            "oldest_time = excluded.oldest_time, entry_count = excluded.entry_count, "
            "updated_at = excluded.updated_at",
            (str(nickname), int(page), newest_time, oldest_time,
             _to_int_safe(entry_count), now_iso()),
        )
        conn.commit()
    except (sqlite3.Error, TypeError, ValueError):  # 缓存写失败不影响主流程
        pass


def _to_int_safe(value: Any) -> Optional[int]:
    """宽松转 int（锚点写入用；失败返回 None）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_battlelog_cap(conn: sqlite3.Connection, nickname: Any) -> Optional[dict]:
    """读取某昵称的 battleLog 封顶页缓存（未探测过 / 表缺失返回 None）。"""
    if conn is None or not nickname:
        return None
    try:
        row = conn.execute(
            "SELECT nickname, cap_page, oldest_time, newest_time, updated_at "
            "FROM battlelog_cap WHERE nickname = ?",
            (str(nickname),),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {
        "nickname": row["nickname"],
        "cap_page": row["cap_page"],
        "oldest_time": row["oldest_time"],
        "newest_time": row["newest_time"],
        "updated_at": row["updated_at"],
    }


def set_battlelog_cap(conn: sqlite3.Connection, nickname: Any, cap_page: Any,
                      oldest_time: Optional[str] = None,
                      newest_time: Optional[str] = None) -> None:
    """写入/更新 battleLog 封顶页缓存（按昵称 upsert，幂等；写失败不抛）。"""
    if conn is None or not nickname or cap_page in (None, ""):
        return
    try:
        conn.execute(
            "INSERT INTO battlelog_cap(nickname, cap_page, oldest_time, newest_time, updated_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(nickname) DO UPDATE SET cap_page = excluded.cap_page, "
            "oldest_time = excluded.oldest_time, newest_time = excluded.newest_time, "
            "updated_at = excluded.updated_at",
            (str(nickname), _to_int_safe(cap_page), oldest_time, newest_time, now_iso()),
        )
        conn.commit()
    except (sqlite3.Error, TypeError, ValueError):  # 缓存写失败不影响主流程
        pass


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
