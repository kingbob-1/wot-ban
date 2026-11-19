# -*- coding: utf-8 -*-
"""P2.5 战绩接入测试：JSONP 去壳、180 天 guard、失败重试 1 次、缓存、限流、TTL。

全部用假 session / 注入 sleep，不发真实网络请求。
"""
import json
import re
import sqlite3
import sys
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import models  # noqa: E402
from src.config import Config  # noqa: E402
from src.stats import box_client  # noqa: E402
from src.store import db  # noqa: E402

ARENA_ID = 2315991864109994844


# ---------------------------------------------------------------- 假 HTTP
class FakeResponse:
    """最小响应对象：只带 status_code / text / content。"""

    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code
        self.content = text.encode("utf-8")


class FakeSession:
    """按顺序返回预设响应；异常对象会被抛出。记录请求 URL。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []
        self.headers = {}

    def get(self, url, timeout=None):
        self.urls.append(url)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def make_payload(team_size=2, arena_id=ARENA_ID, code="200", team_a_kills=14, team_b_kills=6):
    """构造 battledetail JSONP 内层 payload（team_a/team_b 各 team_size 人，含盒子全字段）。"""
    def side(prefix, team, base):
        players = []
        for index in range(team_size):
            players.append({
                "name": "%s%d" % (prefix, index),
                "clanAbbrev": "K-G",
                "clanDBID": 5136,
                "team": team,
                "tank_title": "Pz.55",
                "vehicle_icon": "germany-G55_Pz_Kpfw_55",
                "combat": 1000.5 + index,
                "vehicle": {
                    "accountDBID": base + index,
                    "damageDealt": 2000 + index,
                    "kills": index,
                    "xp": 500 + index,
                    "shots": 10 + index,
                    "piercings": 5 + index,
                    "spotted": 2 + index,
                    "capturePoints": 1 + index,
                    # 盒子同款明细（界面新增列的数据源）
                    "directHitsReceived": 10 + index,
                    "potentialDamageReceived": 4000 + index,
                    "damageBlockedByArmor": 1700 + index,
                    "damaged": 1 + index,
                    "damageAssistedRadio": 900 + index,
                    "damageAssistedTrack": 40 + index,
                    "numDefended": index,
                    "mileage": 1100 + index,
                },
            })
        return players

    return {
        "code": code,
        "message": "success" if code == "200" else "empty",
        "result": {
            "start_time": 1794674575,
            "end_time": "15:00",
            "duration": 900,
            "win_team": "team_a",
            "team_a_kills": team_a_kills,
            "team_b_kills": team_b_kills,
            "team_a": side("A", 1, 7000000001),
            "team_b": side("B", 2, 7000000101),
        } if code == "200" else "",
    }


def jsonp(payload):
    """把 payload 包成 JSONP 文本。"""
    return "cb(%s);" % json.dumps(payload, ensure_ascii=False)


class FakePagedSession:
    """按 URL 中的 ``p=`` 返回预设页 HTML（深翻页/封顶重复页测试用）。

    ``pages``：``{页号: html}``；未定义的页返回 200 空页（模拟超过最后一页）。
    """

    def __init__(self, pages, status=200):
        self.pages = dict(pages)
        self.status = status
        self.urls = []
        self.headers = {}

    def get(self, url, timeout=None):
        self.urls.append(url)
        match = re.search(r"[?&]p=(\d+)", url)
        page = int(match.group(1)) if match else 1
        item = self.pages.get(page)
        if isinstance(item, Exception):
            raise item
        if item is None:
            return FakeResponse("<html><body>空页</body></html>", self.status)
        return FakeResponse(item, self.status)


def make_client(tmp_path, responses, sleep=None, conn=None, **client_kwargs):
    """构造 BoxClient：内存库 + 假 session + 可注入 sleep（可覆盖熔断参数）。"""
    config = Config(project_root=tmp_path, db_path=Path(tmp_path) / "test.db")
    if conn is None:
        conn = db.connect(":memory:")
    if isinstance(responses, FakePagedSession):
        session = responses
    else:
        session = FakeSession(responses)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)

    client = box_client.BoxClient(config=config, conn=conn, session=session,
                                  sleep=sleep or fake_sleep, **client_kwargs)
    return client, conn, session, sleeps


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """每个测试前后重置进程级熔断状态，避免用例间串扰。"""
    box_client.reset_circuit_breaker()
    yield
    box_client.reset_circuit_breaker()


def recent_battle_time(days_ago=1):
    """返回 N 天前的 ISO 时间串（本地时区）。"""
    return (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat(timespec="seconds")


# ---------------------------------------------------------------- JSONP / 解析
def test_unwrap_jsonp_variants():
    """JSONP 去壳：cb(...); / 纯 JSON / 前后空白 / 非法输入。"""
    assert box_client.unwrap_jsonp('cb({"code":"200","result":{"x":1}});') == \
        {"code": "200", "result": {"x": 1}}
    assert box_client.unwrap_jsonp('  cb({"code":"200"});  ') == {"code": "200"}
    assert box_client.unwrap_jsonp('{"code":"200"}') == {"code": "200"}
    assert box_client.unwrap_jsonp('/*x*/ cb({"a":1})') == {"a": 1}
    assert box_client.unwrap_jsonp("garbage") is None
    assert box_client.unwrap_jsonp("") is None
    assert box_client.unwrap_jsonp(None) is None


def test_parse_battle_detail_fields(tmp_path):
    """team_a/team_b 30 人结构解析：dbid/team/战力/伤害/击杀/经验/点亮/占领 + 新字段。"""
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse("")])
    try:
        payload = make_payload(team_size=15)
        players = client.parse_battle_detail(payload)
        assert len(players) == 30
        assert {player.team for player in players} == {1, 2}
        first = players[0]
        assert first.dbid == 7000000001
        assert first.nickname == "A0"
        assert first.combat == 1000.5
        assert first.damage == 2000
        assert first.kills == 0
        assert first.xp == 500
        assert first.shots == 10
        assert first.piercings == 5
        assert first.spotted == 2
        assert first.capture_points == 1
        # ---- 盒子同款明细（界面新增列）----
        assert first.tank == "Pz.55"
        assert first.tank_code == "germany-G55_Pz_Kpfw_55"
        assert first.hits_received == 10
        assert first.potential_damage == 4000
        assert first.damage_blocked == 1700
        assert first.vehicles_damaged == 1
        assert first.assisted_damage == 940          # damageAssistedRadio + Track
        assert first.defense_points == 0
        assert first.mileage == 1100
        # 第二名玩家偏移 +1，确认逐行映射不是写死值
        second = players[1]
        assert second.hits_received == 11
        assert second.assisted_damage == 942
        assert second.defense_points == 1
        assert second.mileage == 1101
    finally:
        conn.close()


def test_battle_score_from_payload(tmp_path):
    """比分 = 盒子 team_a_kills v team_b_kills（如 14v6）；缺失字段返回空串。"""
    assert box_client.battle_score(make_payload(team_a_kills=14, team_b_kills=6)) == "14v6"
    assert box_client.battle_score(make_payload(team_a_kills=15, team_b_kills=7)) == "15v7"
    assert box_client.battle_score({"code": "100", "result": ""}) == ""
    assert box_client.battle_score(None) == ""


def test_arena_win_to_outcome_label():
    """battleLog arena-win：1→胜利 / 2→平局 / 0→失败；其它→未知。"""
    assert box_client.outcome_from_arena_win(1) == ("win", "胜利")
    assert box_client.outcome_from_arena_win(2) == ("draw", "平局")
    assert box_client.outcome_from_arena_win(0) == ("loss", "失败")
    assert box_client.outcome_from_arena_win("2") == ("draw", "平局")
    assert box_client.outcome_from_arena_win(None) == (None, "")


# ---------------------------------------------------------------- 180 天 guard
def test_180_day_guard_never_requests(tmp_path):
    """超期局（>180 天）返回 expired 且 **零请求**。"""
    client, conn, session, _sleeps = make_client(tmp_path, [FakeResponse(jsonp(make_payload()))])
    try:
        old_time = (datetime.now().astimezone() - timedelta(days=400)).isoformat(timespec="seconds")
        stats = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=old_time)
        assert stats is not None
        assert stats.status == models.STATS_STATUS_EXPIRED
        assert stats.players == []
        assert client.request_count == 0
        assert session.urls == []
        assert conn.execute("SELECT COUNT(*) AS n FROM battle_stats").fetchone()["n"] == 0
    finally:
        conn.close()


def test_guard_boundary_179_and_181_days(tmp_path):
    """窗口边界：179 天可查、181 天超期；battle_time 缺失时保守不发请求。"""
    client, conn, session, _sleeps = make_client(
        tmp_path, [FakeResponse(jsonp(make_payload()))])
    try:
        assert client.is_within_window(recent_battle_time(179)) is True
        assert client.is_within_window(recent_battle_time(181)) is False
        assert client.is_within_window(None) is False
        assert client.is_within_window("not-a-time") is False

        unavailable = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=None)
        assert unavailable is not None
        assert unavailable.status == models.STATS_STATUS_UNAVAILABLE
        assert client.request_count == 0
    finally:
        conn.close()


# ---------------------------------------------------------------- 重试 / 失败
def test_retry_once_then_success(tmp_path):
    """首次 HTTP 500，重试 1 次成功：共 2 次请求，返回 ok。"""
    client, conn, session, sleeps = make_client(
        tmp_path, [FakeResponse("", 500), FakeResponse(jsonp(make_payload()), 200)])
    try:
        stats = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=recent_battle_time(1))
        assert stats is not None
        assert stats.status == models.STATS_STATUS_OK
        assert len(stats.players) == 4
        assert client.request_count == 2
        assert len(session.urls) == 2
        assert any(wait >= 1.0 for wait in sleeps)  # 重试前退避
    finally:
        conn.close()


def test_failure_returns_none_after_one_retry(tmp_path):
    """持续失败：只重试 1 次（共 2 次请求），返回 None 且不抛异常。"""
    client, conn, session, _sleeps = make_client(tmp_path, [FakeResponse("", 500)])
    try:
        stats = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=recent_battle_time(1))
        assert stats is None
        assert client.request_count == 2
        assert len(session.urls) == 2
        assert client.is_in_cooldown() is False  # HTTP 500 不在 403/412/429/503 风控码表，仅重试不熔断
    finally:
        conn.close()


def test_network_exception_returns_none(tmp_path):
    """网络异常（requests 异常）同样不抛，返回 None；连续 2 次计入熔断。"""
    client, conn, _session, _sleeps = make_client(
        tmp_path, [ConnectionError("boom"), ConnectionError("boom")])
    try:
        stats = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=recent_battle_time(1))
        assert stats is None
        assert client.request_count == 2
        assert client.is_in_cooldown() is True  # 网络异常计入连续失败 → 熔断
    finally:
        conn.close()


def test_jsonp_code_not_200_retries_once(tmp_path):
    """JSONP code=100 empty：重试 1 次后成功（findings §3.5 实测场景）。"""
    empty = FakeResponse(jsonp(make_payload(code="100")), 200)
    ok = FakeResponse(jsonp(make_payload()), 200)
    client, conn, session, _sleeps = make_client(tmp_path, [empty, ok])
    try:
        stats = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=recent_battle_time(1))
        assert stats is not None
        assert stats.status == models.STATS_STATUS_OK
        assert client.request_count == 2
        assert len(session.urls) == 2
    finally:
        conn.close()


# ---------------------------------------------------------------- battle_stats 缓存
def test_battle_stats_cache_by_arena_id(tmp_path):
    """同一局第二次查看零请求（battle_stats 按 arena_id 缓存）。"""
    client, conn, session, _sleeps = make_client(
        tmp_path, [FakeResponse(jsonp(make_payload()))])
    try:
        first = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=recent_battle_time(1))
        assert first is not None and first.status == models.STATS_STATUS_OK
        assert client.request_count == 1
        second = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=recent_battle_time(1))
        assert second is not None
        assert client.request_count == 1  # 缓存命中，无新请求
        assert [player.dbid for player in second.players] == \
            [player.dbid for player in first.players]
        row = conn.execute("SELECT status, source, raw_json FROM battle_stats WHERE arena_id = ?",
                           (str(ARENA_ID),)).fetchone()
        assert row["status"] == models.STATS_STATUS_OK
        assert row["source"] == "box"
        assert json.loads(row["raw_json"])["code"] == "200"
        # force_refresh 绕过缓存
        third = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=recent_battle_time(1),
                                        force_refresh=True)
        assert third is not None
        assert client.request_count == 2
    finally:
        conn.close()


def test_battle_stats_raw_evidence_written(tmp_path):
    """成功响应可原样落盘（原始 JSONP 证据）。"""
    raw_path = tmp_path / "raw" / "battledetail.jsonp"
    client, conn, _session, _sleeps = make_client(
        tmp_path, [FakeResponse(jsonp(make_payload()))])
    try:
        stats = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=recent_battle_time(1),
                                        raw_out=raw_path)
        assert stats is not None
        assert raw_path.is_file()
        assert raw_path.read_text(encoding="utf-8").startswith("cb(")
    finally:
        conn.close()


# ---------------------------------------------------------------- 个人战绩 index
INDEX_HTML = """
<html><body>
<input type="hidden" id="p_id" value="7048141181">
<input type="hidden" id="wu_id" value="30245539">
<p class="name">kingbob_</p>
<span class="win-rate-1k" win-rate="56"></span>
<em>战斗力</em><span class="num">1376</span>
<p class="total">总 1500</p>
<span>更新时间：2026-11-15 06:35</span>
<ul class="userRecord-data clearfix">
<li><p class="title">杀伤</p><p class="num">2614</p></li>
</ul>
</body></html>
"""


def test_player_stats_parse_and_same_day_cache(tmp_path):
    """index 点查解析 + 自然日缓存（同一天零请求、跨天过期、force 强制刷新）。"""
    client, conn, session, _sleeps = make_client(tmp_path, [FakeResponse(INDEX_HTML)])
    try:
        first = client.get_player_stats(account_id=7048141181)
        assert first is not None
        assert first.account_id == 7048141181
        assert first.combat_power == 1376
        assert first.win_rate == 56
        assert first.avg_damage == 2614
        assert first.battles_hint == 1500
        assert first.update_time == "2026-11-15 06:35"
        assert client.request_count == 1
        # 同一天内：缓存命中，零请求
        second = client.get_player_stats(account_id=7048141181)
        assert second is not None and client.request_count == 1
        # force_refresh=True：跳过缓存强制重新请求（界面「刷新综合战绩」）
        third = client.get_player_stats(account_id=7048141181, force_refresh=True)
        assert third is not None
        assert client.request_count == 2
        # 缓存时间改成「昨天」→ 跨天视为过期，重新请求
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
        conn.execute("UPDATE player_stats SET fetched_at = ?", (yesterday,))
        conn.commit()
        fourth = client.get_player_stats(account_id=7048141181)
        assert fourth is not None
        assert client.request_count == 3
        # 兼容旧配置：TTL 小时字段仍在 12–24h 区间
        assert client.config.player_stats_cache_by_day is True
        ttl_hours = client.config.player_stats_ttl_hours
        assert 12 <= ttl_hours <= 24
    finally:
        conn.close()


def test_player_stats_same_day_boundary(tmp_path):
    """自然日口径：今天凌晨抓的缓存当天仍命中；昨天 23:59 的缓存跨天即过期。"""
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse(INDEX_HTML)])
    try:
        now = datetime.now().astimezone()
        today_early = now.replace(hour=0, minute=1, second=0, microsecond=0)
        assert client._player_stats_cache_fresh(today_early, now) is True
        yesterday_late = (now - timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)
        assert client._player_stats_cache_fresh(yesterday_late, now) is False
        assert client._player_stats_cache_fresh(None, now) is False
        # 旧口径（按小时 TTL）可通过配置关闭自然日模式，保持向后兼容
        client.config.player_stats_cache_by_day = False
        assert client._player_stats_cache_fresh(today_early, now) is True
        assert client._player_stats_cache_fresh(now - timedelta(hours=19), now) is False
    finally:
        conn.close()


def test_player_stats_rate_limit_2s(tmp_path):
    """index 点查限流：相邻请求间隔 ≥ 2 s（注入 sleep 记录等待时长）。"""
    client, conn, session, sleeps = make_client(tmp_path, [FakeResponse(INDEX_HTML)])
    try:
        client.get_player_stats(account_id=7048141181)
        client.get_player_stats(account_id=7048141182)
        assert client.request_count == 2
        assert any(wait >= client.config.request_interval_s - 0.1 for wait in sleeps)
        assert client.config.request_interval_s >= 2.0
    finally:
        conn.close()


def test_player_stats_not_found(tmp_path):
    """查不到玩家（页面含固定提示）→ None。"""
    html = "<html><body>没有找到您搜索的玩家！</body></html>"
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse(html)])
    try:
        assert client.get_player_stats(nickname="不存在的人") is None
    finally:
        conn.close()


def test_player_stats_nickname_url_encoded(tmp_path):
    """中文昵称 URL 编码后请求（pn=完整昵称）。"""
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse(INDEX_HTML)])
    try:
        client.get_player_stats(nickname="玩家甲")
        assert "%E7%8E%A9%E5%AE%B6%E7%94%B2" in client.last_url
        assert "pn=" in client.last_url
    finally:
        conn.close()


def test_fixed_user_agent(tmp_path):
    """请求头 UA 必须是实测可用值（Chrome/86 系，来自 Config；升级 UA 会 403）。"""
    config = Config(project_root=tmp_path, db_path=Path(tmp_path) / "test.db")
    conn = db.connect(":memory:")
    client = box_client.BoxClient(config=config, conn=conn)
    try:
        ua = client.session.headers.get("User-Agent", "")
        assert "Chrome/86.0.4240.198" in ua
        assert ua == config.box_user_agent
        assert config.box_headers()["Referer"].startswith("http://wotbox.ouj.com")
    finally:
        client.close()
        conn.close()


# ---------------------------------------------------------------- 连续失败熔断（QA D2 修复）
def test_circuit_breaker_after_two_403(tmp_path):
    """连续 2 次 403 → 熔断冷却；冷却期所有请求零请求并返回 cooldown 状态。"""
    client, conn, session, _sleeps = make_client(tmp_path, [FakeResponse("", 403)])
    try:
        assert client.get_battle_stats(ARENA_ID, "kingbob_",
                                       battle_time=recent_battle_time(1)) is None
        assert client.request_count == 2          # 单次重试，共 2 次尝试
        assert client.is_in_cooldown() is True
        state = box_client.circuit_breaker_state()
        assert state["in_cooldown"] is True
        assert state["cooldown_remaining_s"] > 0
        assert "HTTP 403" in state["last_reason"]

        before = client.request_count
        stats = client.get_battle_stats(ARENA_ID + 1, "kingbob_",
                                        battle_time=recent_battle_time(1))
        assert stats is not None
        assert stats.status == box_client.STATS_STATUS_COOLDOWN
        assert client.get_player_stats(account_id=7048141181) is None
        assert client.request_count == before      # 冷却期零网络请求
        assert len(session.urls) == 2
    finally:
        conn.close()


def test_circuit_breaker_after_network_exceptions(tmp_path):
    """连续 2 次网络异常同样触发熔断冷却（冷却期零请求）。"""
    client, conn, _session, _sleeps = make_client(tmp_path, [ConnectionError("boom")])
    try:
        assert client.get_battle_stats(ARENA_ID, "kingbob_",
                                       battle_time=recent_battle_time(1)) is None
        assert client.request_count == 2
        assert client.is_in_cooldown() is True
        assert box_client.circuit_breaker_state()["last_reason"].startswith("network")
        before = client.request_count
        stats = client.get_battle_stats(ARENA_ID + 1, "kingbob_",
                                        battle_time=recent_battle_time(1))
        assert stats.status == box_client.STATS_STATUS_COOLDOWN
        assert client.request_count == before
    finally:
        conn.close()


def test_circuit_breaker_success_resets_counter(tmp_path):
    """成功响应清零连续失败计数；只有再次连续失败才熔断。"""
    responses = [FakeResponse("", 403), FakeResponse(jsonp(make_payload())),
                 FakeResponse("", 403), FakeResponse("", 403)]
    client, conn, _session, _sleeps = make_client(tmp_path, responses)
    try:
        first = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=recent_battle_time(1))
        assert first is not None and first.status == models.STATS_STATUS_OK
        assert client.is_in_cooldown() is False    # 失败 1 次后成功 → 清零
        assert client.request_count == 2
        second = client.get_battle_stats(ARENA_ID + 1, "kingbob_",
                                         battle_time=recent_battle_time(1))
        assert second is None
        assert client.is_in_cooldown() is True     # 再次连续 2 次 403 → 熔断
        assert client.request_count == 4
    finally:
        conn.close()


def test_cooldown_expiry_resumes_requests(tmp_path):
    """冷却到期后自动恢复请求；成功响应清除冷却状态。"""
    responses = [FakeResponse("", 403), FakeResponse("", 403),
                 FakeResponse(jsonp(make_payload()))]
    client, conn, _session, _sleeps = make_client(tmp_path, responses, cooldown_seconds=0.05)
    try:
        assert client.get_battle_stats(ARENA_ID, "kingbob_",
                                       battle_time=recent_battle_time(1)) is None
        assert client.is_in_cooldown() is True
        time.sleep(0.08)
        assert client.is_in_cooldown() is False
        stats = client.get_battle_stats(ARENA_ID + 1, "kingbob_",
                                        battle_time=recent_battle_time(1))
        assert stats is not None and stats.status == models.STATS_STATUS_OK
        assert client.request_count == 3
        assert box_client.circuit_breaker_state()["in_cooldown"] is False
    finally:
        conn.close()


def test_cooldown_logs_chinese_warning(tmp_path, caplog):
    """进入冷却时打中文日志（含「冷却」字样），便于排障。"""
    import logging as logging_module

    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse("", 429)])
    try:
        with caplog.at_level(logging_module.WARNING, logger="src.stats.box_client"):
            client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=recent_battle_time(1))
        assert any("冷却" in record.getMessage() for record in caplog.records)
        assert client.is_in_cooldown() is True
    finally:
        conn.close()


# ---------------------------------------------------------------- 真实 raw HTML 字段映射回归
BOX_RAW_DIR = PROJECT_ROOT.parent / "research" / "wot-ban" / "box" / "raw"


@pytest.mark.skipif(not (BOX_RAW_DIR / "page_index_kingbob_.html").is_file(),
                    reason="research 盒子原始 HTML 样本不存在")
def test_parse_index_html_real_raw_fixture(tmp_path):
    """用调研原始 HTML 回归 index 字段映射（期望值取自 probe_box.parse_index 同源结果）。"""
    html = (BOX_RAW_DIR / "page_index_kingbob_.html").read_text(encoding="utf-8")
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse(html)])
    try:
        stats = client.parse_index_html(html)
        assert stats is not None
        assert stats.account_id == 7048141181
        assert stats.combat_power == 1376
        assert stats.win_rate == 56
        assert stats.avg_damage == 2614
        assert stats.battles_hint == 1500
        assert stats.update_time == "2026-11-15 06:35"
        assert stats.source == "box_index"
    finally:
        conn.close()


def test_user_facing_messages_have_no_local_drive_paths():
    """公开版用户提示不得出现本机绝对路径（QA D3）：依赖提示用通用 python 表述。"""
    source = (PROJECT_ROOT / "src" / "stats" / "box_client.py").read_text(encoding="utf-8")
    assert not re.search(r"[A-Za-z]:\\", source), "box_client.py 含本机盘符绝对路径"
    assert "G:\\" not in source and "F:\\" not in source
    assert "python -m pip install requests" in source
    assert r"runtime\\python.exe" in source  # 相对说法可保留


# ---------------------------------------------------------------- battleLog（partial 局）
PARTIAL_ARENA_ID = "10974034077980049"
PARTIAL_TANK_SLUG = "japan-j20_type_2605"
REPLAY_SAMPLES_DIR = PROJECT_ROOT.parent / "research" / "wot-ban" / "replay" / "samples"


def battlelog_time_text(iso_time):
    """ISO 时间 → battleLog 时间文案（``MM-DD HH:MM``，与盒子页面一致）。"""
    return datetime.fromisoformat(str(iso_time)).strftime("%m-%d %H:%M")


def battlelog_item(arena_id, arena_win, tank_code, time_text, state_text="平局 14v6",
                   combat=868, tank_title="五式重战"):
    """构造一条 battleLog 条目 HTML（结构与 research 原始页一致）。"""
    return (
        '<li class="log-list" arena-id="%s" arena-win=%s >'
        '<div class="list-tank">'
        '<img class="list-img" src="//news.ouj.com/wot/s/external/img.dwstatic.com/pic/app/%s.jpg?v=20261016" alt="%s">'
        '<div title="%s" class="list-tank-title">%s</div>'
        '</div>'
        '<div class="cont list-cont-fix">'
        '<span class="state even">%s</span>'
        '<span class="game">标准<em style="color:#666666;margin-left: 5px;">%s</em></span>'
        '<span class="list-cont-time">%s</span>'
        '</div></li>'
    ) % (arena_id, arena_win, tank_code, tank_title, tank_title, tank_title,
         state_text, combat, time_text)


def battlelog_page(items):
    """构造一页 battleLog HTML（含分页链接，与真实页结构同源）。"""
    return (
        '<html><body><div id="battle-log" class="log-tab battle-log">'
        '<div class="log-tab-trigger"><ul class="J_nav">%s</ul>'
        '<div class="mod-page"><a href="/wotbox/index.php?r=default/battleLog&pn=x&p=1">1</a>'
        '<a href="/wotbox/index.php?r=default/battleLog&pn=x&p=2">2</a></div>'
        '</div></div></body></html>'
    ) % "".join(items)


def make_partial_detail(tmp_path, battle_time=None, tank_code="japan:J20_Type_2605",
                        recorder_name="kingbob_",
                        file_name="20261115_0108_japan-J20_Type_2605_99_poland.wotreplay"):
    """构造 partial 录像解析结果（无 results 块：无 arena_unique_id / winner_team）。"""
    battle_time = battle_time or (
        datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    players = [
        models.ReplayPlayer(nickname="队友甲", team=1, tank="uk:GB86_Centurion_Action_X"),
        models.ReplayPlayer(nickname=recorder_name, team=2, tank=tank_code),
    ]
    return models.ReplayDetail(
        file_path=str(tmp_path / file_name), file_name=file_name,
        battle_time=battle_time, map_name="99_poland", block_count=1,
        recorder_dbid=None, recorder_name=recorder_name,
        parse_status=models.PARSE_STATUS_PARTIAL, players=players,
    )


def test_parse_battlelog_page_entries(tmp_path):
    """battleLog 页解析：arena-id / arena-win / 坦克 slug / 时间 / 结果+比分 / 战力。"""
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse("")])
    try:
        time_text = battlelog_time_text(recent_battle_time(1))
        html = battlelog_page([battlelog_item(PARTIAL_ARENA_ID, 2, PARTIAL_TANK_SLUG, time_text)])
        entries = client.parse_battlelog_page(html)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["arena_id"] == PARTIAL_ARENA_ID
        assert entry["arena_win"] == 2
        assert entry["tank_code"] == PARTIAL_TANK_SLUG
        assert entry["tank_title"] == "五式重战"
        assert entry["time_text"] == time_text
        assert entry["state_text"] == "平局 14v6"
        assert entry["score"] == "14v6"
        assert entry["combat"] == 868
        assert client.parse_battlelog_page("") == []
        assert client.parse_battlelog_page("<html>空页</html>") == []
    finally:
        conn.close()


def test_battlelog_candidates_data_source_contract(tmp_path):
    """可替换数据源 battlelog_candidates 返回统一字段（arena_id/arena_win/score_label/…）。"""
    battle_time = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    detail = make_partial_detail(tmp_path, battle_time=battle_time)
    time_text = battlelog_time_text(battle_time)
    html = battlelog_page([battlelog_item(PARTIAL_ARENA_ID, 2, PARTIAL_TANK_SLUG, time_text)])
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse(html)])
    try:
        candidates = client.battlelog_candidates(detail)
        assert len(candidates) == 1
        candidate = candidates[0]
        for key in ("arena_id", "arena_win", "score_label", "battle_time", "tank"):
            assert key in candidate, "候选缺少统一字段: %s" % key
        assert candidate["arena_id"] == PARTIAL_ARENA_ID
        assert candidate["arena_win"] == 2
        assert candidate["score_label"] == "平局 14v6"
        assert candidate["battle_time"] == time_text
        assert candidate["tank"] == PARTIAL_TANK_SLUG
        assert client.request_count == 1
    finally:
        conn.close()


def test_battlelog_match_case_insensitive_and_tank_mismatch(tmp_path):
    """匹配口径：坦克 slug 大小写不敏感；同时间但坦克不符不匹配（且不再翻页）。"""
    battle_time = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    detail = make_partial_detail(tmp_path, battle_time=battle_time)
    time_text = battlelog_time_text(battle_time)
    upper_html = battlelog_page([
        battlelog_item(PARTIAL_ARENA_ID, 2, "Japan-J20_Type_2605", time_text)])
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse(upper_html)])
    try:
        candidates = client.battlelog_candidates(detail)
        assert len(candidates) == 1
        assert candidates[0]["arena_id"] == PARTIAL_ARENA_ID
    finally:
        conn.close()
    other_tank_html = battlelog_page([
        battlelog_item("1234567890123456", 1, "ussr-R45_IS_7", time_text)])
    client2, conn2, session2, _sleeps2 = make_client(tmp_path, [FakeResponse(other_tank_html)])
    try:
        assert client2.find_partial_arena(detail) is None
        assert client2.request_count == 1          # 目标已落在本页时间范围 → 不翻后续页
        assert len(session2.urls) == 1
        assert conn2.execute("SELECT COUNT(*) AS n FROM replay_arena_map").fetchone()["n"] == 0
    finally:
        conn2.close()


def test_battlelog_pages_until_match_and_persist(tmp_path):
    """battleLog 分页：目标在 p=2 时翻页匹配；命中后映射持久化、二次打开零请求。"""
    battle_time = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    detail = make_partial_detail(tmp_path, battle_time=battle_time)
    time_text = battlelog_time_text(battle_time)
    target_dt = datetime.fromisoformat(battle_time)
    newer_a = battlelog_time_text((target_dt + timedelta(minutes=40)).isoformat())
    newer_b = battlelog_time_text((target_dt + timedelta(minutes=35)).isoformat())
    newer_c = battlelog_time_text((target_dt + timedelta(minutes=30)).isoformat())
    newer_d = battlelog_time_text((target_dt + timedelta(minutes=25)).isoformat())
    newer_e = battlelog_time_text((target_dt + timedelta(minutes=20)).isoformat())
    older_a = battlelog_time_text((target_dt - timedelta(minutes=5)).isoformat())
    older_b = battlelog_time_text((target_dt - timedelta(minutes=10)).isoformat())
    older_c = battlelog_time_text((target_dt - timedelta(minutes=15)).isoformat())
    older_d = battlelog_time_text((target_dt - timedelta(minutes=20)).isoformat())
    page1 = battlelog_page([
        battlelog_item("1111111111111111", 1, PARTIAL_TANK_SLUG, newer_a),
        battlelog_item("1111111111111112", 1, PARTIAL_TANK_SLUG, newer_b),
        battlelog_item("1111111111111113", 1, PARTIAL_TANK_SLUG, newer_c),
        battlelog_item("1111111111111114", 1, PARTIAL_TANK_SLUG, newer_d),
        battlelog_item("1111111111111115", 1, PARTIAL_TANK_SLUG, newer_e),
    ])
    page2 = battlelog_page([
        battlelog_item(PARTIAL_ARENA_ID, 2, PARTIAL_TANK_SLUG, time_text),
        battlelog_item("2222222222222222", 0, PARTIAL_TANK_SLUG, older_a),
        battlelog_item("2222222222222223", 0, PARTIAL_TANK_SLUG, older_b),
        battlelog_item("2222222222222224", 0, PARTIAL_TANK_SLUG, older_c),
        battlelog_item("2222222222222225", 0, PARTIAL_TANK_SLUG, older_d),
    ])
    client, conn, session, _sleeps = make_client(
        tmp_path, [FakeResponse(page1), FakeResponse(page2)])
    try:
        mapping = client.find_partial_arena(detail)
        assert mapping is not None
        assert mapping["arena_id"] == PARTIAL_ARENA_ID
        assert mapping["arena_win"] == 2
        assert mapping["score_label"] == "平局 14v6"
        assert client.request_count == 2
        assert len(session.urls) == 2
        assert "p=1" in session.urls[0] and "p=2" in session.urls[1]
        row = conn.execute(
            "SELECT arena_id, arena_win FROM replay_arena_map WHERE replay_path = ?",
            (detail.file_path,)).fetchone()
        assert row["arena_id"] == PARTIAL_ARENA_ID
        assert row["arena_win"] == 2
        # 二次打开：映射命中，零请求
        again = client.find_partial_arena(detail)
        assert again["arena_id"] == PARTIAL_ARENA_ID
        assert client.request_count == 2
    finally:
        conn.close()


def test_partial_chain_success_then_zero_network(tmp_path):
    """partial 全链路：battleLog 匹配 → battledetail → 新字段/胜负平/比分；二次打开零网络。"""
    battle_time = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    detail = make_partial_detail(tmp_path, battle_time=battle_time)
    time_text = battlelog_time_text(battle_time)
    page = battlelog_page([battlelog_item(PARTIAL_ARENA_ID, 2, PARTIAL_TANK_SLUG, time_text)])
    payload = make_payload(team_size=15, team_a_kills=14, team_b_kills=6)
    client, conn, session, _sleeps = make_client(
        tmp_path, [FakeResponse(page), FakeResponse(jsonp(payload))])
    try:
        stats = client.get_partial_battle_stats(detail)
        assert stats is not None
        assert stats.status == models.STATS_STATUS_OK
        assert len(stats.players) == 30
        assert stats.outcome == models.OUTCOME_DRAW
        assert stats.outcome_label == "平局"
        assert stats.score == "14v6"
        assert stats.arena_source == models.ARENA_SOURCE_BOX_BATTLELOG
        assert client.request_count == 2
        assert len(session.urls) == 2
        # 新字段解析自 battledetail
        assert stats.players[0].hits_received == 10
        assert stats.players[0].potential_damage == 4000
        assert stats.players[0].assisted_damage == 940
        assert conn.execute("SELECT COUNT(*) AS n FROM replay_arena_map").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM battle_stats WHERE arena_id = ?",
                            (PARTIAL_ARENA_ID,)).fetchone()["n"] == 1
        # 二次打开：映射 + battle_stats 双缓存命中 → 零新增请求
        before = client.request_count
        second = client.get_partial_battle_stats(detail)
        assert second is not None and second.score == "14v6"
        assert second.outcome_label == "平局"
        assert client.request_count == before
    finally:
        conn.close()


def test_partial_chain_180_day_guard_zero_request(tmp_path):
    """partial 超 180 天：零请求直接返回 None（guard 硬约束）。"""
    old_time = (datetime.now().astimezone() - timedelta(days=400)).isoformat(timespec="seconds")
    detail = make_partial_detail(tmp_path, battle_time=old_time)
    client, conn, session, _sleeps = make_client(tmp_path, [FakeResponse("SHOULD-NOT-REQUEST")])
    try:
        assert client.get_partial_battle_stats(detail) is None
        assert client.request_count == 0
        assert session.urls == []
        assert "window" in (client.last_error or "")
        assert conn.execute("SELECT COUNT(*) AS n FROM replay_arena_map").fetchone()["n"] == 0
    finally:
        conn.close()


def test_partial_chain_battlelog_failure_fail_fast(tmp_path):
    """battleLog 请求失败：单次 GET 不重试、不翻页，直接降级 None。"""
    battle_time = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    detail = make_partial_detail(tmp_path, battle_time=battle_time)
    client, conn, session, _sleeps = make_client(tmp_path, [FakeResponse("", 500)])
    try:
        assert client.get_partial_battle_stats(detail) is None
        assert client.request_count == 1
        assert len(session.urls) == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM replay_arena_map").fetchone()["n"] == 0
    finally:
        conn.close()


def test_partial_chain_detail_failure_keeps_mapping(tmp_path):
    """battleLog 匹配成功但 battledetail 失败：保留映射（下次只需重试详情请求）。"""
    battle_time = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    detail = make_partial_detail(tmp_path, battle_time=battle_time)
    time_text = battlelog_time_text(battle_time)
    page = battlelog_page([battlelog_item(PARTIAL_ARENA_ID, 2, PARTIAL_TANK_SLUG, time_text)])
    client, conn, _session, _sleeps = make_client(
        tmp_path, [FakeResponse(page), FakeResponse("", 500)])
    try:
        assert client.get_partial_battle_stats(detail) is None
        assert client.request_count == 3           # 1 页 battleLog + battledetail 重试 1 次
        assert conn.execute("SELECT COUNT(*) AS n FROM replay_arena_map").fetchone()["n"] == 1
        before = client.request_count
        assert client.find_partial_arena(detail) is not None
        assert client.request_count == before      # 映射命中不再请求 battleLog
    finally:
        conn.close()


def test_partial_chain_missing_recorder_tank(tmp_path):
    """partial 名册缺录像者坦克：不猜、不发请求，返回 None。"""
    battle_time = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    detail = make_partial_detail(tmp_path, battle_time=battle_time)
    for player in detail.players:
        player.tank = None
    client, conn, session, _sleeps = make_client(tmp_path, [FakeResponse("SHOULD-NOT-REQUEST")])
    try:
        assert client.get_partial_battle_stats(detail) is None
        assert client.request_count == 0
        assert session.urls == []
    finally:
        conn.close()


# ---------------------------------------------------------------- battleLog 锚点/封顶页
def build_paged_battlelog(base_dt, total_pages, targets=None, page_span_minutes=10,
                          per_page=5, target_index=2):
    """构造按页时间递减的 battleLog 页面字典（每页 5 条）。

    第 p 页第 i 条时间 = ``base_dt - (p-1)*page_span_minutes - i 分钟``；
    ``targets``：``{页号: (arena_id, arena_win)}``，命中目标固定放在 target_index 条。
    """
    targets = targets or {}
    pages = {}
    for page in range(1, total_pages + 1):
        items = []
        for index in range(per_page):
            entry_dt = base_dt - timedelta(
                minutes=(page - 1) * page_span_minutes + index)
            arena_id = "%016d" % (page * 100 + index)
            arena_win = 1
            if page in targets and index == target_index:
                arena_id, arena_win = targets[page]
            items.append(battlelog_item(
                arena_id, arena_win, PARTIAL_TANK_SLUG,
                battlelog_time_text(entry_dt.isoformat())))
        pages[page] = battlelog_page(items)
    return pages


def target_time_for_page(base_dt, page, index=2, page_span_minutes=10):
    """某页命中目标条目的 battle_time。"""
    return base_dt - timedelta(minutes=(page - 1) * page_span_minutes + index)


def test_battlelog_anchor_estimate_not_linear_scan(tmp_path):
    """深翻页：锚点估算页号（不线性扫），请求数 < 目标页号；锚点跨局复用。"""
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    base = now - timedelta(minutes=5)
    targets = {5: (PARTIAL_ARENA_ID, 2), 8: (PARTIAL_ARENA_ID, 2)}
    pages = build_paged_battlelog(base, total_pages=12, targets=targets)
    detail = make_partial_detail(
        tmp_path, battle_time=target_time_for_page(base, 5).isoformat(timespec="seconds"))
    session = FakePagedSession(pages)
    client, conn, _session, _sleeps = make_client(tmp_path, session)
    try:
        mapping = client.find_partial_arena(detail)
        assert mapping is not None
        assert mapping["arena_id"] == PARTIAL_ARENA_ID
        assert mapping["arena_win"] == 2
        # 非线扫：请求数 < 目标页号 5，且不超过预算
        assert client.request_count < 5
        assert client.request_count <= client.config.box_battlelog_budget
        assert client.battlelog_pages_fetched == client.request_count
        # 锚点已持久化（跨进程复用）
        anchors = db.get_battlelog_anchors(conn, "kingbob_")
        assert len(anchors) >= 2
        # 二次打开同局：映射命中，零请求
        before = client.request_count
        assert client.find_partial_arena(detail) is not None
        assert client.request_count == before
        # 更旧的 partial 局（page 8）：复用锚点估算，请求数很少
        detail8 = make_partial_detail(
            tmp_path, battle_time=target_time_for_page(base, 8).isoformat(timespec="seconds"),
            file_name="older.wotreplay")
        mapping8 = client.find_partial_arena(detail8)
        assert mapping8 is not None and mapping8["arena_id"] == PARTIAL_ARENA_ID
        assert client.request_count - before <= 3
    finally:
        conn.close()


def test_battlelog_estimate_with_real_reference_anchors(tmp_path):
    """用 Lead 实测锚点（p=1/200/205/210/215）回归页号估算：插值/有界外推/单锚点粗估。"""
    tz = datetime.fromisoformat("2026-11-15T01:08:00+08:00").tzinfo

    def anchor(page, newest, oldest):
        return {
            "page": page,
            "newest": datetime.fromisoformat(newest),
            "oldest": datetime.fromisoformat(oldest),
            "count": 5,
        }

    anchors = [
        anchor(1, "2026-11-15T01:08:00+08:00", "2026-11-15T00:17:00+08:00"),
        anchor(200, "2026-08-25T03:21:00+08:00", "2026-08-25T02:40:00+08:00"),
        anchor(205, "2026-08-21T00:57:00+08:00", "2026-08-21T00:10:00+08:00"),
        anchor(210, "2026-08-12T23:47:00+08:00", "2026-01-22T00:21:00+08:00"),
        anchor(215, "2026-01-21T00:32:00+08:00", "2026-01-21T00:01:00+08:00"),
    ]
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse("")])
    try:
        def estimate(iso_time, anchor_list=anchors):
            return client._estimate_battlelog_page(
                datetime.fromisoformat(iso_time).astimezone(tz), anchor_list, 1000)

        assert estimate("2026-11-15T01:00:00+08:00") == 1          # 比 p1 最新还新
        assert estimate("2026-11-15T00:30:00+08:00") == 1          # 落在 p1 范围
        assert estimate("2026-08-25T03:00:00+08:00") == 200        # 落在 p200 范围
        between = estimate("2026-08-23T00:00:00+08:00")            # p200~p205 之间
        assert 200 <= between <= 205
        assert estimate("2026-08-21T00:50:00+08:00") == 205
        assert estimate("2026-01-22T00:21:00+08:00") == 210        # 跨季跳变页下界
        assert estimate("2026-01-21T00:20:00+08:00") == 215
        assert estimate("2025-12-01T00:00:00+08:00") > 215         # 比最旧还旧 → 外推
        # 单锚点首次探测：按约 2 页/天粗估（10-03 14:35 距 11-15 01:08 ≈ 42.4 天 → 85）
        assert estimate("2026-10-03T14:35:00+08:00", [anchors[0]]) == 85
        # 有界局部外推：最后两锚点 p85/p87 时间很密，单步最多前进 8 页（此处 +8）
        dense = [
            anchor(1, "2026-11-15T01:08:00+08:00", "2026-11-15T00:17:00+08:00"),
            anchor(85, "2026-10-04T15:00:00+08:00", "2026-10-04T14:30:00+08:00"),
            anchor(87, "2026-10-04T13:54:00+08:00", "2026-10-04T13:19:00+08:00"),
        ]
        assert estimate("2026-10-03T14:35:00+08:00", dense) == 95
        # 远锚点（p168 在 19 天外）不参与插值 → 从较新锚点局部外推（最多 +8 页）
        far_gap = [
            anchor(1, "2026-11-15T01:08:00+08:00", "2026-11-15T00:17:00+08:00"),
            anchor(91, "2026-10-03T19:05:00+08:00", "2026-10-03T18:39:00+08:00"),
            anchor(92, "2026-10-03T18:36:00+08:00", "2026-10-03T18:15:00+08:00"),
            anchor(168, "2026-09-14T06:22:00+08:00", "2026-09-14T05:50:00+08:00"),
        ]
        assert estimate("2026-10-03T14:35:00+08:00", far_gap) == 100
        # 已探测页重复命中估算时，邻页方向按目标时间决定（页内容更新→目标更旧→页号更大）
        target_dt = datetime.fromisoformat("2026-10-03T14:35:00+08:00").astimezone(tz)
        newer_entries = [{"time_text": "10-04 15:00", "arena_id": "1", "tank_code": "x"}]
        older_entries = [{"time_text": "10-03 10:00", "arena_id": "2", "tank_code": "x"}]
        assert client._next_unprobed_neighbor(
            101, {101: newer_entries}, 1000, target=target_dt) == 102
        assert client._next_unprobed_neighbor(
            101, {101: older_entries}, 1000, target=target_dt) == 100
    finally:
        conn.close()


def test_battlelog_budget_limits_requests(tmp_path):
    """请求预算：budget=1 时只探测首页，不会线性翻页。"""
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    base = now - timedelta(minutes=5)
    pages = build_paged_battlelog(base, total_pages=12,
                                  targets={5: (PARTIAL_ARENA_ID, 2)})
    detail = make_partial_detail(
        tmp_path, battle_time=target_time_for_page(base, 5).isoformat(timespec="seconds"))
    client, conn, _session, _sleeps = make_client(tmp_path, FakePagedSession(pages))
    client.config.box_battlelog_budget = 1
    try:
        assert client.find_partial_arena(detail) is None
        assert client.request_count == 1
    finally:
        conn.close()


def test_battlelog_cap_duplicate_detection_and_window(tmp_path):
    """封顶重复页：同账号重复数据 → 钉住 cap_page；超覆盖窗口后 0 后续请求。"""
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    cap_content_oldest = now - timedelta(days=100)
    pages = {}
    for page in range(1, 5):
        entries = []
        for index in range(5):
            entry_dt = now - timedelta(days=(page - 1) * 20, minutes=index)
            entries.append(battlelog_item("%016d" % (page * 100 + index), 1,
                                          PARTIAL_TANK_SLUG,
                                          battlelog_time_text(entry_dt.isoformat())))
        pages[page] = battlelog_page(entries)
    cap_entries = [
        battlelog_item("55555555555555%02d" % index, 1, PARTIAL_TANK_SLUG,
                       battlelog_time_text(
                           (cap_content_oldest + timedelta(minutes=4 - index)).isoformat()))
        for index in range(5)
    ]
    cap_html = battlelog_page(cap_entries)
    for page in range(5, 401):
        pages[page] = cap_html
    # 目标 150 天前：仍在 180 天 guard 内，但早于该账号覆盖下界（100 天）
    target_dt = now - timedelta(days=150)
    detail = make_partial_detail(
        tmp_path, battle_time=target_dt.isoformat(timespec="seconds"))
    client, conn, _session, _sleeps = make_client(tmp_path, FakePagedSession(pages))
    try:
        assert client.get_partial_battle_stats(detail) is None
        assert "超出盒子保留窗口" in (client.last_error or "")
        assert client.request_count <= client.config.box_battlelog_budget
        cap = db.get_battlelog_cap(conn, "kingbob_")
        assert cap is not None and cap["cap_page"] is not None
        assert cap["oldest_time"] is not None
        # 二次打开：cap 已知且目标早于覆盖下界 → 零请求直接判不可用
        before = client.request_count
        assert client.find_partial_arena(detail) is None
        assert client.request_count == before
        assert "超出盒子保留窗口" in (client.last_error or "")
    finally:
        conn.close()


def test_battlelog_configured_cap_page_limits_search(tmp_path):
    """配置已知 cap_page 时：搜索范围限定在 cap 内，超窗口只探测 2 页即停。"""
    now = datetime.now().astimezone().replace(second=0, microsecond=0)
    cap_content_oldest = now - timedelta(days=100)
    pages = {}
    for page in range(1, 3):
        entries = []
        for index in range(5):
            entry_dt = now - timedelta(days=(page - 1) * 20, minutes=index)
            entries.append(battlelog_item("%016d" % (page * 100 + index), 1,
                                          PARTIAL_TANK_SLUG,
                                          battlelog_time_text(entry_dt.isoformat())))
        pages[page] = battlelog_page(entries)
    cap_entries = [
        battlelog_item("66666666666666%02d" % index, 1, PARTIAL_TANK_SLUG,
                       battlelog_time_text(
                           (cap_content_oldest + timedelta(minutes=4 - index)).isoformat()))
        for index in range(5)
    ]
    cap_html = battlelog_page(cap_entries)
    for page in range(3, 401):
        pages[page] = cap_html
    target_dt = now - timedelta(days=150)
    detail = make_partial_detail(
        tmp_path, battle_time=target_dt.isoformat(timespec="seconds"))
    client, conn, _session, _sleeps = make_client(tmp_path, FakePagedSession(pages))
    client.config.box_battlelog_cap_page = 3
    try:
        assert client.find_partial_arena(detail) is None
        assert "超出盒子保留窗口" in (client.last_error or "")
        assert client.request_count == 2       # 首页 + cap_page，不探测 cap 之外
        before = client.request_count
        assert client.find_partial_arena(detail) is None
        assert client.request_count == before  # cap 覆盖下界已缓存 → 零请求
    finally:
        conn.close()


def test_battlelog_403_stops_and_enters_cooldown(tmp_path):
    """battleLog 遇 403：立即停（不重试/不翻页）；连续 2 次 403 → 熔断冷却期零请求。"""
    battle_time = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    detail = make_partial_detail(tmp_path, battle_time=battle_time)
    client, conn, session, _sleeps = make_client(tmp_path, [FakeResponse("", 403)])
    try:
        assert client.get_partial_battle_stats(detail) is None
        assert client.request_count == 1        # 单次 GET，403 即停
        assert len(session.urls) == 1
        assert client.is_in_cooldown() is False  # 单次失败未达熔断阈值
        # 第二次打开再次 403 → 连续失败 2 次 → 进入冷却
        assert client.get_partial_battle_stats(detail) is None
        assert client.request_count == 2
        assert client.is_in_cooldown() is True
        before = client.request_count
        assert client.get_partial_battle_stats(detail) is None
        assert client.request_count == before    # 冷却期零请求
        assert "cooldown" in (client.last_error or "")
        assert conn.execute("SELECT COUNT(*) AS n FROM replay_arena_map").fetchone()["n"] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------- 缓存语义
def test_battle_stats_permanent_cache_never_refetch(tmp_path):
    """对局战绩成功后即最终版：缓存再旧也不二次抓取（0 请求）。"""
    client, conn, session, _sleeps = make_client(
        tmp_path, [FakeResponse(jsonp(make_payload()))])
    try:
        first = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=recent_battle_time(1))
        assert first is not None and first.status == models.STATS_STATUS_OK
        assert client.request_count == 1
        old_fetch = (datetime.now().astimezone() - timedelta(days=400)).isoformat(timespec="seconds")
        conn.execute("UPDATE battle_stats SET fetched_at = ?", (old_fetch,))
        conn.commit()
        second = client.get_battle_stats(ARENA_ID, "kingbob_", battle_time=recent_battle_time(1))
        assert second is not None and second.status == models.STATS_STATUS_OK
        assert client.request_count == 1
        assert len(session.urls) == 1
        assert second.score == "14v6"
        assert second.players[0].hits_received == 10
    finally:
        conn.close()


def test_peek_player_stats_ignores_ttl_and_zero_request(tmp_path):
    """peek 只读缓存：跨天过期仍返回（带 fetched_at）、零请求、无缓存返回 None。"""
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse(INDEX_HTML)])
    try:
        assert client.peek_player_stats(7048141181) is None      # 无缓存
        client.get_player_stats(account_id=7048141181)
        assert client.request_count == 1
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
        conn.execute("UPDATE player_stats SET fetched_at = ?", (yesterday,))
        conn.commit()
        # 跨天过期：get_player_stats 视为未命中（会请求），peek 仍只读返回
        peeked = client.peek_player_stats(7048141181)
        assert peeked is not None
        assert peeked.combat_power == 1376
        assert peeked.fetched_at == yesterday
        assert client.request_count == 1        # peek 零请求
        assert client.peek_player_stats(999999999) is None
        assert client.peek_player_stats(None) is None
    finally:
        conn.close()


def test_service_peek_player_stats_cache_zero_network(tmp_path):
    """门面 peek_player_stats_cache：无缓存 None；有缓存返回带缓存时间提示，零网络。"""
    service = _make_real_service(tmp_path)
    matched = models.MatchedPlayer(
        nickname="kingbob_", dbid=7048141181, match_type=models.MATCH_TYPE_ID)
    try:
        assert service.peek_player_stats_cache(None) is None
        assert service.peek_player_stats_cache(matched) is None      # 无缓存
        conn = service._thread_conn()
        old = (datetime.now().astimezone() - timedelta(days=2)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO player_stats(account_id, source, fetched_at, update_time, "
            "combat_power, win_rate, avg_damage, battles_hint) VALUES(?, 'box_index', ?, "
            "'2026-11-15 06:35', 1376, 56, 2614, 1500)", (7048141181, old))
        conn.commit()
        result = service.peek_player_stats_cache(matched)
        assert result is not None and result.available is True
        assert result.stats.combat_power == 1376
        assert "盒子缓存于" in result.message
        assert "刷新综合战绩" in result.message
    finally:
        service.dispose()


class FakePlayerStatsClient:
    """假盒子客户端：记录 get_player_stats 调用参数并返回预设 PlayerStats（零网络）。"""

    def __init__(self, result=None):
        self.result = result
        self.calls = []
        self.request_count = 0

    def get_player_stats(self, account_id=None, nickname=None, force_refresh=False):
        self.calls.append({"account_id": account_id, "nickname": nickname,
                           "force_refresh": force_refresh})
        self.request_count += 1
        return self.result


def test_service_get_player_stats_unmatched_player_with_dbid(tmp_path):
    """未命中封禁名单的玩家（有 dbid）也可点查综合战绩（不再 is_hit 硬拦截）。"""
    service = _make_real_service(tmp_path)
    matched = models.MatchedPlayer(nickname="路人甲", dbid=7048141181,
                                   match_type=models.MATCH_TYPE_NONE)
    stats = models.PlayerStats(account_id=7048141181, source="box_index",
                               combat_power=1376, win_rate=56)
    fake = FakePlayerStatsClient(result=stats)
    service._make_box_client = lambda module, kwargs: fake
    try:
        result = service.get_player_stats(matched)
        assert result.available is True
        assert result.stats.combat_power == 1376
        assert fake.calls == [{"account_id": 7048141181, "nickname": "路人甲",
                               "force_refresh": False}]
    finally:
        service.dispose()


def test_service_get_player_stats_partial_by_nickname(tmp_path):
    """partial 局无 dbid：按昵称（pn）查询综合战绩。"""
    service = _make_real_service(tmp_path)
    matched = models.MatchedPlayer(nickname="队友甲", dbid=None, partial=True,
                                   match_type=models.MATCH_TYPE_NICKNAME)
    stats = models.PlayerStats(account_id=999999, source="box_index", combat_power=900)
    fake = FakePlayerStatsClient(result=stats)
    service._make_box_client = lambda module, kwargs: fake
    try:
        result = service.get_player_stats(matched)
        assert result.available is True
        assert fake.calls[0]["account_id"] is None
        assert fake.calls[0]["nickname"] == "队友甲"
        assert "按昵称查询" in result.message
    finally:
        service.dispose()


def test_service_get_player_stats_missing_identity(tmp_path):
    """无 dbid 且无昵称：返回中文错误，且不构造客户端、不发请求。"""
    service = _make_real_service(tmp_path)
    matched = models.MatchedPlayer(nickname="", dbid=None, match_type=models.MATCH_TYPE_NONE)
    called = {"value": False}

    def fake_make(module, kwargs):
        called["value"] = True
        raise AssertionError("不应构造盒子客户端")

    service._make_box_client = fake_make
    try:
        result = service.get_player_stats(matched)
        assert result.available is False
        assert "缺少账号信息" in result.message
        assert called["value"] is False
    finally:
        service.dispose()


def test_service_get_player_stats_mock_allows_unmatched(tmp_path):
    """mock 分支同步放开：未命中玩家 / partial 仅昵称都可返回演示战绩。"""
    from src.app.service import AppService

    cfg = Config(project_root=tmp_path, db_path=Path(tmp_path) / "mock.db")
    service = AppService(cfg=cfg, mock=True)
    try:
        unmatched = models.MatchedPlayer(nickname="路人甲", dbid=7048141181,
                                         match_type=models.MATCH_TYPE_NONE)
        assert service.get_player_stats(unmatched).available is True
        partial = models.MatchedPlayer(nickname="队友甲", dbid=None, partial=True)
        assert service.get_player_stats(partial).available is True
        missing = models.MatchedPlayer(nickname="", dbid=None)
        missing_result = service.get_player_stats(missing)
        assert missing_result.available is False
        assert "缺少账号信息" in missing_result.message
    finally:
        service.dispose()


def test_service_peek_player_stats_cache_unmatched_player(tmp_path):
    """未命中玩家只要有 dbid 且有缓存，peek 也能返回（零网络）。"""
    service = _make_real_service(tmp_path)
    matched = models.MatchedPlayer(nickname="路人甲", dbid=7048141181,
                                   match_type=models.MATCH_TYPE_NONE)
    try:
        assert service.peek_player_stats_cache(matched) is None
        conn = service._thread_conn()
        conn.execute(
            "INSERT INTO player_stats(account_id, source, fetched_at, update_time, "
            "combat_power, win_rate, avg_damage, battles_hint) VALUES(?, 'box_index', ?, "
            "'2026-11-15 06:35', 1376, 56, 2614, 1500)",
            (7048141181, datetime.now().astimezone().isoformat(timespec="seconds")))
        conn.commit()
        result = service.peek_player_stats_cache(matched)
        assert result is not None and result.available is True
        assert result.stats.combat_power == 1376
        assert "盒子缓存于" in result.message
    finally:
        service.dispose()


# ---------------------------------------------------------------- DB 幂等迁移
def test_db_idempotent_migration_legacy_v1(tmp_path):
    """旧库（schema_version=1）幂等迁移：补 v2 新表，既有数据一条不丢。"""
    legacy = tmp_path / "legacy-v1.db"
    raw = sqlite3.connect(str(legacy))
    try:
        raw.executescript(
            "CREATE TABLE bans (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_day TEXT NOT NULL, "
            "source_type TEXT NOT NULL DEFAULT 'csv', source_url TEXT, nickname TEXT NOT NULL, "
            "reason TEXT, duration TEXT, ban_times INTEGER NOT NULL DEFAULT 1, wgid INTEGER, "
            "raw_sheet TEXT, raw_nickname TEXT, fetched_at TEXT, credibility TEXT);"
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);"
        )
        raw.execute("INSERT INTO bans(batch_day, source_type, nickname) VALUES('2026-11-06','csv','玩家甲')")
        raw.execute("INSERT INTO bans(batch_day, source_type, nickname) VALUES('2026-10-21','csv','玩家乙')")
        raw.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
        raw.commit()
    finally:
        raw.close()

    conn = db.connect(legacy)
    try:
        assert db.SCHEMA_VERSION == 2
        assert db.get_meta(conn, db.META_SCHEMA_VERSION) == 2
        tables = db.list_tables(conn)
        for name in ("replay_arena_map", "battlelog_anchor", "battlelog_cap"):
            assert name in tables, "迁移后缺少 v2 新表: %s" % name
        assert conn.execute("SELECT COUNT(*) AS n FROM bans").fetchone()["n"] == 2
        # 重复迁移幂等：不重复建表、不丢数据
        db.init_schema(conn)
        db.init_schema(conn)
        assert conn.execute("SELECT COUNT(*) AS n FROM bans").fetchone()["n"] == 2
        assert db.get_meta(conn, db.META_SCHEMA_VERSION) == 2
        # 映射 upsert 幂等（同一录像路径只一行）
        for _ in range(2):
            db.set_replay_arena(conn, "F:/replays/a.wotreplay", PARTIAL_ARENA_ID, arena_win=2,
                                tank_code=PARTIAL_TANK_SLUG,
                                battle_time="2026-11-15T01:08:10+08:00")
        assert conn.execute("SELECT COUNT(*) AS n FROM replay_arena_map").fetchone()["n"] == 1
        mapping = db.get_replay_arena(conn, "F:/replays/a.wotreplay")
        assert mapping["arena_id"] == PARTIAL_ARENA_ID
        assert mapping["arena_win"] == 2
        assert mapping["tank_code"] == PARTIAL_TANK_SLUG
        assert db.get_replay_arena(conn, "F:/replays/不存在.wotreplay") is None
        # 锚点 / 封顶页 upsert 幂等
        for _ in range(2):
            db.upsert_battlelog_anchor(conn, "kingbob_", 5,
                                       newest_time="2026-11-15T01:20:00+08:00",
                                       oldest_time="2026-11-15T01:08:00+08:00",
                                       entry_count=5)
            db.set_battlelog_cap(conn, "kingbob_", 240,
                                 oldest_time="2026-01-21T00:32:00+08:00",
                                 newest_time="2026-01-21T00:55:00+08:00")
        anchors = db.get_battlelog_anchors(conn, "kingbob_")
        assert len(anchors) == 1 and anchors[0]["page"] == 5
        cap = db.get_battlelog_cap(conn, "kingbob_")
        assert cap["cap_page"] == 240 and cap["oldest_time"].startswith("2026-01-21")
    finally:
        conn.close()


# ---------------------------------------------------------------- 业务门面（胜负平 / partial / 地图名）
def _make_real_service(tmp_path):
    """构造真实模式门面（临时库；盒子客户端在用例里替换为假实现）。"""
    from src.app.service import AppService

    cfg = Config(project_root=tmp_path, db_path=Path(tmp_path) / "service.db")
    return AppService(cfg=cfg, mock=False)


def make_complete_detail(tmp_path, winner_team, recorder_team=1):
    """构造完整录像解析结果（30 人简化为 2 人，含录像者 dbid/昵称）。"""
    other_team = 2 if recorder_team == 1 else 1
    players = [
        models.ReplayPlayer(nickname="recorder", dbid=111, team=recorder_team),
        models.ReplayPlayer(nickname="other", dbid=222, team=other_team),
    ]
    return models.ReplayDetail(
        file_path=str(tmp_path / "full.wotreplay"), file_name="full.wotreplay",
        battle_time=(datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds"),
        map_name="02_malinovka", block_count=2, winner_team=winner_team,
        arena_unique_id=10973119249944463, recorder_dbid=111, recorder_name="recorder",
        parse_status=models.PARSE_STATUS_OK, players=players)


def test_service_complete_outcome_labels_without_box(tmp_path):
    """完整录像：盒子不可用也要用 replay 结果给胜负平（录像者队反查，不写死 team==1）。"""
    from src.app.service import BattleStatsResult

    service = _make_real_service(tmp_path)
    try:
        service._battle_stats_real = lambda detail: BattleStatsResult(
            available=False, message="stub 盒子不可用")
        cases = [
            (0, 1, "平局"),
            (1, 1, "胜利"),
            (2, 1, "失败"),
            (1, 2, "失败"),
            (2, 2, "胜利"),
        ]
        for winner_team, recorder_team, expected in cases:
            detail = make_complete_detail(tmp_path, winner_team, recorder_team)
            result = service.get_battle_stats(detail)
            assert result.available is False
            assert result.outcome_label == expected, (winner_team, recorder_team, result)
        # 录像者 dbid 缺失时按昵称反查
        detail = make_complete_detail(tmp_path, 2, recorder_team=2)
        detail.recorder_dbid = None
        assert service.get_battle_stats(detail).outcome_label == "胜利"
    finally:
        service.dispose()


def test_service_partial_chain_success_cached_and_failure(tmp_path):
    """partial 局：门面走盒子链路；成功带胜负平/比分，缓存命中标 cached，失败中文降级。"""
    service = _make_real_service(tmp_path)
    detail = make_partial_detail(tmp_path)
    stats = models.BattleStats(
        arena_id=PARTIAL_ARENA_ID, status=models.STATS_STATUS_OK,
        players=[models.BattlePlayerStats(
            dbid=7048141181, nickname="kingbob_", team=2, tank="五式重战",
            tank_code=PARTIAL_TANK_SLUG, combat=868.464, damage=2252, xp=438,
            hits_received=12, potential_damage=4520, damage_blocked=1800,
            vehicles_damaged=2, assisted_damage=941, defense_points=0, mileage=1158)],
        outcome=models.OUTCOME_DRAW, outcome_label="平局", score="14v6",
        arena_source=models.ARENA_SOURCE_BOX_BATTLELOG)

    class FakeBoxClient:
        def __init__(self, result=None, requests=2, error=None):
            self.request_count = 0
            self.last_error = error
            self._result = result
            self._requests = requests

        def get_partial_battle_stats(self, detail, force_refresh=False):
            self.request_count += self._requests
            return self._result

    try:
        service._make_box_client = lambda module, kwargs: FakeBoxClient(result=stats, requests=2)
        result = service.get_battle_stats(detail)
        assert result.available is True
        assert result.outcome_label == "平局"
        assert result.score == "14v6"
        assert result.cached is False
        assert result.stats.players[0].hits_received == 12
        # 缓存命中（零新增请求）→ cached=True
        service._make_box_client = lambda module, kwargs: FakeBoxClient(result=stats, requests=0)
        cached_result = service.get_battle_stats(detail)
        assert cached_result.available is True and cached_result.cached is True
        # 未匹配到 battleLog：中文降级提示，不抛异常
        service._make_box_client = lambda module, kwargs: FakeBoxClient(
            result=None, requests=1, error="no battleLog entry matched")
        failed = service.get_battle_stats(detail)
        assert failed.available is False
        assert "暂不可用" in failed.message and "稍后再试" in failed.message
    finally:
        service.dispose()


def test_service_map_display_fallback(monkeypatch):
    """地图代码 → 中文名：game_names 可用时替换，缺失/未命中时优雅降级保留原值。"""
    from src.app import service as service_module

    fake_names = types.SimpleNamespace(
        map_name=lambda map_code, game_dir=None: {"99_poland": "斯杜季昂奇"}.get(map_code))
    monkeypatch.setitem(sys.modules, "src.game_names", fake_names)
    info = service_module._as_replay_info({"path": "x.wotreplay", "map_name": "99_poland"})
    assert info.map_name == "斯杜季昂奇"
    info_unknown = service_module._as_replay_info({"path": "x.wotreplay", "map_name": "unknown_code"})
    assert info_unknown.map_name == "unknown_code"
    # 映射模块缺失（sys.modules 置 None 模拟 ImportError）→ 保留原值，不抛异常
    monkeypatch.setitem(sys.modules, "src.game_names", None)
    info_missing = service_module._as_replay_info({"path": "x.wotreplay", "map_name": "99_poland"})
    assert info_missing.map_name == "99_poland"


# ---------------------------------------------------------------- 真实原始响应回归（离线）
@pytest.mark.skipif(not (BOX_RAW_DIR / "detail_kingbob__10974034077980049.jsonp").is_file(),
                    reason="research 盒子 partial 局 battledetail 原始响应不存在")
def test_parse_real_partial_battledetail_fixture(tmp_path):
    """真实 partial 局 battledetail（arena 10974034077980049）新字段逐项回归。"""
    text = (BOX_RAW_DIR / "detail_kingbob__10974034077980049.jsonp").read_text(encoding="utf-8")
    payload = box_client.unwrap_jsonp(text)
    assert payload is not None
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse("")])
    try:
        players = client.parse_battle_detail(payload)
        assert len(players) == 30
        king = next(player for player in players if player.nickname == "kingbob_")
        assert king.xp == 438
        assert king.shots == 6
        assert king.piercings == 3
        assert king.damage == 2252
        assert king.hits_received == 12
        assert king.potential_damage == 4520
        assert king.damage_blocked == 1800
        assert king.spotted == 2
        assert king.vehicles_damaged == 2
        assert king.kills == 0
        assert king.assisted_damage == 941
        assert king.capture_points == 0
        assert king.defense_points == 0
        assert king.mileage == 1158
        assert round(king.combat) == 868
        assert king.tank_code == PARTIAL_TANK_SLUG
        assert box_client.battle_score(payload) == "14v6"
    finally:
        conn.close()


@pytest.mark.skipif(not (BOX_RAW_DIR / "battlelog_kingbob_.html").is_file(),
                    reason="research 盒子 battleLog 原始 HTML 不存在")
def test_parse_real_battlelog_fixture(tmp_path):
    """真实 battleLog 页解析：5 条 / arena-id / arena-win / 时间 / 结果比分。"""
    html = (BOX_RAW_DIR / "battlelog_kingbob_.html").read_text(encoding="utf-8")
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse("")])
    try:
        entries = client.parse_battlelog_page(html)
        assert len(entries) == 5
        assert entries[0]["arena_id"] == "10974034077980049"
        assert entries[0]["arena_win"] == 2
        assert entries[0]["tank_code"] == PARTIAL_TANK_SLUG
        assert entries[0]["time_text"] == "11-15 01:08"
        assert entries[0]["state_text"] == "平局 14v6"
        assert entries[0]["combat"] == 868
        assert entries[1]["arena_id"] == "2315991864109994844"
        assert entries[1]["arena_win"] == 1
        assert entries[2]["arena_id"] == "10973119249944463"
        assert entries[2]["arena_win"] == 2
    finally:
        conn.close()


@pytest.mark.skipif(
    not (REPLAY_SAMPLES_DIR / "20261115_0108_japan-J20_Type_2605_99_poland.wotreplay").is_file()
    or not (BOX_RAW_DIR / "battlelog_kingbob_.html").is_file(),
    reason="真实 partial 录像样本 / battleLog 原始 HTML 不存在")
def test_offline_partial_match_real_sample(tmp_path):
    """离线真实样本：partial 录像（99_poland）+ 真实 battleLog → arena 10974034077980049/平局。"""
    from src.replay import parser

    detail = parser.parse_replay(
        REPLAY_SAMPLES_DIR / "20261115_0108_japan-J20_Type_2605_99_poland.wotreplay")
    assert detail.is_partial is True
    assert detail.map_name == "99_poland"
    html = (BOX_RAW_DIR / "battlelog_kingbob_.html").read_text(encoding="utf-8")
    client, conn, _session, _sleeps = make_client(tmp_path, [FakeResponse(html)])
    try:
        if not client.is_within_window(detail.battle_time):
            pytest.skip("真实样本已超 180 天窗口（guard 正确行为）")
        candidates = client.battlelog_candidates(detail)
        assert len(candidates) == 1
        assert candidates[0]["arena_id"] == PARTIAL_ARENA_ID
        assert candidates[0]["arena_win"] == 2
        assert candidates[0]["score_label"] == "平局 14v6"
        assert client.request_count == 1
    finally:
        conn.close()


@pytest.mark.skipif(
    not (REPLAY_SAMPLES_DIR / "20261115_0108_japan-J20_Type_2605_99_poland.wotreplay").is_file()
    or not (BOX_RAW_DIR / "battlelog_kingbob_.html").is_file()
    or not (BOX_RAW_DIR / "detail_kingbob__10974034077980049.jsonp").is_file(),
    reason="真实 partial 录像 / battleLog / battledetail 原始证据不存在")
def test_offline_partial_full_chain_real_sample(tmp_path):
    """离线真实样本全链路：partial → battleLog 匹配 → battledetail → kingbob_ 逐项与盒子一致。"""
    from src.replay import parser

    detail = parser.parse_replay(
        REPLAY_SAMPLES_DIR / "20261115_0108_japan-J20_Type_2605_99_poland.wotreplay")
    battlelog_html = (BOX_RAW_DIR / "battlelog_kingbob_.html").read_text(encoding="utf-8")
    detail_text = (BOX_RAW_DIR / "detail_kingbob__10974034077980049.jsonp").read_text(encoding="utf-8")
    client, conn, session, _sleeps = make_client(
        tmp_path, [FakeResponse(battlelog_html), FakeResponse(detail_text)])
    try:
        if not client.is_within_window(detail.battle_time):
            pytest.skip("真实样本已超 180 天窗口（guard 正确行为）")
        stats = client.get_partial_battle_stats(detail)
        assert stats is not None
        assert stats.status == models.STATS_STATUS_OK
        assert len(stats.players) == 30
        assert stats.outcome_label == "平局"
        assert stats.score == "14v6"
        assert stats.arena_source == models.ARENA_SOURCE_BOX_BATTLELOG
        king = next(player for player in stats.players if player.nickname == "kingbob_")
        assert king.xp == 438 and king.shots == 6 and king.piercings == 3
        assert king.damage == 2252 and king.hits_received == 12
        assert king.potential_damage == 4520 and king.damage_blocked == 1800
        assert king.vehicles_damaged == 2 and king.assisted_damage == 941
        assert king.defense_points == 0 and king.mileage == 1158
        assert round(king.combat) == 868
        assert client.request_count == 2
        # 二次打开：零新增请求
        before = client.request_count
        second = client.get_partial_battle_stats(detail)
        assert second is not None and second.score == "14v6"
        assert client.request_count == before
    finally:
        conn.close()


@pytest.mark.skipif(
    not (REPLAY_SAMPLES_DIR / "20261115_0058_japan-J20_Type_2605_121_lost_paradise_v.wotreplay").is_file()
    or not (REPLAY_SAMPLES_DIR / "20261115_0042_japan-J20_Type_2605_02_malinovka.wotreplay").is_file(),
    reason="真实完整录像样本不存在")
def test_complete_replay_outcome_labels_real_samples():
    """真实完整录像：0058 winner_team=1（录像者队=1）→胜利；0042 winner_team=0→平局。"""
    from src.app import service as service_module
    from src.replay import parser

    win_detail = parser.parse_replay(
        REPLAY_SAMPLES_DIR / "20261115_0058_japan-J20_Type_2605_121_lost_paradise_v.wotreplay")
    assert win_detail.arena_unique_id == 2315991864109994844
    assert win_detail.winner_team == 1
    assert service_module.recorder_team(win_detail) == 1
    assert service_module.replay_outcome(win_detail) == ("win", "胜利")
    draw_detail = parser.parse_replay(
        REPLAY_SAMPLES_DIR / "20261115_0042_japan-J20_Type_2605_02_malinovka.wotreplay")
    assert draw_detail.arena_unique_id == 10973119249944463
    assert service_module.replay_outcome(draw_detail) == ("draw", "平局")


def main(argv):
    """独立运行入口：顺序执行关键测试（便于手工冒烟）。"""
    import tempfile

    checks = [test_unwrap_jsonp_variants, test_battle_score_from_payload,
              test_arena_win_to_outcome_label, test_180_day_guard_never_requests,
              test_retry_once_then_success, test_failure_returns_none_after_one_retry,
              test_battle_stats_cache_by_arena_id, test_player_stats_parse_and_same_day_cache]
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for check in checks:
            try:
                check(Path(tmp))
                print("[ok] %s" % check.__name__)
            except Exception as exc:  # noqa: BLE001 - 冒烟脚本需汇总全部失败
                failures += 1
                print("[fail] %s: %s" % (check.__name__, exc))
    print("[done] %d 项通过，%d 项失败" % (len(checks) - failures, failures))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
