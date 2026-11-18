# -*- coding: utf-8 -*-
"""双档匹配：ID 确认 / 昵称匹配、黄红判定、记录按封禁日期降序。

匹配口径（对齐计划文档 §2.7-2 / §6.4 / §9 #4-#5，A2 已定）：
- 有 ``wgid`` 的名单记录 ↔ 完整录像玩家 ``dbid`` → **ID 确认**（``MATCH_TYPE_ID``）；
- 无 ID 时同昵称视为同一人 → **昵称匹配**（``MATCH_TYPE_NICKNAME``），仍计入黄/红；
- partial 录像没有其他玩家 dbid，昵称命中一律标「仅昵称匹配」（``partial=True``）；
- 黄/红 = 该玩家可归集的封禁记录条数：1 条黄、≥2 条红；
- 同一玩家多次记录：**同昵称跨批次累加 + 有 ID 时优先按 ID 归并**（ID 命中时把同昵称
  历史记录一并归集、按主键去重；匹配档位以 ID 优先，不因昵称记录降级）；
- 记录按封禁日期（``batch_day``）降序；同日期按 id 降序。

入口：``match_replay(detail, ban_index=None, conn=None)`` → ``list[models.MatchedPlayer]``。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from ..models import (
        MATCH_TYPE_ID,
        MATCH_TYPE_NICKNAME,
        MATCH_TYPE_NONE,
        LEVEL_NONE,
        LEVEL_RED,
        LEVEL_YELLOW,
        MatchedPlayer,
        ReplayPlayer,
    )
    from ..store.bans import BanIndex, load_ban_index, normalize_nickname
except ImportError:  # 直接以脚本方式运行（python src/match/matcher.py）时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.models import (
        MATCH_TYPE_ID,
        MATCH_TYPE_NICKNAME,
        MATCH_TYPE_NONE,
        LEVEL_NONE,
        LEVEL_RED,
        LEVEL_YELLOW,
        MatchedPlayer,
        ReplayPlayer,
    )
    from src.store.bans import BanIndex, load_ban_index, normalize_nickname

YELLOW_MIN_RECORDS = 1
RED_MIN_RECORDS = 2


# ---------------------------------------------------------------- 黄红判定
def level_for_count(ban_count: int) -> str:
    """按归集记录条数判定黄/红：1 条 = 黄，≥2 条 = 红，0 条 = 无。"""
    if ban_count >= RED_MIN_RECORDS:
        return LEVEL_RED
    if ban_count >= YELLOW_MIN_RECORDS:
        return LEVEL_YELLOW
    return LEVEL_NONE


def _as_index(ban_index: Any = None, conn: Any = None) -> BanIndex:
    """把调用方传入的索引/连接归一为 ``BanIndex``（应用层契约两种都传）。"""
    if isinstance(ban_index, BanIndex):
        return ban_index
    if ban_index is not None and hasattr(ban_index, "lookup"):
        return ban_index
    if conn is None and ban_index is None:
        conn = None
    return load_ban_index(conn)


def _sort_records(records: Iterable) -> list:
    """记录按封禁日期降序（同日期按主键降序）。"""
    return sorted(records, key=lambda item: (item.batch_day or "", item.id or 0), reverse=True)


# ---------------------------------------------------------------- 单玩家匹配
def match_player(player: Any, ban_index: BanIndex, partial_replay: bool = False) -> MatchedPlayer:
    """匹配一名参战玩家，返回 ``MatchedPlayer``（含黄/红与记录）。"""
    nickname = getattr(player, "nickname", None)
    dbid = getattr(player, "dbid", None)
    match_type, records = ban_index.lookup(dbid, nickname)
    records = _sort_records(records)
    matched = MatchedPlayer(
        nickname=nickname or "",
        dbid=dbid if isinstance(dbid, int) or dbid is None else None,
        clan_abbrev=getattr(player, "clan_abbrev", None),
        clan_id=getattr(player, "clan_id", None),
        team=getattr(player, "team", None),
        tank=getattr(player, "tank", None),
        match_type=match_type,
        ban_count=len(records),
        latest_ban_day=records[0].batch_day if records else None,
        level=level_for_count(len(records)),
        records=records,
        partial=bool(partial_replay and match_type == MATCH_TYPE_NICKNAME),
    )
    if match_type == MATCH_TYPE_ID:
        player.name_resolution = MATCH_TYPE_ID
    elif match_type == MATCH_TYPE_NICKNAME:
        player.name_resolution = MATCH_TYPE_NICKNAME
    return matched


# ---------------------------------------------------------------- 整局匹配
def match_replay(detail: Any, ban_index: Any = None, conn: Any = None) -> list:
    """匹配一局录像的全部参战玩家（应用层契约主入口）。

    detail: ``models.ReplayDetail``（也接受带 ``players`` 属性的对象）。
    ban_index: ``load_ban_index`` 返回的索引；为空时用 conn / 默认库现建。
    """
    players = list(getattr(detail, "players", None) or [])
    index = _as_index(ban_index, conn)
    partial = bool(getattr(detail, "is_partial", False)) or (getattr(detail, "block_count", 2) or 2) < 2
    return [match_player(player, index, partial_replay=partial) for player in players]


def match_players(players: Iterable, ban_index: Any = None, partial: bool = False,
                  conn: Any = None) -> list:
    """按玩家列表匹配（partial 由调用方显式给出；应用层备用入口）。"""
    index = _as_index(ban_index, conn)
    return [match_player(player, index, partial_replay=partial) for player in players or []]


def match_detail(detail: Any, ban_index: Any = None, conn: Any = None) -> list:
    """``match_replay`` 的别名（应用层候选名之一）。"""
    return match_replay(detail, ban_index=ban_index, conn=conn)


def summarize_matches(matches: Iterable) -> dict:
    """汇总匹配结果（命中人数 / ID 命中 / 昵称命中 / 黄 / 红），供界面顶栏。"""
    matches = list(matches or [])
    id_hits = [item for item in matches if item.match_type == MATCH_TYPE_ID]
    nickname_hits = [item for item in matches if item.match_type == MATCH_TYPE_NICKNAME]
    return {
        "total": len(matches),
        "hits": len(id_hits) + len(nickname_hits),
        "id_hits": len(id_hits),
        "nickname_hits": len(nickname_hits),
        "yellow": sum(1 for item in matches if item.level == LEVEL_YELLOW),
        "red": sum(1 for item in matches if item.level == LEVEL_RED),
        "partial": sum(1 for item in matches if item.partial),
    }


def main(argv: list) -> int:
    """自检：用内存库构造三条封禁记录，验证双档匹配与黄红（独立运行入口）。"""
    import time

    start = time.time()
    try:
        from ..store import db
        from ..config import load_config
    except ImportError:
        from src.store import db
        from src.config import load_config
    print("[1/3] 构造内存名单库（base_path=%r）" % ".")
    conn = db.connect(":memory:")
    rows = [
        ("2026-11-06", "测试红名", "违规使用脚本", "永久", 7053765071),
        ("2024-03-27", "测试红名", "违规使用插件", "7天", None),
        ("2021-10-25", "测试黄名", "违规使用脚本", "永久", None),
    ]
    for day, nickname, reason, duration, wgid in rows:
        conn.execute(
            "INSERT OR IGNORE INTO bans(batch_day, source_type, nickname, reason, duration, ban_times, wgid) "
            "VALUES(?, 'csv', ?, ?, ?, 1, ?)", (day, nickname, reason, duration, wgid))
    conn.commit()
    index = load_ban_index(conn)
    print("[2/3] 索引：%d 条记录 / %d 个 wgid" % (index.total_records, index.wgid_records))
    detail = type("Detail", (), {})()
    detail.block_count = 2
    detail.players = [
        ReplayPlayer(nickname="测试红名", dbid=7053765071, team=1),
        ReplayPlayer(nickname="测试黄名", dbid=7999999999, team=2),
        ReplayPlayer(nickname="路人玩家", dbid=7000000001, team=2),
    ]
    matches = match_replay(detail, index)
    print("[3/3] 匹配结果：%s" % summarize_matches(matches))
    for item in matches:
        print("      %-8s %-6s %s 条 最近 %s" % (item.nickname, item.level, item.ban_count, item.latest_ban_day))
    print("[done] 耗时 %.2f 秒" % (time.time() - start))
    conn.close()
    return 0


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
