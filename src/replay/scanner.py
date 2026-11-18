# -*- coding: utf-8 -*-
"""录像扫描与索引（P2）：递归发现、最近 N 天 / 全部、完整优先、增量缓存入 SQLite。

职责
----
- 递归扫描 ``Config.replay_dir`` 下全部 ``*.wotreplay``（含归档子目录）；
- 「最近 N 天」按 mtime 过滤 / 不传 days 即「全部」；
- **增量缓存**：按 ``(绝对路径, size, mtime)`` 命中 ``replays`` 表则直接复用，
  只解析新增/变更文件；解析结果写入 ``replays`` + ``battle_players``；
- **完整优先**：返回列表把 ``block_count==2`` 的完整局排在 partial 之前（均按新→旧）；
  ``find_latest_complete_replay`` 从新到旧找到第一个完整局即停（「找到即用」）；
- **异常容忍**：损坏/截断/正在写 → 跳过并记入 ``ScanStats.errors``，不中断整批；
- ``progress_cb(done, total, path, detail)`` 逐文件回调；``cancel_event``（threading.Event）
  置位后尽快停止并返回已扫结果。

注意（schema 限制，见计划 §6.5）
--------------------------------
``replays`` 表没有 ``arena_unique_id`` 列，因此**缓存命中的条目** arena id 为 None；
需要按局查战绩时，对用户选中的那一个文件调用 ``parser.parse_replay``（单文件约 2 ms）即可。

独立运行
--------
::

    python src/replay/scanner.py [--days N | --all] [--limit N] [--no-recursive]
        [--latest-complete] [--no-cache] [--db PATH] [--out 结果.json]
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

try:
    from .. import models
    from ..config import Config, load_config
    from ..store import db
    from . import parser
except ImportError:  # 直接以脚本方式运行（python src/replay/scanner.py）时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src import models
    from src.config import load_config
    from src.store import db
    from src.replay import parser

REPLAY_SUFFIX = ".wotreplay"
# 返回列表排序：完整局（block_count>=2）在前，同组内按 mtime 新→旧。
_COMPLETE_RANK = 0
_PARTIAL_RANK = 1


@dataclass
class ScanStats:
    """一次扫描的统计（界面进度条 / 验收日志的数据源）。"""

    total: int = 0          # 本次纳入扫描的文件数
    parsed: int = 0         # 实际解析数（缓存未命中）
    cached: int = 0         # 增量缓存命中数
    failed: int = 0         # 损坏/读取失败跳过数
    partial: int = 0        # 解析结果中 partial 数
    complete: int = 0       # 解析结果中完整局数
    aliases_written: int = 0  # 本次写入 aliases 的 (account_id,nickname) upsert 条数
    elapsed_s: float = 0.0
    cancelled: bool = False
    errors: list = field(default_factory=list)   # [(path, message)]


@dataclass
class ScanResult:
    """扫描结果：``replays`` 为 ``models.ReplayDetail`` 列表（完整优先排序）。"""

    replays: list = field(default_factory=list)
    stats: ScanStats = field(default_factory=ScanStats)


# ---------------------------------------------------------------- 文件发现
def discover_replays(replay_dir: Any, days: Optional[float] = None,
                     recursive: bool = True, now: Optional[datetime] = None) -> list:
    """发现录像文件并按 mtime 新→旧排序；``days`` 非空时只保留最近 N 天。"""
    root = Path(replay_dir)
    if not root.is_dir():
        return []
    current = now or datetime.now().astimezone()
    cutoff = None if days is None else current.timestamp() - float(days) * 86400.0
    pattern = "**/*.wotreplay" if recursive else "*.wotreplay"
    found = []
    for path in root.glob(pattern):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if cutoff is not None and stat.st_mtime < cutoff:
            continue
        found.append((path, stat.st_mtime))
    found.sort(key=lambda item: (item[1], item[0].name), reverse=True)
    return [item[0] for item in found]


# ---------------------------------------------------------------- SQLite 缓存
def _load_players(conn, replay_id: int) -> list:
    """从 ``battle_players`` 载入一局玩家（schema 无 real_name/battle_vehicle_id）。"""
    rows = conn.execute(
        "SELECT dbid, nickname, clan_abbrev, clan_id, team, tank, name_resolution "
        "FROM battle_players WHERE replay_id = ? ORDER BY team, nickname",
        (replay_id,),
    ).fetchall()
    return [
        models.ReplayPlayer(
            nickname=row["nickname"],
            dbid=row["dbid"],
            clan_abbrev=row["clan_abbrev"],
            clan_id=row["clan_id"],
            team=row["team"],
            tank=row["tank"],
            name_resolution=row["name_resolution"],
        )
        for row in rows
    ]


def load_detail(conn, replay_id: int) -> Optional[models.ReplayDetail]:
    """从 ``replays`` + ``battle_players`` 还原一个 ReplayDetail（缓存命中路径）。"""
    row = conn.execute("SELECT * FROM replays WHERE id = ?", (replay_id,)).fetchone()
    if row is None:
        return None
    detail = models.ReplayDetail(
        file_path=row["path"],
        file_name=Path(row["path"]).name,
        file_size=row["size"] or 0,
        mtime=row["mtime"] or 0.0,
        battle_time=row["battle_time"],
        map_name=row["map"],
        client_version=row["client_version"],
        block_count=row["block_count"] or 1,
        parse_status=row["parse_status"] or models.PARSE_STATUS_PARTIAL,
        players=_load_players(conn, replay_id),
    )
    detail.notes.append("cached: loaded from replays/battle_players")
    return detail


def find_cached(conn, path: Any, size: int, mtime: float) -> Optional[models.ReplayDetail]:
    """按 ``(path, size, mtime)`` 精确匹配增量缓存；命中返回 ReplayDetail。"""
    row = conn.execute(
        "SELECT id FROM replays WHERE path = ? AND size = ? AND mtime = ?",
        (str(path), int(size), float(mtime)),
    ).fetchone()
    if row is None:
        return None
    return load_detail(conn, row["id"])


def save_detail(conn, detail: models.ReplayDetail) -> int:
    """把解析结果 upsert 进 ``replays`` 并重建该局 ``battle_players``，返回 replay_id。"""
    conn.execute(
        "INSERT INTO replays(path, size, mtime, battle_time, map, client_version, "
        "block_count, parse_status, player_count, parsed_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET size = excluded.size, mtime = excluded.mtime, "
        "battle_time = excluded.battle_time, map = excluded.map, "
        "client_version = excluded.client_version, block_count = excluded.block_count, "
        "parse_status = excluded.parse_status, player_count = excluded.player_count, "
        "parsed_at = excluded.parsed_at",
        (
            detail.file_path,
            int(detail.file_size),
            float(detail.mtime),
            detail.battle_time,
            detail.map_name,
            detail.client_version,
            int(detail.block_count),
            detail.parse_status,
            detail.player_count,
            db.now_iso(),
        ),
    )
    row = conn.execute("SELECT id FROM replays WHERE path = ?", (detail.file_path,)).fetchone()
    replay_id = int(row["id"])
    conn.execute("DELETE FROM battle_players WHERE replay_id = ?", (replay_id,))
    conn.executemany(
        "INSERT INTO battle_players(replay_id, dbid, nickname, clan_abbrev, clan_id, team, tank, "
        "name_resolution) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (replay_id, player.dbid, player.nickname, player.clan_abbrev, player.clan_id,
             player.team, player.tank, player.name_resolution)
            for player in detail.players
        ],
    )
    return replay_id


def upsert_aliases(conn, detail: models.ReplayDetail) -> int:
    """把完整局玩家写入 ``aliases`` 表（改名链考古，计划 §6.4），返回本次 upsert 条数。

    - 只处理 **完整局**（``block_count>=2``）中 **有 dbid + 昵称** 的玩家；partial 无 dbid 不写；
    - 键 ``(account_id, nickname)``：``first_seen`` 取 min、``last_seen`` 取 max，
      ``source='replay'``；时间取该局 ``battle_time``（缺失时用当前时间）；
    - **幂等**：重复扫描同一局不产生新行、不改变已写入的 min/max。
    """
    if (detail.block_count or 0) < 2:
        return 0
    seen_at = detail.battle_time or db.now_iso()
    rows = []
    for player in detail.players or []:
        if player.dbid is None or not player.nickname:
            continue
        rows.append((int(player.dbid), str(player.nickname), seen_at, seen_at))
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO aliases(account_id, nickname, first_seen, last_seen, source) "
        "VALUES(?, ?, ?, ?, 'replay') "
        "ON CONFLICT(account_id, nickname) DO UPDATE SET "
        "first_seen = CASE WHEN aliases.first_seen IS NULL "
        "OR excluded.first_seen < aliases.first_seen "
        "THEN excluded.first_seen ELSE aliases.first_seen END, "
        "last_seen = CASE WHEN aliases.last_seen IS NULL "
        "OR excluded.last_seen > aliases.last_seen "
        "THEN excluded.last_seen ELSE aliases.last_seen END, "
        "source = 'replay'",
        rows,
    )
    return len(rows)


# ---------------------------------------------------------------- 单文件解析（缓存优先）
def parse_one(conn, path: Any, persist: bool = True) -> tuple:
    """解析一个文件（缓存优先）。返回 ``(detail, from_cache)``；异常向上抛给调用方。"""
    stat = Path(path).stat()
    if persist and conn is not None:
        cached = find_cached(conn, path, stat.st_size, stat.st_mtime)
        if cached is not None:
            return cached, True
    detail = parser.parse_replay(path)
    if persist and conn is not None:
        save_detail(conn, detail)
    return detail, False


def _detail_sort_key(detail: models.ReplayDetail) -> tuple:
    """完整局在前，同组内按 mtime 新→旧。"""
    rank = _COMPLETE_RANK if (detail.block_count or 0) >= 2 else _PARTIAL_RANK
    return (rank, -(detail.mtime or 0.0))


def _report(progress_cb: Optional[Callable], done: int, total: int, path: str,
            detail: Optional[models.ReplayDetail]) -> None:
    """安全调用进度回调（回调异常不得中断扫描）。"""
    if progress_cb is None:
        return
    try:
        progress_cb(done, total, path, detail)
    except Exception:  # noqa: BLE001 - 进度回调属于 UI 侧，失败不能影响扫描
        pass


# ---------------------------------------------------------------- 对外主函数
def scan_replays(config: Optional[Config] = None, conn=None, days: Optional[float] = None,
                 recursive: bool = True, complete_only: bool = False,
                 progress_cb: Optional[Callable] = None, cancel_event=None,
                 limit: Optional[int] = None, persist: bool = True) -> ScanResult:
    """扫描录像目录并建立索引。

    参数
    ----
    config: 配置；None 时 ``load_config()``。
    conn: SQLite 连接（测试传 ``db.connect(":memory:")``）；None 时用 ``config.db_path``。
    days: 最近 N 天（按 mtime）；None = 全部。
    complete_only: True 时返回列表只保留完整局（partial 仍会被索引/缓存）。
    progress_cb: ``progress_cb(done, total, path, detail_or_None)``。
    cancel_event: threading.Event；置位后停止。
    limit: 最多扫描前 N 个文件（调试/预览用）。
    persist: False 时不读不写 SQLite（纯解析，供测试/预览）。
    """
    cfg = config or load_config()
    owns_conn = False
    if persist and conn is None:
        conn = db.connect(cfg.db_path)
        owns_conn = True
    start = time.time()
    result = ScanResult()
    try:
        paths = discover_replays(cfg.replay_dir, days=days, recursive=recursive)
        if limit is not None:
            paths = paths[: int(limit)]
        result.stats.total = len(paths)
        for index, path in enumerate(paths):
            if cancel_event is not None and cancel_event.is_set():
                result.stats.cancelled = True
                break
            detail = None
            try:
                detail, from_cache = parse_one(conn, path, persist=persist)
                if from_cache:
                    result.stats.cached += 1
                else:
                    result.stats.parsed += 1
            except (OSError, ValueError) as exc:
                result.stats.failed += 1
                result.stats.errors.append((str(path), str(exc)))
            if detail is not None:
                if detail.parse_status == models.PARSE_STATUS_OK:
                    result.stats.complete += 1
                elif detail.parse_status == models.PARSE_STATUS_PARTIAL:
                    result.stats.partial += 1
                if persist and conn is not None:
                    result.stats.aliases_written += upsert_aliases(conn, detail)
                if not complete_only or (detail.block_count or 0) >= 2:
                    result.replays.append(detail)
            _report(progress_cb, index + 1, len(paths), str(path), detail)
        if persist and conn is not None:
            conn.commit()
    finally:
        if owns_conn and conn is not None:
            conn.close()
    result.replays.sort(key=_detail_sort_key)
    result.stats.elapsed_s = time.time() - start
    return result


def find_latest_complete_replay(config: Optional[Config] = None, conn=None,
                                days: Optional[float] = None, recursive: bool = True,
                                progress_cb: Optional[Callable] = None, cancel_event=None,
                                persist: bool = True, fallback_all: bool = True) -> Optional[models.ReplayDetail]:
    """从新到旧找第一个 ``block_count==2`` 的完整局（找到即停，不扫全量）。

    ``days`` 为空时默认取 ``config.default_scan_days``；窗口内没有完整局且
    ``fallback_all=True`` 时退化为全量（仍按新→旧、找到即停）。
    """
    cfg = config or load_config()
    window = cfg.default_scan_days if days is None else days
    detail = _find_latest_in_window(cfg, conn, window, recursive, progress_cb,
                                    cancel_event, persist)
    if detail is None and fallback_all and window is not None:
        detail = _find_latest_in_window(cfg, conn, None, recursive, progress_cb,
                                        cancel_event, persist)
    return detail


def _find_latest_in_window(cfg: Config, conn, days: Optional[float], recursive: bool,
                           progress_cb: Optional[Callable], cancel_event,
                           persist: bool) -> Optional[models.ReplayDetail]:
    """在给定时间窗口内从新到旧扫描，命中第一个完整局即返回。"""
    owns_conn = False
    if persist and conn is None:
        conn = db.connect(cfg.db_path)
        owns_conn = True
    found = None
    try:
        paths = discover_replays(cfg.replay_dir, days=days, recursive=recursive)
        for index, path in enumerate(paths):
            if cancel_event is not None and cancel_event.is_set():
                break
            detail = None
            try:
                detail, _from_cache = parse_one(conn, path, persist=persist)
            except (OSError, ValueError):
                _report(progress_cb, index + 1, len(paths), str(path), None)
                continue
            _report(progress_cb, index + 1, len(paths), str(path), detail)
            if (detail.block_count or 0) >= 2:
                if persist and conn is not None:
                    upsert_aliases(conn, detail)
                found = detail
                break
        if persist and conn is not None:
            conn.commit()
    finally:
        if owns_conn and conn is not None:
            conn.close()
    return found


# ---------------------------------------------------------------- 命令行入口
def parse_args(argv: list) -> dict:
    """解析命令行参数。"""
    options = {
        "days": None, "recursive": True, "latest_complete": False, "complete_only": False,
        "persist": True, "limit": None, "db_path": None, "out_path": None,
    }
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--days" and index + 1 < len(argv):
            options["days"] = float(argv[index + 1])
            index += 2
            continue
        if arg == "--limit" and index + 1 < len(argv):
            options["limit"] = int(argv[index + 1])
            index += 2
            continue
        if arg == "--db" and index + 1 < len(argv):
            options["db_path"] = argv[index + 1]
            index += 2
            continue
        if arg == "--out" and index + 1 < len(argv):
            options["out_path"] = argv[index + 1]
            index += 2
            continue
        if arg == "--all":
            options["days"] = None
        elif arg == "--latest-complete":
            options["latest_complete"] = True
        elif arg == "--complete-only":
            options["complete_only"] = True
        elif arg == "--no-cache":
            options["persist"] = False
        elif arg == "--no-recursive":
            options["recursive"] = False
        index += 1
    return options


def main(argv: list) -> int:
    """独立运行入口：扫描配置的录像目录，打印统计并可选落 JSON。"""
    start = time.time()
    base_path = "."
    options = parse_args(argv)
    cfg = load_config()
    conn = None
    if options["persist"]:
        conn = db.connect(options["db_path"] or cfg.db_path)
    progress_calls = {"count": 0}

    def progress_cb(done: int, total: int, path: str, detail) -> None:
        """命令行进度：每 200 个文件或最后一个文件打印一行。"""
        progress_calls["count"] += 1
        if done == total or done % 200 == 0:
            print("[progress] %d/%d  %s" % (done, total, Path(path).name))

    try:
        if options["latest_complete"]:
            detail = find_latest_complete_replay(
                config=cfg, conn=conn, days=options["days"],
                recursive=options["recursive"], progress_cb=progress_cb,
                persist=options["persist"])
            if detail is None:
                print("[warn] 未找到完整录像")
                return 1
            parser.print_detail(detail)
            if options["out_path"]:
                parser.save_json([parser.detail_to_dict(detail)], options["out_path"])
            return 0
        result = scan_replays(
            config=cfg, conn=conn, days=options["days"], recursive=options["recursive"],
            complete_only=options["complete_only"], progress_cb=progress_cb,
            limit=options["limit"], persist=options["persist"])
    finally:
        if conn is not None:
            conn.close()
    stats = result.stats
    print("scan dir   : %s" % cfg.replay_dir)
    print("files      : total=%d parsed=%d cached=%d failed=%d complete=%d partial=%d aliases=%d" %
          (stats.total, stats.parsed, stats.cached, stats.failed, stats.complete,
           stats.partial, stats.aliases_written))
    print("elapsed    : %.2f s (%.2f ms/file)" %
          (stats.elapsed_s, (stats.elapsed_s / stats.total * 1000.0) if stats.total else 0.0))
    if stats.errors:
        print("errors     : %d" % len(stats.errors))
        for path, message in stats.errors[:10]:
            print("  - %s: %s" % (path, message))
    if options["out_path"]:
        payload = {
            "replay_dir": str(cfg.replay_dir),
            "days": options["days"],
            "recursive": options["recursive"],
            "stats": {
                "total": stats.total, "parsed": stats.parsed, "cached": stats.cached,
                "failed": stats.failed, "complete": stats.complete, "partial": stats.partial,
                "aliases_written": stats.aliases_written,
                "elapsed_s": round(stats.elapsed_s, 3),
                "cancelled": stats.cancelled, "errors": stats.errors,
            },
            "replays": [parser.detail_to_dict(detail) for detail in result.replays],
        }
        target = Path(options["out_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8",
                          newline="\n")
        print("evidence   : %s (%d replays)" % (target, len(result.replays)))
    print("[done] base_path=%r 耗时 %.2f 秒" % (base_path, time.time() - start))
    return 0 if stats.failed == 0 else 1


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
