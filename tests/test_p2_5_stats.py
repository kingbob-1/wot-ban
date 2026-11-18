# -*- coding: utf-8 -*-
"""P2.5 战绩接入测试：JSONP 去壳、180 天 guard、失败重试 1 次、缓存、限流、TTL。

全部用假 session / 注入 sleep，不发真实网络请求。
"""
import json
import re
import sys
import time
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


def make_payload(team_size=2, arena_id=ARENA_ID, code="200"):
    """构造 battledetail JSONP 内层 payload（team_a/team_b 各 team_size 人）。"""
    def side(prefix, team, base):
        players = []
        for index in range(team_size):
            players.append({
                "name": "%s%d" % (prefix, index),
                "clanAbbrev": "K-G",
                "clanDBID": 5136,
                "team": team,
                "tank_title": "Pz.55",
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
            "win_team": 1,
            "team_a": side("A", 1, 7000000001),
            "team_b": side("B", 2, 7000000101),
        } if code == "200" else "",
    }


def jsonp(payload):
    """把 payload 包成 JSONP 文本。"""
    return "cb(%s);" % json.dumps(payload, ensure_ascii=False)


def make_client(tmp_path, responses, sleep=None, **client_kwargs):
    """构造 BoxClient：内存库 + 假 session + 可注入 sleep（可覆盖熔断参数）。"""
    config = Config(project_root=tmp_path, db_path=Path(tmp_path) / "test.db")
    conn = db.connect(":memory:")
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
    """team_a/team_b 30 人结构解析：dbid/team/战力/伤害/击杀/经验/点亮/占领。"""
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
    finally:
        conn.close()


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


def test_player_stats_parse_and_cache_ttl(tmp_path):
    """index 点查解析 + TTL 缓存（12–24h 内命中，过期后重新请求）。"""
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
        # TTL 内：缓存命中，零请求
        second = client.get_player_stats(account_id=7048141181)
        assert second is not None and client.request_count == 1
        # 把 fetched_at 改成 19 小时前（> 18h TTL）→ 缓存过期，重新请求
        old = (datetime.now().astimezone() - timedelta(hours=19)).isoformat(timespec="seconds")
        conn.execute("UPDATE player_stats SET fetched_at = ?", (old,))
        conn.commit()
        third = client.get_player_stats(account_id=7048141181)
        assert third is not None
        assert client.request_count == 2
        ttl_hours = client.config.player_stats_ttl_hours
        assert 12 <= ttl_hours <= 24
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


def main(argv):
    """独立运行入口：顺序执行关键测试（便于手工冒烟）。"""
    import tempfile

    checks = [test_unwrap_jsonp_variants, test_180_day_guard_never_requests,
              test_retry_once_then_success, test_failure_returns_none_after_one_retry,
              test_battle_stats_cache_by_arena_id, test_player_stats_parse_and_cache_ttl]
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
