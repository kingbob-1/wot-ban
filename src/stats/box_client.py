# -*- coding: utf-8 -*-
"""战绩数据接入（P2.5）：偶游盒子 ``wotapp.ouj.com`` 当局战绩 + ``wotbox.ouj.com`` 个人战绩点查。

两个入口
--------
1. :meth:`BoxClient.get_battle_stats` —— 当局战绩（battledetail JSONP，按 arena_id 缓存）
   - **180 天 guard**：对局时间超出 ``Config.battle_stats_window_days``（180 天）时
     **绝不发起请求**，直接返回 ``status=expired``；
   - 失败重试 1 次（共 2 次尝试），仍失败返回 ``None``，**不抛异常**；
   - 成功后整段原始 JSON 落 ``battle_stats.raw_json``，同一局重复查看零请求。
2. :meth:`BoxClient.get_player_stats` —— 个人综合战绩（index SSR 页点查）
   - 仅点查、**≥ ``Config.request_interval_s``（2 s）/请求** 限流；
   - ``player_stats`` 缓存 TTL ``Config.player_stats_ttl_hours``（默认 18 h，12–24 h 区间）。

硬约束（计划 §6.9 / R17）
-------------------------
- **UA 必须用实测可用值**：``Config.box_headers()`` 里的 Chrome/86 系（从
  ``research\\wot-ban\\box\\probe_box.py`` 抄录）；Chrome/120 会 403，不要随意升级。
- ``battledetail`` 必须带 ``pn``（任一参战者昵称，否则 code=100 empty）。
- 不调用任何需登录接口（登录/月卡/refreshCombat）。
- 一个 ``BoxClient`` 实例请单线程使用（内部 SQLite 连接默认同线程）。

独立运行（实测取证）
--------------------
::

    python src/stats/box_client.py --arena-id 2315991864109994844 --pn kingbob_ \\
        --battle-time 2026-11-15T00:58:00 --raw-out data/evidence/raw.jsonp
    python src/stats/box_client.py --index-pid 7048141181 --out data/evidence/index.json
"""
from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

try:
    import requests
except ImportError:  # pragma: no cover - 共享 runtime 已装 requests
    print("需要 requests：..\\runtime\\python.exe -m pip install requests",
          file=sys.stderr)
    raise

try:
    from .. import models
    from ..config import Config, load_config
    from ..store import db
except ImportError:  # 直接以脚本方式运行（python src/stats/box_client.py）时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src import models
    from src.config import load_config
    from src.store import db

NOT_FOUND_MARK = "没有找到您搜索的玩家"
# 失败重试次数（硬要求：重试 1 次，即共 2 次尝试）。
DEFAULT_RETRY_TIMES = 1
# 连续失败熔断（计划 §6.9 硬要求：连续 403/异常停 5–10 分钟）。
COOLDOWN_FAILURE_THRESHOLD = 2      # 连续 ≥2 次风控状态码/网络异常即进入冷却
COOLDOWN_SECONDS = 600.0            # 默认冷却 10 分钟（5–10 分钟档内，可构造参数覆盖）
# 冷却期状态值：models 无该枚举，按「不改 models」约定在模块级补充（BattleStats.status 为 str）。
STATS_STATUS_COOLDOWN = "cooldown"
# JSONP 外壳：``cb({...});``（允许前后空白与注释）。
JSONP_RE = re.compile(r"^\s*(?:/\*.*?\*/\s*)?[A-Za-z_$][\w$.]*\s*\(\s*(\{.*\})\s*\)\s*;?\s*$", re.S)
# 个人战绩页正则（与 probe_box.parse_index 同源，只取本项目需要的字段）。
P_ID_RE = re.compile(r'id="p_id"\s+value="(\d+)"')
NAME_RE = re.compile(r'<p class="name">([^<]+)</p>')
COMBAT_POWER_RE = re.compile(r'战斗力</em>\s*<span class="num">(\d+)</span>')
WIN_RATE_RE = re.compile(r"win-rate-1k\"\s+win-rate=['\"](\d+)")
TOTAL_BATTLES_RE = re.compile(r'<p class="total">总\s*(\d+)</p>')
UPDATE_TIME_RE = re.compile(r"更新时间：([\d\-: ]+)")
AVG_DAMAGE_RE = re.compile(
    r'<li><p class="title">杀伤</p><p class="num">([\d.\-]+)</p>')
LOGGER = logging.getLogger(__name__)


class _CircuitBreaker:
    """进程级熔断状态（所有 BoxClient 实例共享，GUI 反复创建客户端也持续生效）。"""

    def __init__(self) -> None:
        self.failures = 0
        self.cooldown_until = 0.0
        self.last_reason: Optional[str] = None

    def reset(self) -> None:
        """清零连续失败计数与冷却时间。"""
        self.failures = 0
        self.cooldown_until = 0.0
        self.last_reason = None


_CIRCUIT = _CircuitBreaker()


def reset_circuit_breaker() -> None:
    """重置进程级熔断状态（测试/手动恢复用）。"""
    _CIRCUIT.reset()


def circuit_breaker_state() -> dict:
    """返回熔断状态快照（验收/日志用）。"""
    remaining = max(0.0, _CIRCUIT.cooldown_until - time.monotonic())
    return {
        "consecutive_failures": _CIRCUIT.failures,
        "in_cooldown": remaining > 0,
        "cooldown_remaining_s": round(remaining, 1),
        "last_reason": _CIRCUIT.last_reason,
    }



# ---------------------------------------------------------------- 工具函数
def unwrap_jsonp(text: Optional[str]) -> Optional[dict]:
    """去掉 JSONP 外壳（``cb({...});``）并解析为 dict；失败返回 None。"""
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None
    if raw.startswith("{"):
        payload = raw
    else:
        match = JSONP_RE.match(raw)
        if match:
            payload = match.group(1)
        else:
            start = raw.find("{")
            end = raw.rfind("}")
            if start < 0 or end <= start:
                return None
            payload = raw[start:end + 1]
    try:
        obj = json.loads(payload)
    except (TypeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    """解析 ISO 8601 时间；无时区按本地时区处理；失败返回 None。"""
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _to_int(value: Any) -> Optional[int]:
    """安全转 int。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    """安全转 float。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 客户端
class BoxClient:
    """偶游盒子只读客户端：当局战绩（battledetail）+ 个人战绩点查（index）。"""

    def __init__(self, config: Optional[Config] = None, conn=None,
                 session: Any = None, sleep: Any = time.sleep,
                 retry_times: int = DEFAULT_RETRY_TIMES,
                 cooldown_threshold: int = COOLDOWN_FAILURE_THRESHOLD,
                 cooldown_seconds: float = COOLDOWN_SECONDS) -> None:
        self.config = config or load_config()
        self.session = session if session is not None else self._make_session()
        self._sleep = sleep
        self.retry_times = max(0, int(retry_times))
        # 熔断参数：连续 >= threshold 次风控状态码/网络异常 → 冷却 cooldown_seconds 秒
        self.cooldown_threshold = max(1, int(cooldown_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._conn = conn
        self._owns_conn = False
        self._lock = threading.Lock()
        self._last_request_ts = 0.0
        self.request_count = 0        # 实际 HTTP 请求次数（验收/日志用）
        self.last_error: Optional[str] = None
        self.last_raw_text: Optional[str] = None
        self.last_url: Optional[str] = None

    # ------------------------------------------------------------ 会话
    def _make_session(self) -> Any:
        """建立带实测可用 UA 的 requests 会话（UA 来自 Config.box_headers）。"""
        session = requests.Session()
        session.headers.update(self.config.box_headers())
        return session

    def close(self) -> None:
        """关闭自建的 SQLite 连接（外部传入的 conn 不关）。"""
        if self._owns_conn and self._conn is not None:
            self._conn.close()
            self._conn = None
            self._owns_conn = False

    # ------------------------------------------------------------ 缓存连接
    def _connection(self):
        """懒加载 SQLite 连接（缓存读写用）。"""
        if self._conn is None:
            self._conn = db.connect(self.config.db_path)
            self._owns_conn = True
        return self._conn

    # ------------------------------------------------------------ 限流 / HTTP
    def _rate_limit(self) -> None:
        """保证相邻请求间隔 ≥ ``Config.request_interval_s``（默认 2 s）。"""
        interval = float(self.config.request_interval_s)
        with self._lock:
            if self._last_request_ts > 0.0:
                elapsed = time.monotonic() - self._last_request_ts
                wait = interval - elapsed
                if wait > 0:
                    self._sleep(wait)
            self._last_request_ts = time.monotonic()

    def _http_get_once(self, url: str) -> Optional[str]:
        """单次 GET；返回文本（HTTP 200）或 None（异常/非 200 均不抛）。

        403/412/429/503 与网络异常会计入「连续失败」；连续达到阈值即触发熔断冷却。
        """
        self._rate_limit()
        self.last_url = url
        try:
            self.request_count += 1
            response = self.session.get(url, timeout=self.config.http_timeout_s)
        except Exception as exc:  # noqa: BLE001 - 网络异常一律降级，不向 UI 抛
            self.last_error = "request failed: %s" % exc
            self._register_failure("network: %s" % exc)
            return None
        status = getattr(response, "status_code", 0)
        if status != 200:
            self.last_error = "HTTP %s" % status
            if status in tuple(self.config.abort_status_codes):
                self._register_failure("HTTP %s" % status)
            return None
        text = getattr(response, "text", None)
        if text is None:
            self.last_error = "empty response"
            return None
        self.last_raw_text = text
        self._register_success()
        return text

    # ------------------------------------------------------------ 熔断 / 冷却
    def is_in_cooldown(self) -> bool:
        """当前是否处于熔断冷却期（冷却期内绝不发起网络请求）。"""
        return time.monotonic() < _CIRCUIT.cooldown_until

    def cooldown_remaining_s(self) -> float:
        """冷却剩余秒数（不在冷却期返回 0）。"""
        return max(0.0, _CIRCUIT.cooldown_until - time.monotonic())

    def _register_failure(self, reason: str) -> None:
        """登记一次风控/网络失败；连续达到阈值 → 进入冷却并打中文日志。"""
        with self._lock:
            _CIRCUIT.failures += 1
            _CIRCUIT.last_reason = reason
            if _CIRCUIT.failures >= self.cooldown_threshold:
                _CIRCUIT.failures = 0
                _CIRCUIT.cooldown_until = time.monotonic() + self.cooldown_seconds
                LOGGER.warning(
                    "盒子接口连续失败 %d 次（最近：%s），进入冷却 %.0f 秒（计划要求 5–10 分钟），"
                    "冷却期内不发起任何请求", self.cooldown_threshold, reason, self.cooldown_seconds)

    def _register_success(self) -> None:
        """成功响应：清零连续失败计数；若刚从冷却恢复则打日志。"""
        with self._lock:
            had_cooldown = _CIRCUIT.cooldown_until > 0
            _CIRCUIT.failures = 0
            _CIRCUIT.last_reason = None
            if had_cooldown:
                _CIRCUIT.cooldown_until = 0.0
                LOGGER.info("盒子接口恢复正常，冷却结束")

    def _cooldown_blocked(self) -> bool:
        """冷却期守卫：置错误信息并返回 True（调用方应直接返回，不发请求）。"""
        if not self.is_in_cooldown():
            return False
        self.last_error = "cooldown: 熔断中，剩余 %.0f 秒，未发起请求" % self.cooldown_remaining_s()
        return True

    def _request_text(self, url: str) -> Optional[str]:
        """带「失败重试 1 次」的 GET：共 ``1 + retry_times`` 次尝试。"""
        if self._cooldown_blocked():
            return None
        attempts = 1 + self.retry_times
        for attempt in range(attempts):
            if attempt > 0:
                self._sleep(float(self.config.request_interval_s))
            text = self._http_get_once(url)
            if text is not None:
                return text
        return None

    def _request_jsonp(self, url: str) -> Optional[dict]:
        """带重试的 JSONP 请求：HTTP 失败或 ``code != 200`` 均重试 1 次。"""
        if self._cooldown_blocked():
            return None
        attempts = 1 + self.retry_times
        for attempt in range(attempts):
            if attempt > 0:
                self._sleep(float(self.config.request_interval_s))
            text = self._http_get_once(url)
            if text is None:
                continue
            payload = unwrap_jsonp(text)
            if payload is None:
                self.last_error = "invalid jsonp payload"
                continue
            if str(payload.get("code")) == "200":
                return payload
            self.last_error = "code=%s message=%s" % (payload.get("code"), payload.get("message"))
        return None

    # ------------------------------------------------------------ 180 天 guard
    def is_within_window(self, battle_time: Any, now: Optional[datetime] = None) -> bool:
        """对局时间是否在战绩接口覆盖窗口（默认 180 天）内。

        无法解析对局时间时返回 False（保守：宁可不请求，也不越界请求）。
        """
        battle_dt = parse_iso_datetime(battle_time)
        if battle_dt is None:
            return False
        current = now or datetime.now().astimezone()
        if current.tzinfo is None:
            current = current.astimezone()
        window = timedelta(days=float(self.config.battle_stats_window_days))
        return (current - battle_dt) <= window

    # ------------------------------------------------------------ 当局战绩
    def fetch_battle_detail(self, arena_id: Any, pn: str) -> Optional[dict]:
        """请求一次 battledetail（内部含失败重试 1 次），返回 JSONP 解析后的 dict。"""
        if not pn:
            self.last_error = "pn is required for battledetail"
            return None
        url = self.config.box_battledetail_url_template.format(
            nickname=urllib.parse.quote(str(pn), safe=""), arena_id=str(arena_id))
        return self._request_jsonp(url)

    def parse_battle_detail(self, payload: Optional[dict]) -> list:
        """把 battledetail payload 解析为 ``models.BattlePlayerStats`` 列表。"""
        if not isinstance(payload, dict):
            return []
        result = payload.get("result")
        if not isinstance(result, dict):
            return []
        rows = []
        for side, default_team in (("team_a", 1), ("team_b", 2)):
            for item in result.get(side) or []:
                if not isinstance(item, dict):
                    continue
                vehicle = item.get("vehicle") or {}
                rows.append(models.BattlePlayerStats(
                    dbid=_to_int(vehicle.get("accountDBID")),
                    nickname=item.get("name"),
                    team=_to_int(item.get("team")) or default_team,
                    tank=item.get("tank_title"),
                    combat=_to_float(item.get("combat")),
                    damage=_to_int(vehicle.get("damageDealt")),
                    kills=_to_int(vehicle.get("kills")),
                    xp=_to_int(vehicle.get("xp")),
                    shots=_to_int(vehicle.get("shots")),
                    piercings=_to_int(vehicle.get("piercings")),
                    spotted=_to_int(vehicle.get("spotted")),
                    capture_points=_to_int(vehicle.get("capturePoints")),
                ))
        return rows

    def _cache_get_battle_stats(self, arena_id: str) -> Optional[models.BattleStats]:
        """按 arena_id 读 battle_stats 缓存（仅 status=ok 且 raw_json 可解析时命中）。"""
        row = self._connection().execute(
            "SELECT * FROM battle_stats WHERE arena_id = ?", (arena_id,)).fetchone()
        if row is None:
            return None
        if row["status"] != models.STATS_STATUS_OK or not row["raw_json"]:
            return None
        try:
            payload = json.loads(row["raw_json"])
        except (TypeError, ValueError):
            return None
        return models.BattleStats(
            arena_id=row["arena_id"],
            replay_id=row["replay_id"],
            fetched_at=row["fetched_at"],
            source=row["source"] or "box",
            status=row["status"],
            players=self.parse_battle_detail(payload),
            raw_json=row["raw_json"],
        )

    def _cache_put_battle_stats(self, stats: models.BattleStats) -> None:
        """写入/更新 battle_stats 缓存（按 arena_id 覆盖）。"""
        conn = self._connection()
        conn.execute(
            "INSERT INTO battle_stats(arena_id, replay_id, fetched_at, source, status, raw_json) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(arena_id) DO UPDATE SET replay_id = excluded.replay_id, "
            "fetched_at = excluded.fetched_at, source = excluded.source, "
            "status = excluded.status, raw_json = excluded.raw_json",
            (stats.arena_id, stats.replay_id, stats.fetched_at, stats.source,
             stats.status, stats.raw_json),
        )
        conn.commit()

    def get_battle_stats(self, arena_id: Any, pn: str, battle_time: Any = None,
                         replay_id: Optional[int] = None, force_refresh: bool = False,
                         raw_out: Any = None) -> Optional[models.BattleStats]:
        """取一局当局战绩（缓存优先、180 天 guard、失败重试 1 次后返回 None）。

        返回 ``models.BattleStats``：``status=ok`` 有玩家数据；``status=expired`` 表示
        对局超出 180 天窗口（**未发任何请求**）；``status=cooldown`` 表示盒子接口连续
        风控/网络失败后处于熔断冷却期（同样**未发任何请求**，见 ``is_in_cooldown``）；
        网络/接口失败返回 ``None``。
        """
        arena_key = str(arena_id)
        if battle_time is None:
            # 无法判断对局时间 → 保守处理：不发请求（guard 硬约束）
            return models.BattleStats(
                arena_id=arena_key, replay_id=replay_id, source="box",
                status=models.STATS_STATUS_UNAVAILABLE, players=[])
        if not self.is_within_window(battle_time):
            # 超出 180 天窗口 → 绝不发起请求
            return models.BattleStats(
                arena_id=arena_key, replay_id=replay_id, source="box",
                status=models.STATS_STATUS_EXPIRED, players=[])
        if not force_refresh:
            cached = self._cache_get_battle_stats(arena_key)
            if cached is not None:
                if replay_id is not None:
                    cached.replay_id = replay_id
                return cached
        if self._cooldown_blocked():
            # 熔断冷却期：不发请求，直接返回 cooldown 状态
            return models.BattleStats(
                arena_id=arena_key, replay_id=replay_id, source="box",
                status=STATS_STATUS_COOLDOWN, players=[])
        payload = self.fetch_battle_detail(arena_key, pn)
        if payload is None:
            return None
        stats = models.BattleStats(
            arena_id=arena_key,
            replay_id=replay_id,
            fetched_at=db.now_iso(),
            source="box",
            status=models.STATS_STATUS_OK,
            players=self.parse_battle_detail(payload),
            raw_json=json.dumps(payload, ensure_ascii=False),
        )
        if raw_out:
            target = Path(raw_out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.last_raw_text or "", encoding="utf-8", newline="\n")
        self._cache_put_battle_stats(stats)
        return stats

    # ------------------------------------------------------------ 个人战绩
    def _cache_get_player_stats(self, account_id: int) -> Optional[models.PlayerStats]:
        """按 account_id 读 player_stats 缓存（超过 TTL 视为未命中）。"""
        row = self._connection().execute(
            "SELECT * FROM player_stats WHERE account_id = ?", (account_id,)).fetchone()
        if row is None:
            return None
        fetched_at = parse_iso_datetime(row["fetched_at"])
        if fetched_at is None:
            return None
        ttl = timedelta(hours=float(self.config.player_stats_ttl_hours))
        if (datetime.now().astimezone() - fetched_at) > ttl:
            return None
        return models.PlayerStats(
            account_id=row["account_id"],
            source=row["source"] or "box_index",
            fetched_at=row["fetched_at"],
            update_time=row["update_time"],
            combat_power=row["combat_power"],
            win_rate=row["win_rate"],
            avg_damage=row["avg_damage"],
            battles_hint=row["battles_hint"],
            raw_json=row["raw_json"],
        )

    def _cache_put_player_stats(self, stats: models.PlayerStats) -> None:
        """写入/更新 player_stats 缓存。"""
        conn = self._connection()
        conn.execute(
            "INSERT INTO player_stats(account_id, source, fetched_at, update_time, combat_power, "
            "win_rate, avg_damage, battles_hint, raw_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET source = excluded.source, "
            "fetched_at = excluded.fetched_at, update_time = excluded.update_time, "
            "combat_power = excluded.combat_power, win_rate = excluded.win_rate, "
            "avg_damage = excluded.avg_damage, battles_hint = excluded.battles_hint, "
            "raw_json = excluded.raw_json",
            (stats.account_id, stats.source, stats.fetched_at, stats.update_time,
             stats.combat_power, stats.win_rate, stats.avg_damage, stats.battles_hint,
             stats.raw_json),
        )
        conn.commit()

    def parse_index_html(self, html: str, account_id: Optional[int] = None,
                         nickname: Optional[str] = None) -> Optional[models.PlayerStats]:
        """解析个人战绩 SSR 页 → ``models.PlayerStats``；查不到玩家返回 None。"""
        if not html or NOT_FOUND_MARK in html:
            return None
        parsed_id = _to_int(P_ID_RE.search(html).group(1)) if P_ID_RE.search(html) else account_id
        if parsed_id is None:
            return None
        name_match = NAME_RE.search(html)
        power_match = COMBAT_POWER_RE.search(html)
        win_match = WIN_RATE_RE.search(html)
        total_match = TOTAL_BATTLES_RE.search(html)
        update_match = UPDATE_TIME_RE.search(html)
        damage_match = AVG_DAMAGE_RE.search(html)
        return models.PlayerStats(
            account_id=parsed_id,
            source="box_index",
            fetched_at=db.now_iso(),
            update_time=update_match.group(1).strip() if update_match else None,
            combat_power=_to_float(power_match.group(1)) if power_match else None,
            win_rate=_to_float(win_match.group(1)) if win_match else None,
            avg_damage=_to_float(damage_match.group(1)) if damage_match else None,
            battles_hint=_to_int(total_match.group(1)) if total_match else None,
            raw_json=json.dumps({"nickname": name_match.group(1).strip() if name_match else nickname},
                                ensure_ascii=False),
        )

    def get_player_stats(self, account_id: Optional[int] = None, nickname: Optional[str] = None,
                         force_refresh: bool = False) -> Optional[models.PlayerStats]:
        """点查一名玩家的综合战绩（≥2 s 限流 + TTL 缓存；失败返回 None）。"""
        if account_id is not None and not force_refresh:
            cached = self._cache_get_player_stats(int(account_id))
            if cached is not None:
                return cached
        if self._cooldown_blocked():
            # 熔断冷却期：不发请求，直接返回 None（last_error 说明冷却剩余）
            return None
        if account_id is not None and not nickname:
            url = self.config.box_index_pid_url_template.format(account_id=int(account_id))
        elif nickname:
            url = self.config.box_index_url_template.format(
                nickname=urllib.parse.quote(str(nickname), safe=""))
        else:
            return None
        html = self._request_text(url)
        if html is None:
            return None
        stats = self.parse_index_html(html, account_id=account_id, nickname=nickname)
        if stats is None:
            return None
        self._cache_put_player_stats(stats)
        return stats


# ---------------------------------------------------------------- 命令行入口
def parse_args(argv: list) -> dict:
    """解析命令行参数。"""
    options = {
        "arena_id": None, "pn": None, "battle_time": None, "raw_out": None,
        "index_pid": None, "index_nickname": None, "db_path": None, "out_path": None,
        "no_cache": False,
    }
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--arena-id", "--pn", "--battle-time", "--raw-out", "--index-pid",
                   "--index-nickname", "--db", "--out") and index + 1 < len(argv):
            key = {
                "--arena-id": "arena_id", "--pn": "pn", "--battle-time": "battle_time",
                "--raw-out": "raw_out", "--index-pid": "index_pid",
                "--index-nickname": "index_nickname", "--db": "db_path", "--out": "out_path",
            }[arg]
            options[key] = argv[index + 1]
            index += 2
            continue
        if arg == "--no-cache":
            options["no_cache"] = True
        index += 1
    return options


def stats_to_dict(stats: Any) -> dict:
    """把 BattleStats / PlayerStats 转成可 JSON 序列化的字典。"""
    if stats is None:
        return {}
    if isinstance(stats, models.BattleStats):
        return {
            "arena_id": stats.arena_id,
            "replay_id": stats.replay_id,
            "fetched_at": stats.fetched_at,
            "source": stats.source,
            "status": stats.status,
            "player_count": len(stats.players or []),
            "players": [
                {
                    "dbid": player.dbid, "nickname": player.nickname, "team": player.team,
                    "tank": player.tank, "combat": player.combat, "damage": player.damage,
                    "kills": player.kills, "xp": player.xp, "shots": player.shots,
                    "piercings": player.piercings, "spotted": player.spotted,
                    "capture_points": player.capture_points,
                }
                for player in stats.players or []
            ],
        }
    return {
        "account_id": stats.account_id, "source": stats.source, "fetched_at": stats.fetched_at,
        "update_time": stats.update_time, "combat_power": stats.combat_power,
        "win_rate": stats.win_rate, "avg_damage": stats.avg_damage,
        "battles_hint": stats.battles_hint,
    }


def main(argv: list) -> int:
    """独立运行入口：按 arena_id 取当局战绩 / 按 pid 点查个人战绩。"""
    start = time.time()
    base_path = "."
    options = parse_args(argv)
    cfg = load_config()
    conn = None
    if not options["no_cache"]:
        conn = db.connect(options["db_path"] or cfg.db_path)
    client = BoxClient(config=cfg, conn=conn)
    try:
        if options["arena_id"]:
            if not options["pn"]:
                print("[error] battledetail 必须提供 --pn（任一参战者昵称）")
                return 1
            stats = client.get_battle_stats(
                options["arena_id"], options["pn"], battle_time=options["battle_time"],
                raw_out=options["raw_out"])
            if stats is None:
                print("[warn] 当局战绩不可用（重试 1 次后仍失败）: %s" % client.last_error)
                return 1
            print("arena_id   : %s" % stats.arena_id)
            print("status     : %s" % stats.status)
            print("players    : %d" % len(stats.players))
            print("requests   : %d" % client.request_count)
            if options["out_path"]:
                target = Path(options["out_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(stats_to_dict(stats), ensure_ascii=False, indent=1),
                                  encoding="utf-8", newline="\n")
                print("evidence   : %s" % target)
            return 0
        if options["index_pid"] or options["index_nickname"]:
            account_id = _to_int(options["index_pid"])
            stats = client.get_player_stats(
                account_id=account_id, nickname=options["index_nickname"])
            if stats is None:
                print("[warn] 个人战绩不可用: %s" % client.last_error)
                return 1
            print("account_id : %s" % stats.account_id)
            print("combat     : %s | win_rate=%s | avg_damage=%s | update_time=%s" %
                  (stats.combat_power, stats.win_rate, stats.avg_damage, stats.update_time))
            print("requests   : %d" % client.request_count)
            if options["out_path"]:
                target = Path(options["out_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(stats_to_dict(stats), ensure_ascii=False, indent=1),
                                  encoding="utf-8", newline="\n")
                print("evidence   : %s" % target)
            return 0
        print("usage: python src/stats/box_client.py --arena-id <id> --pn <nickname> "
              "[--battle-time ISO] [--raw-out raw.jsonp]")
        print("       python src/stats/box_client.py --index-pid <account_id> [--out stats.json]")
        return 1
    finally:
        client.close()
        if conn is not None:
            conn.close()
        print("[done] base_path=%r 耗时 %.2f 秒" % (base_path, time.time() - start))


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
