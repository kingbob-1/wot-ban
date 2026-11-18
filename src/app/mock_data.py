# -*- coding: utf-8 -*-
"""内置演示数据（mock）：数据层未就绪时驱动界面完整流程。

用途
----
T2/T3（名单库 / 录像解析 / 匹配 / 战绩）尚未就绪时，``AppService`` 自动进入
mock 模式，用本模块数据跑通「选择录像 -> 查询 -> 表格 / 明细 / 汇总 / 战绩」
全流程，保证 P3 GUI 骨架可独立开发、演示与截图。

数据是固定可复现的（不使用随机数），便于截图与手工核对。
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from .. import models
except ImportError:  # 直接以脚本方式运行时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src import models

# 演示基准时间（固定值，保证截图与对账稳定）。
MOCK_NOW = datetime(2026, 11, 18, 15, 30, 0)
MOCK_NOW_ISO = "2026-11-18T15:30:00+08:00"

# 录像目录 / 文件名只用于展示，不要求真实存在。
MOCK_REPLAY_DIR = r"X:\World_of_Tanks_CN\replays"

_PLAYER_NAMES = [
    "铁血战狼2020", "GhostShell", "炮火连天", "Panzerfaust", "北境守望者",
    "钢铁洪流", "ArtyParty", "KV2_神教", "豹一骑士", "迷雾行者",
    "疾风骤雨", "_Viper_", "蹲坑大师", "NightHawk", "雷霆万钧",
    "老兵不死", "IS7_推土机", "沉默的羔羊", "WarDrum", "一炮一个小朋友",
    "沙漠之狐", "T95慢吞吞", "星空下的猎手", "HeavyMetal", "百步穿杨",
    "临阵磨枪", "燃烧的远征", "FoxInTheBox", "雨夜屠夫", "最后一个武士",
]

_CLAN_ABBREVS = [
    "-TNT-", "WOLF", "ACE", "RUSH", "CN-1", "GHOST", "", "TIGER", "-TNT-", "WOLF",
    "", "ACE", "RUSH", "", "GHOST", "TIGER", "", "CN-1", "WOLF", "-TNT-",
    "ACE", "", "GHOST", "RUSH", "TIGER", "", "WOLF", "-TNT-", "", "ACE",
]

_TANKS = [
    "59式", "IS-7", "豹1", "鼠式", "E-100", "T-62A", "百夫长 Mk.VII", "Pz.Kpfw. VII",
    "FV4005", "AMX 50 B", "Object 277", "Progetto 65", "Type 5 Heavy", "M48A5 Patton",
    "T110E5", "Conqueror Gun Carriage", "Object 268 Version 4", "Strv 103B",
    "Rhm.-Borsig Waffenträger", "T57 Heavy", "Bat.-Châtillon 25 t", "Leopard 1",
    "Maus", "IS-4", "Super Conqueror", "Kranvagn", "WZ-111 model 5A", "S. Conqueror",
    "T-100 LT", "EBR 105",
]

_REASONS = [
    "使用违规插件",
    "使用外挂程序",
    "违规使用脚本",
    "使用违规插件（自瞄）",
    "使用违规插件（去草）",
    "使用挂机脚本",
]

_DURATIONS = ["永久", "7天", "30天", "永久", "永久", "7天"]

_MAPS = ["锡默尔斯多夫", "鲁别克", "普罗霍罗夫卡", "拉斯威利", "荒蛮之地"]

# 完整录像命中玩家：昵称下标 -> (红/黄, 是否 ID 命中, 记录条数)
_FULL_HITS = {
    2: (models.LEVEL_RED, True, 3),
    5: (models.LEVEL_YELLOW, False, 1),
    8: (models.LEVEL_YELLOW, True, 1),
    11: (models.LEVEL_YELLOW, False, 1),
    17: (models.LEVEL_RED, True, 2),
    26: (models.LEVEL_YELLOW, True, 1),
}

# partial 录像命中玩家（无 dbid，只能昵称匹配）：昵称下标 -> (红/黄, 记录条数)
_PARTIAL_HITS = {
    3: (models.LEVEL_RED, 2),
    9: (models.LEVEL_YELLOW, 1),
    14: (models.LEVEL_YELLOW, 1),
    21: (models.LEVEL_YELLOW, 1),
    25: (models.LEVEL_YELLOW, 1),
}

# 演示用录像（新的在前）：文件名 / 地图 / 对局时间 / 是否完整 / 玩家数。
_REPLAY_SPECS = [
    {
        "file_name": "20261117_201400_CN_锡默尔斯多夫.wotreplay",
        "battle_time": "2026-11-17T20:14:00+08:00",
        "map_name": "锡默尔斯多夫",
        "block_count": 2,
        "player_count": 30,
        "arena_unique_id": 1045123,
    },
    {
        "file_name": "20261116_193100_CN_普罗霍罗夫卡.wotreplay",
        "battle_time": "2026-11-16T19:31:00+08:00",
        "map_name": "普罗霍罗夫卡",
        "block_count": 1,
        "player_count": 28,
        "arena_unique_id": None,
    },
    {
        "file_name": "20261110_210200_CN_鲁别克.wotreplay",
        "battle_time": "2026-11-10T21:02:00+08:00",
        "map_name": "鲁别克",
        "block_count": 2,
        "player_count": 30,
        "arena_unique_id": 1042988,
    },
    {
        "file_name": "20260502_221500_CN_拉斯威利.wotreplay",
        "battle_time": "2026-05-02T22:15:00+08:00",
        "map_name": "拉斯威利",
        "block_count": 2,
        "player_count": 30,
        "arena_unique_id": 998211,
    },
]

# 名单库批次（最近 12 批；整体统计见 build_data_status）。
_BATCH_ROWS = [
    ("2026-11-06", "json", 129, 100),
    ("2026-10-21", "csv", 88, 0),
    ("2026-09-18", "csv", 76, 0),
    ("2026-08-14", "csv", 95, 0),
    ("2026-07-17", "csv", 64, 0),
    ("2026-06-12", "csv", 141, 0),
    ("2026-05-15", "csv", 102, 0),
    ("2026-04-10", "csv", 73, 0),
    ("2026-03-13", "csv", 115, 0),
    ("2026-02-14", "csv", 58, 0),
    ("2026-01-09", "csv", 89, 0),
    ("2025-12-12", "csv", 97, 0),
]


# ---------------------------------------------------------------- 工具函数
def _iso(dt: datetime) -> str:
    """datetime -> 本地 ISO 8601 字符串。"""
    return dt.astimezone().isoformat(timespec="seconds") if dt.tzinfo else dt.isoformat(timespec="seconds")


def _player_name(index: int) -> str:
    return _PLAYER_NAMES[index % len(_PLAYER_NAMES)]


def _team_of(index: int) -> int:
    return 1 if index < 15 else 2


def _tank_of(index: int) -> str:
    return _TANKS[index % len(_TANKS)]


def _clan_of(index: int) -> Optional[str]:
    value = _CLAN_ABBREVS[index % len(_CLAN_ABBREVS)]
    return value or None


def _dbid_of(index: int) -> int:
    """演示账号 ID（固定值，7 开头的 10 位）。"""
    return 7048100000 + index * 137


# ---------------------------------------------------------------- 录像
def build_replays() -> list:
    """构造演示录像列表（新到旧）。"""
    replays = []
    for seq, spec in enumerate(_REPLAY_SPECS, start=1):
        battle_dt = datetime.fromisoformat(spec["battle_time"])
        path = "%s\\%s" % (MOCK_REPLAY_DIR, spec["file_name"])
        replays.append(
            models.ReplayInfo(
                path=path,
                size=1_284_000 + seq * 4096,
                mtime=battle_dt.timestamp(),
                battle_time=spec["battle_time"],
                map_name=spec["map_name"],
                client_version="2.4.1.1 #994",
                block_count=spec["block_count"],
                parse_status=models.PARSE_STATUS_OK
                if spec["block_count"] >= 2
                else models.PARSE_STATUS_PARTIAL,
                player_count=spec["player_count"],
                replay_id=seq,
            )
        )
    return replays


def build_detail(replay) -> models.ReplayDetail:
    """按录像索引构造一份完整解析结果（含 30/28 名玩家）。"""
    file_path = str(getattr(replay, "path", replay))
    file_name = Path(file_path).name
    partial = bool(getattr(replay, "is_partial", False))
    map_name = getattr(replay, "map_name", None) or _MAPS[0]
    player_count = int(getattr(replay, "player_count", 30) or 30)
    battle_time = getattr(replay, "battle_time", None) or "2026-11-17T20:14:00+08:00"
    battle_dt = datetime.fromisoformat(battle_time)

    players = []
    for index in range(player_count):
        players.append(
            models.ReplayPlayer(
                nickname=_player_name(index),
                dbid=None if partial else _dbid_of(index),
                real_name=_player_name(index) if index % 7 == 0 else None,
                clan_abbrev=_clan_of(index),
                clan_id=None if not _clan_of(index) else 100000 + index,
                team=_team_of(index),
                tank=_tank_of(index),
                battle_vehicle_id="%08d" % (90000 + index),
            )
        )
    notes = []
    if partial:
        notes.append("partial 录像：未打完 / 缺少 results 块，只有昵称与军团，无账号 ID")
    return models.ReplayDetail(
        file_path=file_path,
        file_name=file_name,
        file_size=int(getattr(replay, "size", 0) or 0),
        mtime=float(getattr(replay, "mtime", battle_dt.timestamp()) or 0.0),
        battle_time=battle_time,
        map_name=map_name,
        map_display_name=map_name,
        client_version="2.4.1.1 #994",
        block_count=1 if partial else 2,
        duration_s=0 if partial else 512,
        winner_team=2,
        arena_unique_id=None if partial else int(getattr(replay, "arena_unique_id", 0) or 0),
        recorder_dbid=None if partial else _dbid_of(0),
        recorder_name=_player_name(0),
        parse_status=models.PARSE_STATUS_PARTIAL if partial else models.PARSE_STATUS_OK,
        players=players,
        notes=notes,
    )


# ---------------------------------------------------------------- 名单匹配
def _build_records(index: int, nickname: str, level: str, count: int, with_wgid: bool) -> list:
    """构造某玩家归集到的封禁记录（批次日期互不相同）。"""
    records = []
    day_pool = ["2026-11-06", "2026-10-21", "2025-08-14", "2024-06-12", "2023-03-13", "2021-08-16"]
    for seq in range(count):
        batch_day = day_pool[(index + seq) % len(day_pool)]
        reason = _REASONS[(index + seq) % len(_REASONS)]
        duration = _DURATIONS[(index + seq) % len(_DURATIONS)]
        records.append(
            models.BanRecord(
                batch_day=batch_day,
                nickname=nickname,
                source_type=models.SOURCE_TYPE_CSV,
                reason=reason,
                duration=duration,
                ban_times=1,
                wgid=_dbid_of(index) if with_wgid else None,
                source_url="https://profile.wg.360.cn/wstop/down?day=%s" % batch_day,
                raw_sheet=None,
                raw_nickname=nickname,
                fetched_at="2026-11-18T15:20:00+08:00",
                credibility="official",
            )
        )
    records.sort(key=lambda item: item.batch_day, reverse=True)
    return records


def build_matches(detail: models.ReplayDetail) -> list:
    """构造单局匹配结果（完整录像含 ID 命中；partial 仅昵称匹配）。"""
    partial = bool(getattr(detail, "is_partial", False))
    hits = _PARTIAL_HITS if partial else _FULL_HITS
    matched = []
    for index, player in enumerate(detail.players or []):
        item = models.MatchedPlayer(
            nickname=player.nickname or "",
            dbid=player.dbid,
            clan_abbrev=player.clan_abbrev,
            clan_id=player.clan_id,
            team=player.team,
            tank=player.tank,
            match_type=models.MATCH_TYPE_NONE,
            ban_count=0,
            level=models.LEVEL_NONE,
            records=[],
            partial=partial,
        )
        if index in hits:
            if partial:
                level, count = hits[index]
                with_wgid = False
            else:
                level, with_wgid, count = hits[index]
            records = _build_records(index, item.nickname, level, count, with_wgid)
            item.records = records
            item.ban_count = len(records)
            item.latest_ban_day = records[0].batch_day if records else None
            item.level = models.LEVEL_RED if item.ban_count >= 2 else models.LEVEL_YELLOW
            if partial:
                item.match_type = models.MATCH_TYPE_NICKNAME
            elif with_wgid:
                item.match_type = models.MATCH_TYPE_ID
            else:
                item.match_type = models.MATCH_TYPE_NICKNAME
        matched.append(item)
    return matched


def build_battle_stats(detail: models.ReplayDetail) -> models.BattleStats:
    """构造当局战绩（30 人，字段口径与盒子 battledetail 对齐）。"""
    players = []
    for index, player in enumerate(detail.players or []):
        shots = 4 + (index * 3) % 17
        piercings = max(0, shots - (index % 5))
        damage = 380 + (index * 211) % 5400
        players.append(
            models.BattlePlayerStats(
                dbid=player.dbid,
                nickname=player.nickname,
                team=player.team,
                tank=player.tank,
                combat=520 + (index * 97) % 1450,
                damage=damage,
                kills=index % 6,
                xp=180 + (index * 137) % 1500,
                shots=shots,
                piercings=piercings,
                spotted=(index * 3) % 9,
                capture_points=(index * 2) % 100,
            )
        )
    return models.BattleStats(
        arena_id=str(detail.arena_unique_id or 0),
        replay_id=None,
        fetched_at=MOCK_NOW_ISO,
        source="box",
        status=models.STATS_STATUS_OK,
        players=players,
        raw_json=None,
    )


def build_player_stats(matched: models.MatchedPlayer) -> models.PlayerStats:
    """构造综合战绩（盒子 index 口径，点查返回）。"""
    seed = int(matched.dbid or (sum(ord(ch) for ch in matched.nickname) * 17))
    return models.PlayerStats(
        account_id=seed,
        source="box_index",
        fetched_at=MOCK_NOW_ISO,
        update_time="2026-11-18T12:05:00+08:00",
        combat_power=4200 + seed % 6800,
        win_rate=round(44.0 + (seed % 160) / 10.0, 1),
        avg_damage=round(700 + (seed % 2600), 1),
        battles_hint=1200 + seed % 42000,
        raw_json=None,
    )


# ---------------------------------------------------------------- 数据状态 / 更新
def build_data_status() -> dict:
    """构造名单库状态（供顶栏与「数据状态」页展示）。"""
    batches = [
        {
            "batch_day": row[0],
            "source_type": row[1],
            "records": row[2],
            "wgid_records": row[3],
        }
        for row in _BATCH_ROWS
    ]
    return {
        "available": True,
        "db_path": r"<项目目录>\data\wotban.db",
        "batch_count": 114,
        "record_count": 322806,
        "official_record_count": 322941,
        "replay_index_count": 12851,
        "first_batch_day": "2020-08-14",
        "latest_batch_day": "2026-11-06",
        "wgid_days": 1,
        "wgid_records": 100,
        "wgid_total_records": 129,
        "missing_days": [],
        "last_check_at": "2026-11-18T15:20:00+08:00",
        "last_update_at": "2026-11-18T15:20:00+08:00",
        "batches": batches,
        "message": "演示数据：114 批 / 去重入库 322,806 条（官方源 322,941 物理行）；wgid 覆盖 1 期（100/129 条）；无缺失批次",
        "mock": True,
    }


def build_update_result() -> models.UpdateResult:
    """构造「更新名单」演示结果（已是最新，不产生新批次）。"""
    return models.UpdateResult(
        ok=True,
        checked_at=MOCK_NOW_ISO,
        latest_day="2026-11-06",
        new_days=[],
        downloaded_csv=[],
        saved_wgid=[],
        imported_records=0,
        wgid_coverage={
            "latest_day": "2026-11-06",
            "with_wgid": 100,
            "total": 129,
            "days": ["2026-11-06"],
        },
        errors=[],
        aborted=False,
        message="检查完成：当前已是最新批次（2026-11-06，129 条），无需更新",
    )


def main(argv: list) -> int:
    """自检：构造全套演示数据并打印关键计数（独立运行入口）。"""
    import time

    start = time.time()
    base_path = "."
    replays = build_replays()
    print("[1/3] 演示录像 %d 个（base_path=%r）" % (len(replays), base_path))
    for replay in replays:
        print("      %s partial=%s players=%d"
              % (Path(replay.path).name, replay.is_partial, replay.player_count))

    detail = build_detail(replays[0])
    matches = build_matches(detail)
    hits = [item for item in matches if item.is_hit]
    stats = build_battle_stats(detail)
    print(
        "[2/3] 完整局：玩家=%d 命中=%d（ID=%d 昵称=%d 黄=%d 红=%d）当局战绩=%d 人"
        % (
            len(detail.players),
            len(hits),
            len([x for x in hits if x.match_type == models.MATCH_TYPE_ID]),
            len([x for x in hits if x.match_type == models.MATCH_TYPE_NICKNAME]),
            len([x for x in hits if x.level == models.LEVEL_YELLOW]),
            len([x for x in hits if x.level == models.LEVEL_RED]),
            len(stats.players),
        )
    )
    partial_detail = build_detail(replays[1])
    partial_hits = [item for item in build_matches(partial_detail) if item.is_hit]
    print("[3/3] partial 局：玩家=%d 命中=%d 全部仅昵称匹配=%s" % (
        len(partial_detail.players), len(partial_hits), all(x.partial for x in partial_hits)))
    print("[done] mock_data self-check passed，耗时 %.2f 秒" % (time.time() - start))
    return 0


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
