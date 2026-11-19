# -*- coding: utf-8 -*-
"""主窗口：1.0 定稿界面骨架（顶栏 / 录像区 / 结果表 / 明细 / 汇总 / 免责声明）。

界面全部代码构建（无 .ui 文件）。窗口 16:9 可缩放，默认 1920×1080（屏幕可用区域
不足时等比收缩）；字体微软雅黑、高 DPI 由 Qt 处理。1.0 定稿顶栏只保留标题、版本与
三个操作按钮（重新检测 / 手动选择游戏目录 / 更新名单），不再提供「数据状态」入口；
录像区只保留「最新完整录像 / 全部录像 / 手动选择…」三个扫描入口。数据状态页仍保留
``open_data_status()`` 供开发 / QA 脚本直接调用。

线程模型
--------
所有耗时操作（更新名单 / 扫描录像 / 解析查询 / 当局战绩 / 综合战绩）都放进
``ServiceWorker``（QThread），通过信号回到主线程更新界面，避免卡顿；扫描与
更新支持「取消」。
"""
from __future__ import annotations

import sys
import time
from functools import partial
from pathlib import Path
from typing import Any, Optional

try:
    from PySide6.QtCore import Qt, QTimer, QUrl
    from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QIcon
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QFrame,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QSplitter,
        QStackedWidget,
        QStyle,
        QStyledItemDelegate,
        QStyleOptionViewItem,
        QTableView,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # 便于无 PySide6 环境下静态检查
    Qt = QApplication = QMainWindow = QWidget = QFrame = QDialog = object  # type: ignore[assignment]
    QTimer = QUrl = QDesktopServices = QIcon = QFont = QFontMetrics = None  # type: ignore[assignment]
    QColor = QStyle = QStyleOptionViewItem = None  # type: ignore[assignment]
    QStyledItemDelegate = object  # type: ignore[assignment]

try:
    from ..app.service import (
        AppService,
        ServiceError,
        latest_complete_replay,
        replay_label,
    )
    from ..app.workers import ServiceWorker
    from . import theme
    from .table_model import (
        COLUMNS,
        COLUMN_INDEX,
        STATS_STATE_READY,
        STATS_STATE_UNAVAILABLE,
        PlayerTableModel,
    )
    from .widgets import (
        DataStatusDialog,
        FirstRunScanDialog,
        FirstRunUpdateDialog,
        PlayerDetailPanel,
        ScrollableComboBox,
    )
except ImportError:  # 直接以脚本方式运行时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.app.service import (
        AppService,
        ServiceError,
        latest_complete_replay,
        replay_label,
    )
    from src.app.workers import ServiceWorker
    from src.ui import theme
    from src.ui.table_model import (
        COLUMNS,
        COLUMN_INDEX,
        STATS_STATE_READY,
        STATS_STATE_UNAVAILABLE,
        PlayerTableModel,
    )
    from src.ui.widgets import (
        DataStatusDialog,
        FirstRunScanDialog,
        FirstRunUpdateDialog,
        PlayerDetailPanel,
        ScrollableComboBox,
    )

# 1.0 定稿：录像区只保留「最新完整录像 / 全部录像 / 手动选择…」三个入口；
# service 层 recent 能力与 config.default_scan_days 不变（仅拆掉 UI 入口）。
SCAN_MODE_LABELS = {"latest": "最新完整录像", "all": "全部录像", "file": "手动选择"}

# 首次使用流程完成标记（持久化在 data/wotban.db 的 meta 表；名单更新成功且 bans>0 后写入）。
FIRST_RUN_FLOW_META_KEY = "first_run_flow_done"

# 「查询结果」标题行：本局胜负平文案（胜绿 / 平橙 / 负红，见 theme.APP_QSS）。
RESULT_TITLE_BASE = "查询结果"
OUTCOME_KIND_MAP = {"胜利": "victory", "胜": "victory", "平局": "draw", "平": "draw",
                    "失败": "defeat", "负": "defeat"}
OUTCOME_TITLE_KINDS = ("victory", "draw", "defeat", "loading")

# 结果表行高 / 字号（小字号 + 紧凑行高）：30 人 + 2 组头 = 32 行，在 1920×1080
# 默认窗口下一屏显示且不出现垂直滚动条；60 人（62 行）保留垂直滚动条，视口约 30 名玩家。
RESULT_FONT_POINT_SIZE = 7.5
RESULT_ROW_HEIGHT = 24
RESULT_HEADER_HEIGHT = 26

# 窗口默认 / 最小尺寸（16:9）。默认 1920×1080；屏幕可用区域不足时等比收缩。
DEFAULT_WINDOW_WIDTH = 1920
DEFAULT_WINDOW_HEIGHT = 1080
MIN_WINDOW_WIDTH = 1280
MIN_WINDOW_HEIGHT = 720

# Windows 任务栏按 AppUserModelID 分组：显式声明后任务栏 / 托盘才显示本项目图标。
APP_USER_MODEL_ID = "kingbob.wot-ban.tank-ban-lookup"

# 窗口持有的后台 worker 属性名：worker 结束后统一置 None，避免访问已 deleteLater 的 C++ 对象。
WORKER_ATTR_NAMES = (
    "_scan_worker",
    "_query_worker",
    "_update_worker",
    "_battle_worker",
    "_player_stats_worker",
    "_library_worker",
    "_env_worker",
)


class ResultRowDelegate(QStyledItemDelegate):
    """结果表行委托：选中行叠加半透明主色，保留模型背景色（封禁黄/红、本人浅蓝）。

    Qt 默认选中高亮会整块盖掉 ``BackgroundRole``；这里把 ``State_Selected`` 从基类
    绘制选项中移除（底纹仍按模型背景色绘制），再叠加一层半透明蓝作为选中提示，
    从而「选中不盖掉封禁红黄底色 / 本人行浅蓝底色」（第二轮反馈）。
    """

    SELECTION_ALPHA = 46  # 半透明叠加（0-255），既可见选中又不遮底色

    def paint(self, painter: Any, option: Any, index: Any) -> None:
        if QStyleOptionViewItem is None:
            return
        opt = QStyleOptionViewItem(option)
        # 按普通行绘制：底纹来自 model.BackgroundRole（封禁黄/红 / 本人浅蓝）。
        opt.state = opt.state & ~QStyle.StateFlag.State_Selected
        super().paint(painter, opt, index)
        if QColor is not None and (option.state & QStyle.StateFlag.State_Selected):
            tint = QColor(theme.COLOR_ACCENT)
            tint.setAlpha(self.SELECTION_ALPHA)
            painter.save()
            painter.fillRect(option.rect, tint)
            painter.restore()


def application_icon_path(cfg: Any = None) -> Optional[Path]:
    """定位 assets/app.ico（与 exe 使用同一张原创坦克剪影图标）。"""
    candidates = []
    root = getattr(cfg, "project_root", None)
    if root:
        candidates.append(Path(root) / "assets" / "app.ico")
    candidates.append(Path(__file__).resolve().parents[2] / "assets" / "app.ico")
    for path in candidates:
        if path.is_file():
            return path
    return None


def apply_application_icon(app: Any, cfg: Any = None) -> None:
    """设置窗口 / 任务栏 / 托盘图标，并声明 AppUserModelID（Windows）。

    图标与 ``坦克世界封号查询.exe`` 使用同一张 ``assets/app.ico``：未设置时 Qt 窗口
    在任务栏/托盘会显示系统占位图标。
    """
    if app is None:
        return
    if QIcon is not None:
        path = application_icon_path(cfg)
        if path is not None:
            icon = QIcon(str(path))
            if not icon.isNull():
                app.setWindowIcon(icon)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except Exception:  # noqa: BLE001 - 非图形环境 / API 不可用时不影响启动
            pass


def create_application(argv: Optional[list] = None) -> Any:
    """创建 / 复用 QApplication，并应用主题、图标与高 DPI 策略。"""
    if QGuiApplication is not None and hasattr(QGuiApplication, "setHighDpiScaleFactorRoundingPolicy"):
        try:
            QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
                Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
            )
        except Exception:  # noqa: BLE001 - 高 DPI 策略设置失败不影响启动
            pass
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    cfg = None
    try:
        from ..config import load_config
    except ImportError:  # 直接以脚本方式运行时的兜底
        from src.config import load_config
    try:
        cfg = load_config()
        app.setApplicationName(cfg.app_name)
        app.setApplicationVersion(cfg.app_version)
    except Exception:  # noqa: BLE001 - 配置不可用时退回默认名称，不阻断启动
        app.setApplicationName("坦克世界封号案底查询工具")
    theme.configure_application(app)
    apply_application_icon(app, cfg)
    return app


def default_window_size(cfg: Any = None) -> tuple:
    """默认 1920×1080（16:9，取 ``Config.window_width/height``）；屏幕不足时等比收缩。"""
    width = int(getattr(cfg, "window_width", DEFAULT_WINDOW_WIDTH) or DEFAULT_WINDOW_WIDTH)
    height = int(getattr(cfg, "window_height", DEFAULT_WINDOW_HEIGHT) or DEFAULT_WINDOW_HEIGHT)
    screen = QApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        if width > int(available.width() * 0.94):
            width = int(available.width() * 0.94)
            height = int(round(width * 9.0 / 16.0))
        if height > int(available.height() * 0.94):
            height = int(available.height() * 0.94)
            width = int(round(height * 16.0 / 9.0))
    return width, height


class MainWindow(QMainWindow):
    """主窗口。"""

    def __init__(self, service: AppService, parent: Any = None) -> None:
        super().__init__(parent)
        self.service = service
        self.model = PlayerTableModel(self)
        self._workers: dict = {}
        self._scan_worker: Optional[ServiceWorker] = None
        self._query_worker: Optional[ServiceWorker] = None
        self._update_worker: Optional[ServiceWorker] = None
        self._battle_worker: Optional[ServiceWorker] = None
        self._player_stats_worker: Optional[ServiceWorker] = None
        self._current_replays: list = []
        self._current_replay: Any = None
        self._query_result: Any = None
        self._data_status: Any = None
        self._status_dialog: Any = None
        self._library_worker: Optional[ServiceWorker] = None
        self._env_worker: Optional[ServiceWorker] = None
        self._environment: Any = None
        self._env_seq = 0
        self._update_timer: Any = None
        # 启动名单检查：改为「扫描完成后触发」一次性调度（替代固定 15s 延时，
        # 避免冷启动全量扫描（50–165s）期间并发写 SQLite 触发 database is locked）。
        self._startup_update_pending = False
        self._startup_update_done = False
        # 首次使用流程：空库首启时先弹自绘弹窗①（扫描）→ 弹窗②（下载官方名单）。
        self._first_run_flow_active = False
        self._first_run_update_active = False
        # 更新失败提示保留窗口（秒）：期间状态栏不被扫描/查询进度静默覆盖。
        self._update_failure_message = ""
        self._update_failure_until = 0.0
        self._query_seq = 0
        self._scan_mode = "latest"
        # 综合战绩：本会话内缓存（选中玩家只展示缓存，不发请求）+ 胜负平标题状态。
        self._player_stats_cache: dict = {}
        self._player_stats_pending: Any = None
        self._outcome_label = ""
        self._outcome_kind = ""
        self._build_ui()
        self._apply_window_geometry()
        self._connect_signals()
        self._refresh_data_status()
        self._setup_update_timer()

    # ------------------------------------------------------------ 构建界面
    def _build_ui(self) -> None:
        self.setWindowTitle("%s v%s" % (self.service.cfg.app_name, self.service.cfg.app_version))
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())
        root.addWidget(self._build_replay_bar())
        root.addWidget(self._build_body(), 1)
        root.addWidget(self._build_footer())
        self.setStatusBar(self.statusBar())
        self.statusBar().showMessage("就绪：正在准备界面…")

    def _build_header(self) -> Any:
        """顶栏（单行）：标题 + 版本 + 三个操作按钮。

        v0.3 精简：原先的「数据更新 / 名单规模 / wgid 覆盖 / 缺失批次 / 上次检查」
        元信息行、「本地真实数据」徽标与「本机环境」路径信息行全部移除。
        1.0 定稿：再移除「数据状态」按钮，顶栏只留「重新检测 / 手动选择游戏目录 /
        更新名单」；数据状态页保留 ``open_data_status()`` 内部入口。
        """
        header = QFrame()
        header.setObjectName("Header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(18, 8, 18, 8)
        layout.setSpacing(10)

        title = QLabel(self.service.cfg.app_name)
        title.setObjectName("Title")
        version = QLabel("v%s" % self.service.cfg.app_version)
        version.setObjectName("SubTitle")
        layout.addWidget(title)
        layout.addWidget(version)
        layout.addStretch(1)
        self.redetect_button = QPushButton("重新检测")
        self.redetect_button.setObjectName("Ghost")
        self.redetect_button.setToolTip("重新探测游戏安装目录与我的账号（注册表卸载项 + 最新录像 meta）")
        self.manual_game_button = QPushButton("手动选择游戏目录")
        self.manual_game_button.setToolTip("手动指定《坦克世界》安装目录，选择后保存到 config.local.json")
        self.update_button = QPushButton("更新名单")
        self.update_button.setObjectName("Primary")
        self.update_button.setToolTip("检查官方名单接口并增量更新（启动时自动检查一次，运行期每 6 小时）")
        for button in (self.redetect_button, self.manual_game_button, self.update_button):
            layout.addWidget(button)
        return header

    def _build_replay_bar(self) -> Any:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(8)
        title = QLabel("录像区")
        title.setObjectName("SectionTitle")
        row.addWidget(title)
        self.scan_latest_button = QPushButton("最新完整录像")
        self.scan_latest_button.setObjectName("Primary")
        self.scan_all_button = QPushButton("全部录像")
        self.manual_button = QPushButton("手动选择…")
        for button in (self.scan_latest_button, self.scan_all_button, self.manual_button):
            row.addWidget(button)
        row.addSpacing(10)
        row.addWidget(QLabel("当前对局"))
        # 下拉弹窗最多显示 15 局，超出部分用竖向滚动条查看（见 widgets.ScrollableComboBox）
        self.replay_combo = ScrollableComboBox()
        self.replay_combo.setMinimumWidth(520)
        self.replay_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        row.addWidget(self.replay_combo, 1)
        self.replay_kind_badge = theme.make_badge("未选择", "none")
        row.addWidget(self.replay_kind_badge)
        layout.addLayout(row)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedWidth(300)
        self.progress_bar.setTextVisible(True)
        self.progress_label = QLabel("就绪：点击「最新完整录像」自动定位最近一个完整录像")
        self.progress_label.setObjectName("MetaHint")
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setVisible(False)
        progress_row.addWidget(self.progress_bar)
        progress_row.addWidget(self.progress_label, 1)
        progress_row.addWidget(self.cancel_button)
        layout.addLayout(progress_row)
        return card

    def _build_body(self) -> Any:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 8, 12, 4)
        layout.setSpacing(0)
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(self._build_table_area())
        self._splitter.addWidget(self._build_detail_area())
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setSizes([1420, 500])
        layout.addWidget(self._splitter)
        return container

    def _build_table_area(self) -> Any:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 6)
        layout.setSpacing(6)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        # 「查询结果」标题行：异步战绩到达后追加「· 平局 14v6」并染色（需求 4）。
        # 第二轮用户反馈：查询结果区域（标题 / 筛选控件 / 表格）一律不设悬停提示。
        self.result_title = QLabel(RESULT_TITLE_BASE)
        self.result_title.setObjectName("SectionTitle")
        filter_row.addWidget(self.result_title)
        self.id_only_check = QCheckBox("仅看 ID 命中")
        self.hits_only_check = QCheckBox("只看命中")
        filter_row.addWidget(self.id_only_check)
        filter_row.addWidget(self.hits_only_check)
        filter_row.addWidget(QLabel("标色"))
        self.level_combo = QComboBox()
        self.level_combo.addItems(["全部", "红标（≥2 次）", "黄标（1 次）"])
        self.level_combo.setFixedWidth(130)
        filter_row.addWidget(self.level_combo)
        filter_row.addStretch(1)
        self.count_label = QLabel("—")
        self.count_label.setObjectName("MetaHint")
        filter_row.addWidget(self.count_label)
        layout.addLayout(filter_row)

        self.stack = QStackedWidget()
        empty_widget = QWidget()
        empty_layout = QVBoxLayout(empty_widget)
        empty_layout.setContentsMargins(0, 40, 0, 40)
        empty_layout.setSpacing(10)
        empty_layout.addStretch(1)
        self.empty_title = QLabel("暂无对局数据")
        self.empty_title.setObjectName("EmptyState")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint = QLabel(
            "请先点击「最新完整录像」扫描录像；也可以「手动选择…」单个 .wotreplay 文件。"
        )
        self.empty_hint.setObjectName("EmptyHint")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_hint)
        empty_layout.addStretch(1)
        self.stack.addWidget(empty_widget)

        self.table = QTableView()
        self.table.setObjectName("ResultTable")
        # 选中行半透明叠加：不盖掉封禁黄/红底色与本人行浅蓝底色。
        self.table.setItemDelegate(ResultRowDelegate(self.table))
        self.table.setModel(self.model)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(RESULT_ROW_HEIGHT)
        self.table.verticalHeader().setMinimumSectionSize(18)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        header.setSortIndicatorShown(True)
        header.setFixedHeight(RESULT_HEADER_HEIGHT)
        header.setMinimumSectionSize(28)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # 结果表 7.5pt 小字号（24 列 + 30 人一屏）；代码设置兜底 QSS 字体（跨平台字体度量差异）。
        if QFont is not None:
            table_font = QFont(theme.FONT_FAMILY)
            table_font.setPointSizeF(RESULT_FONT_POINT_SIZE)
            self.table.setFont(table_font)
            header.setFont(table_font)
        for index, column in enumerate(COLUMNS):
            self.table.setColumnWidth(index, column["width"])
        # 表头标题逐字完整显示兜底：列宽不小于「标题文字宽度 + 内边距」（字体度量差异时自动加宽）。
        if QFontMetrics is not None:
            metrics_font = QFont(theme.FONT_FAMILY) if QFont is not None else header.font()
            if QFont is not None:
                metrics_font.setPointSizeF(RESULT_FONT_POINT_SIZE)
            metrics = QFontMetrics(metrics_font)
            for index, column in enumerate(COLUMNS):
                need = metrics.horizontalAdvance(column["title"]) + 6
                if need > self.table.columnWidth(index):
                    self.table.setColumnWidth(index, need)
        header.setSectionResizeMode(COLUMN_INDEX["reason"], QHeaderView.ResizeMode.Stretch)
        header.setSortIndicator(COLUMN_INDEX["ban_count"], Qt.SortOrder.DescendingOrder)
        self.stack.addWidget(self.table)
        layout.addWidget(self.stack, 1)

        self.summary_label = QLabel("—")
        self.summary_label.setObjectName("MetaHint")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        return card

    def _build_detail_area(self) -> Any:
        self.detail_panel = PlayerDetailPanel()
        self.detail_panel.setMinimumWidth(430)
        return self.detail_panel

    def _build_footer(self) -> Any:
        footer = QFrame()
        footer.setObjectName("Footer")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(18, 6, 18, 6)
        disclaimer = QLabel(theme.DISCLAIMER_TEXT)
        disclaimer.setObjectName("Disclaimer")
        disclaimer.setWordWrap(True)
        layout.addWidget(disclaimer)
        return footer

    def _apply_window_geometry(self) -> None:
        width, height = default_window_size(self.service.cfg)
        self.resize(width, height)
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.move(
                available.x() + max(0, (available.width() - width) // 2),
                available.y() + max(0, (available.height() - height) // 2),
            )

    def _apply_splitter_sizes(self) -> None:
        """按窗口实际宽度分配左右比例：结果表约 74%，明细面板约 26%（最小 430）。

        结果表 24 列在 1920 逻辑像素下尽量一屏横向放下；明细面板保持紧凑多列。
        """
        if not hasattr(self, "_splitter"):
            return
        total = self._splitter.width()
        if total <= 0:
            return
        right = max(430, int(total * 0.26))
        left = max(600, total - right - self._splitter.handleWidth())
        self._splitter.setSizes([left, right])

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_splitter_sizes)

    def _connect_signals(self) -> None:
        self.update_button.clicked.connect(self._on_update_clicked)
        self.scan_latest_button.clicked.connect(partial(self.start_scan, "latest"))
        self.scan_all_button.clicked.connect(partial(self.start_scan, "all"))
        self.manual_button.clicked.connect(self._on_manual_clicked)
        self.redetect_button.clicked.connect(self._on_redetect_environment_clicked)
        self.manual_game_button.clicked.connect(self._on_manual_game_dir_clicked)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        self.replay_combo.currentIndexChanged.connect(self._on_replay_index_changed)
        self.id_only_check.stateChanged.connect(self._on_filter_changed)
        self.hits_only_check.stateChanged.connect(self._on_filter_changed)
        self.level_combo.currentIndexChanged.connect(self._on_filter_changed)
        self.table.selectionModel().currentRowChanged.connect(self._on_current_row_changed)
        # 模型行结构变化（查询 / 筛选 / 战绩到达后重排）后重新跨列合并组头行。
        self.model.modelReset.connect(self._apply_group_spans)
        # 第二轮反馈：综合战绩只剩一个「刷新」按钮（force_refresh=True）。
        if self.detail_panel.player_stats_refresh_requested is not None:
            self.detail_panel.player_stats_refresh_requested.connect(
                partial(self._on_player_stats_requested, force=True)
            )

    # ------------------------------------------------------------ 后台 worker
    def _start_worker(self, title: str, fn: Any, on_success: Any, on_error: Any = None,
                      on_progress: Any = None) -> ServiceWorker:
        """启动一个后台 worker 并登记引用（结束后自动移除）。"""
        worker = ServiceWorker(fn, title=title, parent=self)
        key = id(worker)
        self._workers[key] = worker
        if on_progress is not None:
            worker.progressChanged.connect(on_progress)
        worker.succeeded.connect(partial(self._on_worker_success, key, on_success))
        worker.failed.connect(partial(self._on_worker_failed, key, on_error))
        worker.cancelled.connect(partial(self._on_worker_cancelled, key))
        worker.finished.connect(partial(self._release_worker, key))
        worker.start()
        return worker

    def _on_worker_success(self, key: int, callback: Any, result: Any) -> None:
        if key not in self._workers:
            return
        callback(result)

    def _on_worker_failed(self, key: int, callback: Any, message: str) -> None:
        if key not in self._workers:
            return
        if callback is not None:
            callback(message)
        else:
            self._show_error("操作失败", message)

    def _on_worker_cancelled(self, key: int) -> None:
        self._show_status_message("操作已取消", 5000)
        self._set_scan_busy(False)
        self._set_query_busy(False)
        # 启动全量扫描被取消：同样视为「扫描结束」，走首启弹窗②/启动名单检查。
        if self._scan_worker is not None and key == id(self._scan_worker):
            self._after_scan_completed()

    def _release_worker(self, key: int) -> None:
        """worker 结束：从登记表移除、清空窗口属性引用、再 deleteLater。

        F1 修复：6 个 ``self._xxx_worker`` 属性必须在结束时置 None，否则下一次
        操作在 ``isRunning()`` 处会访问已删除的 C++ 对象抛 RuntimeError。
        """
        worker = self._workers.pop(key, None)
        if worker is None:
            return
        self._clear_worker_ref(worker)
        worker.deleteLater()
        try:
            self._sync_action_buttons()
        except RuntimeError:  # 窗口已销毁
            pass

    def _clear_worker_ref(self, worker: Any) -> None:
        """把指向该 worker 的窗口属性统一置 None（幂等）。"""
        for name in WORKER_ATTR_NAMES:
            if getattr(self, name, None) is worker:
                setattr(self, name, None)

    def _worker_running(self, worker: Any) -> bool:
        """安全判断 worker 是否运行中：None / C++ 对象已销毁一律返回 False。"""
        if worker is None:
            return False
        try:
            return bool(worker.isRunning())
        except RuntimeError:  # libshiboken: Internal C++ object already deleted
            return False

    # ------------------------------------------------------------ 数据状态
    def _refresh_data_status(self) -> None:
        """读取名单库状态，缓存到 ``_data_status`` 供内部状态与状态页使用。

        1.0 定稿：顶栏已无「数据状态」入口，本方法仅保留为内部状态刷新；
        数据状态页仍可用 ``open_data_status()`` 直接打开（开发 / QA 脚本入口）。
        """
        try:
            status = self.service.get_data_status()
        except ServiceError as exc:
            self.statusBar().showMessage("读取名单库状态失败：%s" % exc, 8000)
            return
        self._data_status = status
        self._refresh_status_dialog()

    def open_data_status(self, modal: bool = True) -> Any:
        """打开名单库状态页（modal=True 模态；截图脚本用 modal=False）。"""
        try:
            status = self.service.get_data_status()
        except ServiceError as exc:
            self._show_error("读取数据状态失败", str(exc))
            return None
        dialog = DataStatusDialog(self)
        dialog.set_status(status)
        dialog.set_environment(self._environment or self.service.current_environment())
        if dialog.clear_requested is not None:
            dialog.clear_requested.connect(partial(self._on_clear_library_requested, dialog))
            dialog.rebuild_requested.connect(partial(self._on_rebuild_library_requested, dialog))
            dialog.open_data_dir_requested.connect(self._on_open_data_dir)
            dialog.open_log_dir_requested.connect(self._on_open_log_dir)
        self._status_dialog = dialog
        if modal:
            dialog.exec()
        else:
            dialog.show()
        return dialog

    def _refresh_status_dialog(self) -> None:
        """状态页打开时同步刷新其内容。"""
        dialog = getattr(self, "_status_dialog", None)
        if dialog is None or not dialog.isVisible():
            return
        try:
            dialog.set_status(self.service.get_data_status())
            dialog.set_environment(self._environment or self.service.current_environment())
        except ServiceError as exc:
            dialog.set_action_message("刷新数据状态失败：%s" % exc)

    # ------------------------------------------------------------ 数据管理（M6）
    def _on_open_data_dir(self) -> None:
        self._open_directory(self.service.data_dir(), "数据目录")

    def _on_open_log_dir(self) -> None:
        self._open_directory(self.service.log_dir(), "日志目录")

    def _open_directory(self, path: Any, label: str) -> None:
        """在资源管理器中打开目录（不存在则创建）。"""
        target = Path(path)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._show_error("打开%s失败" % label, "无法创建目录 %s：%s" % (target, exc))
            return
        if QDesktopServices is not None and QUrl is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        self.statusBar().showMessage("已打开%s：%s" % (label, target), 8000)

    def _library_action_running(self) -> bool:
        return self._worker_running(self._library_worker)

    def _on_clear_library_requested(self, dialog: Any) -> None:
        """清空名单库：二次确认 + 后台执行 + 进度 + 中文提示。"""
        if self._library_action_running():
            dialog.set_action_message("已有数据管理操作正在执行，请稍候…")
            return
        try:
            current = self.service.get_data_status()
            count_text = "{:,}".format(current.record_count)
        except ServiceError:
            count_text = "未知"
        first = QMessageBox.warning(
            self,
            "清空名单库",
            "即将清空本地名单库（库内 %s 条）。此操作不可撤销，清空后必须「重建名单库」才能查询封禁记录。\n\n确认继续？"
            % count_text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if first != QMessageBox.StandardButton.Yes:
            dialog.set_action_message("已取消清空名单库")
            return
        second = QMessageBox.warning(
            self,
            "再次确认：清空名单库",
            "二次确认：确定清空名单库？清空期间与清空后，查询将查不到任何历史封禁记录。\n\n仍要继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if second != QMessageBox.StandardButton.Yes:
            dialog.set_action_message("已取消清空名单库（二次确认未通过）")
            return
        fn = lambda progress, should_cancel: self.service.clear_ban_library(  # noqa: E731
            progress_cb=progress
        )
        self._run_library_action(dialog, "清空名单库", fn)

    def _on_rebuild_library_requested(self, dialog: Any) -> None:
        """重建名单库：确认 + 后台归档导入 + 进度 + 中文提示。"""
        if self._library_action_running():
            dialog.set_action_message("已有数据管理操作正在执行，请稍候…")
            return
        answer = QMessageBox.question(
            self,
            "重建名单库",
            "将从本地归档（96 期 CSV + 18 期 xlsx，约 15 MB）重新导入并覆盖当前名单库。\n"
            "期间界面仍可查看录像，但名单查询结果可能暂时为空。\n\n确认重建？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            dialog.set_action_message("已取消重建名单库")
            return
        fn = lambda progress, should_cancel: self.service.rebuild_ban_library(  # noqa: E731
            progress_cb=progress, cancel_cb=should_cancel
        )
        self._run_library_action(dialog, "重建名单库", fn)

    def _run_library_action(self, dialog: Any, title: str, fn: Any) -> None:
        """在后台线程执行数据管理操作，并把进度同步到状态页 / 状态栏。"""
        dialog.set_actions_enabled(False)
        dialog.set_action_message("正在%s…" % title)
        self.statusBar().showMessage("正在%s…" % title)
        self._library_worker = self._start_worker(
            title,
            fn,
            on_success=partial(self._on_library_action_finished, dialog, title),
            on_error=partial(self._on_library_action_failed, dialog, title),
            on_progress=partial(self._on_library_action_progress, dialog),
        )

    def _on_library_action_progress(self, dialog: Any, percent: int, message: str) -> None:
        dialog.set_action_message("%d%% · %s" % (int(percent), message))
        self.statusBar().showMessage(message, 5000)

    def _on_library_action_finished(self, dialog: Any, title: str, result: Any) -> None:
        message = ""
        if isinstance(result, dict):
            message = str(result.get("message") or "")
        if not message:
            message = "%s完成" % title
        dialog.set_actions_enabled(True)
        dialog.set_action_message(message)
        self._refresh_data_status()
        self.statusBar().showMessage(message, 15000)
        QMessageBox.information(self, title, message)

    def _on_library_action_failed(self, dialog: Any, title: str, message: str) -> None:
        dialog.set_actions_enabled(True)
        dialog.set_action_message(message)
        self.statusBar().showMessage(message, 15000)
        QMessageBox.warning(self, "%s失败" % title, message)

    # ------------------------------------------------------------ A1 自动检查
    def _setup_update_timer(self) -> None:
        """A1：运行期按配置间隔（默认 6 小时）自动检查名单。"""
        if QTimer is None:
            return
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(self.update_timer_interval_ms())
        self._update_timer.timeout.connect(self._on_scheduled_update)
        self._update_timer.start()

    def update_timer_interval_ms(self) -> int:
        """自动检查定时器间隔（毫秒），供界面显示与测试校验。"""
        return int(self.service.update_interval_seconds * 1000)

    def _on_scheduled_update(self) -> None:
        """定时器到点：静默检查一次（force=False，不打断本地查询）。

        扫描进行中不并发检查（冷启动全量扫描 12,851 个录像可能持续 50–165s，
        并发写 SQLite 会 ``database is locked``）；本次顺延到扫描完成后。
        """
        if self._worker_running(self._update_worker):
            return
        if self._worker_running(self._scan_worker):
            self._show_status_message("正在扫描录像，名单自动检查已顺延到扫描完成后…", 8000)
            return
        self._show_status_message("已到自动检查间隔，正在后台检查名单更新…", 8000)
        self._start_update_check(silent=True, force=False)

    def trigger_scheduled_update(self) -> None:
        """手动触发一次「定时检查」逻辑（测试 / 截图脚本用）。"""
        self._on_scheduled_update()

    # ------------------------------------------------------------ 启动检查（扫描后触发）
    def _schedule_startup_update_check(self) -> None:
        """启动后名单检查：扫描结束后统一调度一次（P0 修复，替代固定 15s 定时器）。

        新用户首启冷启动全量扫描 12,851 个录像耗时 50.8–165.6s；原实现用固定
        ``QTimer.singleShot(15000)`` 在扫描进行中触发更新检查，导致扫描线程与更新
        线程并发写 SQLite → ``database is locked``、官方请求 0 次、名单库为空。
        现在只在扫描 worker 结束（成功/失败/取消）后经事件循环调度一次，确保扫描
        线程已结束、DB 不再被其占用；``_startup_update_done`` 保证仅触发一次，
        不与 6h ``_update_timer`` 重复。
        """
        if not self._startup_update_pending or self._startup_update_done:
            return
        self._startup_update_done = True
        if QTimer is None:
            self._start_update_check(silent=True, force=False)
            return
        # singleShot(0)：等当前事件（含 worker finished / _release_worker）处理完再启动。
        QTimer.singleShot(0, partial(self._start_update_check, True, False))

    # ------------------------------------------------------------ 首次使用流程（空库首启）
    def _safe_data_status(self) -> Any:
        """读取名单库状态；失败返回 None（首次使用判定降级为老用户流程）。"""
        try:
            return self.service.get_data_status()
        except Exception:  # noqa: BLE001 - 状态读取失败不得阻断启动
            return None

    def _meta_get(self, key: str, default: Any = None) -> Any:
        """读取 ``data/wotban.db`` 的 meta（首次使用标记持久化用）。"""
        try:
            try:
                from ..store import db as db_module
            except ImportError:
                from src.store import db as db_module
            return db_module.get_meta(self.service._thread_conn(), key, default)
        except Exception:  # noqa: BLE001 - 读取失败按未完成处理
            return default

    def _meta_set(self, key: str, value: Any) -> None:
        """写入 ``data/wotban.db`` 的 meta（幂等；失败不阻断界面）。"""
        try:
            try:
                from ..store import db as db_module
            except ImportError:
                from src.store import db as db_module
            db_module.set_meta(self.service._thread_conn(), key, value)
        except Exception:  # noqa: BLE001
            pass

    def _record_count(self) -> int:
        """当前名单库条数（用最近一次刷新的数据状态）。"""
        status = getattr(self, "_data_status", None)
        return int(getattr(status, "record_count", 0) or 0)

    def _first_run_flow_state(self) -> dict:
        """首次使用流程状态。

        - 老用户（已有名单数据）或首启流程已完成 → 不弹窗，走 1.0.1 自动流程；
        - 空库且首启未完成：无录像索引 → 弹窗①（先扫描）；已有录像索引 → 直接弹窗②。
        """
        if getattr(self.service, "is_mock", False):
            return {"active": False}
        status = self._safe_data_status()
        if status is None:
            return {"active": False}
        if int(getattr(status, "record_count", 0) or 0) > 0:
            return {"active": False}
        if bool(self._meta_get(FIRST_RUN_FLOW_META_KEY, False)):
            return {"active": False}
        if int(getattr(status, "replay_index_count", 0) or 0) > 0:
            return {"active": True, "need_scan": False, "need_update": True}
        return {"active": True, "need_scan": True, "need_update": False}

    def _ask_first_run_scan(self) -> bool:
        """弹软件内自绘弹窗①（无系统消息框/提示音）；测试可 monkeypatch。"""
        dialog = FirstRunScanDialog(self)
        return dialog.exec_centered() == QDialog.DialogCode.Accepted

    def _ask_first_run_update(self, error_message: str = "") -> bool:
        """弹软件内自绘弹窗②；``error_message`` 非空时显示失败原因并允许重试。"""
        dialog = FirstRunUpdateDialog(self, error_message=error_message)
        return dialog.exec_centered() == QDialog.DialogCode.Accepted

    def _start_first_run_flow(self, flow: dict) -> None:
        """首次使用流程：不自动扫描，先弹窗①；扫描结束后再弹窗②。"""
        if flow.get("need_scan"):
            self._show_empty(
                "首次使用",
                "首次使用需要先扫描录像并下载官方封禁名单：请按弹窗提示操作。",
            )
            self.progress_label.setText("首次使用：等待确认开始扫描录像…")
            if not self.service.replay_dir_available():
                self.progress_label.setText(
                    "首次使用：未找到录像目录，请先点「重新检测」或「手动选择游戏目录」")
                return
            if self._ask_first_run_scan():
                self._first_run_flow_active = True
                self.progress_label.setText("首次使用：正在扫描录像（冷启动约 1–3 分钟）…")
                self.start_scan("all")
            else:
                self.progress_label.setText("首次使用：已跳过扫描（下次启动会再次提示）")
            return
        self._show_empty(
            "首次使用",
            "首次使用还需要下载官方公开封禁名单：请按弹窗提示操作。",
        )
        self.progress_label.setText("首次使用：等待确认下载官方封禁名单…")
        self._first_run_flow_active = False
        self._show_first_run_update_dialog()

    def _show_first_run_update_dialog(self, error_message: str = "") -> None:
        """弹窗②：名单库仍为空时提示下载官方名单；点击后执行更新（失败可重试）。"""
        status = self._safe_data_status()
        if status is not None and int(getattr(status, "record_count", 0) or 0) > 0:
            self._mark_first_run_flow_done()
            return
        if self._ask_first_run_update(error_message=error_message):
            self._first_run_update_active = True
            self.progress_label.setText(
                "首次使用：正在下载官方封禁名单（约 5–6 分钟，只读低频）…")
            self._start_update_check(silent=True, force=True)
        else:
            self._first_run_update_active = False
            self.progress_label.setText("首次使用：已跳过名单下载（下次启动会再次提示）")

    def _mark_first_run_flow_done(self) -> None:
        """首启流程完成：持久化标记（名单更新成功且 bans>0 后写入）。"""
        self._first_run_flow_active = False
        self._first_run_update_active = False
        self._meta_set(FIRST_RUN_FLOW_META_KEY, True)

    def _after_scan_completed(self) -> None:
        """扫描结束（成功/失败/取消）统一入口。

        - 首启流程：不自动更新，改为弹窗②（名单库仍为空时）；
        - 老用户：走「扫描完成后串行触发启动名单检查」（P0 防御，避免并发写库）。
        """
        if self._first_run_flow_active:
            self._first_run_flow_active = False
            self._show_first_run_update_dialog()
            return
        self._schedule_startup_update_check()

    # ------------------------------------------------------------ 更新名单
    def _on_update_clicked(self) -> None:
        """手动「更新名单」按钮：force=True 强制检查官方接口（防重入 + 中文提示）。"""
        if self._worker_running(self._update_worker):
            self.statusBar().showMessage("正在更新名单，请稍候…", 8000)
            return
        if self._worker_running(self._scan_worker):
            self.statusBar().showMessage(
                "正在扫描录像（冷启动可能约需 1 分钟），请等扫描完成后再更新名单", 12000)
            return
        self._start_update_check(silent=False, force=True)

    def _start_update_check(self, silent: bool = False, force: bool = True,
                            _retry: int = 0) -> None:
        """启动名单更新检查（silent=True 时不弹结果框，仅状态栏提示）。

        force=False 用于启动检查与 6 小时定时检查（未到间隔则直接返回「未到检查间隔」）。
        扫描 worker 未释放时静默顺延（每 100ms 重试，最多约 5s），保证不与扫描并发写库。
        """
        if self._worker_running(self._update_worker):
            if not silent:
                self.statusBar().showMessage("正在更新名单，请稍候…", 8000)
            return
        if self._worker_running(self._scan_worker):
            if _retry < 50 and QTimer is not None:
                QTimer.singleShot(
                    100, partial(self._start_update_check, silent, force, _retry + 1))
            return
        self._clear_update_failure()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_label.setText(
            "正在检查官方名单接口…（%s）" % ("强制更新" if force else "自动检查")
        )
        self.statusBar().showMessage(
            "正在检查官方名单接口…（%s）" % ("强制更新" if force else "自动检查"), 8000)
        fn = lambda progress, should_cancel: self.service.update_ban_list(  # noqa: E731
            force=force, progress_cb=progress, cancel_cb=should_cancel
        )
        self._update_worker = self._start_worker(
            "更新名单", fn,
            on_success=partial(self._on_update_finished, silent),
            on_error=partial(self._on_update_failed, silent),
            on_progress=self._on_update_progress,
        )
        self._sync_action_buttons()

    def _on_update_progress(self, percent: int, message: str) -> None:
        self.progress_bar.setValue(int(percent))
        self.progress_label.setText(message)

    def _mark_update_failure(self, message: str) -> None:
        """记录更新失败信息：状态栏保留 60s，期间不被扫描/查询进度静默覆盖。"""
        self._update_failure_message = str(message or "更新名单失败")
        self._update_failure_until = time.monotonic() + 60.0
        self.statusBar().showMessage(self._update_failure_message, 60000)

    def _clear_update_failure(self) -> None:
        """清除失败保留标记（新一轮更新开始时调用，允许用户手动重试）。"""
        self._update_failure_message = ""
        self._update_failure_until = 0.0

    def _update_failure_visible(self) -> bool:
        """更新失败提示是否仍在保留窗口内。"""
        return bool(self._update_failure_message) and time.monotonic() < self._update_failure_until

    def _show_status_message(self, message: str, timeout: int = 8000,
                             force: bool = False) -> None:
        """状态栏提示（更新失败保留期内不覆盖失败信息，避免被进度静默吞掉）。"""
        if self._update_failure_visible() and not force:
            return
        self.statusBar().showMessage(message, timeout)

    def _sync_action_buttons(self) -> None:
        """统一刷新操作按钮状态：扫描/更新互斥，防重入（含取消按钮显隐）。"""
        scan_running = self._worker_running(self._scan_worker)
        update_running = self._worker_running(self._update_worker)
        idle = not scan_running and not update_running
        available = self.service.replay_dir_available()
        for button in (self.scan_latest_button, self.scan_all_button, self.manual_button):
            button.setEnabled(bool(available) and idle)
        self.cancel_button.setVisible(scan_running or update_running)
        self.update_button.setEnabled(idle)
        self.update_button.setText("正在更新…" if update_running else "更新名单")

    def _on_update_finished(self, silent: bool, result: Any) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        message = getattr(result, "message", "") or "检查完成"
        errors = list(getattr(result, "errors", []) or [])
        if errors:
            message = "%s；异常：%s" % (message, "；".join(errors[:3]))
        failed = (not getattr(result, "ok", True)) or bool(errors)
        self.progress_label.setText(message)
        if failed:
            self._mark_update_failure("更新名单失败：%s" % message)
        else:
            self._clear_update_failure()
            self._show_status_message("更新名单：%s" % message, 15000, force=True)
        self._refresh_data_status()
        self._sync_action_buttons()
        first_run_update = self._first_run_update_active
        self._first_run_update_active = False
        # 首启流程：名单更新成功且 bans>0 后写持久化完成标记（老用户升级不受影响）。
        if not failed and self._record_count() > 0:
            if not bool(self._meta_get(FIRST_RUN_FLOW_META_KEY, False)):
                self._mark_first_run_flow_done()
        if first_run_update:
            if failed or self._record_count() <= 0:
                reason = message if failed else "官方名单下载完成，但名单库仍为空，请重试"
                # 失败不静默：状态栏已保留中文错误 + 软件内自绘弹窗允许重试（无系统提示音）。
                self._show_first_run_update_dialog(error_message=reason)
            else:
                self._show_status_message(
                    "首次使用完成：名单库已就绪（%s 条）"
                    % "{:,}".format(self._record_count()), 15000, force=True)
        if not silent:
            if failed:
                QMessageBox.warning(self, "更新名单失败", message)
            else:
                QMessageBox.information(self, "更新名单", message)

    def _on_update_failed(self, silent: bool, message: str) -> None:
        self.progress_label.setText(message)
        self._mark_update_failure(message)
        self._sync_action_buttons()
        first_run_update = self._first_run_update_active
        self._first_run_update_active = False
        if first_run_update:
            # 首启流程更新失败：状态栏保留错误 + 弹软件内自绘重试弹窗（不静默、无提示音）。
            self._show_first_run_update_dialog(error_message=message)
        if not silent:
            QMessageBox.warning(self, "更新名单失败", message)

    # ------------------------------------------------------------ 录像扫描
    def start_scan(self, mode: str = "latest") -> None:
        """扫描录像（mode: latest / all；recent 能力仍保留在 service，1.0 UI 无入口）。"""
        if self._worker_running(self._scan_worker):
            return
        if self._worker_running(self._update_worker):
            self.statusBar().showMessage("正在更新名单，请稍候再扫描录像…", 10000)
            return
        if not self.service.is_mock and not self.service.replay_dir_available():
            self._apply_replay_dir_state()
            return
        self._scan_mode = mode
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        days = int(self.service.cfg.default_scan_days)
        fn = lambda progress, should_cancel: self.service.list_replays(  # noqa: E731
            mode=mode, days=days, progress_cb=progress, cancel_cb=should_cancel
        )
        self._scan_worker = self._start_worker(
            "扫描录像", fn,
            on_success=partial(self._on_scan_finished, mode),
            on_error=self._on_scan_failed,
            on_progress=self._on_scan_progress,
        )
        self._sync_action_buttons()

    def _set_scan_busy(self, busy: bool) -> None:
        if busy:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
        else:
            self.progress_bar.setRange(0, 100)
        self._sync_action_buttons()

    def _on_scan_progress(self, percent: int, message: str) -> None:
        self.progress_bar.setValue(int(percent))
        self.progress_label.setText(message)

    def _on_scan_finished(self, mode: str, replays: list) -> None:
        self._set_scan_busy(False)
        self._current_replays = list(replays or [])
        self.replay_combo.blockSignals(True)
        self.replay_combo.clear()
        for replay in self._current_replays:
            self.replay_combo.addItem(self._localized_replay_label(replay), replay.path)
        target_index = 0
        if self._current_replays:
            if mode == "latest":
                latest = latest_complete_replay(self._current_replays)
                if latest is not None:
                    for index, replay in enumerate(self._current_replays):
                        if replay.path == latest.path:
                            target_index = index
                            break
            self.replay_combo.setCurrentIndex(target_index)
        self.replay_combo.blockSignals(False)

        if not self._current_replays:
            self._show_empty(
                "未找到录像文件",
                "录像目录（%s）中没有找到 .wotreplay 文件：请确认游戏已产生录像，或点击「手动选择…」指定文件。"
                % self.service.cfg.replay_dir,
            )
            self.progress_label.setText("扫描完成：0 个录像")
            self._show_status_message("扫描完成：未找到录像文件", 8000)
            self._after_scan_completed()
            return
        complete_count = len([item for item in self._current_replays if not item.is_partial])
        partial_count = len(self._current_replays) - complete_count
        self.progress_label.setText(
            "扫描完成：%d 个录像（完整 %d / partial %d）"
            % (len(self._current_replays), complete_count, partial_count)
        )
        self._show_status_message(self.progress_label.text(), 8000)
        if target_index >= 0:
            self._on_replay_index_changed(target_index)
        # P0 修复：全量扫描结束（无论成功/空/失败/取消）后再触发启动名单检查或首启弹窗②，
        # 保证更新线程不会与扫描线程并发写 SQLite（database is locked）。
        self._after_scan_completed()

    def _on_scan_failed(self, message: str) -> None:
        self._set_scan_busy(False)
        self.progress_label.setText(message)
        self._show_empty("扫描录像失败", message)
        self._show_status_message("扫描录像失败：%s" % message, 15000)
        QMessageBox.warning(self, "扫描录像失败", message)
        # 扫描失败同样视为「扫描结束」：启动名单检查/首启弹窗②不能被跳过。
        self._after_scan_completed()

    def _on_cancel_clicked(self) -> None:
        scan_worker = self._scan_worker
        if self._worker_running(scan_worker):
            scan_worker.cancel()
            self.progress_label.setText("正在取消扫描…")
        update_worker = self._update_worker
        if self._worker_running(update_worker):
            update_worker.cancel()
            self.progress_label.setText("正在取消更新…")

    def _on_manual_clicked(self) -> None:
        replay_dir = self.service._replay_dir_path()
        initial = str(replay_dir) if replay_dir is not None else str(Path.home())
        path, _selected = QFileDialog.getOpenFileName(
            self, "选择坦克世界录像文件", initial,
            "坦克世界录像 (*.wotreplay);;所有文件 (*)",
        )
        if not path:
            return
        self.open_replay_path(path)

    def open_replay_path(self, replay_path: str) -> bool:
        """手动载入单个录像文件（真实截图 / 测试脚本复用）。"""
        if not replay_path:
            return False
        if self._worker_running(self._update_worker):
            self.statusBar().showMessage("正在更新名单，请稍候再解析录像…", 10000)
            return False
        self.progress_label.setText("正在解析手动选择的录像…")
        self.progress_bar.setRange(0, 0)
        fn = lambda progress, should_cancel: self.service.list_replays(  # noqa: E731
            mode="file", manual_path=replay_path, progress_cb=progress, cancel_cb=should_cancel
        )
        self._scan_worker = self._start_worker(
            "解析录像文件", fn,
            on_success=partial(self._on_manual_finished, replay_path),
            on_error=self._on_scan_failed,
            on_progress=self._on_scan_progress,
        )
        self._sync_action_buttons()
        return True

    def _on_manual_finished(self, replay_path: str, replays: list) -> None:
        self._set_scan_busy(False)
        self.progress_bar.setRange(0, 100)
        if replays:
            for replay in replays:
                existing = [index for index in range(self.replay_combo.count())
                            if self.replay_combo.itemData(index) == replay.path]
                if existing:
                    self.replay_combo.setCurrentIndex(existing[0])
                else:
                    self._current_replays.insert(0, replay)
                    self.replay_combo.insertItem(0, self._localized_replay_label(replay), replay.path)
                    self.replay_combo.setCurrentIndex(0)
            self.progress_label.setText("已载入手动选择的录像：%s" % Path(replay_path).name)
        else:
            self._show_error("解析录像失败", "未能解析该录像文件：%s" % replay_path)

    # ------------------------------------------------------------ 单局查询
    def _on_replay_index_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._current_replays):
            return
        replay = self._current_replays[index]
        self._current_replay = replay
        if replay.is_partial:
            theme.set_badge(self.replay_kind_badge, "partial · 未打完（仅昵称匹配）", "partial")
        else:
            theme.set_badge(self.replay_kind_badge, "完整 · %d 人（可核对账号 ID）" % int(replay.player_count or 0),
                            "complete")
        self._start_query(replay.path)

    def _start_query(self, path: str) -> None:
        self._query_seq += 1
        seq = self._query_seq
        self._set_query_busy(True)
        self.model.set_stats("idle")
        self._set_outcome_title("", "", kind="")
        fn = lambda progress, should_cancel: self.service.query_replay(path)  # noqa: E731
        self._query_worker = self._start_worker(
            "查询录像", fn,
            on_success=partial(self._on_query_finished, seq),
            on_error=partial(self._on_query_failed, seq),
        )

    def _set_query_busy(self, busy: bool) -> None:
        if busy:
            self.progress_bar.setRange(0, 0)
            self.progress_label.setText("正在解析录像并匹配名单…")
            self._show_status_message("正在解析录像并匹配名单…", 0)
        else:
            self.progress_bar.setRange(0, 100)

    def _on_query_finished(self, seq: int, result: Any) -> None:
        if seq != self._query_seq:
            return
        self._set_query_busy(False)
        self._query_result = result
        # 新对局：标题先回到基础文案（胜负平随异步战绩到达后再显示）。
        self._set_outcome_title("", "", kind="")
        self.model.set_rows(result.matches, result.replay)
        self._apply_filters()
        self.stack.setCurrentIndex(1)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.progress_label.setText(result.message)
        self._show_status_message(result.message, 10000)
        self._refresh_current_replay_label(result.replay)
        self._select_first_player()
        self._refresh_summary()
        self._request_battle_stats(seq, result.replay)

    def _on_query_failed(self, seq: int, message: str) -> None:
        if seq != self._query_seq:
            return
        self._set_query_busy(False)
        self.progress_label.setText(message)
        self._show_empty("查询对局失败", "%s\n\n排查建议：确认录像文件完整、名单库已更新；partial 录像仍可查询（仅昵称匹配）。" % message)
        self._show_error("查询对局失败", message)

    # ------------------------------------------------------------ 当局战绩
    def _request_battle_stats(self, seq: int, detail: Any) -> None:
        """按需请求当局战绩（打开某局才请求；partial 局同样异步请求，由数据层决定）。"""
        if detail is None:
            return
        self._set_outcome_title("", "", kind="loading")
        fn = lambda progress, should_cancel: self.service.get_battle_stats(detail)  # noqa: E731
        self._battle_worker = self._start_worker(
            "获取当局战绩", fn,
            on_success=partial(self._on_battle_finished, seq),
            on_error=partial(self._on_battle_failed, seq),
        )

    def _on_battle_finished(self, seq: int, result: Any) -> None:
        if seq != self._query_seq:
            return
        selected = self.selected_player()
        if getattr(result, "available", False) and getattr(result, "stats", None) is not None:
            stats_map = {}
            for player in getattr(result.stats, "players", []) or []:
                if getattr(player, "dbid", None) is not None:
                    stats_map[("id", int(player.dbid))] = player
                if getattr(player, "nickname", None):
                    stats_map[("nick", player.nickname)] = player
            self.model.set_stats(STATS_STATE_READY, stats_map, getattr(result, "message", ""))
        else:
            self.model.set_stats(
                STATS_STATE_UNAVAILABLE, {},
                getattr(result, "message", "当局战绩不可用"),
            )
        # 胜负平：优先 BattleStatsResult.outcome_label/score（partial 局也可用），
        # 盒子不可用时数据层仍会用录像 winner_team 给出 outcome_label。
        self._apply_outcome_from_result(result)
        self._reselect_player(selected)
        self._refresh_selected_detail()

    def _on_battle_failed(self, seq: int, message: str) -> None:
        if seq != self._query_seq:
            return
        self._set_battle_unavailable(message)

    def _set_battle_unavailable(self, message: str) -> None:
        selected = self.selected_player()
        self.model.set_stats(STATS_STATE_UNAVAILABLE, {}, message)
        self._set_outcome_title("", "", kind="")
        self.statusBar().showMessage(message, 10000)
        self._reselect_player(selected)
        self._refresh_selected_detail()

    def _apply_outcome_from_result(self, result: Any) -> None:
        """从 BattleStatsResult（或 BattleStats）提取胜负平文案并更新标题行。"""
        stats = getattr(result, "stats", None)
        label = getattr(result, "outcome_label", "") or getattr(stats, "outcome_label", "") or ""
        score = getattr(result, "score", "") or getattr(stats, "score", "") or ""
        label = str(label).strip()
        score = str(score).strip()
        if not label:
            self._set_outcome_title("", "", kind="")
            return
        kind = OUTCOME_KIND_MAP.get(label, "")
        self._set_outcome_title(label, score, kind)

    def _set_outcome_title(self, label: str, score: str = "", kind: str = "") -> None:
        """更新「查询结果」标题行：胜利绿 / 平局橙 / 失败红；加载中灰色占位。"""
        self._outcome_label = label or ""
        self._outcome_kind = kind or ""
        if label:
            text = "%s · %s" % (RESULT_TITLE_BASE, label)
            if score:
                text += " %s" % score
        elif kind == "loading":
            text = "%s · 战绩加载中…" % RESULT_TITLE_BASE
        else:
            text = RESULT_TITLE_BASE
        self.result_title.setText(text)
        self.result_title.setProperty("outcome", kind if kind in OUTCOME_TITLE_KINDS else "")
        style = self.result_title.style()
        style.unpolish(self.result_title)
        style.polish(self.result_title)
        self.result_title.update()

    def current_outcome_text(self) -> str:
        """「查询结果」标题行当前文案（测试 / 截图脚本用）。"""
        return self.result_title.text()

    # ------------------------------------------------------------ 分组 / 地图名辅助
    def _apply_group_spans(self) -> None:
        """组头行跨列合并（跨全部列），不参与排序 / 明细联动。"""
        if not hasattr(self, "table") or not hasattr(self, "model"):
            return
        self.table.clearSpans()
        column_count = len(COLUMNS)
        for row in range(self.model.rowCount()):
            if self.model.is_group_row(row):
                self.table.setSpan(row, 0, 1, column_count)

    # ------------------------------------------------------------ 地图 / 车名辅助
    @staticmethod
    def _game_names_module() -> Any:
        """惰性导入 ``src.game_names``；模块未交付 / 导入失败返回 None（绝不抛异常）。"""
        try:
            from .. import game_names  # type: ignore[attr-defined]

            return game_names
        except Exception:  # noqa: BLE001 - 模块未交付时优雅降级
            try:
                from src import game_names  # type: ignore[attr-defined]

                return game_names
            except Exception:  # noqa: BLE001
                return None

    @classmethod
    def _game_vehicle_name(cls, tank_code: Any) -> str:
        """录像 tank 代码 -> 游戏车名；映射失败返回空串（**绝不回退显示代码**）。"""
        if not tank_code:
            return ""
        module = cls._game_names_module()
        if module is None:
            return ""
        try:
            name = module.vehicle_name(str(tank_code))
        except Exception:  # noqa: BLE001 - 映射失败不影响界面
            return ""
        return str(name) if name else ""

    @classmethod
    def _localized_map_name(cls, code: Any) -> str:
        """地图代码 -> 游戏中文名；映射模块缺失 / 无结果时原样返回（不显示报错）。"""
        if not code:
            return ""
        text = str(code)
        module = cls._game_names_module()
        if module is None:
            return text
        try:
            name = module.map_name(text)
        except Exception:  # noqa: BLE001 - 映射失败不影响界面
            return text
        return str(name) if name else text

    @classmethod
    def _recorder_vehicle_name(cls, detail: Any) -> str:
        """录像者本局车名：players 按 recorder_dbid 优先、recorder_name 兜底反查 tank。

        仅返回 ``game_names.vehicle_name()`` 映射出的名称；映射失败返回空串，
        调用方必须省略该项而不是显示原始代码。
        """
        if detail is None:
            return ""
        players = list(getattr(detail, "players", None) or [])
        recorder_dbid = getattr(detail, "recorder_dbid", None)
        recorder_name = getattr(detail, "recorder_name", None)
        tank = None
        if recorder_dbid is not None:
            for player in players:
                dbid = getattr(player, "dbid", None)
                if dbid is not None and int(dbid) == int(recorder_dbid):
                    tank = getattr(player, "tank", None)
                    break
        if tank is None and recorder_name:
            for player in players:
                if getattr(player, "nickname", None) == recorder_name:
                    tank = getattr(player, "tank", None)
                    break
        return cls._game_vehicle_name(tank)

    @classmethod
    def _vehicle_name_from_replay_path(cls, path: Any) -> str:
        """从录像文件名提取车辆代码 -> 车名（下拉列表 ReplayInfo 无解析结果时用）。

        文件名形如 ``20261115_0108_japan-J20_Type_2605_99_poland.wotreplay``；
        车辆代码与地图代码都可能含下划线，因此只在「车辆代码可映射 **且** 地图代码
        可映射」的切分点取车名；提取 / 映射失败返回空串（绝不显示代码）。
        """
        if not path:
            return ""
        file_name = Path(str(path)).name
        if not file_name.lower().endswith(".wotreplay"):
            return ""
        stem = file_name[: -len(".wotreplay")]
        parts = stem.split("_")
        if len(parts) < 4:
            return ""
        module = cls._game_names_module()
        if module is None:
            return ""
        rest = parts[2:]  # 前两段是日期 / 时间
        for split in range(1, len(rest)):
            code = "_".join(rest[:split])
            map_code = "_".join(rest[split:])
            try:
                vehicle = module.vehicle_name(code)
                map_ok = module.map_name(map_code)
            except Exception:  # noqa: BLE001 - 映射失败按无法提取处理
                return ""
            if vehicle and map_ok:
                return str(vehicle)
        return ""

    @classmethod
    def _localized_replay_label(cls, replay: Any) -> str:
        """录像下拉文案：中文地图名 + 本人本局车名（只显示名称，绝不显示代码）。"""
        if replay is None:
            return ""
        base = replay_label(replay)
        raw = getattr(replay, "map_name", None)
        display = getattr(replay, "map_display_name", None) or cls._localized_map_name(raw)
        marker = str(display) if display else (str(raw) if raw else "未知地图")
        if raw and display and str(display) != str(raw):
            base = base.replace(str(raw), str(display), 1)
        # 车名：有解析结果（players）时按录像者反查；ReplayInfo 从文件名提取。
        vehicle = cls._recorder_vehicle_name(replay) or cls._vehicle_name_from_replay_path(
            getattr(replay, "path", None)
        )
        if vehicle:
            anchor = "%s · " % marker
            if anchor in base:
                base = base.replace(anchor, "%s · %s · " % (marker, vehicle), 1)
        return base

    def _refresh_current_replay_label(self, detail: Any) -> None:
        """查询完成后刷新「当前对局」文案：中文地图名 + 本人本局车名。"""
        if detail is None:
            return
        index = self.replay_combo.currentIndex()
        if index < 0:
            return
        self.replay_combo.setItemText(index, self._localized_replay_label(detail))

    # ------------------------------------------------------------ 选中行辅助
    def _select_first_player(self) -> None:
        """选中第一名玩家行（跳过组头行）。"""
        row = self.model.first_player_row()
        if row >= 0:
            self.table.selectRow(row)
        else:
            self.detail_panel.clear()

    def _reselect_player(self, player: Any) -> None:
        """模型重建后尽量恢复原选中玩家（找不到则选第一名玩家）。"""
        row = self.model.row_for_player(player) if player is not None else -1
        if row < 0:
            row = self.model.first_player_row()
        if row >= 0:
            self.table.selectRow(row)
        else:
            self.detail_panel.clear()

    # ------------------------------------------------------------ 筛选 / 选中 / 汇总
    def _apply_filters(self) -> None:
        id_only = self.id_only_check.isChecked()
        hits_only = self.hits_only_check.isChecked()
        level_index = self.level_combo.currentIndex()
        level = "all" if level_index <= 0 else ("red" if level_index == 1 else "yellow")
        self.model.apply_filters(id_only=id_only, hits_only=hits_only, level=level)

    def _on_filter_changed(self) -> None:
        selected = self.selected_player()
        self._apply_filters()
        self._refresh_summary()
        self._reselect_player(selected)

    def _on_current_row_changed(self, current: Any, previous: Any = None) -> None:
        if not getattr(current, "isValid", lambda: False)():
            self.detail_panel.clear()
            return
        row = current.row()
        if self.model.is_group_row(row):
            # 组头行不参与明细联动（仅作分组标识）。
            return
        self._show_player_detail(self.model.row_object(row))

    def _show_player_detail(self, player: Any) -> None:
        """刷新右侧明细：封禁记录 + 当局战绩 + 已有缓存综合战绩（不发请求）。"""
        if player is None:
            self.detail_panel.clear()
            return
        self.detail_panel.set_player(
            player, self.model.stats_map(), self.model.stats_state(), self.model.stats_message(),
            recorder_team=self.model.recorder_team(),
        )
        self._show_cached_player_stats(player)

    def _refresh_selected_detail(self) -> None:
        current = self.table.currentIndex()
        if not current.isValid() or self.model.is_group_row(current.row()):
            return
        self._show_player_detail(self.model.row_object(current.row()))

    def _refresh_summary(self) -> None:
        visible = self.model.player_row_count()
        total = self.model.total_player_count()
        self.count_label.setText("显示 %d / %d 人" % (visible, total))
        if self._query_result is None:
            self.summary_label.setText("—")
            return
        summary = self._query_result.summary
        text = (
            "本局 %d 人 · 命中 %d 人（ID 确认 %d / 昵称匹配 %d / 黄标 %d / 红标 %d）"
            % (summary["total"], summary["hits"], summary["id_hits"],
               summary["nickname_hits"], summary["yellow"], summary["red"])
        )
        if self._query_result.is_partial:
            text += " · partial 录像：仅昵称匹配（同昵称视为同一人，计入黄/红）"
        if visible != summary["total"]:
            text += " · 当前筛选显示 %d 人" % visible
        text += " · 默认按封禁次数降序 → 本局效率降序 → 伤害降序（None 置后）"
        self.summary_label.setText(text)

    # ------------------------------------------------------------ 综合战绩点查 / 刷新
    @staticmethod
    def _player_stats_key(player: Any) -> tuple:
        if player is None:
            return ("none", "")
        dbid = getattr(player, "dbid", None)
        if dbid is not None:
            return ("id", int(dbid))
        return ("nick", getattr(player, "nickname", "") or "")

    def _peek_cached_player_stats(self, player: Any) -> Any:
        """只读探测数据层已缓存的综合战绩（不得触发网络请求）。

        数据层若提供 ``peek_player_stats_cache`` / ``get_cached_player_stats`` 之类的
        只读接口则直接使用；否则返回 None（界面仅展示本会话缓存）。
        """
        for name in ("peek_player_stats_cache", "get_cached_player_stats", "peek_player_stats"):
            probe = getattr(self.service, name, None)
            if not callable(probe):
                continue
            try:
                result = probe(player)
            except Exception:  # noqa: BLE001 - 探测失败按无缓存处理
                return None
            if result is not None:
                return result
        return None

    def _show_cached_player_stats(self, player: Any) -> None:
        """选中玩家：有缓存直接展示（含更新时间 / 缓存时间），无缓存保持空态、不发请求。"""
        if player is None:
            return
        key = self._player_stats_key(player)
        cached = self._player_stats_cache.get(key)
        if cached is None:
            cached = self._peek_cached_player_stats(player)
            if cached is not None:
                self._player_stats_cache[key] = cached
        if cached is not None:
            self.detail_panel.show_player_stats(cached, from_cache=True)

    def _on_player_stats_requested(self, player: Any, force: bool = False) -> None:
        """手动查看（缓存优先）/ 强制刷新综合战绩；选中玩家不会自动触发。"""
        if player is None:
            return
        if self._worker_running(self._player_stats_worker):
            return
        self._player_stats_pending = player
        self.detail_panel.set_player_stats_loading(refresh=bool(force))
        fn = lambda progress, should_cancel: self.service.get_player_stats(  # noqa: E731
            player, force=bool(force)
        )
        self._player_stats_worker = self._start_worker(
            "刷新综合战绩" if force else "查看综合战绩", fn,
            on_success=self._on_player_stats_finished,
            on_error=self._on_player_stats_failed,
        )

    def _on_player_stats_finished(self, result: Any) -> None:
        player = self._player_stats_pending
        self._player_stats_pending = None
        if player is not None and getattr(result, "available", False):
            self._player_stats_cache[self._player_stats_key(player)] = result
        message = getattr(result, "message", "综合战绩查询完成")
        current = self.selected_player()
        if (player is None or current is None
                or self._player_stats_key(current) != self._player_stats_key(player)):
            # 结果不属于当前选中玩家：只写入缓存，不改动明细面板。
            self.statusBar().showMessage(message, 10000)
            return
        self.detail_panel.show_player_stats(result)
        self.statusBar().showMessage(message, 10000)

    def _on_player_stats_failed(self, message: str) -> None:
        self._player_stats_pending = None
        self.detail_panel.show_player_stats(None)
        self.statusBar().showMessage(message, 10000)

    # ------------------------------------------------------------ 空态 / 错误 / 启动
    def _show_empty(self, title: str, hint: str) -> None:
        self.empty_title.setText(title)
        self.empty_hint.setText(hint)
        self.stack.setCurrentIndex(0)
        self.summary_label.setText("—")
        self.count_label.setText("—")
        self._set_outcome_title("", "", kind="")

    def _show_error(self, title: str, message: str) -> None:
        self.statusBar().showMessage("%s：%s" % (title, message), 15000)
        QMessageBox.warning(self, title, message)

    def auto_start(self) -> None:
        """窗口显示后的自动动作：刷新数据状态 + 探测本机环境 + 首启流程 / 自动扫描。

        首次使用（空名单库且首启流程未完成）：环境探测后**不自动扫描**，先弹软件内自绘
        弹窗①（开始扫描录像）→ 扫描结束后弹弹窗②（下载官方名单）→ 更新成功（bans>0）
        后持久化 ``meta.first_run_flow_done=true``；中途关闭/失败则下次启动继续提示
        （已有录像索引时跳过弹窗①）。

        老用户（已有名单数据）或首启已完成：保持 1.0.1 行为——自动扫描全部录像 +
        扫描结束后串行触发启动名单检查（P0 防御：冷启动全量扫描 50–165s 期间不再并发写库）。
        """
        self._refresh_data_status()
        self._env_seq += 1
        env = self.service.detect_environment(apply=True)
        self._apply_environment(env)
        flow = self._first_run_flow_state()
        if flow.get("active"):
            # 首启：不自动扫描/更新，等待用户在弹窗里确认。
            self._startup_update_pending = False
            self._start_first_run_flow(flow)
            return
        if not self._startup_update_done:
            self._startup_update_pending = True
        if self.service.replay_dir_available():
            self.start_scan("all")
        else:
            # 扫描未启动（录像目录不可用）：仍在 auto_start 尾部触发一次名单检查。
            self._schedule_startup_update_check()

    def start_environment_detect(self, then_scan: bool = True) -> None:
        """后台探测本机环境（游戏目录 / 我的账号），完成后刷新按钮提示与录像区。"""
        self._env_seq += 1
        seq = self._env_seq
        fn = lambda progress, should_cancel: self.service.detect_environment(apply=True)  # noqa: E731
        self._env_worker = self._start_worker(
            "环境检测", fn,
            on_success=partial(self._on_environment_detected, seq, then_scan),
            on_error=partial(self._on_environment_failed, seq),
        )

    def _apply_environment(self, env: Any) -> None:
        """刷新本机环境（v0.3：顶栏不再显示路径，仅更新按钮 tooltip 与状态页）。"""
        self._environment = env
        game_text = getattr(env, "game_dir_text", "未检测到游戏目录")
        account_text = getattr(env, "account_text", "未检测到账号")
        source_text = getattr(env, "source_text", "")
        message = getattr(env, "message", "") or ""
        env_lines = [line for line in (
            "当前游戏目录：%s" % game_text,
            "当前账号：%s" % account_text,
            source_text,
            message,
        ) if line]
        env_detail = "\n".join(env_lines)
        self.redetect_button.setToolTip(
            "重新探测游戏安装目录与我的账号（注册表卸载项 + 最新录像 meta）\n" + env_detail
        )
        self.manual_game_button.setToolTip(
            "手动指定《坦克世界》安装目录，选择后保存到 config.local.json\n"
            "当前游戏目录：%s" % game_text
        )
        self._apply_replay_dir_state()
        self._refresh_status_dialog()

    def _apply_replay_dir_state(self) -> None:
        """录像区按 cfg.replay_dir 状态启用/禁用扫描，并给中文提示。"""
        available = self.service.replay_dir_available()
        self._sync_action_buttons()
        replay_dir = getattr(self.service.cfg, "replay_dir", None)
        if not available:
            self.progress_label.setText(
                "未找到录像目录：请点击「重新检测」或「手动选择游戏目录」（当前：%s）"
                % (replay_dir or "未设置")
            )
        elif not self._worker_running(self._scan_worker):
            self.progress_label.setText("录像目录：%s" % replay_dir)

    def _on_environment_detected(self, seq: int, then_scan: bool, env: Any) -> None:
        if seq != self._env_seq:
            return
        self._apply_environment(env)
        message = getattr(env, "message", "") or "环境检测完成"
        self.statusBar().showMessage(message, 10000)
        if then_scan and self.service.replay_dir_available():
            self.start_scan("latest")

    def _on_environment_failed(self, seq: int, message: str) -> None:
        if seq != self._env_seq:
            return
        self.redetect_button.setToolTip("环境检测失败：%s" % message)
        self.statusBar().showMessage(message, 10000)
        self._apply_replay_dir_state()

    def _on_redetect_environment_clicked(self) -> None:
        """「重新检测」：重新探测并刷新录像区（探测成功则自动扫描）。"""
        self.statusBar().showMessage("正在重新检测本机环境…", 5000)
        self._env_seq += 1
        env = self.service.detect_environment(apply=True)
        self._apply_environment(env)
        self.statusBar().showMessage(getattr(env, "message", "") or "环境检测完成", 10000)
        if self.service.replay_dir_available():
            self.start_scan("latest")

    def _on_manual_game_dir_clicked(self) -> None:
        """「手动选择游戏目录」：选择后 save_config 记忆并刷新。"""
        replay_dir = self.service._replay_dir_path()
        initial = str(replay_dir) if replay_dir is not None else str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "选择《坦克世界》安装目录", initial)
        if not selected:
            return
        env = self.service.select_game_dir(selected)
        self._apply_environment(env)
        self.statusBar().showMessage(getattr(env, "message", "已选择游戏目录"), 12000)
        if self.service.replay_dir_available():
            self.start_scan("latest")

    # ------------------------------------------------------------ 截图辅助（脚本用）
    def select_replay_by_path(self, path: str) -> bool:
        """按路径选中录像（截图 / 自检脚本用）。"""
        for index in range(self.replay_combo.count()):
            if self.replay_combo.itemData(index) == path:
                self.replay_combo.setCurrentIndex(index)
                return True
        return False

    def capture_ready(self) -> bool:
        """当前是否处于可截图状态（已出结果且战绩状态确定）。"""
        if self.stack.currentIndex() != 1 or self.model.rowCount() <= 0:
            return False
        return self.model.stats_state() in (STATS_STATE_READY, STATS_STATE_UNAVAILABLE)

    def shutdown(self) -> None:
        """窗口退出前显式停止定时器 / 后台线程并清引用（Windows + Qt 6.12 防退出崩溃）。

        必须在 ``app.exec()`` 返回后、进程退出前调用；配合 ``os._exit(0)`` 跳过
        Python 解释器退出阶段的 Qt 全局析构（实测偶发 Qt6Core.dll 0xc0000409）。
        """
        timer = getattr(self, "_update_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:  # noqa: BLE001 - 退出阶段不得抛异常
                pass
        workers = list(self._workers.values())
        for worker in workers:
            try:
                worker.cancel()
            except Exception:  # noqa: BLE001
                pass
        for worker in workers:
            try:
                if worker.isRunning():
                    worker.wait(1500)
            except Exception:  # noqa: BLE001
                pass
        self._workers.clear()
        for name in WORKER_ATTR_NAMES:
            setattr(self, name, None)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        """关闭窗口时先停线程 / 定时器，避免 QThread 析构崩溃。"""
        try:
            self.shutdown()
        finally:
            super().closeEvent(event)

    def current_summary_text(self) -> str:
        return self.summary_label.text()

    def selected_player(self) -> Any:
        index = self.table.currentIndex()
        if not index.isValid():
            return None
        return self.model.row_object(index.row())


def run_app(cfg: Any, mock: bool = False) -> int:
    """启动 GUI 主循环，返回退出码。

    退出路径（Windows + PySide6 6.12 防崩溃）：
    ``app.exec()`` 返回 -> ``window.shutdown()``（停定时器/worker）-> ``service.dispose()``
    -> 刷新日志；调用方 ``src/main.py`` 随后用 ``os._exit(0)`` 跳过解释器退出阶段的
    Qt 全局析构（实测偶发 Qt6Core.dll 0xc0000409 崩溃弹窗）。
    """
    app = create_application()
    service = AppService(cfg=cfg, mock=mock)
    window = MainWindow(service)
    window.show()
    window.auto_start()
    code = 0
    try:
        code = int(app.exec())
    finally:
        try:
            window.shutdown()
        except Exception:  # noqa: BLE001 - 退出阶段不得抛异常
            pass
        try:
            service.dispose()
        except Exception:  # noqa: BLE001
            pass
        try:
            import logging

            logging.shutdown()
        except Exception:  # noqa: BLE001
            pass
    return code


def main(argv: list) -> int:
    """独立运行入口：启动主窗口（无参数时用默认配置）。"""
    from ..config import load_config

    cfg = load_config()
    return run_app(cfg)


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
