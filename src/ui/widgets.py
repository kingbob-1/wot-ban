# -*- coding: utf-8 -*-
"""界面组件：右侧明细面板、名单库状态对话框、可滚动下拉框。

- ``PlayerDetailPanel``：选中玩家的封禁记录 + 当局战绩 + 综合战绩点查；
- ``DataStatusDialog``：名单库状态页（批次数 / 条数 / 更新时间 / wgid 覆盖 / 缺失批次）；
- ``ScrollableComboBox``：弹窗最多显示 N 行、超出部分用竖向滚动条查看的下拉框。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

try:
    from PySide6.QtCore import QPoint, Qt, Signal
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QListView,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # 便于无 PySide6 环境下静态检查
    Qt = None  # type: ignore[assignment]
    Signal = None  # type: ignore[assignment]
    QDialog = QFrame = QWidget = object  # type: ignore[assignment]

try:
    from .. import models
    from ..app.service import format_iso_text
    from . import theme
    from .table_model import (
        format_combat_int,
        match_label,
        team_display,
        vehicle_display,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src import models
    from src.app.service import format_iso_text
    from src.ui import theme
    from src.ui.table_model import (
        format_combat_int,
        match_label,
        team_display,
        vehicle_display,
    )


# F2：BanRecord.source_type -> 界面文案（官方 CSV/xlsx/JSON）。
SOURCE_TYPE_LABELS = {
    models.SOURCE_TYPE_CSV: "官方 CSV 名单",
    models.SOURCE_TYPE_XLSX: "官方 XLSX 公告",
    models.SOURCE_TYPE_JSON: "官方 JSON（含 wgid）",
    models.SOURCE_TYPE_EXTERNAL: "外部来源",
}

# F4：综合战绩口径（盒子 index 近 1500 场标准局，非官方数据）。
PLAYER_STATS_SCOPE = "偶游盒子 · 最近 1500 场标准局 · 仅供参考、非官方数据"


def _table_item(text: str, align: Any = None, color: Any = None) -> Any:
    """构造只读表格单元格。"""
    item = QTableWidgetItem(str(text))
    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    if align is not None:
        item.setTextAlignment(align)
    if color is not None:
        item.setForeground(color)
    return item


def _plain_table(columns: list, stretch_last: bool = True, widths: Optional[list] = None) -> Any:
    """构造统一风格的只读表格。

    widths 给出时按固定列宽 + 最后一列拉伸（避免内容撑宽右侧明细面板）；
    未给出时用 ResizeToContents。
    """
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    header = table.horizontalHeader()
    if widths:
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for index, width in enumerate(widths):
            if 0 <= index < len(columns):
                table.setColumnWidth(index, width)
    else:
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    if stretch_last:
        header.setStretchLastSection(True)
    table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    return table


# 当前对局下拉框弹窗最多显示的行数（更多内容用滚动条查看）。
COMBO_VISIBLE_ROWS = 15


class ScrollableComboBox(QComboBox):
    """下拉框：弹窗最多显示 ``visible_rows`` 行，超出部分用竖向滚动条查看。

    背景（2026-11-19 实测）：应用级 QSS 会让 Qt 的 QComboBox 弹窗退化成「菜单式」——
    弹窗铺满整屏、``maxVisibleItems`` 失效、只有菜单式滚动箭头而没有滚动条。这里改用
    独立的 ``Qt.Popup`` 窗口 + ``QListView`` 自绘弹窗：复用下拉框自身的数据模型与
    item delegate，选中项同步 ``currentIndex``，点击外部 / Esc 自动关闭，外观由主题 QSS 控制。
    """

    def __init__(self, visible_rows: int = COMBO_VISIBLE_ROWS, parent: Any = None) -> None:
        super().__init__(parent)
        self.visible_rows = max(1, int(visible_rows))
        self.setMaxVisibleItems(self.visible_rows)  # 键盘 / 无障碍语义保持一致
        self._popup: Any = None
        self._list: Any = None

    # ------------------------------------------------------------ 弹窗构建
    def _ensure_popup(self) -> None:
        """惰性构建弹窗（复用 QComboBox 的模型与 item delegate）。"""
        if self._popup is not None:
            return
        popup = QFrame(self, Qt.WindowType.Popup)
        popup.setObjectName("ComboPopup")
        popup.setFrameShape(QFrame.Shape.NoFrame)
        layout = QVBoxLayout(popup)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        view = QListView(popup)
        view.setObjectName("ComboPopupList")
        view.setUniformItemSizes(True)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view.setModel(self.model())
        try:
            view.setItemDelegate(self.itemDelegate())
        except Exception:  # noqa: BLE001 - 取不到 delegate 时退回默认渲染
            pass
        view.clicked.connect(self._on_popup_index_activated)
        view.activated.connect(self._on_popup_index_activated)
        layout.addWidget(view)
        self._popup = popup
        self._list = view

    def popup_height(self) -> int:
        """弹窗内容高度：最多 ``visible_rows`` 行的像素高度（含边框）。"""
        self._ensure_popup()
        view = self._list
        row_height = 0
        model = view.model() if view is not None else None
        if model is not None and model.rowCount() > 0:
            row_height = int(view.sizeHintForRow(0))
        if row_height <= 0:
            row_height = max(18, self.fontMetrics().height() + 12)
        rows = max(1, min(self.count(), self.visible_rows))
        # +4 像素余量：sizeHintForRow 在弹出前的估算可能比实际行高小 1~2 px，
        # 余量保证最后一行不被裁切、也不误触发滚动条。
        return row_height * rows + 2 * int(view.frameWidth()) + 4

    # ------------------------------------------------------------ 弹出 / 收起
    def showPopup(self) -> None:  # noqa: N802 - Qt 命名
        """显示弹窗；已显示时再次触发则收起（点击切换）。"""
        if self._popup is not None and self._popup.isVisible():
            self.hidePopup()
            return
        if self.count() <= 0:
            return
        self._ensure_popup()
        view = self._list
        height = self.popup_height()
        width = max(320, self.width())
        anchor = self.mapToGlobal(QPoint(0, self.height()))
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, available.width())
            height = min(height, available.height())
            if anchor.y() + height > available.bottom():
                anchor.setY(max(available.top(), self.mapToGlobal(QPoint(0, 0)).y() - height))
            anchor.setX(max(available.left(), min(anchor.x(), available.right() - width)))
        self._popup.setFixedSize(width, height)
        self._popup.move(anchor)
        model = view.model()
        current = max(0, self.currentIndex())
        if model is not None and 0 <= current < model.rowCount():
            index = model.index(current, 0)
            view.setCurrentIndex(index)
            view.scrollTo(index)
        self._popup.show()
        self._popup.raise_()
        view.setFocus()

    def hidePopup(self) -> None:  # noqa: N802 - Qt 命名
        """收起弹窗（不改变当前选中）。"""
        if self._popup is not None:
            self._popup.hide()

    def _on_popup_index_activated(self, index: Any) -> None:
        """弹窗里点选 / 回车选中一项：同步 ``currentIndex`` 并收起。"""
        if index is not None and index.isValid():
            self.setCurrentIndex(index.row())
        self.hidePopup()

    def clear(self) -> None:
        """清空选项前先收起弹窗，避免弹窗展示已清空的模型。"""
        self.hidePopup()
        super().clear()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        """下拉框自身被隐藏时顺带收起弹窗。"""
        self.hidePopup()
        super().hideEvent(event)

    def wheelEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        """弹窗打开时吞掉滚轮事件，避免误改当前对局（滚动请在弹窗内操作）。"""
        if self._popup is not None and self._popup.isVisible():
            event.accept()
            return
        super().wheelEvent(event)


# 当局战绩字段（盒子 battledetail 同款顺序）：(战绩字段, 展示名)。
# 「坦克」显示游戏车名（盒子 tank_title 优先，缺失时用 game_names 兜底）。
BATTLE_FIELDS = [
    ("tank", "坦克"),
    ("combat", "本局战力"),
    ("xp", "经验值"),
    ("shots", "射击"),
    ("piercings", "击穿"),
    ("damage", "造成伤害"),
    ("hits_received", "被击中"),
    ("potential_damage", "受到的潜在伤害"),
    ("damage_blocked", "装甲抵消伤害"),
    ("spotted", "发现敌人"),
    ("vehicles_damaged", "击伤敌方战车"),
    ("kills", "击毁敌方战车"),
    ("assisted_damage", "协助盟友造成伤害"),
    ("capture_points", "占领敌方基地点数"),
    ("defense_points", "保卫基地防御点数"),
    ("mileage", "行驶里程"),
]
# 每行展示 2 组「字段名 + 值」：紧凑多列，同时保证长车名（如「E-100坦克歼击车」）
# 不会把明细面板撑出横向滚动条（否则标题行右上角「刷新」按钮会被挤出可视区）。
BATTLE_FIELDS_PER_ROW = 2


class PlayerDetailPanel(QWidget):
    """右侧明细面板：玩家信息 + 封禁记录 + 当局战绩 + 综合战绩点查。

    - 当局战绩：按盒子 battledetail 字段顺序展示（坦克 / 本局战力 / 经验值 /
      射击 / 击穿 / 造成伤害 / 被击中 / 受到的潜在伤害 / 装甲抵消伤害 / 发现敌人 /
      击伤敌方战车 / 击毁敌方战车 / 协助盟友造成伤害 / 占领敌方基地点数 /
      保卫基地防御点数 / 行驶里程），坦克显示游戏车名；
    - 阵营按「绿方 = 录像者本人所在队」口径展示；
    - 综合战绩：选中玩家只展示已缓存结果（只读 peek，零网络）；标题行右上角单个
      「刷新」按钮手动强制刷新（force_refresh=True），任意选中玩家均可点击。
    """

    player_stats_refresh_requested = Signal(object) if Signal is not None else None

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._player = None
        self._records: list = []
        self._current_source_url = ""
        self._recorder_team: Any = None
        self._build_ui()
        self.clear()

    # ------------------------------------------------------------ 构建
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        scroll.setWidget(content)

        # 玩家头部
        header_card = QFrame()
        header_card.setObjectName("Card")
        header_layout = QVBoxLayout(header_card)
        header_layout.setContentsMargins(14, 12, 14, 12)
        header_layout.setSpacing(6)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        self.name_label = QLabel("未选择玩家")
        self.name_label.setObjectName("DetailName")
        self.match_badge = theme.make_badge("—", "none")
        self.level_badge = theme.make_badge("未命中", "none")
        name_row.addWidget(self.name_label)
        name_row.addWidget(self.match_badge)
        name_row.addWidget(self.level_badge)
        name_row.addStretch(1)
        header_layout.addLayout(name_row)
        self.sub_label = QLabel("在左侧结果表中点击一行查看明细")
        self.sub_label.setObjectName("DetailSub")
        self.sub_label.setWordWrap(True)
        header_layout.addWidget(self.sub_label)
        layout.addWidget(header_card)

        # 封禁记录
        record_card = QFrame()
        record_card.setObjectName("Card")
        record_layout = QVBoxLayout(record_card)
        record_layout.setContentsMargins(14, 12, 14, 12)
        record_layout.setSpacing(8)
        self.record_title = QLabel("封禁记录")
        self.record_title.setObjectName("SectionTitle")
        record_layout.addWidget(self.record_title)
        self.record_table = _plain_table(
            ["批次日期", "原因", "时长", "次数", "来源类型"],
            widths=[88, 104, 50, 40, 92],
        )
        self.record_table.horizontalHeader().setStretchLastSection(False)
        self.record_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.record_table.setMinimumHeight(150)
        self.record_table.itemSelectionChanged.connect(self._on_record_selected)
        record_layout.addWidget(self.record_table)

        # F2：选中记录的来源明细（批次 / 类型 / 抓取时间 / 公告链接，可点击或复制）
        source_grid = QGridLayout()
        source_grid.setHorizontalSpacing(14)
        source_grid.setVerticalSpacing(4)
        self.source_values = {}
        source_fields = [
            ("batch", "来源批次"),
            ("type", "来源类型"),
            ("fetched", "抓取时间"),
            ("url", "来源公告"),
        ]
        for index, (key, label) in enumerate(source_fields):
            name_label = QLabel(label)
            name_label.setObjectName("FieldName")
            value_label = QLabel("—")
            value_label.setObjectName("StatsHint")
            value_label.setWordWrap(True)
            self.source_values[key] = value_label
            row = index // 2
            column = (index % 2) * 2
            source_grid.addWidget(name_label, row, column)
            source_grid.addWidget(value_label, row, column + 1)
        self.source_values["url"].setOpenExternalLinks(True)
        self.source_values["url"].setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        record_layout.addLayout(source_grid)
        copy_row = QHBoxLayout()
        self.copy_source_button = QPushButton("复制来源链接")
        self.copy_source_button.setObjectName("Ghost")
        self.copy_source_button.setEnabled(False)
        self.copy_source_button.clicked.connect(self._copy_source_url)
        copy_row.addWidget(self.copy_source_button)
        copy_row.addStretch(1)
        record_layout.addLayout(copy_row)
        self.source_hint = QLabel("")
        self.source_hint.setObjectName("StatsHint")
        self.source_hint.setWordWrap(True)
        record_layout.addWidget(self.source_hint)

        self.record_hint = QLabel("未命中任何历史封禁记录")
        self.record_hint.setObjectName("EmptyHint")
        record_layout.addWidget(self.record_hint)
        layout.addWidget(record_card)

        # 当局战绩（盒子同款明细，紧凑多列）
        battle_card = QFrame()
        battle_card.setObjectName("Card")
        battle_layout = QVBoxLayout(battle_card)
        battle_layout.setContentsMargins(14, 12, 14, 12)
        battle_layout.setSpacing(8)
        battle_title = QLabel("当局战绩（本局，来自偶游盒子）")
        battle_title.setObjectName("SectionTitle")
        battle_layout.addWidget(battle_title)
        self.battle_grid = QGridLayout()
        self.battle_grid.setHorizontalSpacing(12)
        self.battle_grid.setVerticalSpacing(3)
        self.battle_values = {}
        for index, (key, label) in enumerate(BATTLE_FIELDS):
            name_label = QLabel(label)
            name_label.setObjectName("FieldName")
            value_label = QLabel("—")
            value_label.setObjectName("StatsValue")
            self.battle_values[key] = value_label
            row = index // BATTLE_FIELDS_PER_ROW
            column = (index % BATTLE_FIELDS_PER_ROW) * 2
            self.battle_grid.addWidget(name_label, row, column)
            self.battle_grid.addWidget(value_label, row, column + 1)
        battle_layout.addLayout(self.battle_grid)
        layout.addWidget(battle_card)

        # 综合战绩（缓存优先展示 + 标题行右上角单个「刷新」按钮）
        player_card = QFrame()
        player_card.setObjectName("Card")
        player_layout = QVBoxLayout(player_card)
        player_layout.setContentsMargins(14, 12, 14, 12)
        player_layout.setSpacing(8)
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        # 标题单行（口径细节放 tooltip），给右上角「刷新」按钮留出稳定位置。
        player_title = QLabel("综合战绩（偶游盒子 · 近 1500 场）")
        player_title.setObjectName("SectionTitle")
        player_title.setWordWrap(False)
        player_title.setToolTip("数据口径：%s。盒子 index 为近 1500 场标准局统计，非官方数据。" % PLAYER_STATS_SCOPE)
        self.player_title = player_title
        # 第二轮反馈：合并为单个「刷新」按钮，放标题行右上角（不再单独一行）。
        self.refresh_player_stats_button = QPushButton("刷新")
        self.refresh_player_stats_button.setObjectName("Ghost")
        self.refresh_player_stats_button.setEnabled(False)
        self.refresh_player_stats_button.setToolTip(
            "强制刷新综合战绩（force_refresh=True，忽略当日缓存重新点查）：%s" % PLAYER_STATS_SCOPE
        )
        # 兼容旧引用（截图 / 自检脚本）：旧「查看综合战绩」按钮已合并，别名指向同一个刷新按钮。
        self.player_stats_button = self.refresh_player_stats_button
        title_row.addWidget(player_title)
        title_row.addStretch(1)
        title_row.addWidget(self.refresh_player_stats_button)
        player_layout.addLayout(title_row)
        self.player_stats_meta = QLabel("")
        self.player_stats_meta.setObjectName("StatsHint")
        self.player_stats_meta.setWordWrap(True)
        player_layout.addWidget(self.player_stats_meta)
        self.player_grid = QGridLayout()
        self.player_grid.setHorizontalSpacing(18)
        self.player_grid.setVerticalSpacing(6)
        self.player_values = {}
        player_fields = [("combat_power", "战力"), ("win_rate", "胜率"), ("avg_damage", "场均伤害"),
                         ("battles_hint", "出战场次")]
        for index, (key, label) in enumerate(player_fields):
            name_label = QLabel(label)
            name_label.setObjectName("FieldName")
            value_label = QLabel("—")
            value_label.setObjectName("StatsValue")
            self.player_values[key] = value_label
            row = index // 2
            column = (index % 2) * 2
            self.player_grid.addWidget(name_label, row, column)
            self.player_grid.addWidget(value_label, row, column + 1)
        player_layout.addLayout(self.player_grid)
        layout.addWidget(player_card)
        layout.addStretch(1)

        self.refresh_player_stats_button.clicked.connect(self._on_player_stats_refresh_clicked)

    # ------------------------------------------------------------ 交互
    def _on_player_stats_refresh_clicked(self) -> None:
        if self._player is not None and self.player_stats_refresh_requested is not None:
            self.player_stats_refresh_requested.emit(self._player)

    # ------------------------------------------------------------ 更新
    def clear(self) -> None:
        """清空为初始空态。"""
        self._player = None
        self._records = []
        self._recorder_team = None
        self.name_label.setText("未选择玩家")
        self.sub_label.setText("在左侧结果表中点击一行查看明细")
        self.record_title.setText("封禁记录")
        self.record_table.setRowCount(0)
        self._clear_record_source()
        self.record_hint.setText("未命中任何历史封禁记录")
        self.record_hint.setVisible(True)
        for label in self.battle_values.values():
            label.setText("—")
        self._reset_player_stats()
        self.refresh_player_stats_button.setEnabled(False)
        theme.set_badge(self.match_badge, "—", "none")
        theme.set_badge(self.level_badge, "未命中", "none")

    def set_player(self, player: Any, stats_map: dict, stats_state: str, stats_message: str = "",
                   recorder_team: Any = None) -> None:
        """展示选中玩家的封禁记录与当局战绩（阵营按「绿方 = 本人队」口径）。"""
        self._player = player
        self._recorder_team = recorder_team
        if player is None:
            self.clear()
            return
        stats = self._stats_for(player, stats_map)
        self.name_label.setText(player.nickname or "（无昵称）")
        sub_parts = []
        sub_parts.append("账号 ID：%s" % (player.dbid if player.dbid else "—（partial 录像无 ID）"))
        sub_parts.append("军团：%s" % (player.clan_abbrev or "—"))
        sub_parts.append("坦克：%s" % vehicle_display(player, stats))
        sub_parts.append("阵营：%s" % team_display(getattr(player, "team", None), recorder_team))
        self.sub_label.setText("　·　".join(sub_parts))

        label_text = match_label(player)
        if label_text == "ID 确认":
            theme.set_badge(self.match_badge, label_text, "id")
        elif label_text == "仅昵称匹配":
            theme.set_badge(self.match_badge, label_text, "partial")
        elif label_text == "昵称匹配":
            theme.set_badge(self.match_badge, label_text, "nick")
        else:
            theme.set_badge(self.match_badge, "未命中", "none")
        if player.level == models.LEVEL_RED:
            theme.set_badge(self.level_badge, "红标 · %d 次封禁" % int(player.ban_count or 0), "red")
        elif player.level == models.LEVEL_YELLOW:
            theme.set_badge(self.level_badge, "黄标 · %d 次封禁" % int(player.ban_count or 0), "yellow")
        else:
            theme.set_badge(self.level_badge, "未命中", "none")

        self._fill_records(player)
        self._fill_battle(player, stats_map, stats_state)
        self._reset_player_stats()
        # 第二轮反馈：任意选中玩家都可点「刷新」（不再限制必须命中封禁名单）；
        # 未命中玩家的请求若被数据层拒绝，界面按返回的中文提示优雅展示。
        self.refresh_player_stats_button.setEnabled(True)

    def _fill_records(self, player: Any) -> None:
        records = list(player.records or [])
        self._records = records
        self.record_title.setText("封禁记录（%d 条）" % len(records))
        self.record_table.setRowCount(len(records))
        for row, record in enumerate(records):
            type_label = SOURCE_TYPE_LABELS.get(
                str(record.source_type or "").lower(), str(record.source_type or "—")
            )
            tooltip = (
                "来源批次：%s\n来源类型：%s\n抓取时间：%s\n公告链接：%s"
                % (record.batch_day or "—", type_label, record.fetched_at or "—",
                   record.source_url or "—")
            )
            items = [
                _table_item(record.batch_day or "—"),
                _table_item(record.reason or "—"),
                _table_item(record.duration or "—"),
                _table_item(str(record.ban_times or 1)),
                _table_item(type_label),
            ]
            for column, item in enumerate(items):
                item.setToolTip(tooltip)
                self.record_table.setItem(row, column, item)
        self.record_hint.setText("未命中任何历史封禁记录" if not records else
                                 "共 %d 条记录，按封禁日期降序；选中一条可查看来源批次 / 公告链接 / 抓取时间"
                                 % len(records))
        self.record_hint.setVisible(True)
        if records:
            self.record_table.blockSignals(True)
            self.record_table.selectRow(0)
            self.record_table.blockSignals(False)
            self._show_record_source(0)
        else:
            self._clear_record_source()

    # ------------------------------------------------------------ F2 来源信息
    def _on_record_selected(self) -> None:
        """封禁记录表选中行变化：刷新来源明细。"""
        self._show_record_source(self.record_table.currentRow())

    def _show_record_source(self, row: int) -> None:
        """显示某条封禁记录的来源批次 / 类型 / 抓取时间 / 公告链接。"""
        if not (0 <= row < len(self._records)):
            self._clear_record_source()
            return
        record = self._records[row]
        type_label = SOURCE_TYPE_LABELS.get(
            str(record.source_type or "").lower(), str(record.source_type or "—")
        )
        self.source_values["batch"].setText(record.batch_day or "—")
        self.source_values["type"].setText(type_label)
        self.source_values["fetched"].setText(format_iso_text(record.fetched_at))
        url = record.source_url or ""
        if url:
            self.source_values["url"].setText('<a href="%s">查看来源公告</a>' % url)
            self.source_values["url"].setToolTip(url)
            self.copy_source_button.setEnabled(True)
        else:
            self.source_values["url"].setText("—")
            self.source_values["url"].setToolTip("")
            self.copy_source_button.setEnabled(False)
        self._current_source_url = url

    def _clear_record_source(self) -> None:
        """清空来源明细区。"""
        for key in ("batch", "type", "fetched", "url"):
            if key in getattr(self, "source_values", {}):
                self.source_values[key].setText("—")
                self.source_values[key].setToolTip("")
        self._current_source_url = ""
        if hasattr(self, "copy_source_button"):
            self.copy_source_button.setEnabled(False)
        if hasattr(self, "source_hint"):
            self.source_hint.setText("")

    def _copy_source_url(self) -> None:
        """复制当前记录的来源公告链接到剪贴板。"""
        url = self._current_source_url
        if not url:
            self.source_hint.setText("当前记录没有可复制的来源公告链接")
            return
        if QApplication is not None:
            QApplication.clipboard().setText(url)
        self.source_hint.setText("已复制来源公告链接：%s" % url)

    def _stats_for(self, player: Any, stats_map: dict) -> Any:
        """按 dbid 优先、昵称兜底取某玩家的当局战绩对象。"""
        dbid = getattr(player, "dbid", None)
        stats = stats_map.get(("id", int(dbid))) if dbid is not None else None
        if stats is None and getattr(player, "nickname", None):
            stats = stats_map.get(("nick", player.nickname))
        return stats

    @staticmethod
    def _stat_value(stats: Any, name: str) -> Any:
        """取当局战绩字段（dataclass / dict 均可）；缺失返回 None。"""
        if stats is None:
            return None
        if isinstance(stats, dict):
            return stats.get(name)
        return getattr(stats, name, None)

    def _fill_battle(self, player: Any, stats_map: dict, stats_state: str) -> None:
        """当局战绩：按盒子字段顺序填充；缺失显示「—」，加载中显示「…」。"""
        stats = self._stats_for(player, stats_map)
        placeholder = "…" if stats_state == "loading" else "—"
        for key, _label in BATTLE_FIELDS:
            if key == "tank":
                # 坦克始终显示车名（盒子 tank_title 优先，其次 game_names 兜底）。
                self.battle_values["tank"].setText(vehicle_display(player, stats))
                continue
            if stats is None:
                self.battle_values[key].setText(placeholder)
                continue
            value = self._stat_value(stats, key)
            if key == "combat":
                self.battle_values[key].setText(format_combat_int(value))
            else:
                self.battle_values[key].setText(self._fmt_plain(value))

    # ------------------------------------------------------------ 综合战绩
    def _reset_player_stats(self) -> None:
        """综合战绩区回到「未查询」空态（不发请求）。"""
        for label in self.player_values.values():
            label.setText("—")
        self.player_stats_meta.setText("")

    def set_player_stats_loading(self, refresh: bool = False) -> None:
        """综合战绩请求中（手动查看 / 强制刷新时调用）。"""
        for label in self.player_values.values():
            label.setText("…")
        if refresh:
            self.player_stats_meta.setText("正在刷新综合战绩（force_refresh=True，忽略当日缓存）…")
        else:
            self.player_stats_meta.setText("正在读取综合战绩（缓存优先）…")

    def show_player_stats(self, result: Any, from_cache: bool = False) -> None:
        """展示综合战绩结果（PlayerStatsResult）；含数据更新时间 / 缓存时间。"""
        if result is None or not getattr(result, "available", False):
            self._reset_player_stats()
            message = getattr(result, "message", "") if result is not None else ""
            if message:
                self.player_stats_meta.setText(message)
            return
        stats = result.stats
        self.player_values["combat_power"].setText(self._fmt(getattr(stats, "combat_power", None)))
        win_rate = getattr(stats, "win_rate", None)
        self.player_values["win_rate"].setText("%.1f%%" % float(win_rate) if win_rate is not None else "—")
        self.player_values["avg_damage"].setText(self._fmt(getattr(stats, "avg_damage", None)))
        self.player_values["battles_hint"].setText(self._fmt(getattr(stats, "battles_hint", None)))
        meta_parts = []
        update_time = format_iso_text(getattr(stats, "update_time", None))
        fetched_at = format_iso_text(getattr(stats, "fetched_at", None))
        if update_time and update_time != "—":
            meta_parts.append("数据更新时间：%s" % update_time)
        if from_cache:
            # 选中玩家只读缓存展示：标注缓存时间并提示手动刷新（不发请求）。
            if fetched_at and fetched_at != "—":
                meta_parts.append("缓存于 %s（点刷新更新）" % fetched_at)
        elif fetched_at and fetched_at != "—":
            meta_parts.append("缓存时间：%s" % fetched_at)
        message = getattr(result, "message", "") or ""
        if "缓存" in message and not from_cache:
            meta_parts.append("缓存命中")
        self.player_stats_meta.setText(" ｜ ".join(meta_parts))

    def _fmt(self, value: Any) -> str:
        if value is None:
            return "—"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if abs(number - round(number)) < 0.05:
            return "{:,}".format(int(round(number)))
        return "{:,.1f}".format(number)

    def _fmt_plain(self, value: Any) -> str:
        """紧凑数字（不加千分位，贴近盒子原始展示）；None -> —。"""
        if value is None:
            return "—"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return str(value)
        if abs(number - round(number)) < 0.05:
            return str(int(round(number)))
        return "%.1f" % number


class DataStatusDialog(QDialog):
    """名单库状态页：批次数 / 条数 / 更新时间 / wgid 覆盖 / 缺失批次 / 上次检查时间。

    同时承载 M6 数据管理入口：打开数据目录 / 打开日志目录 / 清空名单库 / 重建名单库。
    """

    clear_requested = Signal() if Signal is not None else None
    rebuild_requested = Signal() if Signal is not None else None
    open_data_dir_requested = Signal() if Signal is not None else None
    open_log_dir_requested = Signal() if Signal is not None else None

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("名单库状态")
        self.resize(1000, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        title = QLabel("名单库状态")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)
        self.summary_label = QLabel("—")
        self.summary_label.setObjectName("SubTitle")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(24)
        self.grid.setVerticalSpacing(8)
        self.values = {}
        fields = [
            ("batch_count", "批次数"),
            ("record_count", "库内条数"),
            ("official", "官方源（物理行）"),
            ("range", "覆盖区间"),
            ("latest", "最新批次"),
            ("wgid", "wgid 覆盖"),
            ("missing", "缺失批次"),
            ("last_check", "上次检查"),
            ("last_update", "上次更新"),
            ("replays", "录像索引"),
        ]
        for index, (key, label) in enumerate(fields):
            name_label = QLabel(label)
            name_label.setObjectName("FieldName")
            value_label = QLabel("—")
            value_label.setObjectName("MetaValue")
            self.values[key] = value_label
            row = index // 4
            column = (index % 4) * 2
            self.grid.addWidget(name_label, row, column)
            self.grid.addWidget(value_label, row, column + 1)
        layout.addLayout(self.grid)

        self.official_note_label = QLabel("")
        self.official_note_label.setObjectName("MetaHint")
        self.official_note_label.setWordWrap(True)
        layout.addWidget(self.official_note_label)

        self.db_label = QLabel("")
        self.db_label.setObjectName("MetaHint")
        self.db_label.setWordWrap(True)
        layout.addWidget(self.db_label)

        # v0.2：本机环境（游戏目录 / 录像目录 / 我的账号 / 最近对局）
        env_title = QLabel("本机环境")
        env_title.setObjectName("SectionTitle")
        layout.addWidget(env_title)
        env_grid = QGridLayout()
        env_grid.setHorizontalSpacing(24)
        env_grid.setVerticalSpacing(6)
        self.env_values = {}
        env_fields = [
            ("game", "游戏目录"),
            ("game_source", "游戏目录来源"),
            ("replay", "录像目录"),
            ("account", "我的账号"),
            ("last_battle", "最近对局"),
        ]
        for index, (key, label) in enumerate(env_fields):
            name_label = QLabel(label)
            name_label.setObjectName("FieldName")
            value_label = QLabel("—")
            value_label.setObjectName("MetaValue")
            value_label.setWordWrap(True)
            self.env_values[key] = value_label
            row = index // 3
            column = (index % 3) * 2
            env_grid.addWidget(name_label, row, column)
            env_grid.addWidget(value_label, row, column + 1)
        layout.addLayout(env_grid)
        self.env_hint = QLabel("")
        self.env_hint.setObjectName("StatsHint")
        self.env_hint.setWordWrap(True)
        layout.addWidget(self.env_hint)

        self.batch_table = _plain_table(["批次日", "来源", "记录数", "带 wgid 记录"])
        self.batch_table.horizontalHeader().setStretchLastSection(False)
        for index in range(4):
            self.batch_table.horizontalHeader().setSectionResizeMode(
                index, QHeaderView.ResizeMode.Stretch
            )
        layout.addWidget(self.batch_table, 1)

        self.message_label = QLabel("数据来源：官方公开名单归档（本地 SQLite）；历史封禁不代表当前违规。")
        self.message_label.setObjectName("StatsHint")
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        self.action_label = QLabel("")
        self.action_label.setObjectName("LoadingHint")
        self.action_label.setWordWrap(True)
        layout.addWidget(self.action_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.open_data_button = QPushButton("打开数据目录")
        self.open_log_button = QPushButton("打开日志目录")
        self.clear_button = QPushButton("清空名单库")
        self.clear_button.setObjectName("Danger")
        self.rebuild_button = QPushButton("重建名单库")
        self.rebuild_button.setObjectName("Primary")
        self.close_button = QPushButton("关闭")
        for button in (self.open_data_button, self.open_log_button, self.clear_button,
                       self.rebuild_button):
            button_row.addWidget(button)
        button_row.addStretch(1)
        button_row.addWidget(self.close_button)
        layout.addLayout(button_row)

        self.open_data_button.clicked.connect(self._emit_open_data)
        self.open_log_button.clicked.connect(self._emit_open_log)
        self.clear_button.clicked.connect(self._emit_clear)
        self.rebuild_button.clicked.connect(self._emit_rebuild)
        self.close_button.clicked.connect(self.accept)

    # ------------------------------------------------------------ 信号
    def _emit_open_data(self) -> None:
        if self.open_data_dir_requested is not None:
            self.open_data_dir_requested.emit()

    def _emit_open_log(self) -> None:
        if self.open_log_dir_requested is not None:
            self.open_log_dir_requested.emit()

    def _emit_clear(self) -> None:
        if self.clear_requested is not None:
            self.clear_requested.emit()

    def _emit_rebuild(self) -> None:
        if self.rebuild_requested is not None:
            self.rebuild_requested.emit()

    # ------------------------------------------------------------ 状态填充
    def set_status(self, status: Any) -> None:
        """填充状态数据（AppService.get_data_status 返回值）。"""
        self.summary_label.setText(getattr(status, "message", "—"))
        self.values["batch_count"].setText(str(getattr(status, "batch_count", 0)))
        record_text = getattr(status, "record_count_text", None)
        self.values["record_count"].setText(
            record_text if record_text else "{:,}".format(int(getattr(status, "record_count", 0)))
        )
        official_count = int(getattr(status, "official_record_count", 0) or 0)
        self.values["official"].setText("{:,} 条".format(official_count) if official_count else "—")
        self.values["range"].setText("%s ~ %s" % (
            getattr(status, "first_batch_day", "") or "—", getattr(status, "latest_batch_day", "") or "—"))
        self.values["latest"].setText(getattr(status, "latest_batch_day", "") or "—")
        self.values["wgid"].setText(getattr(status, "wgid_coverage_text", "—"))
        self.values["missing"].setText(getattr(status, "missing_text", "无"))
        self.values["last_check"].setText(format_iso_text(getattr(status, "last_check_at", "")))
        self.values["last_update"].setText(format_iso_text(getattr(status, "last_update_at", "")))
        replay_count = int(getattr(status, "replay_index_count", 0) or 0)
        self.values["replays"].setText("{:,} 个".format(replay_count) if replay_count else "—")
        official_note = getattr(status, "official_note", "")
        self.official_note_label.setText(("口径说明：%s。" % official_note) if official_note else "")
        self.db_label.setText("数据库：%s" % (getattr(status, "db_path", "") or "—"))
        batches = list(getattr(status, "batches", []) or [])
        self.batch_table.setRowCount(len(batches))
        for row, batch in enumerate(batches):
            self.batch_table.setItem(row, 0, _table_item(batch.get("batch_day", "—")))
            self.batch_table.setItem(row, 1, _table_item(str(batch.get("source_type", "")).upper()))
            self.batch_table.setItem(row, 2, _table_item("{:,}".format(int(batch.get("records", 0))),
                                                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
            self.batch_table.setItem(row, 3, _table_item("{:,}".format(int(batch.get("wgid_records", 0))),
                                                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))

    def set_action_message(self, text: str) -> None:
        """显示数据管理操作进度 / 结果。"""
        self.action_label.setText(text)

    def set_environment(self, env: Any) -> None:
        """填充「本机环境」区（v0.2 自动探测结果）。"""
        if env is None:
            for label in self.env_values.values():
                label.setText("—")
            self.env_hint.setText("")
            return
        self.env_values["game"].setText(getattr(env, "game_dir_text", "—"))
        self.env_values["game_source"].setText(getattr(env, "game_dir_source", "") or "—")
        self.env_values["replay"].setText(str(getattr(env, "replay_dir", "") or "—"))
        self.env_values["account"].setText(getattr(env, "account_text", "—"))
        self.env_values["last_battle"].setText(format_iso_text(getattr(env, "last_battle_time", "")))
        self.env_hint.setText(getattr(env, "message", "") or "")

    def set_actions_enabled(self, enabled: bool) -> None:
        """数据管理操作执行期间禁用按钮。"""
        self.clear_button.setEnabled(enabled)
        self.rebuild_button.setEnabled(enabled)
        self.open_data_button.setEnabled(enabled)
        self.open_log_button.setEnabled(enabled)


def main(argv: list) -> int:
    """自检：打印组件类信息（不创建 QApplication）。"""
    import time

    start = time.time()
    base_path = "."
    print("[1/2] 界面组件自检（base_path=%r）：PlayerDetailPanel / DataStatusDialog" % base_path)
    print("      免责声明=%s" % theme.DISCLAIMER_TEXT[:30])
    print("[2/2] 组件自检完成，耗时 %.2f 秒" % (time.time() - start))
    return 0


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
