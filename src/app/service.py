# -*- coding: utf-8 -*-
"""业务门面（Facade）：界面与底层数据模块之间的唯一通道。

设计要点
--------
- **惰性 import**：``store.bans`` / ``replay.parser`` / ``replay.scanner`` /
  ``match.matcher`` / ``stats.box_client`` / ``fetcher`` 等模块可能尚未就绪
  （由其他成员并行开发），全部在方法内部按需导入；缺失时抛 ``ServiceError``
  （中文可读提示），绝不 import 期崩溃。
- **mock 降级**：启动时探测能力，数据层未就绪则自动进入 mock 模式（内置演示
  数据），界面全流程可跑通；``mock=True`` 可强制演示模式。
- **线程安全**：所有方法均为同步阻塞调用，由界面层放进 ``QThread`` 执行；
  SQLite 连接 / 名单索引按线程隔离（``threading.local``），可安全跨线程调用。
- **异常转中文**：对外只抛 ``ServiceError``（含排查建议），界面直接展示。

对 T2/T3 的接口契约（集成时按此对齐，缺失项见 ``capabilities()``）
------------------------------------------------------------------
- ``src.store.bans.load_ban_index(conn)`` -> 名单索引（供 matcher 使用）
- ``src.store.bans.ensure_fresh(conn, force=False, progress_cb=..., cancel_cb=...)``
  或 ``src.fetcher.update.ensure_fresh(...)`` -> ``models.UpdateResult``
- ``src.replay.parser.parse_replay(path)`` -> ``models.ReplayDetail``
- ``src.replay.scanner.scan_replays(replay_dir, mode="recent", days=7,
  progress_cb=..., cancel_cb=...)`` -> ``list[models.ReplayInfo]``
- ``src.match.matcher.match_replay(detail, ban_index)`` -> ``list[models.MatchedPlayer]``
- ``src.stats.box_client.fetch_battledetail(detail, conn=...)`` -> ``models.BattleStats``
- ``src.stats.box_client.fetch_player_stats(dbid, nickname, conn=...)`` -> ``models.PlayerStats``

以上函数名做过多候选适配（``_call_adaptive`` 只传对方签名接受的参数），
集成时如签名不同，只需在对应 ``_*_real`` 方法里补一个候选名。
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from ..config import Config, load_config
    from .. import models
    from . import mock_data
except ImportError:  # 直接以脚本方式运行（python src/app/service.py）时的兜底
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.config import Config, load_config
    from src import models
    from src.app import mock_data

# 各能力对应的模块（探测 / 惰性导入用）。
CAPABILITY_MODULES = {
    "bans": ("src.store.bans", "名单库（T2 src/store/bans.py）"),
    "parser": ("src.replay.parser", "录像解析（T3 src/replay/parser.py）"),
    "scanner": ("src.replay.scanner", "录像扫描（T3 src/replay/scanner.py）"),
    "matcher": ("src.match.matcher", "匹配（T2 src/match/matcher.py）"),
    "box": ("src.stats.box_client", "战绩接口（T3 src/stats/box_client.py）"),
    "updater": ("src.fetcher.update", "名单更新（T2 src/fetcher/）"),
}

# 真实查询流程必需的能力；缺一个即整体降级 mock（战绩接口可选）。
REQUIRED_CAPABILITIES = ("bans", "parser", "scanner", "matcher")

MODE_MOCK = "mock"
MODE_REAL = "real"

MATCH_TYPE_LABELS = {
    models.MATCH_TYPE_ID: "ID 确认",
    models.MATCH_TYPE_NICKNAME: "昵称匹配",
    models.MATCH_TYPE_NONE: "—",
}
LEVEL_LABELS = {
    models.LEVEL_RED: "红标",
    models.LEVEL_YELLOW: "黄标",
    models.LEVEL_NONE: "未命中",
}

BATTLE_STATS_WINDOW_DAYS = 180


class ServiceError(RuntimeError):
    """业务门面异常：message 为可直接展示给用户的中文提示。"""


# ---------------------------------------------------------------- 结果容器
@dataclass
class DataStatus:
    """名单库状态（顶栏 / 数据状态页的数据源）。"""

    available: bool = False
    db_path: str = ""
    batch_count: int = 0
    record_count: int = 0
    official_record_count: int = 0
    replay_index_count: int = 0
    first_batch_day: str = ""
    latest_batch_day: str = ""
    wgid_days: int = 0
    wgid_records: int = 0
    wgid_total_records: int = 0
    missing_days: list = field(default_factory=list)
    last_check_at: str = ""
    last_update_at: str = ""
    batches: list = field(default_factory=list)
    message: str = ""
    mock: bool = False

    @property
    def wgid_coverage_text(self) -> str:
        """顶栏用的 wgid 覆盖文案。"""
        if not self.wgid_days:
            return "暂无（等待首期名单带 ID）"
        return "%d 期 · %d/%d 条" % (self.wgid_days, self.wgid_records, self.wgid_total_records)

    @property
    def missing_text(self) -> str:
        if not self.missing_days:
            return "无"
        return "%d 批：%s" % (len(self.missing_days), "、".join(self.missing_days[:5]))

    @property
    def record_count_text(self) -> str:
        """库内条数（去重入库口径）。"""
        return "%s 条（去重入库）" % "{:,}".format(self.record_count)

    @property
    def official_note(self) -> str:
        """官方源条数备注（物理行口径，不是库内条数）。"""
        if not self.official_record_count:
            return ""
        return (
            "官方源记录 %s 条（物理行口径）；经去重 / 同批归并后入库 %s 条"
            % ("{:,}".format(self.official_record_count), "{:,}".format(self.record_count))
        )


@dataclass
class EnvironmentInfo:
    """本机环境探测结果（v0.2：游戏目录 / 录像目录 / 我的账号）。

    数据来源：T15 ``src.detect.detect_environment()``（注册表卸载项 + 最新录像 meta）；
    模块未就绪或探测失败时降级为配置值 + ``detected=False``，界面据此给出手动选择入口。
    """

    game_dir: str = ""
    replay_dir: str = ""
    account_id: Optional[int] = None
    account_name: str = ""
    game_dir_source: str = ""
    account_source: str = ""
    last_battle_time: str = ""
    detected: bool = False
    message: str = ""

    @property
    def game_dir_text(self) -> str:
        """游戏目录展示文案。"""
        return self.game_dir or "未检测到游戏目录"

    @property
    def account_text(self) -> str:
        """我的账号展示文案。"""
        if self.account_name and self.account_id:
            return "%s（%s）" % (self.account_name, self.account_id)
        if self.account_name:
            return str(self.account_name)
        if self.account_id:
            return "（ID %s）" % self.account_id
        return "未检测到账号"

    @property
    def source_text(self) -> str:
        """来源展示（游戏目录来源 / 账号来源）。"""
        parts = []
        if self.game_dir_source:
            parts.append("游戏目录：%s" % self.game_dir_source)
        if self.account_source:
            parts.append("账号：%s" % self.account_source)
        return " ｜ ".join(parts)


@dataclass
class ReplayQueryResult:
    """单局查询结果：录像详情 + 匹配行 + 汇总。"""

    replay: Any = None
    matches: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    message: str = ""

    @property
    def is_partial(self) -> bool:
        return bool(getattr(self.replay, "is_partial", False))

    @property
    def hit_count(self) -> int:
        return int(self.summary.get("hits", 0))


@dataclass
class BattleStatsResult:
    """当局战绩结果（含可用性、胜负平/比分与中文提示）。

    - ``outcome_label``：胜负平文案（胜利/平局/失败；完整录像即便盒子不可用也用
      replay ``winner_team`` + 录像者队伍给出；partial 局用 battleLog ``arena-win``）；
    - ``score``：比分文案（如 ``14v6``，盒子 ``team_a_kills v team_b_kills``）；
    - ``cached``：本次是否直接命中本地缓存（True = 未发起任何盒子请求）。
    """

    stats: Any = None
    available: bool = False
    message: str = ""
    outcome_label: str = ""
    score: str = ""
    cached: bool = False


@dataclass
class PlayerStatsResult:
    """综合战绩结果（点查，含来源与提示）。"""

    stats: Any = None
    available: bool = False
    message: str = ""


# ---------------------------------------------------------------- 模块级工具
def _module_available(dotted: str) -> bool:
    """探测模块是否存在（不真正 import，避免副作用）。"""
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _load_module(dotted: str, label: str) -> Any:
    """惰性导入模块；失败转中文 ServiceError。"""
    try:
        return importlib.import_module(dotted)
    except Exception as exc:  # noqa: BLE001 - 导入失败原因要原样透出
        raise ServiceError("数据模块尚未就绪：%s（%s）；请等待 T2/T3 完成后重试" % (label, exc)) from exc


def _call_adaptive(func: Callable, **kwargs: Any) -> Any:
    """按目标函数签名过滤参数后调用（兼容 T2/T3 不同的参数命名）。"""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return func(**kwargs)
    parameters = signature.parameters
    if any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return func(**kwargs)
    accepted = {name: value for name, value in kwargs.items() if name in parameters}
    return func(**accepted)


def _parse_iso(value: Any) -> Optional[datetime]:
    """解析 ISO 时间字符串；失败返回 None。"""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_iso_text(value: Any) -> str:
    """ISO 时间 -> 界面展示文案（``11-18 15:20``；解析失败原样返回）。"""
    parsed = _parse_iso(value)
    if parsed is None:
        return str(value) if value else "—"
    return parsed.strftime("%m-%d %H:%M")


def summarize_matches(matches: list) -> dict:
    """统计单局匹配汇总（供结果表底部汇总条与明细）。"""
    total = len(matches or [])
    hits = [item for item in matches or [] if getattr(item, "is_hit", False)]
    id_hits = [item for item in hits if getattr(item, "match_type", "") == models.MATCH_TYPE_ID]
    nickname_hits = [
        item for item in hits if getattr(item, "match_type", "") == models.MATCH_TYPE_NICKNAME
    ]
    return {
        "total": total,
        "hits": len(hits),
        "id_hits": len(id_hits),
        "nickname_hits": len(nickname_hits),
        "yellow": len([item for item in hits if getattr(item, "level", "") == models.LEVEL_YELLOW]),
        "red": len([item for item in hits if getattr(item, "level", "") == models.LEVEL_RED]),
        "partial": len([item for item in hits if getattr(item, "partial", False)]),
    }


def latest_complete_replay(replays: list) -> Any:
    """从录像列表中挑「最新一个完整录像」（block_count>=2）。"""
    complete = [item for item in replays or [] if not getattr(item, "is_partial", True)]
    if not complete:
        return None

    def _key(item: Any) -> tuple:
        battle_dt = _parse_iso(getattr(item, "battle_time", None))
        stamp = battle_dt.timestamp() if battle_dt is not None else float(getattr(item, "mtime", 0.0) or 0.0)
        return (stamp, str(getattr(item, "path", "")))

    return max(complete, key=_key)


def replay_label(replay: Any) -> str:
    """录像列表项的展示文案。"""
    battle_dt = _parse_iso(getattr(replay, "battle_time", None))
    time_text = battle_dt.strftime("%Y-%m-%d %H:%M") if battle_dt else format_iso_text(
        getattr(replay, "battle_time", None)
    )
    map_name = getattr(replay, "map_name", None) or "未知地图"
    kind = "partial（未打完）" if getattr(replay, "is_partial", True) else "完整"
    return "%s · %s · %s · %d 人" % (time_text, map_name, kind, int(getattr(replay, "player_count", 0) or 0))


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 胜负平 / 地图名
def recorder_team(detail: Any) -> Optional[int]:
    """从 ``ReplayDetail.players`` 反查录像者所在队（按 dbid 优先、昵称兜底）。

    不能写死 ``team == 1``：国服录像的阵营 1/2 与盒子 team_a/team_b 不保证对应。
    partial 录像没有 dbid，但名册里有昵称/阵营，同样适用。
    """
    players = getattr(detail, "players", None) or []
    recorder_dbid = getattr(detail, "recorder_dbid", None)
    if recorder_dbid is not None:
        for player in players:
            if getattr(player, "dbid", None) == recorder_dbid:
                return getattr(player, "team", None)
    recorder_name = str(getattr(detail, "recorder_name", None) or "").strip().lower()
    if recorder_name:
        for player in players:
            nickname = str(getattr(player, "nickname", None) or "").strip().lower()
            if nickname and nickname == recorder_name:
                return getattr(player, "team", None)
    return None


def replay_outcome(detail: Any) -> tuple:
    """完整录像的胜负平：``(outcome, outcome_label)``；无法判定返回 ``(None, "")``。

    ``winner_team == 0`` → 平局；等于录像者所在队 → 胜利；否则失败。
    partial 录像没有结算块（``winner_team`` 为空），返回 ``(None, "")``。
    """
    winner_team = getattr(detail, "winner_team", None)
    if winner_team is None:
        return None, ""
    try:
        winner = int(winner_team)
    except (TypeError, ValueError):
        return None, ""
    if winner == 0:
        return models.OUTCOME_DRAW, models.OUTCOME_LABELS[models.OUTCOME_DRAW]
    team = recorder_team(detail)
    if team is None:
        return None, ""
    outcome = models.OUTCOME_WIN if int(team) == winner else models.OUTCOME_LOSS
    return outcome, models.OUTCOME_LABELS[outcome]


def _game_names_module() -> Any:
    """惰性导入 ``src.game_names``（coder-names 交付；缺失时返回 None，绝不抛异常）。"""
    try:
        return importlib.import_module("src.game_names")
    except Exception:  # noqa: BLE001 - 车名/地图名映射属可选增强，缺失只降级
        return None


def map_display_name(code: Any, game_dir: Any = None) -> str:
    """地图代码（如 ``99_poland``）→ 游戏中文名；映射模块不可用时返回空串。"""
    if not code:
        return ""
    module = _game_names_module()
    func = getattr(module, "map_name", None) if module is not None else None
    if not callable(func):
        return ""
    try:
        display = _call_adaptive(func, map_code=code, code=code, game_dir=game_dir)
    except Exception:  # noqa: BLE001 - 映射失败只降级为原代码
        return ""
    return str(display) if display else ""


def _apply_map_display(info: models.ReplayInfo, game_dir: Any = None) -> models.ReplayInfo:
    """ReplayInfo.map_name 若仍是代码，用 ``game_names.map_name`` 换成中文名。"""
    code = getattr(info, "map_name", None)
    if not code:
        return info
    display = map_display_name(code, game_dir)
    if display:
        info.map_name = display
    return info


def battle_score_from_payload(raw: Any) -> str:
    """从 battledetail 原始 payload 取比分文案（``team_a_kills v team_b_kills``）。"""
    if not isinstance(raw, dict):
        return ""
    result = raw.get("result") if isinstance(raw.get("result"), dict) else raw
    if not isinstance(result, dict):
        return ""
    team_a = _as_int(result.get("team_a_kills"), -1)
    team_b = _as_int(result.get("team_b_kills"), -1)
    if team_a < 0 or team_b < 0:
        return ""
    return "%dv%d" % (team_a, team_b)


def _as_replay_info(item: Any, game_dir: Any = None) -> models.ReplayInfo:
    """把扫描器返回的 ReplayDetail / dict / 对象归一为 ReplayInfo（地图代码换中文名）。"""
    if isinstance(item, models.ReplayInfo):
        return _apply_map_display(item, game_dir)
    if isinstance(item, models.ReplayDetail) or hasattr(item, "file_path"):
        info = models.ReplayInfo(
            path=str(getattr(item, "file_path", "") or ""),
            size=int(getattr(item, "file_size", 0) or 0),
            mtime=float(getattr(item, "mtime", 0.0) or 0.0),
            battle_time=getattr(item, "battle_time", None),
            map_name=getattr(item, "map_display_name", None) or getattr(item, "map_name", None),
            client_version=getattr(item, "client_version", None),
            block_count=int(getattr(item, "block_count", 1) or 1),
            parse_status=getattr(item, "parse_status", models.PARSE_STATUS_OK),
            player_count=int(getattr(item, "player_count", 0) or 0),
        )
        return _apply_map_display(info, game_dir)
    if isinstance(item, dict):
        known = {name for name in models.ReplayInfo.__dataclass_fields__}
        payload = {key: value for key, value in item.items() if key in known}
        payload.setdefault("path", item.get("file_path") or item.get("filename") or "")
        if not isinstance(payload.get("path"), str):
            payload["path"] = str(payload.get("path") or "")
        return _apply_map_display(models.ReplayInfo(**payload), game_dir)
    return item


def _as_player(item: Any) -> models.ReplayPlayer:
    """把解析器返回的 dict / 对象归一为 ReplayPlayer。"""
    if isinstance(item, models.ReplayPlayer):
        return item
    if isinstance(item, dict):
        known = {name for name in models.ReplayPlayer.__dataclass_fields__}
        payload = {key: value for key, value in item.items() if key in known}
        return models.ReplayPlayer(**payload)
    return item


def _as_detail(raw: Any, path: str) -> models.ReplayDetail:
    """把解析器返回值归一为 ReplayDetail。"""
    if isinstance(raw, models.ReplayDetail):
        return raw
    if isinstance(raw, dict):
        known = {name for name in models.ReplayDetail.__dataclass_fields__}
        payload = {key: value for key, value in raw.items() if key in known}
        payload.setdefault("file_path", path)
        payload["players"] = [_as_player(item) for item in raw.get("players") or []]
        return models.ReplayDetail(**payload)
    raise ServiceError("录像解析模块返回了无法识别的结果：%r（期望 models.ReplayDetail）" % type(raw))


def _as_matched(item: Any) -> models.MatchedPlayer:
    """把匹配器返回的 dict / 对象归一为 MatchedPlayer。"""
    if isinstance(item, models.MatchedPlayer):
        return item
    if isinstance(item, dict):
        known = {name for name in models.MatchedPlayer.__dataclass_fields__}
        payload = {key: value for key, value in item.items() if key in known}
        payload.setdefault("nickname", str(item.get("name") or ""))
        payload["records"] = list(item.get("records") or [])
        return models.MatchedPlayer(**payload)
    return item


# ---------------------------------------------------------------- 适配器
class _CancelEventAdapter:
    """把 ``cancel_cb() -> bool`` 适配成 ``threading.Event`` 风格（``is_set()``）。"""

    def __init__(self, cancel_cb: Optional[Callable[[], bool]]) -> None:
        self._cancel_cb = cancel_cb

    def is_set(self) -> bool:
        return bool(self._cancel_cb is not None and self._cancel_cb())


class _ScanProgressAdapter:
    """把扫描器的 ``progress_cb(done, total, path, detail)`` 适配成界面用的 ``(percent, message)``。"""

    def __init__(self, callback: Optional[Callable[[int, str], None]]) -> None:
        self._callback = callback
        self._last_percent = -1

    def __call__(self, done: int, total: int, path: Any = None, detail: Any = None) -> None:
        if self._callback is None:
            return
        total = max(1, int(total or 0))
        percent = int(round(int(done or 0) * 100.0 / total))
        if percent == self._last_percent:
            return
        self._last_percent = percent
        name = Path(str(path)).name if path else ""
        message = "正在扫描录像：%d/%d%s" % (int(done or 0), total, (" · " + name) if name else "")
        self._callback(min(99, max(0, percent)), message)


# ---------------------------------------------------------------- 业务门面
class AppService:
    """界面业务门面：数据状态 / 更新名单 / 录像扫描 / 单局查询 / 战绩。"""

    # 串行化「打开 SQLite 连接 + PRAGMA journal_mode」：多个 worker 线程同时打开同一库
    # 时（扫描 + 更新并发）WAL 切换可能互相等待，串行化可消除启动期卡死风险。
    _conn_open_lock = threading.Lock()

    def __init__(self, cfg: Optional[Config] = None, mock: bool = False, logger: Any = None) -> None:
        self.cfg = cfg if cfg is not None else load_config()
        self._logger = logger
        self._force_mock = bool(mock)
        self._capabilities = {key: _module_available(spec[0]) for key, spec in CAPABILITY_MODULES.items()}
        self._mock = self._force_mock or not all(self._capabilities.get(key) for key in REQUIRED_CAPABILITIES)
        self._local = threading.local()
        self._lock = threading.RLock()
        self._box_rate_lock = threading.Lock()
        self._last_box_request_ts = 0.0
        self._mock_replays = mock_data.build_replays() if self._mock else []
        self._mock_status = mock_data.build_data_status() if self._mock else None
        self._environment: Optional[EnvironmentInfo] = None

    # ------------------------------------------------------------ 能力探测
    @property
    def is_mock(self) -> bool:
        """是否处于演示数据模式。"""
        return self._mock

    @property
    def mode(self) -> str:
        return MODE_MOCK if self._mock else MODE_REAL

    @property
    def mode_label(self) -> str:
        """顶栏模式标签文案。"""
        return "演示数据（mock）" if self._mock else "本地真实数据"

    def capabilities(self) -> dict:
        """各能力模块可用性（True/False）与中文说明。"""
        result = {}
        for key, (dotted, label) in CAPABILITY_MODULES.items():
            result[key] = {"module": dotted, "label": label, "ready": bool(self._capabilities.get(key))}
        return result

    def missing_modules(self) -> list:
        """尚未就绪、且真实流程必需的能力说明列表。"""
        return [
            CAPABILITY_MODULES[key][1]
            for key in REQUIRED_CAPABILITIES
            if not self._capabilities.get(key)
        ]

    def mode_message(self) -> str:
        """模式说明（界面横幅用）。"""
        if not self._mock:
            return "已连接本地名单库与录像模块"
        if self._force_mock:
            return "演示模式：使用内置 mock 数据（未读取本地名单库与真实录像）"
        missing = self.missing_modules()
        return "数据模块未就绪，当前使用内置演示数据（mock）：%s" % "、".join(missing)

    # ------------------------------------------------------------ 本机环境（v0.2）
    def _load_detect_module(self) -> Any:
        """惰性加载 T15 环境探测模块（``src/detect.py``；测试可 monkeypatch 本方法）。"""
        return _load_module("src.detect", "环境探测（T15 src/detect.py）")

    def _replay_dir_path(self) -> Optional[Path]:
        """录像目录（``cfg.replay_dir`` 可能为 None / 不存在）。"""
        value = getattr(self.cfg, "replay_dir", None)
        if not value:
            return None
        return Path(value)

    def replay_dir_available(self) -> bool:
        """录像目录是否可用（存在且为目录；mock 模式恒可用）。"""
        if self._mock:
            return True
        replay_dir = self._replay_dir_path()
        return bool(replay_dir is not None and replay_dir.is_dir())

    def _latest_battle_time(self) -> str:
        """最近对局时间：优先录像目录最新文件 mtime（不依赖 DB，避免启动期连接竞争），
        录像目录不可用时降级查询录像索引库 MAX(battle_time)。"""
        replay_dir = self._replay_dir_path()
        if replay_dir is not None and replay_dir.is_dir():
            try:
                files = list(replay_dir.glob("*.wotreplay"))
                if files:
                    newest = max(files, key=lambda item: item.stat().st_mtime)
                    return datetime.fromtimestamp(newest.stat().st_mtime).astimezone().isoformat(
                        timespec="seconds"
                    )
            except OSError:
                pass
        try:
            conn = self._thread_conn()
            row = conn.execute("SELECT MAX(battle_time) AS latest FROM replays").fetchone()
            if row is not None and row["latest"]:
                return str(row["latest"])
        except Exception:  # noqa: BLE001 - 索引库不可用时降级
            pass
        return ""

    def _synthetic_environment(self) -> EnvironmentInfo:
        """探测不可用时的降级环境（只读配置，不写死任何机器路径）。"""
        replay_dir = self._replay_dir_path()
        game_dir = getattr(self.cfg, "game_dir", None)
        game_text = str(game_dir) if game_dir else ""
        replay_text = str(replay_dir) if replay_dir is not None else ""
        if not game_text and replay_text:
            game_text = str(Path(replay_text).parent)
        return EnvironmentInfo(
            game_dir=game_text,
            replay_dir=replay_text,
            last_battle_time=self._latest_battle_time(),
            detected=bool(game_text or replay_text),
            message="尚未检测到游戏环境：请点击「重新检测」或「手动选择游戏目录」",
        )

    def _normalize_environment(self, raw: Any) -> EnvironmentInfo:
        """把 T15 探测结果（dataclass / dict / 对象）归一为 EnvironmentInfo。"""
        if isinstance(raw, EnvironmentInfo):
            env = raw
        elif isinstance(raw, dict):
            known = {name for name in EnvironmentInfo.__dataclass_fields__}
            env = EnvironmentInfo(**{key: value for key, value in raw.items() if key in known})
        else:
            env = EnvironmentInfo(
                game_dir=str(getattr(raw, "game_dir", "") or ""),
                replay_dir=str(getattr(raw, "replay_dir", "") or ""),
                account_id=getattr(raw, "account_id", None),
                account_name=str(getattr(raw, "account_name", "") or ""),
                game_dir_source=str(getattr(raw, "game_dir_source", "") or ""),
                account_source=str(getattr(raw, "account_source", "") or ""),
                last_battle_time=str(getattr(raw, "last_battle_time", "") or ""),
                detected=bool(getattr(raw, "detected", False)),
                message=str(getattr(raw, "message", "") or ""),
            )
        if not env.replay_dir and env.game_dir:
            candidate = Path(env.game_dir) / "replays"
            if candidate.is_dir():
                env.replay_dir = str(candidate)
        if not env.last_battle_time:
            env.last_battle_time = self._latest_battle_time()
        env.detected = bool(env.game_dir or env.replay_dir)
        if not env.message:
            env.message = "已检测到游戏环境" if env.detected else "未检测到游戏目录 / 账号"
        return env

    def detect_environment(self, apply: bool = True) -> EnvironmentInfo:
        """探测本机游戏目录与我的账号（注册表 + 最新录像 meta）。

        模块未就绪 / 探测失败时不抛异常：返回 ``detected=False`` 的降级结果，
        界面据此显示「未检测到…」与「重新检测 / 手动选择游戏目录」按钮。
        """
        if self._mock:
            env = self._normalize_environment(mock_data.build_mock_environment())
            env.message = "演示环境（mock）：未读取本机注册表与真实录像"
            self._environment = env
            return env
        try:
            module = self._load_detect_module()
            func = getattr(module, "detect_environment", None)
            if not callable(func):
                raise ServiceError("环境探测模块接口不匹配：src.detect 未提供 detect_environment()")
            raw = _call_adaptive(func, cfg=self.cfg, config=self.cfg)
            env = self._normalize_environment(raw)
            if apply:
                apply_func = getattr(module, "apply_to_config", None)
                if callable(apply_func):
                    _call_adaptive(apply_func, cfg=self.cfg, config=self.cfg, env=raw, detected=raw)
            self._environment = env
            return env
        except ServiceError as exc:
            env = self._synthetic_environment()
            env.message = "%s；请点击「重新检测」或「手动选择游戏目录」" % exc
            self._environment = env
            return env
        except Exception as exc:  # noqa: BLE001 - 探测失败必须降级，不能影响主界面
            env = self._synthetic_environment()
            env.message = "环境探测失败：%s；请点击「重新检测」或「手动选择游戏目录」" % exc
            self._environment = env
            return env

    def select_game_dir(self, game_dir: str) -> EnvironmentInfo:
        """手动选择游戏目录：更新配置 -> save_config 记忆 -> 重新探测。"""
        target = Path(game_dir).expanduser()
        replay_dir = target / "replays" if (target / "replays").is_dir() else target
        setattr(self.cfg, "game_dir", target)
        setattr(self.cfg, "replay_dir", replay_dir)
        saved_path = ""
        try:
            try:
                from ..config import save_config
            except ImportError:
                from src.config import save_config
            saved_path = str(save_config(self.cfg))
        except Exception:  # noqa: BLE001 - 保存失败时仅本次有效
            saved_path = ""
        env = self.detect_environment(apply=False)
        if not env.detected:
            env = EnvironmentInfo(
                game_dir=str(target),
                replay_dir=str(replay_dir),
                game_dir_source="手动选择",
                last_battle_time=self._latest_battle_time(),
                detected=True,
                message="已手动选择游戏目录：%s" % target,
            )
        env.message = (env.message + "；" if env.message else "") + (
            "已保存到 config.local.json：%s" % saved_path if saved_path else "配置保存失败，仅本次有效"
        )
        self._environment = env
        return env

    def current_environment(self) -> EnvironmentInfo:
        """当前环境（未探测过则探测一次）。"""
        if self._environment is None:
            return self.detect_environment()
        return self._environment

    # ------------------------------------------------------------ 连接 / 索引
    def _thread_conn(self) -> Any:
        """按线程获取 SQLite 连接（QThread 内调用安全）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                from ..store import db
            except ImportError:
                try:
                    from src.store import db
                except ImportError as exc:
                    raise ServiceError("数据库模块不可用：%s" % exc) from exc
            with AppService._conn_open_lock:
                try:
                    conn = db.connect(self.cfg.db_path)
                except Exception as exc:  # noqa: BLE001 - 建库失败要给用户可读提示
                    raise ServiceError("打开名单库失败：%s（%s）" % (self.cfg.db_path, exc)) from exc
            self._local.conn = conn
        return conn

    def _thread_ban_index(self) -> Any:
        """按线程获取名单索引（首次调用时由 store.bans.load_ban_index 构建）。"""
        cached = getattr(self._local, "ban_index", None)
        if cached is not None:
            return cached
        module = _load_module("src.store.bans", CAPABILITY_MODULES["bans"][1])
        func = getattr(module, "load_ban_index", None)
        if not callable(func):
            raise ServiceError(
                "名单库模块接口不匹配：src.store.bans 未提供 load_ban_index(conn)（T2 契约）"
            )
        index = _call_adaptive(func, conn=self._thread_conn(), db_path=self.cfg.db_path, path=self.cfg.db_path)
        self._local.ban_index = index
        return index

    def _invalidate_cache(self) -> None:
        """名单库更新后清掉本线程索引缓存（各线程下次重建）。"""
        self._local.ban_index = None

    # ------------------------------------------------------------ 数据状态
    def get_data_status(self) -> DataStatus:
        """读取名单库状态（批次数 / 条数 / 更新时间 / wgid 覆盖 / 缺失批次）。"""
        if self._mock:
            return DataStatus(**self._mock_status)
        conn = self._thread_conn()
        try:
            rows = conn.execute(
                "SELECT batch_day, source_type, COUNT(*) AS records, "
                "SUM(CASE WHEN wgid IS NOT NULL THEN 1 ELSE 0 END) AS wgid_records "
                "FROM bans GROUP BY batch_day, source_type ORDER BY batch_day DESC"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            raise ServiceError("读取名单库失败：%s" % exc) from exc

        batches = [
            {
                "batch_day": row["batch_day"],
                "source_type": row["source_type"],
                "records": int(row["records"] or 0),
                "wgid_records": int(row["wgid_records"] or 0),
            }
            for row in rows
        ]
        try:
            from ..store import db
        except ImportError:
            from src.store import db

        days_json = db.get_meta(conn, "days_json", []) or []
        last_check_at = db.get_meta(conn, "last_check_at", "") or ""
        wgid_coverage = db.get_meta(conn, "wgid_coverage", {}) or {}
        last_update_at = db.get_meta(conn, "last_update_at", "") or last_check_at
        archive_import = db.get_meta(conn, "archive_import", {}) or {}
        official_count = int(archive_import.get("source_rows_physical_target") or 0)
        try:
            replay_index_count = int(
                conn.execute("SELECT COUNT(*) AS n FROM replays").fetchone()["n"] or 0
            )
        except Exception:  # noqa: BLE001 - 录像索引表异常不影响名单库状态
            replay_index_count = 0

        record_count = sum(item["records"] for item in batches)
        wgid_batches = [item for item in batches if item["wgid_records"] > 0]
        present_days = {item["batch_day"] for item in batches}
        missing_days = [str(day) for day in days_json if str(day) not in present_days]
        if record_count <= 0:
            message = "名单库为空：请点击「更新名单」从官方接口导入（首次约 5-6 分钟）"
        elif missing_days:
            message = "名单库有 %d 个批次缺失，建议点击「更新名单」补漏" % len(missing_days)
        else:
            message = "名单库正常：%d 批 / %d 条" % (len(batches), record_count)

        return DataStatus(
            available=record_count > 0,
            db_path=str(self.cfg.db_path),
            batch_count=len(batches),
            record_count=record_count,
            official_record_count=official_count,
            replay_index_count=replay_index_count,
            first_batch_day=batches[-1]["batch_day"] if batches else "",
            latest_batch_day=batches[0]["batch_day"] if batches else "",
            wgid_days=len(wgid_batches),
            wgid_records=sum(item["wgid_records"] for item in wgid_batches),
            wgid_total_records=sum(item["records"] for item in wgid_batches),
            missing_days=missing_days,
            last_check_at=str(last_check_at),
            last_update_at=str(last_update_at),
            batches=batches,
            message=message,
            mock=False,
        )

    # ------------------------------------------------------------ 数据管理（M6）
    @property
    def update_interval_seconds(self) -> float:
        """A1：运行期自动检查间隔（秒），来自配置 ``update_interval_hours``。"""
        return max(1.0, float(self.cfg.update_interval_hours or 6.0)) * 3600.0

    def data_dir(self) -> Path:
        """数据目录（打开数据目录用）。"""
        return Path(self.cfg.data_dir)

    def log_dir(self) -> Path:
        """日志目录（打开日志目录用）。"""
        return Path(self.cfg.log_dir)

    def clear_ban_library(self, progress_cb: Optional[Callable[[int, str], None]] = None) -> dict:
        """清空名单库（仅 bans 表与名单相关 meta；录像索引 / 别名表保留）。"""
        if self._mock:
            if progress_cb is not None:
                progress_cb(40, "正在清空演示名单库…")
            status = mock_data.build_data_status()
            status.update(
                {
                    "available": False,
                    "batch_count": 0,
                    "record_count": 0,
                    "official_record_count": 0,
                    "first_batch_day": "",
                    "latest_batch_day": "",
                    "wgid_days": 0,
                    "wgid_records": 0,
                    "wgid_total_records": 0,
                    "batches": [],
                    "missing_days": [],
                    "message": "名单库已清空：请点击「重建名单库」从本地归档导入",
                }
            )
            self._mock_status = status
            if progress_cb is not None:
                progress_cb(100, "演示名单库已清空")
            return {"deleted": 322806, "message": "演示名单库已清空（mock）"}

        conn = self._thread_conn()
        try:
            deleted = conn.execute("DELETE FROM bans").rowcount
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - 数据库错误转中文提示
            raise ServiceError("清空名单库失败：%s" % exc) from exc
        try:
            from ..store import db
        except ImportError:
            from src.store import db

        for key, value in (
            ("days_json", []),
            ("record_count", 0),
            ("latest_day", ""),
            ("wgid_coverage", {}),
            ("archive_import", {}),
        ):
            db.set_meta(conn, key, value)
        self._invalidate_cache()
        deleted_count = int(deleted or 0)
        if progress_cb is not None:
            progress_cb(100, "名单库已清空：%s 条" % "{:,}".format(deleted_count))
        return {
            "deleted": deleted_count,
            "message": "已清空名单库（%s 条）；请点击「重建名单库」重新导入本地归档"
            % "{:,}".format(deleted_count),
        }

    def rebuild_ban_library(
        self,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """从本地归档重建名单库（96 期 CSV + 18 期 xlsx + wgid）。

        说明：重建为单次归档导入，进度可见；中途取消仅在开始前生效（导入过程由
        T2 ``store.bans.import_archive`` 控制，不提供中断点）。
        """
        if self._mock:
            steps = [
                (8, "正在读取本地归档清单…"),
                (40, "正在解析 96 期 CSV + 18 期 xlsx…"),
                (76, "正在写入名单库并去重…"),
                (100, "重建完成：去重入库 322,806 条"),
            ]
            for percent, message in steps:
                if progress_cb is not None:
                    progress_cb(percent, message)
                time.sleep(0.03)
            self._mock_status = mock_data.build_data_status()
            return {"imported": 322806, "message": "演示名单库重建完成（mock）"}

        if cancel_cb is not None and cancel_cb():
            raise ServiceError("重建名单库已取消")
        module = _load_module("src.store.bans", CAPABILITY_MODULES["bans"][1])
        func = getattr(module, "import_archive", None)
        if not callable(func):
            raise ServiceError("名单库模块接口不匹配：src.store.bans 未提供 import_archive()（T2 契约）")
        if progress_cb is not None:
            progress_cb(1, "正在清空旧名单库…")
        self.clear_ban_library()
        conn = self._thread_conn()
        try:
            report = _call_adaptive(
                func,
                conn=conn,
                archive_dir=str(self.cfg.archive_dir),
                cfg=self.cfg,
                progress_cb=progress_cb,
                quiet=True,
            )
        except Exception as exc:  # noqa: BLE001 - 导入失败转中文提示
            raise ServiceError("重建名单库失败：%s" % exc) from exc
        self._invalidate_cache()
        imported = int(getattr(report, "db_rows", 0) or 0)
        return {
            "imported": imported,
            "message": "重建完成：去重入库 %s 条（官方源 322,941 物理行口径）"
            % "{:,}".format(imported),
        }

    # ------------------------------------------------------------ 更新名单
    def update_ban_list(
        self,
        force: bool = False,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> models.UpdateResult:
        """检查并更新名单（A1：启动检查一次 + 运行期每 6h + 手动按钮）。"""
        if self._mock:
            return self._mock_update(progress_cb)
        result = self._update_real(force=force, progress_cb=progress_cb, cancel_cb=cancel_cb)
        self._invalidate_cache()
        return result

    def _mock_update(self, progress_cb: Optional[Callable[[int, str], None]]) -> models.UpdateResult:
        steps = [
            (12, "正在检查官方名单接口…"),
            (46, "当前最新批次 2026-11-06，正在核对本地批次…"),
            (82, "本地 114 批 / 322,941 条已完整，无需下载"),
            (100, "检查完成：已是最新"),
        ]
        for percent, message in steps:
            if progress_cb is not None:
                progress_cb(percent, message)
            time.sleep(0.03)
        return mock_data.build_update_result()

    def _update_real(
        self,
        force: bool,
        progress_cb: Optional[Callable[[int, str], None]],
        cancel_cb: Optional[Callable[[], bool]],
    ) -> models.UpdateResult:
        candidates = [
            ("src.store.bans", "ensure_fresh"),
            ("src.fetcher.update", "ensure_fresh"),
            ("src.fetcher.update", "update_ban_list"),
            ("src.fetcher.update", "check_update"),
            ("src.store.bans", "update_ban_list"),
        ]
        errors = []
        for dotted, func_name in candidates:
            if not _module_available(dotted):
                errors.append("%s 不存在" % dotted)
                continue
            module = _load_module(dotted, "名单更新")
            func = getattr(module, func_name, None)
            if not callable(func):
                errors.append("%s.%s 不存在" % (dotted, func_name))
                continue
            raw = _call_adaptive(
                func,
                conn=self._thread_conn(),
                force=force,
                progress_cb=progress_cb,
                on_progress=progress_cb,
                cancel_cb=cancel_cb,
                should_cancel=cancel_cb,
                now=datetime.now().astimezone(),
                cfg=self.cfg,
            )
            return self._normalize_update_result(raw)
        raise ServiceError(
            "更新模块尚未就绪：需要 T2 提供 ensure_fresh()（尝试过 %s）" % "；".join(errors[-3:])
        )

    def _normalize_update_result(self, raw: Any) -> models.UpdateResult:
        """把更新模块返回值归一为 UpdateResult。"""
        if isinstance(raw, models.UpdateResult):
            return raw
        if isinstance(raw, dict):
            known = {name for name in models.UpdateResult.__dataclass_fields__}
            payload = {key: value for key, value in raw.items() if key in known}
            return models.UpdateResult(**payload)
        if raw is None:
            return models.UpdateResult(ok=True, checked_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                                       message="检查完成")
        raise ServiceError("更新模块返回了无法识别的结果：%r（期望 models.UpdateResult）" % type(raw))

    # ------------------------------------------------------------ 录像扫描
    def list_replays(
        self,
        mode: str = "latest",
        days: Optional[int] = None,
        manual_path: Optional[str] = None,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        cancel_cb: Optional[Callable[[], bool]] = None,
    ) -> list:
        """扫描录像目录，返回按时间倒序的 ReplayInfo 列表。

        mode: ``latest``（最近 N 天，界面自动选最新完整）/``recent``（最近 N 天）/
        ``all``（全部）/``file``（手动指定的单个文件）。
        """
        if self._mock:
            return self._mock_scan(mode, manual_path, progress_cb)
        if mode != "file" and not self.replay_dir_available():
            raise ServiceError(
                "未检测到录像目录：请点击「重新检测」或「手动选择游戏目录」（当前配置：%s）"
                % (getattr(self.cfg, "replay_dir", None) or "未设置")
            )
        scan_mode = "all" if mode == "all" else "recent"
        window_days = None if mode == "all" else int(days if days is not None else self.cfg.default_scan_days)
        if mode == "file" and manual_path:
            detail = self._parse_real(manual_path)
            return [self._replay_info_from_detail(detail)]
        raw = self._scan_real(scan_mode, window_days, manual_path, progress_cb, cancel_cb)
        if hasattr(raw, "replays"):
            raw = getattr(raw, "replays")
        replays = [_as_replay_info(item, getattr(self.cfg, "game_dir", None)) for item in raw or []]
        replays = [item for item in replays if getattr(item, "path", None)]
        if progress_cb is not None:
            progress_cb(100, "扫描完成：%d 个录像" % len(replays))
        replays.sort(
            key=lambda item: (
                (_parse_iso(getattr(item, "battle_time", None)) or datetime.min).timestamp()
                if _parse_iso(getattr(item, "battle_time", None)) is not None
                else float(getattr(item, "mtime", 0.0) or 0.0)
            ),
            reverse=True,
        )
        return replays

    def _mock_scan(
        self,
        mode: str,
        manual_path: Optional[str],
        progress_cb: Optional[Callable[[int, str], None]],
    ) -> list:
        steps = [
            (10, "正在定位录像目录：%s" % mock_data.MOCK_REPLAY_DIR),
            (42, "正在读取录像文件头（只读 meta/results 块）…"),
            (74, "已发现 4 个录像文件，正在识别完整 / partial…"),
            (100, "扫描完成：4 个录像（完整 3 / partial 1）"),
        ]
        for percent, message in steps:
            if progress_cb is not None:
                progress_cb(percent, message)
            time.sleep(0.04)
        replays = list(self._mock_replays)
        if manual_path:
            matched = [item for item in replays if item.path == manual_path]
            if matched:
                return matched
            detail = mock_data.build_detail(manual_path)
            replays.append(self._replay_info_from_detail(detail))
            return replays[-1:]
        return replays

    def _scan_real(
        self,
        mode: str,
        days: Optional[int],
        manual_path: Optional[str],
        progress_cb: Optional[Callable[[int, str], None]],
        cancel_cb: Optional[Callable[[], bool]],
    ) -> list:
        module = _load_module("src.replay.scanner", CAPABILITY_MODULES["scanner"][1])
        replay_dir = self._replay_dir_path()
        root = manual_path or (str(replay_dir) if replay_dir is not None else "")
        kwargs = {
            # T3 scanner.scan_replays 实际签名
            "config": self.cfg,
            "conn": self._thread_conn(),
            "days": days,
            "recursive": True,
            "complete_only": False,
            "progress_cb": _ScanProgressAdapter(progress_cb) if progress_cb is not None else None,
            "cancel_event": _CancelEventAdapter(cancel_cb),
            "persist": True,
            # 兼容其它候选命名（_call_adaptive 只传目标函数接受的参数）
            "replay_dir": root,
            "root": root,
            "directory": root,
            "path": root,
            "mode": mode,
            "recent_days": days,
            "on_progress": _ScanProgressAdapter(progress_cb) if progress_cb is not None else None,
            "cancel_cb": cancel_cb,
            "should_cancel": cancel_cb,
            "force": False,
        }
        for name in ("scan_replays", "scan", "scan_directory", "list_replays"):
            func = getattr(module, name, None)
            if callable(func):
                return _call_adaptive(func, **kwargs)
        scanner_cls = getattr(module, "ReplayScanner", None)
        if scanner_cls is not None:
            instance = self._instantiate_adaptive(scanner_cls, **kwargs)
            for name in ("scan", "scan_replays", "list_replays"):
                func = getattr(instance, name, None)
                if callable(func):
                    return _call_adaptive(func, **kwargs)
        raise ServiceError(
            "录像扫描模块接口不匹配：src.replay.scanner 未提供 scan_replays()/ReplayScanner（T3 契约）"
        )

    def _instantiate_adaptive(self, cls: Any, **kwargs: Any) -> Any:
        """按签名构造对象；构造失败时退回无参构造。"""
        try:
            return _call_adaptive(cls, **kwargs)
        except TypeError:
            try:
                return cls()
            except TypeError as exc:
                raise ServiceError("录像扫描器无法初始化：%s" % exc) from exc

    def _replay_info_from_detail(self, detail: models.ReplayDetail) -> models.ReplayInfo:
        """由已解析的详情反推索引行（手动选择单个录像时用；地图代码换中文名）。"""
        info = models.ReplayInfo(
            path=detail.file_path,
            size=detail.file_size,
            mtime=detail.mtime,
            battle_time=detail.battle_time,
            map_name=detail.map_display_name or detail.map_name,
            client_version=detail.client_version,
            block_count=detail.block_count,
            parse_status=detail.parse_status,
            player_count=detail.player_count,
        )
        return _apply_map_display(info, getattr(self.cfg, "game_dir", None))

    # ------------------------------------------------------------ 单局查询
    def query_replay(self, replay: Any) -> ReplayQueryResult:
        """解析录像 + 匹配名单，返回表格 / 明细 / 汇总所需全部数据。"""
        path = replay if isinstance(replay, str) else str(getattr(replay, "path", ""))
        if not path:
            raise ServiceError("查询录像失败：录像路径为空")
        if self._mock:
            return self._mock_query(path)
        detail = self._parse_real(path)
        ban_index = self._thread_ban_index()
        matches = self._match_real(detail, ban_index)
        summary = summarize_matches(matches)
        message = "解析完成：%d 名玩家，命中 %d 人" % (len(detail.players or []), summary["hits"])
        if detail.is_partial:
            message += "；partial 录像无账号 ID，仅做昵称匹配"
        return ReplayQueryResult(replay=detail, matches=matches, summary=summary, message=message)

    def _mock_query(self, path: str) -> ReplayQueryResult:
        replay = next((item for item in self._mock_replays if item.path == path), None)
        if replay is None:
            detail = mock_data.build_detail(path)
        else:
            detail = mock_data.build_detail(replay)
        matches = mock_data.build_matches(detail)
        summary = summarize_matches(matches)
        message = "解析完成：%d 名玩家，命中 %d 人" % (len(detail.players or []), summary["hits"])
        if detail.is_partial:
            message += "；partial 录像无账号 ID，仅做昵称匹配"
        return ReplayQueryResult(replay=detail, matches=matches, summary=summary, message=message)

    def _parse_real(self, path: str) -> models.ReplayDetail:
        module = _load_module("src.replay.parser", CAPABILITY_MODULES["parser"][1])
        for name in ("parse_replay", "parse_replay_file", "parse"):
            func = getattr(module, name, None)
            if callable(func):
                raw = _call_adaptive(func, path=path, file_path=path, replay_path=path)
                return _as_detail(raw, path)
        raise ServiceError("录像解析模块接口不匹配：src.replay.parser 未提供 parse_replay(path)（T3 契约）")

    def _match_real(self, detail: models.ReplayDetail, ban_index: Any) -> list:
        module = _load_module("src.match.matcher", CAPABILITY_MODULES["matcher"][1])
        kwargs = {
            "detail": detail,
            "replay": detail,
            "replay_detail": detail,
            "players": detail.players,
            "ban_index": ban_index,
            "index": ban_index,
            "conn": self._thread_conn(),
        }
        for name in ("match_replay", "match_detail", "match_players", "match"):
            func = getattr(module, name, None)
            if callable(func):
                raw = _call_adaptive(func, **kwargs)
                return [item for item in (_as_matched(entry) for entry in raw or []) if item is not None]
        matcher_cls = getattr(module, "Matcher", None)
        if matcher_cls is not None:
            instance = self._instantiate_adaptive(matcher_cls, **kwargs)
            for name in ("match", "match_replay", "match_players"):
                func = getattr(instance, name, None)
                if callable(func):
                    raw = _call_adaptive(func, **kwargs)
                    return [item for item in (_as_matched(entry) for entry in raw or []) if item is not None]
        raise ServiceError(
            "匹配模块接口不匹配：src.match.matcher 未提供 match_replay(detail, ban_index)（T2 契约）"
        )

    # ------------------------------------------------------------ 当局战绩
    def get_battle_stats(self, detail: models.ReplayDetail) -> BattleStatsResult:
        """取当局战绩（A7：打开某局才请求；180 天外不发请求）。

        - **完整录像**：按 replay ``arena_unique_id`` 请求 battledetail；盒子不可用时
          仍用 replay 结果（winner_team + 录像者队伍）给出 ``outcome_label``；
        - **partial 录像**：走盒子 battleLog 反查 arena_id（时间 + 坦克匹配）后再请求
          battledetail，胜负平取 battleLog ``arena-win``；映射成功后二次打开零网络。
        """
        if detail is None:
            return BattleStatsResult(available=False, message="没有可查询的对局")
        outcome, outcome_label = replay_outcome(detail)
        if getattr(detail, "is_partial", False):
            if not self._within_battle_stats_window(detail):
                return BattleStatsResult(
                    available=False,
                    message="该局超出战绩接口 %d 天范围（仅最近 %d 天对局可查），已跳过请求"
                    % (BATTLE_STATS_WINDOW_DAYS, BATTLE_STATS_WINDOW_DAYS),
                )
            if self._mock:
                return BattleStatsResult(
                    available=False,
                    message="演示模式（mock）：partial 局未接入盒子战斗日志反查",
                )
            return self._battle_stats_partial(detail)
        if not self._within_battle_stats_window(detail):
            return BattleStatsResult(
                available=False,
                message="该局超出战绩接口 %d 天范围（仅最近 %d 天对局可查），已跳过请求"
                % (BATTLE_STATS_WINDOW_DAYS, BATTLE_STATS_WINDOW_DAYS),
                outcome_label=outcome_label,
            )
        if self._mock:
            stats = mock_data.build_battle_stats(detail)
            stats.outcome = outcome
            stats.outcome_label = outcome_label
            stats.arena_source = models.ARENA_SOURCE_REPLAY
            return BattleStatsResult(
                stats=stats,
                available=True,
                message="数据来自偶游盒子（演示数据），仅供参考、非官方数据",
                outcome_label=outcome_label,
                score=stats.score or "",
            )
        result = self._battle_stats_real(detail)
        if outcome_label:
            result.outcome_label = outcome_label
            if result.stats is not None:
                result.stats.outcome = outcome
                result.stats.outcome_label = outcome_label
        if result.stats is not None:
            result.stats.arena_source = models.ARENA_SOURCE_REPLAY
            if not result.score:
                result.score = getattr(result.stats, "score", "") or ""
        return result

    def _battle_stats_partial(self, detail: models.ReplayDetail) -> BattleStatsResult:
        """partial 录像：盒子 battleLog 反查 arena_id → battledetail（失败降级中文提示）。"""
        module = _load_module("src.stats.box_client", CAPABILITY_MODULES["box"][1])
        kwargs = {
            "config": self.cfg,
            "cfg": self.cfg,
            "conn": self._thread_conn(),
            "detail": detail,
            "replay": detail,
            "force_refresh": False,
        }
        client = self._make_box_client(module, kwargs)
        before = int(getattr(client, "request_count", 0) or 0) if client is not None else 0
        raw = None
        if client is not None:
            for name in ("get_partial_battle_stats", "fetch_partial_battle_stats"):
                func = getattr(client, name, None)
                if callable(func):
                    raw = _call_adaptive(func, **kwargs)
                    if raw is not None:
                        break
        after = int(getattr(client, "request_count", 0) or 0) if client is not None else 0
        cached = bool(raw is not None and after == before)
        if raw is None:
            last_error = getattr(client, "last_error", None) if client is not None else None
            return BattleStatsResult(
                available=False,
                cached=False,
                message=(
                    "partial 局战绩暂不可用：%s；请稍后再试"
                    "（盒子限流或未匹配到战报，战斗日志约保留最近 1100 局）"
                    % (last_error or "未匹配到战报")
                ),
            )
        stats = self._normalize_battle_stats(raw, detail)
        if stats is None or getattr(stats, "status", "") != models.STATS_STATUS_OK:
            return BattleStatsResult(
                available=False,
                cached=cached,
                message="partial 局已匹配 arena_id，但盒子当局战绩暂不可用（接口失败或冷却），请稍后再试",
            )
        return BattleStatsResult(
            stats=stats,
            available=True,
            cached=cached,
            message="数据来自偶游盒子（partial 局按战斗日志反查 arena_id；仅供参考、非官方数据）",
            outcome_label=getattr(stats, "outcome_label", "") or "",
            score=getattr(stats, "score", "") or "",
        )

    def _within_battle_stats_window(self, detail: models.ReplayDetail) -> bool:
        """对局时间是否在战绩接口覆盖窗口内。"""
        battle_dt = _parse_iso(getattr(detail, "battle_time", None))
        if battle_dt is None:
            return False
        now = datetime.now().astimezone()
        if battle_dt.tzinfo is None:
            battle_dt = battle_dt.astimezone()
        return (now - battle_dt) <= timedelta(days=BATTLE_STATS_WINDOW_DAYS)

    def _battle_stats_real(self, detail: models.ReplayDetail) -> BattleStatsResult:
        module = _load_module("src.stats.box_client", CAPABILITY_MODULES["box"][1])
        kwargs = {
            # T3 BoxClient 实际接口：get_battle_stats(arena_id, pn, battle_time=...)
            "config": self.cfg,
            "cfg": self.cfg,
            "conn": self._thread_conn(),
            "arena_id": str(detail.arena_unique_id or ""),
            "arena_unique_id": detail.arena_unique_id,
            "pn": detail.recorder_name,
            "nickname": detail.recorder_name,
            "battle_time": detail.battle_time,
            "replay_id": None,
            "force_refresh": False,
            "detail": detail,
            "replay": detail,
        }
        client = self._make_box_client(module, kwargs)
        before = int(getattr(client, "request_count", 0) or 0) if client is not None else 0
        raw = None
        if client is not None:
            for name in ("get_battle_stats", "fetch_battle_detail", "fetch_battledetail", "battledetail"):
                func = getattr(client, name, None)
                if callable(func):
                    raw = _call_adaptive(func, **kwargs)
                    if raw is not None:
                        break
        if raw is None:
            for name in ("get_battle_stats", "fetch_battledetail", "fetch_battle_detail"):
                func = getattr(module, name, None)
                if callable(func):
                    raw = _call_adaptive(func, **kwargs)
                    break
        after = int(getattr(client, "request_count", 0) or 0) if client is not None else 0
        cached = bool(raw is not None and client is not None and after == before)
        if raw is None:
            return BattleStatsResult(
                available=False,
                message="当局战绩暂不可用（接口无数据或请求失败；已按 A7 仅打开该局时请求一次）",
            )
        result = self._battle_stats_from_raw(raw, detail, client)
        result.cached = cached
        return result

    def _make_box_client(self, module: Any, kwargs: dict) -> Any:
        """构造 T3 的 ``BoxClient``（签名差异时退回无参构造）。"""
        client_cls = getattr(module, "BoxClient", None)
        if client_cls is None:
            return None
        try:
            return _call_adaptive(client_cls, **kwargs)
        except TypeError:
            try:
                return client_cls()
            except TypeError:
                return None

    def _wait_box_rate_limit(self) -> None:
        """跨线程 / 跨客户端实例的盒子 index 限流：相邻请求 ≥ request_interval_s（默认 2s）。

        T3 的 ``BoxClient`` 每个实例自带限流，但门面按调用新建实例（线程安全需要），
        因此在门面层再做一次全局限流，保证「仅点查、≥2s/请求」口径真正生效。
        """
        interval = max(0.0, float(self.cfg.request_interval_s or 2.0))
        with self._box_rate_lock:
            now = time.monotonic()
            if self._last_box_request_ts > 0.0:
                elapsed = now - self._last_box_request_ts
                if elapsed < interval:
                    time.sleep(interval - elapsed)
            self._last_box_request_ts = time.monotonic()

    def _battle_stats_from_raw(self, raw: Any, detail: models.ReplayDetail, client: Any) -> BattleStatsResult:
        """归一当局战绩返回值并转成中文结果。"""
        stats = None
        if isinstance(raw, models.BattleStats):
            stats = raw
        elif isinstance(raw, dict) and ("team_a" in raw or "team_b" in raw):
            players = []
            parse_func = getattr(client, "parse_battle_detail", None) if client is not None else None
            if callable(parse_func):
                players = _call_adaptive(parse_func, payload=raw) or []
            stats = models.BattleStats(
                arena_id=str(detail.arena_unique_id or ""),
                replay_id=None,
                fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                source="box",
                status=models.STATS_STATUS_OK,
                players=list(players),
                score=battle_score_from_payload(raw),
            )
        else:
            stats = self._normalize_battle_stats(raw, detail)
        if stats is None or getattr(stats, "status", "") != models.STATS_STATUS_OK:
            return BattleStatsResult(available=False, message="当局战绩暂不可用（接口返回异常），主表不受影响")
        if not getattr(stats, "score", ""):
            stats.score = battle_score_from_payload(raw)
        return BattleStatsResult(
            stats=stats,
            available=True,
            message="数据来自偶游盒子（仅供参考、非官方数据，不作为开挂或处罚依据）",
            score=getattr(stats, "score", "") or "",
        )

    def _normalize_battle_stats(self, raw: Any, detail: models.ReplayDetail) -> Optional[models.BattleStats]:
        """把战绩客户端返回值归一为 BattleStats。"""
        if isinstance(raw, models.BattleStats):
            return raw
        if isinstance(raw, dict):
            known = {name for name in models.BattleStats.__dataclass_fields__}
            payload = {key: value for key, value in raw.items() if key in known}
            payload.setdefault("arena_id", str(detail.arena_unique_id or ""))
            players = []
            for item in raw.get("players") or []:
                if isinstance(item, models.BattlePlayerStats):
                    players.append(item)
                elif isinstance(item, dict):
                    fields = {name for name in models.BattlePlayerStats.__dataclass_fields__}
                    players.append(models.BattlePlayerStats(**{k: v for k, v in item.items() if k in fields}))
            payload["players"] = players
            return models.BattleStats(**payload)
        return None

    # ------------------------------------------------------------ 综合战绩
    def get_player_stats(self, matched: models.MatchedPlayer, force: bool = False) -> PlayerStatsResult:
        """点查某玩家综合战绩（A7：仅点查、限流与缓存由 stats 模块负责）。"""
        if matched is None:
            return PlayerStatsResult(available=False, message="请先选择一名玩家")
        if not getattr(matched, "is_hit", False):
            return PlayerStatsResult(available=False, message="该玩家未命中封禁名单，无需点查综合战绩")
        if self._mock:
            stats = mock_data.build_player_stats(matched)
            return PlayerStatsResult(
                stats=stats,
                available=True,
                message="数据来自偶游盒子 index（最近 1500 场标准局口径，演示数据，仅供参考、非官方数据）",
            )
        if not self._capabilities.get("box"):
            return PlayerStatsResult(available=False, message="战绩模块尚未就绪（T3 src/stats/box_client.py）")
        module = _load_module("src.stats.box_client", CAPABILITY_MODULES["box"][1])
        kwargs = {
            # T3 BoxClient 实际接口：get_player_stats(account_id=..., nickname=...)
            "config": self.cfg,
            "cfg": self.cfg,
            "conn": self._thread_conn(),
            "account_id": matched.dbid,
            "dbid": matched.dbid,
            "pid": matched.dbid,
            "nickname": matched.nickname,
            "pn": matched.nickname,
            "force_refresh": force,
            "force": force,
        }
        client = self._make_box_client(module, kwargs)
        cache_get = getattr(client, "_cache_get_player_stats", None) if client is not None else None
        # 缓存口径由 stats 模块负责（默认同自然日命中）；手动刷新（force=True）跳过缓存。
        if not force and callable(cache_get) and matched.dbid:
            cached = _call_adaptive(cache_get, account_id=int(matched.dbid))
            if cached is not None:
                return PlayerStatsResult(
                    stats=cached,
                    available=True,
                    message="数据来自偶游盒子 index（最近 1500 场标准局口径 · 缓存命中，仅供参考、非官方数据）",
                )
        self._wait_box_rate_limit()
        raw = None
        if client is not None:
            for name in ("get_player_stats", "fetch_player_stats", "query_player_stats", "fetch_index"):
                func = getattr(client, name, None)
                if callable(func):
                    raw = _call_adaptive(func, **kwargs)
                    if raw is not None:
                        break
        if raw is None:
            for name in ("get_player_stats", "fetch_player_stats", "query_player_stats", "fetch_index"):
                func = getattr(module, name, None)
                if callable(func):
                    raw = _call_adaptive(func, **kwargs)
                    if raw is not None:
                        break
        if raw is None:
            return PlayerStatsResult(available=False, message="综合战绩暂不可用（接口请求失败或已限流，请稍后再试）")
        stats = self._normalize_player_stats(raw, matched)
        if stats is None:
            return PlayerStatsResult(available=False, message="综合战绩暂不可用（接口返回异常）")
        return PlayerStatsResult(
            stats=stats,
            available=True,
            message="数据来自偶游盒子 index（最近 1500 场标准局口径，仅供参考、非官方数据）",
        )

    def peek_player_stats_cache(self, matched: models.MatchedPlayer) -> Optional[PlayerStatsResult]:
        """只读本地缓存的玩家综合战绩（界面选中玩家时调用，**零网络**）。

        - 有缓存（即使跨天过期）→ 返回 ``PlayerStatsResult(available=True)``，message
          带缓存时间，提示点「刷新综合战绩」更新；
        - 无缓存 / 无 dbid / 战绩模块不可用 → 返回 ``None``；
        - 不 sleep、不发请求、不触发熔断；刷新由界面手动按钮走 ``get_player_stats(force=True)``。
        """
        if matched is None:
            return None
        account_id = getattr(matched, "dbid", None)
        if account_id is None:
            return None
        if self._mock:
            stats = mock_data.build_player_stats(matched)
            return PlayerStatsResult(
                stats=stats,
                available=True,
                message="演示数据（mock）：综合战绩缓存",
            )
        if not self._capabilities.get("box"):
            return None
        try:
            module = _load_module("src.stats.box_client", CAPABILITY_MODULES["box"][1])
            kwargs = {"config": self.cfg, "cfg": self.cfg, "conn": self._thread_conn()}
            client = self._make_box_client(module, kwargs)
            peek = getattr(client, "peek_player_stats", None) if client is not None else None
            if not callable(peek):
                return None
            stats = _call_adaptive(peek, account_id=int(account_id), dbid=int(account_id))
        except Exception:  # noqa: BLE001 - 只读缓存失败按无缓存处理，不影响界面
            return None
        if stats is None:
            return None
        cached_at = format_iso_text(getattr(stats, "fetched_at", None))
        return PlayerStatsResult(
            stats=stats,
            available=True,
            message="盒子缓存于 %s，点「刷新综合战绩」更新" % cached_at,
        )

    def _normalize_player_stats(self, raw: Any, matched: models.MatchedPlayer) -> Optional[models.PlayerStats]:
        """把综合战绩返回值归一为 PlayerStats。"""
        if isinstance(raw, models.PlayerStats):
            return raw
        if isinstance(raw, dict):
            known = {name for name in models.PlayerStats.__dataclass_fields__}
            payload = {key: value for key, value in raw.items() if key in known}
            payload.setdefault("account_id", int(matched.dbid or 0))
            return models.PlayerStats(**payload)
        return None

    # ------------------------------------------------------------ 生命周期
    def dispose(self) -> None:
        """关闭当前线程数据库连接（界面退出时调用）。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                pass
            self._local.conn = None
        self._local.ban_index = None

    def __del__(self) -> None:  # pragma: no cover - 析构兜底
        try:
            self.dispose()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 自检
def main(argv: list) -> int:
    """自检：mock 模式下跑通「状态 -> 扫描 -> 查询 -> 战绩」全流程。"""
    start = time.time()
    base_path = "."
    service = AppService(mock=True)
    print("[1/5] 门面初始化（base_path=%r）模式=%s" % (base_path, service.mode_label))
    status = service.get_data_status()
    print(
        "      数据状态：%d 批 / %d 条 / 最新 %s / wgid %s / 缺失 %s"
        % (status.batch_count, status.record_count, status.latest_batch_day,
           status.wgid_coverage_text, status.missing_text)
    )

    update = service.update_ban_list()
    print("[2/5] 更新名单：ok=%s message=%s" % (update.ok, update.message))

    replays = service.list_replays(mode="recent")
    print("[3/5] 扫描录像：%d 个" % len(replays))
    latest = latest_complete_replay(replays)
    print("      最新完整录像：%s" % replay_label(latest))

    query = service.query_replay(latest)
    print(
        "[4/5] 查询单局：玩家=%d 命中=%d（ID=%d 昵称=%d 黄=%d 红=%d）"
        % (query.summary["total"], query.summary["hits"], query.summary["id_hits"],
           query.summary["nickname_hits"], query.summary["yellow"], query.summary["red"])
    )
    battle = service.get_battle_stats(query.replay)
    print("      当局战绩：available=%s 人数=%d 胜负=%s 比分=%s 缓存=%s 提示=%s"
          % (battle.available, len(getattr(battle.stats, "players", []) or []),
             battle.outcome_label or "—", battle.score or "—", battle.cached, battle.message))
    hit = next(item for item in query.matches if item.is_hit)
    player_stats = service.get_player_stats(hit)
    print("      综合战绩：available=%s 战力=%s 胜率=%s 提示=%s"
          % (player_stats.available, getattr(player_stats.stats, "combat_power", None),
             getattr(player_stats.stats, "win_rate", None), player_stats.message))

    partial_replay = next(item for item in replays if item.is_partial)
    partial_query = service.query_replay(partial_replay)
    partial_battle = service.get_battle_stats(partial_query.replay)
    print(
        "[5/5] partial 局：玩家=%d 命中=%d 全部仅昵称匹配=%s；当局战绩 available=%s 胜负=%s（%s）"
        % (partial_query.summary["total"], partial_query.summary["hits"],
           all(item.partial for item in partial_query.matches if item.is_hit),
           partial_battle.available, partial_battle.outcome_label or "—",
           partial_battle.message)
    )
    service.dispose()
    print("[done] service mock 全流程自检通过，耗时 %.2f 秒" % (time.time() - start))
    return 0


if __name__ == "__main__":
    import sys

    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
