# -*- coding: utf-8 -*-
"""P1 匹配测试：三档匹配（ID 确认 / 昵称匹配 / 未命中）、黄红判定、排序、partial 标注。"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.match import matcher  # noqa: E402
from src.models import (  # noqa: E402
    LEVEL_NONE,
    LEVEL_RED,
    LEVEL_YELLOW,
    MATCH_TYPE_ID,
    MATCH_TYPE_NICKNAME,
    MATCH_TYPE_NONE,
    ReplayDetail,
    ReplayPlayer,
)
from src.store import bans, db  # noqa: E402


@pytest.fixture()
def conn():
    """内存名单库：1 个红名（ID + 同名历史）、1 个黄名、1 个仅历史昵称。"""
    connection = db.connect(":memory:")
    rows = [
        ("2026-11-06", "RE_7053765071", "违规使用脚本", "永久", 7053765071),
        ("2022-08-09", "RE_7053765071", "违规使用插件", "永久", None),
        ("2024-03-27", "老玩家改名前", "违规使用插件", "7天", None),
        ("2021-10-25", "黄名玩家", "违规使用脚本", "永久", None),
        ("2020-09-15", "黄名玩家", "违规使用插件", "7天", None),
        ("2024-06-11", "历史昵称甲", "违规使用插件", "永久", None),
    ]
    for day, nickname, reason, duration, wgid in rows:
        connection.execute(
            "INSERT INTO bans(batch_day, source_type, nickname, reason, duration, ban_times, wgid, "
            "source_url, fetched_at) VALUES(?, 'csv', ?, ?, ?, 1, ?, ?, ?)",
            (day, nickname, reason, duration, wgid, "https://example.invalid/down?day=%s" % day,
             "2026-11-18T10:00:00+08:00"))
    connection.commit()
    yield connection
    connection.close()


def _detail(players, block_count=2) -> ReplayDetail:
    return ReplayDetail(file_path="demo.wotreplay", block_count=block_count, players=players)


def test_id_match_merges_nickname_history(conn):
    """ID 命中仍归集同昵称历史记录（同昵称跨批次累加 + 有 ID 优先归并）-> 红标。"""
    index = bans.load_ban_index(conn)
    detail = _detail([ReplayPlayer(nickname="RE_7053765071", dbid=7053765071, team=1)])
    matched = matcher.match_replay(detail, index)[0]
    assert matched.match_type == MATCH_TYPE_ID
    assert matched.ban_count == 2 and matched.level == LEVEL_RED
    assert [record.batch_day for record in matched.records] == ["2026-11-06", "2022-08-09"]
    assert matched.latest_ban_day == "2026-11-06"


def test_nickname_match_for_complete_replay(conn):
    """完整录像但该玩家无 wgid 命中 -> 同昵称视为同一人（昵称匹配，计入黄/红）。"""
    index = bans.load_ban_index(conn)
    detail = _detail([ReplayPlayer(nickname="历史昵称甲", dbid=7999999999, team=2)])
    matched = matcher.match_replay(detail, index)[0]
    assert matched.match_type == MATCH_TYPE_NICKNAME
    assert matched.level == LEVEL_YELLOW and matched.partial is False
    assert matched.dbid == 7999999999
    assert matched.records[0].batch_day == "2024-06-11"


def test_red_level_two_or_more_records(conn):
    """≥2 条归集记录 = 红标；记录按封禁日期降序。"""
    index = bans.load_ban_index(conn)
    matched = matcher.match_replay(_detail([ReplayPlayer(nickname="黄名玩家", team=1)]), index)[0]
    assert matched.ban_count == 2 and matched.level == LEVEL_RED
    assert [record.batch_day for record in matched.records] == ["2021-10-25", "2020-09-15"]
    assert matched.latest_ban_day == "2021-10-25"


def test_partial_replay_marks_nickname_only(conn):
    """partial 录像（block_count=1）昵称命中 -> partial=True，界面据此标「仅昵称匹配」。"""
    index = bans.load_ban_index(conn)
    detail = _detail([ReplayPlayer(nickname="历史昵称甲", dbid=None, team=2)], block_count=1)
    assert detail.is_partial
    matched = matcher.match_replay(detail, index)[0]
    assert matched.match_type == MATCH_TYPE_NICKNAME
    assert matched.partial is True
    assert matched.level == LEVEL_YELLOW


def test_no_hit(conn):
    """未命中：match_type=none / level=none / 0 条记录。"""
    index = bans.load_ban_index(conn)
    matched = matcher.match_replay(_detail([ReplayPlayer(nickname="路人玩家", dbid=7000000001)]), index)[0]
    assert matched.match_type == MATCH_TYPE_NONE
    assert matched.level == LEVEL_NONE
    assert matched.ban_count == 0 and matched.records == []
    assert matched.is_hit is False


def test_nickname_normalization(conn):
    """全半角/空白规范化：全角字母昵称可命中半角记录。"""
    conn.execute(
        "INSERT INTO bans(batch_day, source_type, nickname, reason, duration, ban_times, "
        "source_url, fetched_at) VALUES('2025-01-01', 'csv', 'ABC 玩家', '违规使用脚本', '永久', 1, "
        "'https://example.invalid/x', '2026-11-18T10:00:00+08:00')")
    conn.commit()
    index = bans.load_ban_index(conn)
    matched = matcher.match_replay(
        _detail([ReplayPlayer(nickname="ＡＢＣ　玩家", dbid=7000000002)]), index)[0]
    assert matched.match_type == MATCH_TYPE_NICKNAME
    assert matched.ban_count == 1


def test_match_players_and_summary(conn):
    """match_players 备用入口 + summarize_matches 汇总。"""
    index = bans.load_ban_index(conn)
    players = [
        ReplayPlayer(nickname="RE_7053765071", dbid=7053765071),
        ReplayPlayer(nickname="黄名玩家"),
        ReplayPlayer(nickname="历史昵称甲"),
        ReplayPlayer(nickname="路人玩家"),
    ]
    matches = matcher.match_players(players, index, partial=True)
    summary = matcher.summarize_matches(matches)
    assert summary["total"] == 4
    assert summary["id_hits"] == 1
    assert summary["nickname_hits"] == 2
    assert summary["red"] == 2      # RE_7053765071（ID + 同昵称历史）、黄名玩家
    assert summary["yellow"] == 1   # 历史昵称甲
    assert summary["partial"] == 2


def test_level_for_count():
    """黄红规则边界：0 无 / 1 黄 / 2+ 红。"""
    assert matcher.level_for_count(0) == LEVEL_NONE
    assert matcher.level_for_count(1) == LEVEL_YELLOW
    assert matcher.level_for_count(2) == LEVEL_RED
    assert matcher.level_for_count(9) == LEVEL_RED
