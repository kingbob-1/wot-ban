# -*- coding: utf-8 -*-
"""结果表数据模型：单局参战玩家 + 封禁匹配 + 当局战绩列。

设计
----
- ``QAbstractTableModel`` + 视图排序（表头点击 -> ``sort()``），30 人规模但保持
  可扩展到 60 人 / 多局；
- 黄底（1 次封禁）/ 红底（≥2 次封禁）由 ``BackgroundRole`` 提供，匹配方式标签
  文案含「ID 确认 / 昵称匹配 / 仅昵称匹配」；
- 默认排序：封禁次数降序 -> 同次数按最近封禁日期降序 -> ID 确认置前（A4 + §6.6）；
- 「仅看 ID 命中」「只看命中」「黄/红筛选」在模型内过滤（模型行 = 可见行）；
- 当局战绩列由 ``set_stats()`` 填充，带「加载中 / 不可用」占位。
"""
from __future__ import annotations

import sys
from functools import cmp_to_key, partial
from pathlib import Path
from typing import Any, Optional

try:
    from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
    from PySide6.QtGui import QColor, QFont
except ImportError:  # 便于无 PySide6 环境下静态检查
    QAbstractTableModel = object  # type: ignore[assignment]
    QModelIndex = object  # type: ignore[assignment]
    QColor = QFont = None  # type: ignore[assignment]
    Qt = None  # type: ignore[assignment]

try:
    from .. import models
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src import models

# 列定义：key / 表头 / 默认宽度 / 对齐 / 是否当局战绩列。
# 宽度按 1600 逻辑像素窗口（4K 150% 缩放）内可完整显示 14 列来压缩。
COLUMNS = [
    {"key": "team", "title": "阵营", "width": 46, "align": "center"},
    {"key": "nickname", "title": "昵称", "width": 138, "align": "left"},
    {"key": "dbid", "title": "账号 ID", "width": 104, "align": "left"},
    {"key": "clan_abbrev", "title": "军团缩写", "width": 70, "align": "center"},
    {"key": "ban_count", "title": "封禁次数", "width": 74, "align": "center"},
    {"key": "latest_ban_day", "title": "最近封禁日期", "width": 98, "align": "center"},
    {"key": "reason", "title": "原因", "width": 112, "align": "left"},
    {"key": "duration", "title": "时长", "width": 58, "align": "center"},
    {"key": "match_type", "title": "匹配方式", "width": 94, "align": "center"},
    {"key": "combat", "title": "本局战力", "width": 70, "align": "right"},
    {"key": "damage", "title": "伤害", "width": 64, "align": "right"},
    {"key": "kills", "title": "击杀", "width": 48, "align": "right"},
    {"key": "xp", "title": "经验", "width": 56, "align": "right"},
    {"key": "hit_rate", "title": "命中率", "width": 62, "align": "right"},
]
COLUMN_INDEX = {item["key"]: index for index, item in enumerate(COLUMNS)}
STATS_KEYS = ("combat", "damage", "kills", "xp", "hit_rate")
STATS_STATE_IDLE = "idle"
STATS_STATE_LOADING = "loading"
STATS_STATE_READY = "ready"
STATS_STATE_UNAVAILABLE = "unavailable"

MATCH_LABEL_ID = "ID 确认"
MATCH_LABEL_NICKNAME = "昵称匹配"
MATCH_LABEL_PARTIAL = "仅昵称匹配"
MATCH_LABEL_NONE = "—"

TOOLTIPS = {
    MATCH_LABEL_ID: "完整录像账号 ID（dbid）与名单 wgid 精确匹配，可信度最高",
    MATCH_LABEL_NICKNAME: "名单记录无账号 ID：按「同昵称视为同一人」归集（A2），可能受改名影响",
    MATCH_LABEL_PARTIAL: "partial 录像（未打完）没有其他玩家账号 ID，只能按昵称匹配（A2/Q4，计入黄红）",
    MATCH_LABEL_NONE: "未命中任何历史封禁记录",
}


def _format_number(value: Any) -> str:
    """整数加千分位；None -> 占位符。"""
    if value is None:
        return "—"
    try:
        return "{:,}".format(int(round(float(value))))
    except (TypeError, ValueError):
        return str(value)


def _format_combat(value: Any) -> str:
    """单局战力（盒子口径，可能是小数）。"""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number - round(number)) < 0.05:
        return "{:,}".format(int(round(number)))
    return "{:,.1f}".format(number)


def _stats_key(player: models.MatchedPlayer) -> tuple:
    """当局战绩按 dbid 优先、昵称兜底对齐。"""
    if player.dbid is not None:
        return ("id", int(player.dbid))
    return ("nick", player.nickname or "")


def _hit_rate(player: models.MatchedPlayer, stats_map: dict) -> Optional[float]:
    """命中率 = 击穿 / 射击（百分比）；无数据返回 None。"""
    stats = stats_map.get(_stats_key(player))
    if stats is None:
        return None
    shots = getattr(stats, "shots", None)
    piercings = getattr(stats, "piercings", None)
    if not shots:
        return None
    return float(piercings or 0) * 100.0 / float(shots)


def _match_rank(player: models.MatchedPlayer) -> int:
    """匹配方式排序权重：ID 确认置前，昵称匹配其次，未命中最后。"""
    if player.match_type == models.MATCH_TYPE_ID:
        return 0
    if player.match_type == models.MATCH_TYPE_NICKNAME:
        return 1
    return 2


def _sort_value(player: models.MatchedPlayer, column: int, stats_map: dict) -> Any:
    """取某列的排序原始值；无值返回 None（排序时排最后）。"""
    key = COLUMNS[column]["key"]
    if key == "team":
        return player.team
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
    stats = stats_map.get(_stats_key(player))
    if stats is None:
        return None
    if key == "combat":
        return stats.combat
    if key == "damage":
        return stats.damage
    if key == "kills":
        return stats.kills
    if key == "xp":
        return stats.xp
    if key == "hit_rate":
        return _hit_rate(player, stats_map)
    return None


def _compare_players(first: models.MatchedPlayer, second: models.MatchedPlayer,
                     column: int = COLUMN_INDEX["ban_count"], ascending: bool = False,
                     stats_map: Optional[dict] = None) -> int:
    """比较两名玩家：主排序键 + 封禁次数降序 + ID 确认置前 + 最近日期降序。"""
    stats_map = stats_map or {}
    value_a = _sort_value(first, column, stats_map)
    value_b = _sort_value(second, column, stats_map)
    if value_a is None and value_b is not None:
        return 1
    if value_a is not None and value_b is None:
        return -1
    if value_a is not None and value_b is not None:
        comparison = (value_a > value_b) - (value_a < value_b)
        if comparison:
            return comparison if ascending else -comparison
    count_a = int(first.ban_count or 0)
    count_b = int(second.ban_count or 0)
    if count_a != count_b:
        return -1 if count_a > count_b else 1
    rank_a = _match_rank(first)
    rank_b = _match_rank(second)
    if rank_a != rank_b:
        return -1 if rank_a < rank_b else 1
    day_a = first.latest_ban_day or ""
    day_b = second.latest_ban_day or ""
    if day_a != day_b:
        return -1 if day_a > day_b else 1
    name_a = first.nickname or ""
    name_b = second.nickname or ""
    return (name_a > name_b) - (name_a < name_b)


def match_label(player: models.MatchedPlayer) -> str:
    """匹配方式展示文案（partial 标注「仅昵称匹配」）。"""
    if player.match_type == models.MATCH_TYPE_ID:
        return MATCH_LABEL_ID
    if player.match_type == models.MATCH_TYPE_NICKNAME:
        return MATCH_LABEL_PARTIAL if player.partial else MATCH_LABEL_NICKNAME
    return MATCH_LABEL_NONE


class PlayerTableModel(QAbstractTableModel):
    """结果表模型（行 = 单局参战玩家）。"""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._all_rows: list = []
        self._rows: list = []
        self._stats_state = STATS_STATE_IDLE
        self._stats_map: dict = {}
        self._stats_message = ""
        self._filters = {"id_only": False, "hits_only": False, "level": "all"}
        self._sort_column = COLUMN_INDEX["ban_count"]
        self._sort_order = Qt.SortOrder.DescendingOrder if Qt is not None else 1
        self._user_sorted = False

    # ------------------------------------------------------------ Qt 接口
    def rowCount(self, parent: Any = None) -> int:  # noqa: N802 - Qt 命名
        if parent is not None and getattr(parent, "isValid", lambda: False)():
            return 0
        return len(self._rows)

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
        if not (0 <= row < len(self._rows) and 0 <= column < len(COLUMNS)):
            return None
        player = self._rows[row]
        key = COLUMNS[column]["key"]
        display_role = Qt.ItemDataRole.DisplayRole if Qt is not None else 0
        tooltip_role = Qt.ItemDataRole.ToolTipRole if Qt is not None else 3
        background_role = Qt.ItemDataRole.BackgroundRole if Qt is not None else 9
        foreground_role = Qt.ItemDataRole.ForegroundRole if Qt is not None else 10
        font_role = Qt.ItemDataRole.FontRole if Qt is not None else 11
        alignment_role = Qt.ItemDataRole.TextAlignmentRole if Qt is not None else 7

        if role == display_role:
            return self._display_value(player, key)
        if role == tooltip_role:
            return self._tooltip(player, key)
        if role == background_role:
            return self._background(player)
        if role == foreground_role:
            return self._foreground(player, key)
        if role == font_role:
            return self._font(player, key)
        if role == alignment_role:
            return self._alignment(column)
        return None

    def flags(self, index: Any) -> Any:
        if Qt is None:
            return 0
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ------------------------------------------------------------ 数据填充
    def set_rows(self, rows: list) -> None:
        """替换全部行；默认按 A4 口径排序（次数降序 + 同次数日期降序）。"""
        self._all_rows = list(rows or [])
        self._user_sorted = False
        self._sort_column = COLUMN_INDEX["ban_count"]
        self._sort_order = Qt.SortOrder.DescendingOrder if Qt is not None else 1
        self._rebuild()

    def set_stats(self, state: str, stats_map: Optional[dict] = None, message: str = "") -> None:
        """设置当局战绩状态（idle / loading / ready / unavailable）与数据。"""
        self._stats_state = state
        self._stats_map = dict(stats_map or {})
        self._stats_message = message
        if self._rows:
            top = self.index(0, 0)
            bottom = self.index(len(self._rows) - 1, len(COLUMNS) - 1)
            self.dataChanged.emit(top, bottom)

    def stats_message(self) -> str:
        return self._stats_message

    def apply_filters(self, id_only: bool = False, hits_only: bool = False, level: str = "all") -> None:
        """应用筛选并重建可见行。"""
        self._filters = {"id_only": bool(id_only), "hits_only": bool(hits_only), "level": level or "all"}
        self._rebuild()

    def sort(self, column: int, order: Any = None) -> None:  # noqa: N802 - Qt 命名
        """表头点击排序；同键按次数降序 + ID 确认置前 + 最近日期降序。"""
        if not (0 <= column < len(COLUMNS)):
            return
        self._sort_column = column
        if order is None:
            order = Qt.SortOrder.AscendingOrder if Qt is not None else 0
        self._sort_order = order
        self._user_sorted = True
        self._rebuild()

    def reset_sort(self) -> None:
        """恢复默认排序（次数降序）。"""
        self._sort_column = COLUMN_INDEX["ban_count"]
        self._sort_order = Qt.SortOrder.DescendingOrder if Qt is not None else 1
        self._user_sorted = False
        self._rebuild()

    # ------------------------------------------------------------ 查询接口
    def row_object(self, row: int) -> Optional[models.MatchedPlayer]:
        """取模型行对应的玩家对象。"""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def visible_rows(self) -> list:
        return list(self._rows)

    def total_rows(self) -> list:
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
        """按筛选条件与排序规则重建可见行。"""
        self.beginResetModel()
        rows = list(self._all_rows)
        filters = self._filters
        if filters["id_only"]:
            rows = [item for item in rows if item.match_type == models.MATCH_TYPE_ID]
        if filters["hits_only"]:
            rows = [item for item in rows if item.is_hit]
        if filters["level"] == "red":
            rows = [item for item in rows if item.level == models.LEVEL_RED]
        elif filters["level"] == "yellow":
            rows = [item for item in rows if item.level == models.LEVEL_YELLOW]
        comparator = partial(
            _compare_players,
            column=self._sort_column,
            ascending=self._sort_order == (Qt.SortOrder.AscendingOrder if Qt is not None else 0),
            stats_map=self._stats_map,
        )
        rows.sort(key=cmp_to_key(comparator))
        self._rows = rows
        self.endResetModel()

    def _display_value(self, player: models.MatchedPlayer, key: str) -> str:
        if key == "team":
            return "蓝方" if player.team == 1 else ("红方" if player.team == 2 else "—")
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
        if self._stats_state == STATS_STATE_LOADING and stats is None:
            return "…"
        if stats is None:
            return "—"
        if key == "combat":
            return _format_combat(stats.combat)
        if key == "damage":
            return _format_number(stats.damage)
        if key == "kills":
            return _format_number(stats.kills)
        if key == "xp":
            return _format_number(stats.xp)
        if key == "hit_rate":
            rate = _hit_rate(player, self._stats_map)
            return "%.1f%%" % rate if rate is not None else "—"
        return "—"

    def _tooltip(self, player: models.MatchedPlayer, key: str) -> str:
        if key == "match_type":
            return TOOLTIPS.get(match_label(player), "")
        if key in STATS_KEYS:
            if self._stats_state == STATS_STATE_LOADING:
                return "正在加载当局战绩…"
            if self._stats_state == STATS_STATE_UNAVAILABLE:
                return self._stats_message or "当局战绩不可用"
            return "数据来自偶游盒子 battledetail，仅供参考、非官方数据"
        if key == "dbid":
            return "账号 ID（完整录像 dbid）；partial 录像无账号 ID"
        if key == "ban_count":
            return "1 次 = 黄标，2 次及以上 = 红标；同一玩家跨批次记录已归集"
        if key == "nickname":
            return "昵称匹配口径：同昵称视为同一人（A2）"
        return ""

    def _background(self, player: models.MatchedPlayer) -> Any:
        if QColor is None:
            return None
        if player.level == models.LEVEL_RED:
            return QColor("#ffd9d9")
        if player.level == models.LEVEL_YELLOW:
            return QColor("#fff3c4")
        return None

    def _foreground(self, player: models.MatchedPlayer, key: str) -> Any:
        if QColor is None or not player.is_hit:
            return None
        if key in ("ban_count", "match_type"):
            return QColor("#b91c1c") if player.level == models.LEVEL_RED else QColor("#92400e")
        return None

    def _font(self, player: models.MatchedPlayer, key: str) -> Any:
        if QFont is None or not player.is_hit:
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
    """自检：不创建 QApplication，直接验证排序 / 筛选 / 展示文案。"""
    import time

    start = time.time()
    base_path = "."
    print("[1/3] 结果表模型自检（base_path=%r）列数=%d" % (base_path, len(COLUMNS)))
    from src.app import mock_data

    detail = mock_data.build_detail(mock_data.build_replays()[0])
    matches = mock_data.build_matches(detail)
    model = PlayerTableModel()
    model.set_rows(matches)
    print("      总行=%d 命中=%d 首行=%s（次数=%d）"
          % (len(model.total_rows()), len([x for x in matches if x.is_hit]),
             model.row_object(0).nickname, model.row_object(0).ban_count))
    model.apply_filters(id_only=True)
    print("[2/3] 仅看 ID 命中：%d 行，全部 ID 匹配=%s"
          % (model.rowCount(), all(x.match_type == models.MATCH_TYPE_ID for x in model.visible_rows())))
    stats = mock_data.build_battle_stats(detail)
    stats_map = {("id", item.dbid): item for item in stats.players}
    model.apply_filters()
    model.set_stats(STATS_STATE_READY, stats_map)
    print("[3/3] 当局战绩列：首行战力=%s 命中率=%s；耗时 %.2f 秒"
          % (model.data(model.index(0, COLUMN_INDEX["combat"]), Qt.ItemDataRole.DisplayRole if Qt else 0),
             model.data(model.index(0, COLUMN_INDEX["hit_rate"]), Qt.ItemDataRole.DisplayRole if Qt else 0),
             time.time() - start))
    print("[done] table_model self-check passed")
    return 0


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
