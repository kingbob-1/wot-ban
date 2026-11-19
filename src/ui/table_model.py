# -*- coding: utf-8 -*-
"""结果表数据模型：单局参战玩家 + 封禁匹配 + 盒子同款当局战绩列。

设计
----
- ``QAbstractTableModel`` + 视图排序（表头点击 -> ``sort()``），30 人 / 60 人
  均可扩展；
- **列契约（顺序固定、标题逐字）**：删除旧「阵营」「命中率」列，按盒子明细顺序
  重排为 24 列（昵称 … 行驶里程），见 ``COLUMNS``；
- **红绿方分组**：绿方 = 录像者本人所在队（由 ``ReplayDetail.recorder_dbid /
  recorder_name`` 在 players 中反查，**不写死 team==1**），绿方组在上；两组之间
  插入组头行（视图跨列合并，不参与排序）；60 人对局同样两组；
- **默认排序（组内）**：封禁次数降序 -> 本局战力降序 -> 伤害降序（None 置后）；
  表头点击只在组内改排序，组头 / 组顺序不动；
- **本人（录像者）行高亮**：录像者身份与 ``resolve_recorder_team`` 同源
  （``ReplayDetail.recorder_dbid`` 优先、``recorder_name`` 兜底），本人行加浅蓝底
  （``theme.COLOR_RECORDER_BG``，复用既有颜色）、昵称列加粗；封禁黄 / 红底色优先，
  不覆盖封禁标色；
- 黄底（1 次封禁）/ 红底（≥2 次封禁）由 ``BackgroundRole`` 提供；组头行用
  绿 / 红浅底 + 加粗；
- 当局战绩列由 ``set_stats()`` 填充，缺失字段显示「—」，加载中显示「…」；
- 「仅看 ID 命中」「只看命中」「黄/红筛选」在模型内过滤（模型行 = 可见行）；
- **不提供任何 ToolTipRole**（第二轮反馈：查询结果区域全部悬停提示已删除）。
"""
from __future__ import annotations

import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import cmp_to_key
from pathlib import Path
from typing import Any, Optional

try:
    from PySide6.QtCore import QAbstractTableModel, Qt
    from PySide6.QtGui import QColor, QFont
except ImportError:  # 便于无 PySide6 环境下静态检查
    QAbstractTableModel = object  # type: ignore[assignment]
    QColor = QFont = None  # type: ignore[assignment]
    Qt = None  # type: ignore[assignment]

try:
    from .. import models
    from . import theme
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src import models
    from src.ui import theme

# 列定义：key / 表头 / 默认宽度 / 对齐。
# 宽度按 1920×1080 默认窗口（右侧明细约 26%）压缩：7.5pt 字体下 24 列表头逐字完整、
# 横向不出现滚动条；行高 24px 保证 30 人（+2 组头）一屏完整显示（需求 6）。
COLUMNS = [
    {"key": "nickname", "title": "昵称", "width": 80, "align": "left"},
    {"key": "dbid", "title": "账号 ID", "width": 72, "align": "left"},
    {"key": "clan_abbrev", "title": "军团缩写", "width": 46, "align": "center"},
    {"key": "ban_count", "title": "封禁次数", "width": 46, "align": "center"},
    {"key": "latest_ban_day", "title": "最近封禁日期", "width": 68, "align": "center"},
    {"key": "reason", "title": "原因", "width": 56, "align": "left"},
    {"key": "duration", "title": "时长", "width": 34, "align": "center"},
    {"key": "match_type", "title": "匹配方式", "width": 56, "align": "center"},
    {"key": "vehicle", "title": "车型", "width": 66, "align": "left"},
    {"key": "combat", "title": "本局战力", "width": 46, "align": "right"},
    {"key": "xp", "title": "经验值", "width": 36, "align": "right"},
    {"key": "shots", "title": "射击", "width": 28, "align": "right"},
    {"key": "piercings", "title": "击穿", "width": 28, "align": "right"},
    {"key": "damage", "title": "造成伤害", "width": 46, "align": "right"},
    {"key": "hits_received", "title": "被击中", "width": 36, "align": "right"},
    {"key": "potential_damage", "title": "受到的潜在伤害", "width": 72, "align": "right"},
    {"key": "damage_blocked", "title": "装甲抵消伤害", "width": 62, "align": "right"},
    {"key": "spotted", "title": "发现敌人", "width": 46, "align": "right"},
    {"key": "vehicles_damaged", "title": "击伤敌方战车", "width": 62, "align": "right"},
    {"key": "kills", "title": "击毁敌方战车", "width": 62, "align": "right"},
    {"key": "assisted_damage", "title": "协助盟友造成伤害", "width": 80, "align": "right"},
    {"key": "capture_points", "title": "占领敌方基地点数", "width": 80, "align": "right"},
    {"key": "defense_points", "title": "保卫基地防御点数", "width": 80, "align": "right"},
    {"key": "mileage", "title": "行驶里程", "width": 46, "align": "right"},
]
COLUMN_INDEX = {item["key"]: index for index, item in enumerate(COLUMNS)}

# 需要当局战绩数据的列（用于排序取值）。
STATS_KEYS = (
    "combat", "xp", "shots", "piercings", "damage", "hits_received",
    "potential_damage", "damage_blocked", "spotted", "vehicles_damaged",
    "kills", "assisted_damage", "capture_points", "defense_points", "mileage",
)

# 当局战绩列在 BattlePlayerStats 中的字段名（vehicle / combat 单独处理）。
_STATS_FIELD_KEYS = set(STATS_KEYS) - {"combat"}

STATS_STATE_IDLE = "idle"
STATS_STATE_LOADING = "loading"
STATS_STATE_READY = "ready"
STATS_STATE_UNAVAILABLE = "unavailable"

# 分组：绿方 = 录像者本人所在队（我方），红方 = 敌方。
GROUP_GREEN = "green"
GROUP_RED = "red"
GROUP_LABELS = {
    GROUP_GREEN: "绿方 · 我方",
    GROUP_RED: "红方 · 敌方",
}

# 默认排序链：封禁次数降序 -> 本局战力降序 -> 伤害降序（None 置后）。
DEFAULT_SORT_CHAIN = (
    ("ban_count", False),
    ("combat", False),
    ("damage", False),
)

MATCH_LABEL_ID = "ID 确认"
MATCH_LABEL_NICKNAME = "昵称匹配"
MATCH_LABEL_PARTIAL = "仅昵称匹配"
MATCH_LABEL_NONE = "—"


# ---------------------------------------------------------------- 格式化工具
def _format_number(value: Any) -> str:
    """整数加千分位；None / 非数字 -> 占位符 / 原值。"""
    if value is None:
        return "—"
    try:
        return "{:,}".format(int(round(float(value))))
    except (TypeError, ValueError):
        return str(value)


def format_combat_int(value: Any) -> str:
    """本局战力：四舍五入取整（half-up，868.46 -> 868、216.3 -> 216）。

    使用 ``Decimal(str(value))`` 做十进制 half-up，避免二进制浮点在 .5 边界上的
    舍入偏差；非数字返回原值字符串，None 返回「—」。
    """
    if value is None:
        return "—"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    try:
        rounded = number.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return str(value)
    try:
        return "{:,}".format(int(rounded))
    except (ValueError, OverflowError):
        return str(rounded)


def _stat_value(stats: Any, name: str) -> Any:
    """取当局战绩对象（dataclass / dict）的字段值；缺失返回 None。"""
    if stats is None:
        return None
    if isinstance(stats, dict):
        return stats.get(name)
    return getattr(stats, name, None)


def _game_names_module() -> Any:
    """惰性导入 ``src.game_names``（由 coder-names 交付）；缺失返回 None，不抛异常。"""
    try:
        from .. import game_names  # type: ignore[attr-defined]

        return game_names
    except Exception:  # noqa: BLE001 - 模块未交付 / 导入失败时优雅降级
        try:
            from src import game_names  # type: ignore[attr-defined]

            return game_names
        except Exception:  # noqa: BLE001
            return None


def _game_vehicle_name(tank_code: Any) -> Optional[str]:
    """录像 tank 代码 -> 游戏中文车名；映射模块缺失 / 无结果返回 None。"""
    if not tank_code:
        return None
    module = _game_names_module()
    if module is None:
        return None
    try:
        return module.vehicle_name(str(tank_code))
    except Exception:  # noqa: BLE001 - 映射失败不得影响界面
        return None


def vehicle_code(player: Any, stats: Any = None) -> str:
    """车型原始代码：优先战绩 ``tank_code``，其次玩家录像 tank 字段。"""
    code = _stat_value(stats, "tank_code")
    if code:
        return str(code)
    tank = getattr(player, "tank", None)
    return str(tank) if tank else ""


def vehicle_display(player: Any, stats: Any = None) -> str:
    """车型展示名：盒子 tank_title -> src.game_names.vehicle_name(录像 tank) -> 原始值/—。"""
    title = _stat_value(stats, "tank")
    if title:
        return str(title)
    raw = getattr(player, "tank", None)
    if not raw:
        return "—"
    name = _game_vehicle_name(raw)
    if name:
        return str(name)
    return str(raw)


def team_display(team: Any, recorder_team: Any = None) -> str:
    """阵营展示（绿方 = 录像者本人所在队）：绿方 · 我方 / 红方 · 敌方 / —。"""
    if team is None:
        return "—"
    if recorder_team is None:
        return "—"
    try:
        if int(team) == int(recorder_team):
            return "绿方 · 我方"
        return "红方 · 敌方"
    except (TypeError, ValueError):
        return "—"


def _stats_key(player: models.MatchedPlayer) -> tuple:
    """当局战绩按 dbid 优先、昵称兜底对齐。"""
    if player.dbid is not None:
        return ("id", int(player.dbid))
    return ("nick", player.nickname or "")


def match_label(player: models.MatchedPlayer) -> str:
    """匹配方式展示文案（partial 标注「仅昵称匹配」）。"""
    if player.match_type == models.MATCH_TYPE_ID:
        return MATCH_LABEL_ID
    if player.match_type == models.MATCH_TYPE_NICKNAME:
        return MATCH_LABEL_PARTIAL if player.partial else MATCH_LABEL_NICKNAME
    return MATCH_LABEL_NONE


def _match_rank(player: models.MatchedPlayer) -> int:
    """匹配方式排序权重：ID 确认置前，昵称匹配其次，未命中最后。"""
    if player.match_type == models.MATCH_TYPE_ID:
        return 0
    if player.match_type == models.MATCH_TYPE_NICKNAME:
        return 1
    return 2


def resolve_recorder_identity(detail: Any) -> tuple:
    """录像者身份（dbid 优先、昵称兜底），与 ``resolve_recorder_team`` 同源。

    返回 ``(recorder_dbid, recorder_name)``；detail 为空时返回 ``(None, None)``。
    界面「本人行」高亮与绿方分组共用这套身份数据。
    """
    if detail is None:
        return None, None
    return getattr(detail, "recorder_dbid", None), getattr(detail, "recorder_name", None)


def resolve_recorder_team(detail: Any, players: Optional[list] = None) -> Optional[int]:
    """反查录像者本人所在队（绿方）。

    优先用 ``ReplayDetail.recorder_dbid`` 精确匹配，其次用 ``recorder_name``
    匹配昵称；都不行时按 players 中出现的队伍取第一支（仅作兜底，正常录像
    一定能通过 recorder 信息反查到，不写死 team==1）。
    """
    rows = list(players if players is not None else (getattr(detail, "players", None) or []))
    if detail is None:
        return None
    recorder_dbid, recorder_name = resolve_recorder_identity(detail)
    if recorder_dbid is not None:
        for player in rows:
            dbid = getattr(player, "dbid", None)
            if dbid is not None and int(dbid) == int(recorder_dbid):
                team = getattr(player, "team", None)
                if team is not None:
                    return int(team)
    if recorder_name:
        for player in rows:
            if getattr(player, "nickname", None) == recorder_name:
                team = getattr(player, "team", None)
                if team is not None:
                    return int(team)
    teams = sorted({
        int(getattr(player, "team")) for player in rows
        if getattr(player, "team", None) is not None
    })
    return teams[0] if teams else None


# ---------------------------------------------------------------- 排序工具
def _sort_value(player: models.MatchedPlayer, column: int, stats_map: dict) -> Any:
    """取某列的排序原始值；无值返回 None（排序时排最后）。"""
    key = COLUMNS[column]["key"]
    if key == "nickname":
        return (player.nickname or "").lower()
    if key == "dbid":
        return player.dbid
    if key == "clan_abbrev":
        return (player.clan_abbrev or "").lower() or None
    if key == "ban_count":
        return int(player.ban_count or 0)
    if key == "latest_ban_day":
        return player.latest_ban_day
    if key == "reason":
        return (player.records[0].reason or "") if player.records else None
    if key == "duration":
        return (player.records[0].duration or "") if player.records else None
    if key == "match_type":
        return _match_rank(player)
    if key == "vehicle":
        stats = stats_map.get(_stats_key(player))
        return vehicle_display(player, stats).lower()
    stats = stats_map.get(_stats_key(player))
    if key == "combat":
        return _stat_value(stats, "combat")
    if key in _STATS_FIELD_KEYS:
        return _stat_value(stats, key)
    return None


def _compare_values(value_a: Any, value_b: Any, ascending: bool) -> int:
    """比较两个排序值：None 一律置后；其余按升降序比较。"""
    if value_a is None and value_b is None:
        return 0
    if value_a is None:
        return 1
    if value_b is None:
        return -1
    try:
        comparison = (value_a > value_b) - (value_a < value_b)
    except TypeError:  # 混合类型（理论上不会出现）按字符串比较
        text_a, text_b = str(value_a), str(value_b)
        comparison = (text_a > text_b) - (text_a < text_b)
    if comparison == 0:
        return 0
    return comparison if ascending else -comparison


def compare_players(first: models.MatchedPlayer, second: models.MatchedPlayer,
                    chain: Any = DEFAULT_SORT_CHAIN,
                    stats_map: Optional[dict] = None) -> int:
    """按排序链比较两名玩家（组内排序用），最后以昵称升序做稳定兜底。"""
    stats_map = stats_map or {}
    for column_key, ascending in chain:
        column = COLUMN_INDEX[column_key]
        comparison = _compare_values(
            _sort_value(first, column, stats_map),
            _sort_value(second, column, stats_map),
            bool(ascending),
        )
        if comparison:
            return comparison
    name_a = (first.nickname or "").lower()
    name_b = (second.nickname or "").lower()
    return (name_a > name_b) - (name_a < name_b)


class PlayerTableModel(QAbstractTableModel):
    """结果表模型（行 = 组头行 / 单局参战玩家行）。

    组头行由 ``GROUP_GREEN`` / ``GROUP_RED`` 标记，``row_object()`` 返回 None；
    视图通过 ``is_group_row()`` 判定后做跨列合并（见 main_window）。
    """

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._all_rows: list = []
        self._entries: list = []
        self._stats_state = STATS_STATE_IDLE
        self._stats_map: dict = {}
        self._stats_message = ""
        self._filters = {"id_only": False, "hits_only": False, "level": "all"}
        self._sort_chain = tuple(DEFAULT_SORT_CHAIN)
        self._user_sorted = False
        self._recorder_team: Optional[int] = None
        self._recorder_dbid: Any = None
        self._recorder_name: Any = None

    # ------------------------------------------------------------ Qt 接口
    def rowCount(self, parent: Any = None) -> int:  # noqa: N802 - Qt 命名
        if parent is not None and getattr(parent, "isValid", lambda: False)():
            return 0
        return len(self._entries)

    def columnCount(self, parent: Any = None) -> int:  # noqa: N802 - Qt 命名
        if parent is not None and getattr(parent, "isValid", lambda: False)():
            return 0
        return len(COLUMNS)

    def headerData(self, section: int, orientation: Any, role: int = 0) -> Any:  # noqa: N802
        if role != (Qt.ItemDataRole.DisplayRole if Qt is not None else 0):
            return None
        if orientation == (Qt.Orientation.Horizontal if Qt is not None else 1):
            if 0 <= section < len(COLUMNS):
                return COLUMNS[section]["title"]
            return None
        return str(section + 1)

    def data(self, index: Any, role: int = 0) -> Any:
        if not getattr(index, "isValid", lambda: False)():
            return None
        row = index.row()
        column = index.column()
        if not (0 <= row < len(self._entries) and 0 <= column < len(COLUMNS)):
            return None
        entry = self._entries[row]
        display_role = Qt.ItemDataRole.DisplayRole if Qt is not None else 0
        tooltip_role = Qt.ItemDataRole.ToolTipRole if Qt is not None else 3
        background_role = Qt.ItemDataRole.BackgroundRole if Qt is not None else 9
        foreground_role = Qt.ItemDataRole.ForegroundRole if Qt is not None else 10
        font_role = Qt.ItemDataRole.FontRole if Qt is not None else 11
        alignment_role = Qt.ItemDataRole.TextAlignmentRole if Qt is not None else 7

        if role == tooltip_role:
            # 第二轮用户反馈：查询结果区域（含所有列）一律不提供鼠标悬停提示。
            return ""
        if entry[0] == "group":
            return self._group_data(entry, column, role,
                                    display_role, background_role, foreground_role,
                                    font_role, alignment_role)
        player = entry[1]
        key = COLUMNS[column]["key"]
        if role == display_role:
            return self._display_value(player, key)
        if role == background_role:
            return self._background(player)
        if role == foreground_role:
            return self._foreground(player, key)
        if role == font_role:
            return self._font(player, key)
        if role == alignment_role:
            return self._alignment(column)
        return None

    def _group_data(self, entry: tuple, column: int, role: int,
                    display_role: int, background_role: int, foreground_role: int,
                    font_role: int, alignment_role: int) -> Any:
        """组头行：第 0 列显示组名（视图跨列合并），整行浅底 + 加粗。"""
        kind = entry[1]
        if role == display_role:
            return entry[2] if column == 0 else ""
        if role == background_role:
            if QColor is None:
                return None
            return QColor("#e8f7ee") if kind == GROUP_GREEN else QColor("#fdecec")
        if role == foreground_role:
            if QColor is None:
                return None
            return QColor("#166534") if kind == GROUP_GREEN else QColor("#991b1b")
        if role == font_role:
            if QFont is None:
                return None
            font = QFont()
            font.setBold(True)
            return font
        if role == alignment_role:
            if Qt is None:
                return None
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        return None

    def flags(self, index: Any) -> Any:
        if Qt is None:
            return 0
        if not getattr(index, "isValid", lambda: False)():
            return Qt.ItemFlag.NoItemFlags
        row = index.row()
        if 0 <= row < len(self._entries) and self._entries[row][0] == "group":
            # 组头行：可显示但不可选中（不参与排序 / 明细联动）。
            return Qt.ItemFlag.ItemIsEnabled
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ------------------------------------------------------------ 数据填充
    def set_rows(self, rows: list, replay: Any = None,
                 recorder_team: Optional[int] = None) -> None:
        """替换全部玩家行；``replay`` 用于反查录像者身份 / 所在队并做红绿分组。"""
        self._all_rows = list(rows or [])
        self._user_sorted = False
        self._sort_chain = tuple(DEFAULT_SORT_CHAIN)
        self._recorder_dbid, self._recorder_name = resolve_recorder_identity(replay)
        if recorder_team is not None:
            self._recorder_team = int(recorder_team)
        else:
            self._recorder_team = resolve_recorder_team(replay, self._all_rows)
        self._rebuild()

    def set_stats(self, state: str, stats_map: Optional[dict] = None, message: str = "") -> None:
        """设置当局战绩状态（idle / loading / ready / unavailable）与数据。

        战绩异步到达后：默认排序的次级键（本局战力 / 伤害）才可用，因此未手动
        排序时重建一次让默认排序生效；手动排序时仅刷新显示值。
        """
        self._stats_state = state
        self._stats_map = dict(stats_map or {})
        self._stats_message = message
        if not self._entries:
            return
        if not self._user_sorted:
            self._rebuild()
            return
        self._emit_data_changed()

    def stats_message(self) -> str:
        return self._stats_message

    def apply_filters(self, id_only: bool = False, hits_only: bool = False, level: str = "all") -> None:
        """应用筛选并重建可见行。"""
        self._filters = {"id_only": bool(id_only), "hits_only": bool(hits_only), "level": level or "all"}
        self._rebuild()

    def sort(self, column: int, order: Any = None) -> None:  # noqa: N802 - Qt 命名
        """表头点击排序：只在组内改排序，组头 / 组顺序不动。"""
        if not (0 <= column < len(COLUMNS)):
            return
        ascending = order == (Qt.SortOrder.AscendingOrder if Qt is not None else 0)
        self._sort_chain = ((COLUMNS[column]["key"], bool(ascending)),)
        self._user_sorted = True
        self._rebuild()

    def reset_sort(self) -> None:
        """恢复默认排序（封禁次数降序 -> 本局战力降序 -> 伤害降序）。"""
        self._sort_chain = tuple(DEFAULT_SORT_CHAIN)
        self._user_sorted = False
        self._rebuild()

    # ------------------------------------------------------------ 查询接口
    def recorder_team(self) -> Optional[int]:
        """录像者本人所在队（绿方）；无法识别时返回 None。"""
        return self._recorder_team

    def recorder_identity(self) -> tuple:
        """录像者身份 ``(dbid, name)``（与绿方分组 / 本人行高亮同源）。"""
        return self._recorder_dbid, self._recorder_name

    def is_recorder(self, player: Any) -> bool:
        """该玩家是否录像者本人（dbid 优先、昵称兜底；与绿方分组同源）。"""
        if player is None:
            return False
        dbid = getattr(player, "dbid", None)
        if self._recorder_dbid is not None and dbid is not None:
            try:
                return int(dbid) == int(self._recorder_dbid)
            except (TypeError, ValueError):
                return False
        name = getattr(player, "nickname", None)
        return bool(self._recorder_name) and name == self._recorder_name

    def is_recorder_row(self, row: int) -> bool:
        """该模型行是否为录像者本人（组头行返回 False）。"""
        return self.is_recorder(self.row_object(row))

    def is_group_row(self, row: int) -> bool:
        """该模型行是否为组头行。"""
        return 0 <= row < len(self._entries) and self._entries[row][0] == "group"

    def group_kind(self, row: int) -> Optional[str]:
        """组头行类型（GROUP_GREEN / GROUP_RED）；非组头返回 None。"""
        if self.is_group_row(row):
            return self._entries[row][1]
        return None

    def group_label(self, row: int) -> str:
        """组头行完整文案（含人数）；非组头返回空串。"""
        if self.is_group_row(row):
            return self._entries[row][2]
        return ""

    def row_object(self, row: int) -> Optional[models.MatchedPlayer]:
        """取模型行对应的玩家对象（组头行返回 None）。"""
        if 0 <= row < len(self._entries) and self._entries[row][0] == "player":
            return self._entries[row][1]
        return None

    def player_row_count(self) -> int:
        """当前可见的玩家行数（不含组头行）。"""
        return sum(1 for entry in self._entries if entry[0] == "player")

    def total_player_count(self) -> int:
        """全部玩家数（不受筛选影响）。"""
        return len(self._all_rows)

    def first_player_row(self) -> int:
        """第一行玩家行的模型行号；没有玩家返回 -1。"""
        for row, entry in enumerate(self._entries):
            if entry[0] == "player":
                return row
        return -1

    def player_rows(self) -> list:
        """按当前显示顺序返回玩家行号列表（不含组头行）。"""
        return [row for row, entry in enumerate(self._entries) if entry[0] == "player"]

    def row_for_player(self, player: Any) -> int:
        """定位某玩家对象在当前可见行中的模型行号；不可见返回 -1。"""
        if player is None:
            return -1
        for row, entry in enumerate(self._entries):
            if entry[0] != "player":
                continue
            candidate = entry[1]
            if candidate is player:
                return row
            if (getattr(candidate, "dbid", None) is not None
                    and getattr(candidate, "dbid", None) == getattr(player, "dbid", None)):
                return row
            if (getattr(candidate, "nickname", None) == getattr(player, "nickname", None)
                    and getattr(candidate, "team", None) == getattr(player, "team", None)):
                return row
        return -1

    def visible_rows(self) -> list:
        """当前可见玩家对象列表（按显示顺序，不含组头行）。"""
        return [entry[1] for entry in self._entries if entry[0] == "player"]

    def total_rows(self) -> list:
        """全部玩家对象列表（不受筛选影响）。"""
        return list(self._all_rows)

    def stats_for(self, player: models.MatchedPlayer) -> Any:
        """取某玩家的当局战绩对象（无则 None）。"""
        return self._stats_map.get(_stats_key(player))

    def stats_state(self) -> str:
        return self._stats_state

    def stats_map(self) -> dict:
        """当局战绩映射（键：("id", dbid) / ("nick", 昵称)）。"""
        return dict(self._stats_map)

    # ------------------------------------------------------------ 内部实现
    def _rebuild(self) -> None:
        """按筛选条件、红绿分组与组内排序规则重建可见行。"""
        self.beginResetModel()
        players = list(self._all_rows)
        filters = self._filters
        if filters["id_only"]:
            players = [item for item in players if item.match_type == models.MATCH_TYPE_ID]
        if filters["hits_only"]:
            players = [item for item in players if item.is_hit]
        if filters["level"] == "red":
            players = [item for item in players if item.level == models.LEVEL_RED]
        elif filters["level"] == "yellow":
            players = [item for item in players if item.level == models.LEVEL_YELLOW]

        comparator = cmp_to_key(
            lambda first, second: compare_players(
                first, second, chain=self._sort_chain, stats_map=self._stats_map
            )
        )
        entries = []
        for kind, group_players in self._split_groups(players):
            if not group_players:
                continue
            group_players.sort(key=comparator)
            entries.append(("group", kind, "%s（%d 人）" % (GROUP_LABELS[kind], len(group_players))))
            entries.extend(("player", player) for player in group_players)
        self._entries = entries
        self.endResetModel()

    def _split_groups(self, players: list) -> list:
        """按绿方（录像者队）在上、红方在下拆分玩家；无队伍信息时全部归绿方。"""
        green_team = self._recorder_team
        green: list = []
        red: list = []
        for player in players:
            team = getattr(player, "team", None)
            if green_team is None or team is None:
                green.append(player)
            elif int(team) == int(green_team):
                green.append(player)
            else:
                red.append(player)
        return [(GROUP_GREEN, green), (GROUP_RED, red)]

    def _emit_data_changed(self) -> None:
        """整表刷新显示值（不重建行结构）。"""
        if not self._entries:
            return
        top = self.index(0, 0)
        bottom = self.index(len(self._entries) - 1, len(COLUMNS) - 1)
        self.dataChanged.emit(top, bottom)

    def _display_value(self, player: models.MatchedPlayer, key: str) -> str:
        if key == "nickname":
            return player.nickname or "—"
        if key == "dbid":
            return str(player.dbid) if player.dbid else "—"
        if key == "clan_abbrev":
            return player.clan_abbrev or "—"
        if key == "ban_count":
            return "%d 次" % int(player.ban_count or 0) if player.is_hit else "—"
        if key == "latest_ban_day":
            return player.latest_ban_day or "—"
        if key == "reason":
            return (player.records[0].reason or "—") if player.records else "—"
        if key == "duration":
            return (player.records[0].duration or "—") if player.records else "—"
        if key == "match_type":
            return match_label(player)
        stats = self._stats_map.get(_stats_key(player))
        if key == "vehicle":
            # 车型：盒子 tank_title 优先；盒子不可用时用 game_names 把录像 tank
            # 代码翻成中文车名；映射模块缺失时显示原始值（优雅降级）。
            return vehicle_display(player, stats)
        if self._stats_state == STATS_STATE_LOADING and stats is None:
            return "…"
        if stats is None:
            return "—"
        if key == "combat":
            return format_combat_int(_stat_value(stats, "combat"))
        if key in _STATS_FIELD_KEYS:
            return _format_number(_stat_value(stats, key))
        return "—"

    def _background(self, player: models.MatchedPlayer) -> Any:
        if QColor is None:
            return None
        # 封禁黄 / 红标底色优先级最高（本人行高亮不得盖掉封禁标色）。
        if player.level == models.LEVEL_RED:
            return QColor(theme.COLOR_RED_BG)
        if player.level == models.LEVEL_YELLOW:
            return QColor(theme.COLOR_YELLOW_BG)
        # 本人（录像者）行：复用主题既有浅蓝，非封禁行才显示。
        if self.is_recorder(player):
            return QColor(theme.COLOR_RECORDER_BG)
        return None

    def _foreground(self, player: models.MatchedPlayer, key: str) -> Any:
        if QColor is None or not player.is_hit:
            return None
        if key in ("ban_count", "match_type"):
            return QColor(theme.COLOR_RED_TEXT) if player.level == models.LEVEL_RED \
                else QColor(theme.COLOR_YELLOW_TEXT)
        return None

    def _font(self, player: models.MatchedPlayer, key: str) -> Any:
        if QFont is None:
            return None
        # 本人行昵称加粗（与封禁命中的加粗逻辑并存）。
        if key == "nickname" and self.is_recorder(player):
            font = QFont()
            font.setBold(True)
            return font
        if not player.is_hit:
            return None
        if key in ("ban_count", "match_type"):
            font = QFont()
            font.setBold(True)
            return font
        return None

    def _alignment(self, column: int) -> Any:
        if Qt is None:
            return None
        align = COLUMNS[column]["align"]
        if align == "center":
            return Qt.AlignmentFlag.AlignCenter
        if align == "right":
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


def main(argv: list) -> int:
    """自检：不创建 QApplication，直接验证列契约 / 分组 / 排序 / 展示文案。"""
    import time

    start = time.time()
    base_path = "."
    print("[1/4] 结果表模型自检（base_path=%r）列数=%d" % (base_path, len(COLUMNS)))
    print("      列标题=%s" % "、".join(column["title"] for column in COLUMNS))
    from src.app import mock_data

    detail = mock_data.build_detail(mock_data.build_replays()[0])
    matches = mock_data.build_matches(detail)
    model = PlayerTableModel()
    model.set_rows(matches, detail)
    print("      玩家=%d 模型行=%d 绿方=%s 组头=%s"
          % (model.total_player_count(), model.rowCount(), model.recorder_team(),
             [model.group_label(row) for row in range(model.rowCount()) if model.is_group_row(row)]))
    first = model.row_object(model.first_player_row())
    print("[2/4] 默认排序首行=%s（次数=%d）" % (first.nickname, first.ban_count))
    stats = mock_data.build_battle_stats(detail)
    stats_map = {("id", item.dbid): item for item in stats.players}
    model.set_stats(STATS_STATE_READY, stats_map)
    combat_column = COLUMN_INDEX["combat"]
    print("[3/4] 本局战力取整示例：%s（原值 %s）"
          % (model.data(model.index(model.first_player_row(), combat_column),
                        Qt.ItemDataRole.DisplayRole if Qt else 0),
             getattr(stats.players[0], "combat", None)))
    model.apply_filters(id_only=True)
    print("[4/4] 仅看 ID 命中：玩家 %d 行 / 模型 %d 行，全部 ID 匹配=%s；耗时 %.2f 秒"
          % (model.player_row_count(), model.rowCount(),
             all(item.match_type == models.MATCH_TYPE_ID for item in model.visible_rows()),
             time.time() - start))
    print("[done] table_model self-check passed")
    return 0


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
