# -*- coding: utf-8 -*-
"""数据模型（dataclass）：录像、名单记录、匹配结果、战绩缓存、更新结果。

字段契约（对齐计划文档 §6.3 / §6.5 / §6.9，后续模块只读取、不改字段名）：
- 录像：``ReplayInfo``（索引轻量行）/ ``ReplayDetail``（完整解析）/ ``ReplayPlayer``（参战玩家）
- 名单：``BanRecord``（一行 = 一条封禁记录；去重键 batch_day + nickname + reason + duration）
- 匹配：``MatchedPlayer``（单局玩家 + 归集后的封禁记录 + 匹配档位 + 黄/红）
- 战绩：``BattleStats`` / ``BattlePlayerStats``（当局 battledetail）、``PlayerStats``（综合战绩点查）
- 更新：``UpdateResult``（更新名单结果，供界面展示）

注意：这里只放数据结构，不写业务逻辑；字段顺序不重要，构造时请使用关键字参数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------- 枚举值常量
SOURCE_TYPE_XLSX = "xlsx"
SOURCE_TYPE_CSV = "csv"
SOURCE_TYPE_JSON = "json"
SOURCE_TYPE_EXTERNAL = "external"

# 匹配档位：id = 账号 ID 精确命中；nickname = 同昵称视为同一人；none = 未命中。
MATCH_TYPE_ID = "id"
MATCH_TYPE_NICKNAME = "nickname"
MATCH_TYPE_NONE = "none"

# 黄/红档位：1 条封禁记录 = 黄标；>= 2 条 = 红标。
LEVEL_RED = "red"
LEVEL_YELLOW = "yellow"
LEVEL_NONE = "none"

# 录像解析状态。
PARSE_STATUS_OK = "ok"
PARSE_STATUS_PARTIAL = "partial"
PARSE_STATUS_ERROR = "error"

# 战绩缓存状态。
STATS_STATUS_OK = "ok"
STATS_STATUS_EMPTY = "empty"
STATS_STATUS_EXPIRED = "expired"
STATS_STATUS_UNAVAILABLE = "unavailable"


@dataclass
class ReplayPlayer:
    """一局中的一名参战玩家（完整录像含 dbid，partial 录像只有昵称/军团/坦克）。"""

    nickname: Optional[str] = None
    dbid: Optional[int] = None
    real_name: Optional[str] = None
    clan_abbrev: Optional[str] = None
    clan_id: Optional[int] = None
    team: Optional[int] = None
    tank: Optional[str] = None
    battle_vehicle_id: Optional[str] = None
    # 匹配档位/ID 来源标记（由 matcher 填写）：MATCH_TYPE_ID / MATCH_TYPE_NICKNAME / None
    name_resolution: Optional[str] = None


@dataclass
class ReplayInfo:
    """录像索引行（扫描阶段只读文件头，不解析完整内容）。"""

    path: str
    size: int = 0
    mtime: float = 0.0
    battle_time: Optional[str] = None
    map_name: Optional[str] = None
    client_version: Optional[str] = None
    block_count: int = 1
    parse_status: str = PARSE_STATUS_PARTIAL
    player_count: int = 0
    replay_id: Optional[int] = None

    @property
    def is_partial(self) -> bool:
        """block_count < 2 即未打完的 partial 录像（无全部玩家 dbid）。"""
        return (self.block_count or 0) < 2


@dataclass
class ReplayDetail:
    """一个录像的完整解析结果（meta + results + 参战玩家）。"""

    file_path: str
    file_name: str = ""
    file_size: int = 0
    mtime: float = 0.0
    battle_time: Optional[str] = None
    map_name: Optional[str] = None
    map_display_name: Optional[str] = None
    client_version: Optional[str] = None
    block_count: int = 1
    duration_s: Optional[int] = None
    winner_team: Optional[int] = None
    arena_unique_id: Optional[int] = None
    recorder_dbid: Optional[int] = None
    recorder_name: Optional[str] = None
    parse_status: str = PARSE_STATUS_OK
    players: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def is_partial(self) -> bool:
        """partial 录像只能按昵称匹配，界面须标注「仅昵称匹配」。"""
        return (self.block_count or 0) < 2

    @property
    def player_count(self) -> int:
        return len(self.players or [])


@dataclass
class BanRecord:
    """一行 = 一条官方封禁记录（去重键 batch_day + nickname + reason + duration）。"""

    batch_day: str
    nickname: str
    source_type: str = SOURCE_TYPE_CSV
    reason: Optional[str] = None
    duration: Optional[str] = None
    ban_times: int = 1
    wgid: Optional[int] = None
    source_url: Optional[str] = None
    raw_sheet: Optional[str] = None
    raw_nickname: Optional[str] = None
    fetched_at: Optional[str] = None
    credibility: Optional[str] = None
    id: Optional[int] = None


@dataclass
class MatchedPlayer:
    """单局中一名玩家 + 归集到的全部封禁记录（结果表与明细面板的数据源）。"""

    nickname: str
    dbid: Optional[int] = None
    clan_abbrev: Optional[str] = None
    clan_id: Optional[int] = None
    team: Optional[int] = None
    tank: Optional[str] = None
    match_type: str = MATCH_TYPE_NONE
    ban_count: int = 0
    latest_ban_day: Optional[str] = None
    level: str = LEVEL_NONE
    records: list = field(default_factory=list)
    # 该玩家来自 partial 录像：只做了昵称匹配（界面标注「仅昵称匹配」）。
    partial: bool = False

    @property
    def is_id_matched(self) -> bool:
        return self.match_type == MATCH_TYPE_ID

    @property
    def is_hit(self) -> bool:
        return self.match_type != MATCH_TYPE_NONE


@dataclass
class BattlePlayerStats:
    """当局战绩中一名玩家的数据（battledetail 的 team_a/team_b 明细）。"""

    dbid: Optional[int] = None
    nickname: Optional[str] = None
    team: Optional[int] = None
    tank: Optional[str] = None
    combat: Optional[float] = None          # 单局战力（盒子口径，不是伤害）
    damage: Optional[int] = None
    kills: Optional[int] = None
    xp: Optional[int] = None
    shots: Optional[int] = None
    piercings: Optional[int] = None
    spotted: Optional[int] = None
    capture_points: Optional[int] = None


@dataclass
class BattleStats:
    """一场对局的当局战绩（按 arena_id 缓存，同一局重复查看零请求）。"""

    arena_id: str
    replay_id: Optional[int] = None
    fetched_at: Optional[str] = None
    source: str = "box"
    status: str = STATS_STATUS_OK
    players: list = field(default_factory=list)
    raw_json: Optional[str] = None


@dataclass
class PlayerStats:
    """一名玩家的综合战绩（仅点查，TTL 12-24h，界面标「仅供参考」）。"""

    account_id: int
    source: str = "box_index"               # box_index / wotup
    fetched_at: Optional[str] = None
    update_time: Optional[str] = None
    combat_power: Optional[float] = None
    win_rate: Optional[float] = None
    avg_damage: Optional[float] = None
    battles_hint: Optional[int] = None
    raw_json: Optional[str] = None


@dataclass
class UpdateResult:
    """一次「更新名单」检查/抓取的结果（界面展示 + 日志）。"""

    ok: bool = False
    checked_at: Optional[str] = None
    latest_day: Optional[str] = None
    new_days: list = field(default_factory=list)
    downloaded_csv: list = field(default_factory=list)
    saved_wgid: list = field(default_factory=list)
    imported_records: int = 0
    wgid_coverage: Optional[dict] = None
    errors: list = field(default_factory=list)
    aborted: bool = False                   # 遇 403/412/429/503 风控中止
    message: str = ""

    def add_error(self, message: str) -> None:
        """追加一条错误信息（不改变 ok 状态，由调用方决定）。"""
        self.errors.append(str(message))
        self.ok = False


def main(argv: list) -> int:
    """自检：构造全部模型各一个实例，验证 dataclass 契约可用（独立运行入口）。"""
    player = ReplayPlayer(nickname="示例玩家", dbid=7048141181, team=1)
    detail = ReplayDetail(file_path="example.wotreplay", block_count=2, players=[player])
    record = BanRecord(batch_day="2026-11-06", nickname="示例玩家", wgid=7048141181)
    matched = MatchedPlayer(nickname="示例玩家", dbid=7048141181, match_type=MATCH_TYPE_ID,
                            ban_count=1, level=LEVEL_YELLOW, records=[record])
    update = UpdateResult(ok=True, latest_day="2026-11-06")
    print("ReplayDetail: players=%d partial=%s" % (detail.player_count, detail.is_partial))
    print("MatchedPlayer: id_matched=%s hit=%s level=%s" % (matched.is_id_matched, matched.is_hit, matched.level))
    print("UpdateResult: ok=%s latest=%s" % (update.ok, update.latest_day))
    print("[done] models self-check passed")
    return 0


if __name__ == "__main__":
    import sys

    if __package__ in (None, ""):
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main(sys.argv[1:]))
