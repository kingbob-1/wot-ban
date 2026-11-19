# -*- coding: utf-8 -*-
"""战绩数据接入（P2.5）：偶游盒子 ``wotapp.ouj.com`` 当局战绩 + ``wotbox.ouj.com`` 个人战绩点查。

两个入口
--------
1. :meth:`BoxClient.get_battle_stats` —— 当局战绩（battledetail JSONP，按 arena_id 缓存）
   - **180 天 guard**：对局时间超出 ``Config.battle_stats_window_days``（180 天）时
     **绝不发起请求**，直接返回 ``status=expired``；
   - 失败重试 1 次（共 2 次尝试），仍失败返回 ``None``，**不抛异常**；
   - 成功后整段原始 JSON 落 ``battle_stats.raw_json``，同一局重复查看零请求
     （**成功即最终版，永不二次抓取**；``force_refresh`` 仅供测试/排障）。
2. :meth:`BoxClient.get_player_stats` —— 个人综合战绩（index SSR 页点查）
   - 仅点查、**≥ ``Config.request_interval_s``（2 s）/请求** 限流；
   - ``player_stats`` 缓存默认**按自然日**（同一天内零请求、跨天过期；
     ``Config.player_stats_cache_by_day=False`` 时退回 ``player_stats_ttl_hours`` 计时）。

partial（中途退出）录像
-----------------------
:meth:`BoxClient.get_partial_battle_stats`：partial 录像没有 ``arena_unique_id``，
先用录像 ``battle_time``（本地时间 ``MM-DD HH:MM``）+ 录像者坦克 slug 匹配盒子
「战斗日志」页（``Config.box_battlelog_url_template``，每页 5 条、支持深翻页；
**按 battle_time 估算页号 + 锚点/封顶页校正，不线性扫**），拿到
``arena-id`` / ``arena-win``（1胜 / 2平 / 0负）后再调 battledetail；成功映射落
``replay_arena_map`` 表（按录像路径幂等 upsert），**二次打开零网络**。
硬约束：仅 180 天内、仅用户打开该局时触发、串行且相邻请求 ≥3 s、
单局预算 ≤8 次、遇 403 立即停（不重试），失败给中文提示稍后再试。

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
    print("需要 requests 依赖：请先执行 python -m pip install requests"
          "（本工作区共享环境也可用 runtime\\python.exe -m pip install requests）",
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
# 盒子「战斗日志」页条目（每页 5 条；实测结构见 research\\wot-ban\\box\\raw\\battlelog_kingbob_.html）：
#   <li class="log-list" arena-id="10974034077980049" arena-win=2 >
#     <img class="list-img" src="//.../app/japan-j20_type_2605.jpg?v=...">
#     <span class="state even">平局 14v6</span>
#     <span class="game">标准<em ...>868</em></span>
#     <span class="list-cont-time">11-15 01:08</span>
BATTLELOG_ITEM_SPLIT_RE = re.compile(r'class="log-list"', re.I)
BATTLELOG_ARENA_ID_RE = re.compile(r'arena-id\s*=\s*["\']?(\d+)', re.I)
BATTLELOG_ARENA_WIN_RE = re.compile(r'arena-win\s*=\s*["\']?(\d+)', re.I)
BATTLELOG_TANK_SLUG_RE = re.compile(
    r'list-img[^>]*src\s*=\s*["\'][^"\']*?/app/([^"\'?]+?)\.(?:jpg|jpeg|png)', re.I)
BATTLELOG_TANK_TITLE_RE = re.compile(r'class="list-tank-title"[^>]*>([^<]+)<', re.I)
BATTLELOG_TIME_RE = re.compile(r'class="list-cont-time"[^>]*>([^<]+)<', re.I)
BATTLELOG_STATE_RE = re.compile(r'class="state[^"]*"[^>]*>([^<]+)<', re.I)
BATTLELOG_COMBAT_RE = re.compile(
    r'class="game"[^>]*>.*?<em[^>]*>\s*([\d.]+)\s*</em>', re.I | re.S)
BATTLELOG_SCORE_RE = re.compile(r"(\d+)\s*v\s*(\d+)", re.I)
# 战斗日志时间文案：``11-15 01:08``（MM-DD HH:MM，无年份）。
BATTLELOG_TIME_TEXT_RE = re.compile(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})$")
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


def _sum_optional_ints(*values: Any) -> Optional[int]:
    """多项整数求和；全部缺失返回 None（盒子 assisted 类字段用）。"""
    numbers = [_to_int(value) for value in values]
    numbers = [value for value in numbers if value is not None]
    if not numbers:
        return None
    return int(sum(numbers))


def tank_slug(code: Any) -> Optional[str]:
    """把录像/盒子的坦克代码归一为 battleLog img 的文件 slug（小写、``:``→``-``）。

    兼容两种输入：录像 ``japan:J20_Type_2605`` / 盒子 ``japan-j20_type_2605``。
    """
    if code is None or str(code).strip() == "":
        return None
    text = str(code).strip().lower().replace(":", "-").replace(" ", "_")
    return text or None


def battle_score(payload: Optional[dict]) -> str:
    """从 battledetail payload 取比分文案 ``team_a_kills v team_b_kills``（如 ``14v6``）。"""
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    team_a = _to_int(result.get("team_a_kills"))
    team_b = _to_int(result.get("team_b_kills"))
    if team_a is None or team_b is None:
        return ""
    return "%dv%d" % (team_a, team_b)


def outcome_from_arena_win(arena_win: Any) -> tuple:
    """盒子 battleLog 的 ``arena-win``（1胜 / 2平 / 0负）→ (outcome, outcome_label)。"""
    value = _to_int(arena_win)
    if value == 1:
        return models.OUTCOME_WIN, models.OUTCOME_LABELS[models.OUTCOME_WIN]
    if value == 2:
        return models.OUTCOME_DRAW, models.OUTCOME_LABELS[models.OUTCOME_DRAW]
    if value == 0:
        return models.OUTCOME_LOSS, models.OUTCOME_LABELS[models.OUTCOME_LOSS]
    return None, ""


def _battlelog_time_to_datetime(text: Any, reference: Optional[datetime]) -> Optional[datetime]:
    """把 battleLog 时间文案（``11-15 01:08``）还原为 datetime（年份按对局时间推断）。

    battleLog 不显示年份：以录像 battle_time 的年份为基准，跨年（如 12-31 / 01-01）
    时在相邻年份里取与参考时间最接近的一个。
    """
    if reference is None:
        return None
    match = BATTLELOG_TIME_TEXT_RE.match(str(text or "").strip())
    if match is None:
        return None
    month, day, hour, minute = (int(group) for group in match.groups())
    candidates = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(reference.replace(year=year, month=month, day=day, hour=hour,
                                                minute=minute, second=0, microsecond=0))
        except ValueError:  # 如 2-29 遇到非闰年
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda item: abs((item - reference).total_seconds()))


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
        # partial 录像 battleLog 匹配：本次实际翻页数 / 命中的条目（验收日志用）
        self.battlelog_pages_fetched = 0
        self.last_battlelog_entry: Optional[dict] = None

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
    def _rate_limit(self, interval: Optional[float] = None) -> None:
        """保证相邻请求间隔 ≥ 指定值（默认 ``Config.request_interval_s``，约 2 s）。"""
        gap = float(self.config.request_interval_s if interval is None else interval)
        with self._lock:
            if self._last_request_ts > 0.0:
                elapsed = time.monotonic() - self._last_request_ts
                wait = gap - elapsed
                if wait > 0:
                    self._sleep(wait)
            self._last_request_ts = time.monotonic()

    def _http_get_once(self, url: str, interval: Optional[float] = None) -> Optional[str]:
        """单次 GET；返回文本（HTTP 200）或 None（异常/非 200 均不抛）。

        403/412/429/503 与网络异常会计入「连续失败」；连续达到阈值即触发熔断冷却。
        ``interval`` 可覆盖本次请求的限流间隔（battleLog 深翻页要求 ≥3 s）。
        """
        self._rate_limit(interval)
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
        """把 battledetail payload 解析为 ``models.BattlePlayerStats`` 列表（盒子全字段）。"""
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
                    tank_code=item.get("vehicle_icon"),
                    combat=_to_float(item.get("combat")),
                    damage=_to_int(vehicle.get("damageDealt")),
                    kills=_to_int(vehicle.get("kills")),
                    xp=_to_int(vehicle.get("xp")),
                    shots=_to_int(vehicle.get("shots")),
                    piercings=_to_int(vehicle.get("piercings")),
                    spotted=_to_int(vehicle.get("spotted")),
                    capture_points=_to_int(vehicle.get("capturePoints")),
                    hits_received=_to_int(vehicle.get("directHitsReceived")),
                    potential_damage=_to_int(vehicle.get("potentialDamageReceived")),
                    damage_blocked=_to_int(vehicle.get("damageBlockedByArmor")),
                    vehicles_damaged=_to_int(vehicle.get("damaged")),
                    assisted_damage=_sum_optional_ints(vehicle.get("damageAssistedRadio"),
                                                       vehicle.get("damageAssistedTrack")),
                    defense_points=_to_int(vehicle.get("numDefended")),
                    mileage=_to_int(vehicle.get("mileage")),
                ))
        return rows

    def _cache_get_battle_stats(self, arena_id: str) -> Optional[models.BattleStats]:
        """按 arena_id 读 battle_stats 缓存（仅 status=ok 且 raw_json 可解析时命中）。

        缓存为「成功即最终版」：只要命中就直接复用，**永不因时间过期重新抓取**；
        新字段（被击中/潜在伤害/装甲抵消…）从既有 raw_json 解析，无需清库重抓。
        """
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
            score=battle_score(payload),
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

        缓存语义：**成功即最终版**（同 arena_id 后续查看零请求，永不二次抓取）；
        ``force_refresh=True`` 仅供测试/手动排障，服务层不 force。
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
            score=battle_score(payload),
        )
        if raw_out:
            target = Path(raw_out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self.last_raw_text or "", encoding="utf-8", newline="\n")
        self._cache_put_battle_stats(stats)
        return stats

    # ------------------------------------------------------------ partial 录像（battleLog 反查）
    def _recorder_pn(self, detail: Any) -> str:
        """录像者昵称（battledetail 的 pn；partial 局同样可用）。"""
        name = getattr(detail, "recorder_name", None)
        if name:
            return str(name)
        for player in getattr(detail, "players", None) or []:
            nickname = getattr(player, "nickname", None)
            if nickname:
                return str(nickname)
        return ""

    def _recorder_tank_code(self, detail: Any) -> Optional[str]:
        """录像者坦克代码（先按 dbid、再按昵称反查 players；partial 无 dbid 时走昵称）。"""
        recorder_dbid = getattr(detail, "recorder_dbid", None)
        recorder_name = getattr(detail, "recorder_name", None)
        players = getattr(detail, "players", None) or []
        if recorder_dbid is not None:
            for player in players:
                if getattr(player, "dbid", None) == recorder_dbid:
                    return getattr(player, "tank", None)
        if recorder_name:
            lowered = str(recorder_name).strip().lower()
            for player in players:
                nickname = str(getattr(player, "nickname", "") or "").strip().lower()
                if nickname and nickname == lowered:
                    return getattr(player, "tank", None)
        return None

    def parse_battlelog_page(self, html: Optional[str]) -> list:
        """解析盒子「战斗日志」页 → 条目列表（arena_id / arena_win / 坦克 slug / 时间 / 比分）。"""
        if not html:
            return []
        entries = []
        for chunk in BATTLELOG_ITEM_SPLIT_RE.split(html)[1:]:
            end = chunk.lower().find("</li>")
            body = chunk[:end] if end >= 0 else chunk
            arena_match = BATTLELOG_ARENA_ID_RE.search(body)
            if arena_match is None:
                continue
            win_match = BATTLELOG_ARENA_WIN_RE.search(body)
            slug_match = BATTLELOG_TANK_SLUG_RE.search(body)
            title_match = BATTLELOG_TANK_TITLE_RE.search(body)
            time_match = BATTLELOG_TIME_RE.search(body)
            state_match = BATTLELOG_STATE_RE.search(body)
            combat_match = BATTLELOG_COMBAT_RE.search(body)
            state_text = state_match.group(1).strip() if state_match else ""
            score_match = BATTLELOG_SCORE_RE.search(state_text)
            entries.append({
                "arena_id": arena_match.group(1),
                "arena_win": _to_int(win_match.group(1)) if win_match else None,
                "tank_code": tank_slug(slug_match.group(1)) if slug_match else None,
                "tank_title": title_match.group(1).strip() if title_match else None,
                "time_text": time_match.group(1).strip() if time_match else None,
                "state_text": state_text,
                "combat": _to_float(combat_match.group(1)) if combat_match else None,
                "score": "%sv%s" % score_match.groups() if score_match else "",
            })
        return entries

    def match_battlelog_candidates(self, entries: list, battle_time: Any,
                                   tank_code: Any = None) -> list:
        """从 battleLog 条目中筛出与「对局时间 + 坦克 slug」匹配的候选（按时间差升序）。

        大小写不敏感；坦克必须一致（无法确认坦克时不匹配）；时间容差来自
        ``Config.box_battlelog_time_tolerance_minutes``（默认 0 = 必须同一分钟）。
        每个候选至少含 ``arena_id / arena_win / score_label / battle_time / tank``。
        """
        battle_dt = parse_iso_datetime(battle_time)
        expected = tank_slug(tank_code)
        if battle_dt is None or not expected:
            return []
        target = battle_dt.replace(second=0, microsecond=0)
        tolerance = max(0, int(getattr(self.config, "box_battlelog_time_tolerance_minutes", 0) or 0))
        candidates = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            if tank_slug(entry.get("tank_code")) != expected:
                continue
            entry_dt = _battlelog_time_to_datetime(entry.get("time_text"), battle_dt)
            if entry_dt is None:
                continue
            diff_seconds = abs((entry_dt - target).total_seconds())
            if diff_seconds > tolerance * 60:
                continue
            candidate = dict(entry)
            candidate["time_diff_s"] = diff_seconds
            # 数据源统一输出字段（与具体页面结构解耦，替换数据源时保持同名）
            candidate["score_label"] = entry.get("state_text") or ""
            candidate["battle_time"] = entry.get("time_text") or ""
            candidate["tank"] = entry.get("tank_code")
            candidates.append(candidate)
        candidates.sort(key=lambda item: (item.get("time_diff_s", 0.0),
                                          str(item.get("arena_id"))))
        return candidates

    # ------------------------------------------------ battleLog 页号估算（锚点 + 割线）
    def _battlelog_interval(self) -> float:
        """battleLog 深翻页的相邻请求间隔（≥ 常规 2 s，默认 3 s，可配置）。"""
        base = float(self.config.request_interval_s or 2.0)
        extra = float(getattr(self.config, "box_battlelog_interval_s", 3.0) or 0.0)
        return max(base, extra)

    def _battlelog_page_bounds(self, entries: list,
                               reference: Optional[datetime]) -> Optional[tuple]:
        """一页条目的时间范围：``(newest, oldest, count)``；无可用时间返回 None。"""
        times = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            parsed = _battlelog_time_to_datetime(entry.get("time_text"), reference)
            if parsed is not None:
                times.append(parsed)
        if not times:
            return None
        return max(times), min(times), len(entries or [])

    def _load_battlelog_anchors(self, pn: str) -> list:
        """从 DB 载入「页号 → 时间范围」锚点（按页号升序；过期锚点丢弃）。"""
        rows = db.get_battlelog_anchors(self._connection(), pn)
        ttl_hours = float(getattr(self.config, "box_battlelog_anchor_ttl_hours", 24.0) or 0.0)
        now = datetime.now().astimezone()
        anchors = []
        for row in rows:
            updated = parse_iso_datetime(row.get("updated_at"))
            if ttl_hours > 0 and (updated is None or (now - updated) > timedelta(hours=ttl_hours)):
                continue
            newest = parse_iso_datetime(row.get("newest_time"))
            oldest = parse_iso_datetime(row.get("oldest_time"))
            if newest is None or oldest is None:
                continue
            anchors.append({
                "page": int(row["page"]),
                "newest": newest,
                "oldest": oldest,
                "count": row.get("entry_count"),
            })
        anchors.sort(key=lambda item: item["page"])
        return anchors

    def _store_battlelog_anchor(self, pn: str, page: int, newest: datetime,
                                oldest: datetime, count: int) -> None:
        """把一次实际探测到的页时间范围写入锚点表（跨进程复用）。"""
        db.upsert_battlelog_anchor(
            self._connection(), pn, page,
            newest_time=newest.isoformat(timespec="seconds"),
            oldest_time=oldest.isoformat(timespec="seconds"),
            entry_count=count)

    def _merge_battlelog_anchors(self, anchors: list, new_anchor: dict) -> list:
        """把新探测的锚点并入锚点列表（同页覆盖，保持页号升序）。"""
        merged = {int(anchor["page"]): anchor for anchor in anchors or []}
        merged[int(new_anchor["page"])] = new_anchor
        return [merged[key] for key in sorted(merged)]

    def _interpolate_battlelog_page(self, target: datetime, left: dict, right: dict) -> int:
        """按两个锚点的「页最新时间」线性插值估算目标页（页号越大越旧）。"""
        span = (left["newest"] - right["newest"]).total_seconds()
        if span <= 0:
            return int(right["page"])
        ratio = (left["newest"] - target).total_seconds() / span
        estimate = left["page"] + ratio * (right["page"] - left["page"])
        return int(round(estimate))

    def _extrapolate_from_anchor(self, target: datetime, anchor: dict, usable: list,
                                 page_limit: int) -> int:
        """从某个较新锚点向更旧方向做**有界**局部外推（单步最多 8 页）。

        玩家活跃密度差异极大（实测同一天傍晚约 30 分钟一页、稀疏期数天一页），
        用局部割线外推时必须限步，否则短时爆发会把估算页吹飞；多步迭代即可收敛。
        """
        page = int(anchor["page"])
        index = next((i for i, item in enumerate(usable)
                      if int(item["page"]) == page), -1)
        if index >= 1:
            local = self._interpolate_battlelog_page(target, usable[index - 1], anchor)
            step = max(2, min(8, local - page))
            return max(1, min(page_limit, page + step))
        # 仅一个锚点（首次探测）：按「约 2 页/天」粗估，允许一次较大跨度（后续迭代校正）
        days_ago = (anchor["newest"] - target).total_seconds() / 86400.0
        if days_ago < 0.5:
            return max(1, min(page_limit, page + 1))
        estimate = int(round(days_ago * 2.0))
        return max(1, min(page_limit, max(page + 2, estimate)))

    def _estimate_battlelog_page(self, target: datetime, anchors: list,
                                 page_limit: int) -> int:
        """按 battle_time 估算目标所在页号（锚点插值 / 有界外推；不线性逐页扫）。

        battleLog 按时间新→旧分页（p=1 最新），且玩家活跃时段密度差异极大（实测同一天
        傍晚可 20 分钟一页、稀疏期数天一页），因此：
        - 目标落在已探测页时间范围内 → 直接该页；
        - 落在两锚点之间且页距很小（≤4）→ 按时间线性插值；
        - 页距很大时远锚点时间密度不可比 → 从较新锚点做有界局部外推（单步 ≤8 页）；
        - 比最旧锚点还旧 → 同样有界局部外推；
        - 仅 1 个锚点（首次探测）→ 按「约 2 页/天」粗估（实测活跃期 2~3 页/天）。
        """
        usable = [anchor for anchor in anchors or []
                  if 1 <= int(anchor["page"]) <= page_limit]
        if not usable:
            return 1
        usable.sort(key=lambda item: item["page"])
        first = usable[0]
        if target >= first["newest"]:
            return max(1, int(first["page"]))
        # 目标落在某个已探测页的时间范围内 → 该页
        for anchor in usable:
            if anchor["oldest"] <= target <= anchor["newest"]:
                return int(anchor["page"])
        # 目标落在两锚点的时间间隙：优先用页距最小的锚点对
        bracketing = []
        for left, right in zip(usable, usable[1:]):
            if right["newest"] <= target <= left["oldest"]:
                bracketing.append((int(right["page"]) - int(left["page"]), left, right))
        if bracketing:
            distance, left, right = min(bracketing, key=lambda item: item[0])
            if distance <= 4:
                span = (left["oldest"] - right["newest"]).total_seconds()
                if span > 0:
                    ratio = (left["oldest"] - target).total_seconds() / span
                    estimate = left["page"] + ratio * (right["page"] - left["page"])
                    return max(int(left["page"]),
                               min(int(right["page"]), int(round(estimate))))
            return self._extrapolate_from_anchor(target, left, usable, page_limit)
        last = usable[-1]
        if target < last["oldest"]:
            return self._extrapolate_from_anchor(target, last, usable, page_limit)
        return max(1, min(page_limit, int(last["page"]) + 1))

    def _next_unprobed_neighbor(self, page: int, probed: dict, page_limit: int,
                                target: Optional[datetime] = None,
                                reference: Optional[datetime] = None) -> Optional[int]:
        """估算页已探测过时，取最近的未探测邻页（按目标时间方向优先）。"""
        deltas = (1, -1, 2, -2)
        entries = probed.get(int(page))
        if target is not None and entries is not None:
            bounds = self._battlelog_page_bounds(entries, reference or target)
            if bounds is not None:
                newest, oldest, _count = bounds
                if target > newest:
                    deltas = (-1, 1, -2, 2)   # 目标比本页更新 → 往页号更小方向找
                elif target < oldest:
                    deltas = (1, -1, 2, -2)   # 目标比本页更旧 → 往页号更大方向找
        for delta in deltas:
            candidate = int(page) + delta
            if 1 <= candidate <= page_limit and candidate not in probed:
                return candidate
        return None

    def _probe_battlelog_page(self, pn: str, page: int, interval: float) -> Optional[list]:
        """单次探测一页 battleLog（不重试；失败返回 None，由调用方立即停）。"""
        url = self.config.box_battlelog_url_template.format(
            nickname=urllib.parse.quote(str(pn), safe=""), page=page)
        html = self._http_get_once(url, interval=interval)
        if html is None:
            return None
        return self.parse_battlelog_page(html)

    def _battlelog_page_signature(self, entries: list) -> tuple:
        """一页内容的去重签名：``(arena-id 列表, 时间列表)``（封顶重复页检测用）。"""
        arena_ids = tuple(str(entry.get("arena_id")) for entry in entries or []
                          if isinstance(entry, dict))
        times = tuple(str(entry.get("time_text")) for entry in entries or []
                      if isinstance(entry, dict))
        return arena_ids, times

    def _load_battlelog_cap(self, pn: str) -> dict:
        """读取封顶页缓存（DB + ``Config.box_battlelog_cap_page`` 覆盖）。"""
        row = db.get_battlelog_cap(self._connection(), pn)
        configured = getattr(self.config, "box_battlelog_cap_page", None)
        cap_page = _to_int(configured)
        cap_oldest = None
        cap_newest = None
        if row is not None:
            if cap_page is None:
                cap_page = _to_int(row.get("cap_page"))
            cap_oldest = parse_iso_datetime(row.get("oldest_time"))
            cap_newest = parse_iso_datetime(row.get("newest_time"))
        return {"page": cap_page, "oldest": cap_oldest, "newest": cap_newest}

    def _store_battlelog_cap(self, pn: str, cap_page: int, newest: Optional[datetime],
                             oldest: Optional[datetime]) -> None:
        """缓存封顶页与覆盖下界时间（后续同账号直接复用、不再探测）。"""
        db.set_battlelog_cap(
            self._connection(), pn, cap_page,
            oldest_time=oldest.isoformat(timespec="seconds") if oldest else None,
            newest_time=newest.isoformat(timespec="seconds") if newest else None)

    def fetch_battlelog_entries(self, pn: str, battle_time: Any,
                                max_pages: Optional[int] = None) -> list:
        """默认 battleLog 数据源：按 battle_time 估算页号 + 锚点/封顶页校正后抓取条目。

        实测（2026-11-19）：每页 5 条；页号可深翻；从某页起盒子返回**完全重复**的
        同一份数据（封顶页，约 1075 局 / 10 个月覆盖）。因此：
        - **不线性逐页扫、不写死页数**：维护「页号 → 时间范围」锚点（DB 持久化），
          用插值/割线估算目标页，再按探测结果校正；
        - 首次探测固定 p=1（最新页）；目标在 p=1 时间范围内即命中（1 次请求）；
        - **封顶重复页检测**：某页 ``(arena-id 列表 + 时间列表)`` 与已探测页完全相同
          → 判定到封顶页，缓存 cap_page + 覆盖下界后停止继续探测；搜索范围限定在
          cap_page 内；目标早于封顶页时间 → 直接判「超出保留窗口」，不再请求；
        - 单局请求预算 ``Config.box_battlelog_budget``（默认 8）、相邻请求
          ≥ ``Config.box_battlelog_interval_s``（默认 3 s）；
        - 任一页请求失败（含 403）立即停（不重试、不继续翻页），交由熔断冷却。
        """
        reference = parse_iso_datetime(battle_time)
        if reference is None or not pn:
            return []
        target = reference.replace(second=0, microsecond=0)
        tolerance = max(0, int(getattr(self.config, "box_battlelog_time_tolerance_minutes", 0) or 0))
        budget = max(1, int(getattr(self.config, "box_battlelog_budget", 8) or 8))
        page_limit = int(max_pages if max_pages is not None
                         else (getattr(self.config, "box_battlelog_max_pages", 1000) or 1000))
        page_limit = max(1, page_limit)
        interval = self._battlelog_interval()
        cap = self._load_battlelog_cap(pn)
        cap_page = cap.get("page")
        cap_oldest = cap.get("oldest")
        if cap_page is not None:
            page_limit = min(page_limit, int(cap_page))
        if cap_oldest is not None and target < cap_oldest - timedelta(minutes=tolerance):
            self.last_error = "超出盒子保留窗口（约 1075 局），不可用"
            return []
        anchors = self._load_battlelog_anchors(pn)
        probed = {}
        signatures = {}
        collected = []
        forced_pages = []
        upper_page = page_limit
        self.battlelog_pages_fetched = 0
        cap_probe = int(getattr(self.config, "box_battlelog_cap_probe_page", 240) or 240)
        for _attempt in range(budget):
            if forced_pages:
                page = forced_pages.pop(0)
            else:
                page = self._estimate_battlelog_page(target, anchors, upper_page)
                if cap_page is not None:
                    page = min(int(page), int(cap_page))
                elif page > cap_probe:
                    # 封顶页未知且估算已超出预期覆盖：先探测封顶区域（240/241 两页对比）
                    page = min(cap_probe, upper_page)
                    if cap_probe + 1 <= upper_page:
                        forced_pages.append(cap_probe + 1)
            if page in probed:
                neighbor = self._next_unprobed_neighbor(page, probed, upper_page,
                                                        target=target, reference=reference)
                if neighbor is None:
                    break
                page = neighbor
            entries = self._probe_battlelog_page(pn, page, interval)
            self.battlelog_pages_fetched += 1
            if entries is None:  # 请求失败（含 403/网络异常）→ 立即停
                break
            probed[page] = entries
            collected.extend(entries)
            bounds = self._battlelog_page_bounds(entries, reference)
            if bounds is None:
                # 空页 = 超过最后一页：收缩页数上界后重新估算
                upper_page = min(upper_page, page - 1)
                if upper_page < 1:
                    break
                continue
            newest, oldest, count = bounds
            self._store_battlelog_anchor(pn, page, newest, oldest, count)
            anchors = self._merge_battlelog_anchors(
                anchors, {"page": page, "newest": newest, "oldest": oldest, "count": count})
            # 封顶重复页检测：(arena-id 列表 + 时间列表) 与已探测页完全相同
            signature = self._battlelog_page_signature(entries)
            if signature in signatures:
                cap_page = min(int(page), int(signatures[signature]))
                cap_oldest = oldest
                cap_newest = newest
                self._store_battlelog_cap(pn, cap_page, cap_newest, cap_oldest)
                forced_pages = []
                page_limit = min(page_limit, int(cap_page))
                upper_page = min(upper_page, int(cap_page))
                if target < cap_oldest - timedelta(minutes=tolerance):
                    self.last_error = "超出盒子保留窗口（约 1075 局），不可用"
                    break
                continue  # 目标仍在覆盖范围内 → 在 cap_page 内继续估算搜索
            signatures[signature] = page
            if page == 1 and target > newest:
                break  # 比日志最新一条还新 → 盒子尚未收录
            if count < 5:
                # 最后一页（无更多真实数据）→ 等价于封顶页：缓存覆盖下界
                cap_page = int(page)
                cap_oldest = oldest
                self._store_battlelog_cap(pn, cap_page, newest, oldest)
                if target < oldest - timedelta(minutes=tolerance):
                    self.last_error = "超出盒子保留窗口（约 1075 局），不可用"
                break
            if cap_page is not None and page == int(cap_page):
                # 已知封顶页：记录其覆盖下界（供下次 0 请求判定）
                self._store_battlelog_cap(pn, int(cap_page), newest, oldest)
                if target < oldest - timedelta(minutes=tolerance):
                    self.last_error = "超出盒子保留窗口（约 1075 局），不可用"
                    break
            if oldest <= target <= newest:
                break  # 目标落在本页时间范围 → 候选匹配决定命中与否
        return collected

    def battlelog_candidates(self, detail: Any, max_pages: Optional[int] = None) -> list:
        """partial 录像的 battleLog 候选数据源（可替换的单一入口）。

        默认实现 = 抓取 battleLog HTML 页 → 解析条目 → 按「对局时间 + 坦克 slug」
        过滤候选。返回 list[dict]，每项至少含
        ``arena_id / arena_win / score_label / battle_time / tank``。
        后续若发现更全的专用接口（如 JSON 列表），只需替换本方法，
        ``find_partial_arena`` / ``get_partial_battle_stats`` 不关心数据来源。
        """
        if detail is None:
            self.last_error = "detail is required for battleLog matching"
            return []
        battle_time = getattr(detail, "battle_time", None)
        pn = self._recorder_pn(detail)
        if not pn:
            self.last_error = "recorder nickname unavailable; cannot match battleLog"
            return []
        tank_code = self._recorder_tank_code(detail)
        if not tank_code:
            self.last_error = "recorder tank unavailable; cannot match battleLog"
            return []
        entries = self.fetch_battlelog_entries(pn, battle_time, max_pages=max_pages)
        return self.match_battlelog_candidates(entries, battle_time, tank_code)

    def find_partial_arena(self, detail: Any, max_pages: Optional[int] = None,
                           force_refresh: bool = False) -> Optional[dict]:
        """partial 录像 → 盒子 battleLog 反查 arena_id（成功匹配持久化到 DB）。

        约束：仅 180 天内；仅调用方（用户打开该局）触发；串行且相邻请求 ≥3 s；
        按 battle_time 估算页号 + 锚点校正（预算默认 8 次请求），失败即停；
        成功映射按录像路径幂等 upsert，二次打开零网络。
        """
        if detail is None:
            self.last_error = "detail is required for battleLog matching"
            return None
        battle_time = getattr(detail, "battle_time", None)
        if not self.is_within_window(battle_time):
            self.last_error = ("battle_time missing or out of %d-day window; no request"
                               % int(self.config.battle_stats_window_days))
            return None
        replay_path = str(getattr(detail, "file_path", "") or "")
        if replay_path and not force_refresh:
            cached = db.get_replay_arena(self._connection(), replay_path)
            if cached is not None:
                return cached
        if self._cooldown_blocked():
            return None
        candidates = self.battlelog_candidates(detail, max_pages=max_pages)
        if not candidates:
            if not self.last_error:
                self.last_error = ("battleLog 中未匹配到该局（时间+坦克）；"
                                   "可能超出战斗日志覆盖范围")
            return None
        matched = candidates[0]
        mapping = {
            "replay_path": replay_path,
            "arena_id": matched["arena_id"],
            "arena_win": matched.get("arena_win"),
            "tank_code": matched.get("tank_code"),
            "battle_time": battle_time,
            "score": matched.get("score") or "",
            "score_label": matched.get("score_label") or "",
            "matched_at": db.now_iso(),
        }
        if replay_path:
            db.set_replay_arena(
                self._connection(), replay_path, matched["arena_id"],
                arena_win=matched.get("arena_win"),
                tank_code=matched.get("tank_code"), battle_time=battle_time)
        self.last_battlelog_entry = matched
        return mapping

    def get_partial_battle_stats(self, detail: Any, force_refresh: bool = False) -> Optional[models.BattleStats]:
        """partial 录像取当局战绩：battleLog 反查 arena → battledetail（含胜负平/比分）。

        返回 ``models.BattleStats``（``arena_source=box_battlelog``）；无法匹配 / 请求
        失败返回 None（不抛异常）。映射命中 + battle_stats 命中时**零网络请求**。
        """
        mapping = self.find_partial_arena(detail, force_refresh=force_refresh)
        if mapping is None:
            return None
        stats = self.get_battle_stats(
            mapping.get("arena_id"), self._recorder_pn(detail),
            battle_time=getattr(detail, "battle_time", None),
            force_refresh=force_refresh)
        if stats is None:
            return None
        outcome, label = outcome_from_arena_win(mapping.get("arena_win"))
        stats.outcome = outcome
        stats.outcome_label = label
        stats.arena_source = models.ARENA_SOURCE_BOX_BATTLELOG
        if not stats.score and mapping.get("score"):
            stats.score = str(mapping["score"])
        return stats

    # ------------------------------------------------------------ 个人战绩
    def _player_stats_cache_fresh(self, fetched_at: Optional[datetime],
                                  now: Optional[datetime] = None) -> bool:
        """玩家综合战绩缓存是否仍新鲜（默认按自然日：同一天内命中、跨天过期）。"""
        if fetched_at is None:
            return False
        current = now or datetime.now().astimezone()
        if current.tzinfo is None:
            current = current.astimezone()
        if bool(getattr(self.config, "player_stats_cache_by_day", True)):
            return fetched_at.date() == current.date()
        ttl = timedelta(hours=float(self.config.player_stats_ttl_hours))
        return (current - fetched_at) <= ttl

    def _row_to_player_stats(self, row: Any) -> models.PlayerStats:
        """``player_stats`` 行 → ``models.PlayerStats``（缓存读取公用）。"""
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

    def _cache_get_player_stats(self, account_id: int) -> Optional[models.PlayerStats]:
        """按 account_id 读 player_stats 缓存（默认同自然日命中、跨天视为未命中）。"""
        row = self._connection().execute(
            "SELECT * FROM player_stats WHERE account_id = ?", (account_id,)).fetchone()
        if row is None:
            return None
        fetched_at = parse_iso_datetime(row["fetched_at"])
        if not self._player_stats_cache_fresh(fetched_at):
            return None
        return self._row_to_player_stats(row)

    def peek_player_stats(self, account_id: Any) -> Optional[models.PlayerStats]:
        """只读本地 ``player_stats`` 缓存（供界面选中玩家时展示，**零网络**）。

        与 :meth:`get_player_stats` 的区别：**不判 TTL**（跨天过期也返回，附
        ``fetched_at``/``update_time`` 供界面标注缓存时间）、**不 sleep、不发请求、
        不触发熔断**；无缓存返回 None。是否刷新由界面手动按钮（force_refresh）决定。
        """
        account_key = _to_int(account_id)
        if account_key is None:
            return None
        try:
            row = self._connection().execute(
                "SELECT * FROM player_stats WHERE account_id = ?", (account_key,)).fetchone()
        except Exception:  # noqa: BLE001 - 只读查询失败按无缓存处理
            return None
        if row is None:
            return None
        return self._row_to_player_stats(row)

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
        """点查一名玩家的综合战绩（≥2 s 限流 + 缓存；失败返回 None）。

        缓存默认按自然日：同一天内重复点查零请求；跨天视为过期自动重查。
        ``force_refresh=True``（界面「刷新综合战绩」按钮）强制跳过缓存重新请求。
        """
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
        "partial_replay": None, "no_cache": False,
    }
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--arena-id", "--pn", "--battle-time", "--raw-out", "--index-pid",
                   "--index-nickname", "--db", "--out", "--partial-replay") and index + 1 < len(argv):
            key = {
                "--arena-id": "arena_id", "--pn": "pn", "--battle-time": "battle_time",
                "--raw-out": "raw_out", "--index-pid": "index_pid",
                "--index-nickname": "index_nickname", "--db": "db_path", "--out": "out_path",
                "--partial-replay": "partial_replay",
            }[arg]
            options[key] = argv[index + 1]
            index += 2
            continue
        if arg == "--no-cache":
            options["no_cache"] = True
        index += 1
    return options


def stats_to_dict(stats: Any) -> dict:
    """把 BattleStats / PlayerStats 转成可 JSON 序列化的字典（证据/CLI 输出用）。"""
    if stats is None:
        return {}
    if isinstance(stats, models.BattleStats):
        return {
            "arena_id": stats.arena_id,
            "replay_id": stats.replay_id,
            "fetched_at": stats.fetched_at,
            "source": stats.source,
            "status": stats.status,
            "outcome": stats.outcome,
            "outcome_label": stats.outcome_label,
            "score": stats.score,
            "arena_source": stats.arena_source,
            "player_count": len(stats.players or []),
            "players": [
                {
                    "dbid": player.dbid, "nickname": player.nickname, "team": player.team,
                    "tank": player.tank, "tank_code": player.tank_code,
                    "combat": player.combat, "damage": player.damage,
                    "kills": player.kills, "xp": player.xp, "shots": player.shots,
                    "piercings": player.piercings, "spotted": player.spotted,
                    "capture_points": player.capture_points,
                    "hits_received": player.hits_received,
                    "potential_damage": player.potential_damage,
                    "damage_blocked": player.damage_blocked,
                    "vehicles_damaged": player.vehicles_damaged,
                    "assisted_damage": player.assisted_damage,
                    "defense_points": player.defense_points,
                    "mileage": player.mileage,
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


def load_replay_detail(path: Any) -> Any:
    """惰性调用 ``src.replay.parser`` 解析一个录像（CLI/取证用，避免模块级循环依赖）。"""
    try:
        from ..replay import parser
    except ImportError:  # 直接以脚本方式运行时的兜底
        from src.replay import parser
    return parser.parse_replay(path)


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
        if options["partial_replay"]:
            detail = load_replay_detail(options["partial_replay"])
            stats = client.get_partial_battle_stats(detail)
            print("replay     : %s" % getattr(detail, "file_name", ""))
            print("battle_time: %s | recorder=%s | block_count=%s"
                  % (detail.battle_time, detail.recorder_name, detail.block_count))
            print("battlelog  : pages=%d entry=%s"
                  % (client.battlelog_pages_fetched, client.last_battlelog_entry))
            if stats is None:
                print("[warn] partial 局战绩不可用: %s" % client.last_error)
                return 1
            print("arena_id   : %s | status=%s | arena_source=%s"
                  % (stats.arena_id, stats.status, stats.arena_source))
            print("outcome    : %s / %s | score=%s"
                  % (stats.outcome, stats.outcome_label, stats.score))
            print("players    : %d" % len(stats.players))
            print("requests   : %d" % client.request_count)
            if options["out_path"]:
                target = Path(options["out_path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(stats_to_dict(stats), ensure_ascii=False, indent=1),
                                  encoding="utf-8", newline="\n")
                print("evidence   : %s" % target)
            return 0
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
            print("status     : %s | score=%s" % (stats.status, stats.score))
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
        print("       python src/stats/box_client.py --partial-replay <replay.wotreplay> "
              "[--out stats.json]")
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
