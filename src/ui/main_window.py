# -*- coding: utf-8 -*-
"""主窗口：P3 MVP 界面骨架（顶栏 / 录像区 / 结果表 / 明细 / 汇总 / 免责声明）。

界面全部代码构建（无 .ui 文件）。窗口 16:9 可缩放，默认 1920×1080（屏幕可用区域
不足时等比收缩）；字体微软雅黑、高 DPI 由 Qt 处理。顶栏只保留标题、版本与操作按钮，
名单库版本信息与本机环境路径改在「数据状态」页查看。

线程模型
--------
所有耗时操作（更新名单 / 扫描录像 / 解析查询 / 当局战绩 / 综合战绩）都放进
``ServiceWorker``（QThread），通过信号回到主线程更新界面，避免卡顿；扫描与
更新支持「取消」。
"""
from __future__ import annotations

import sys
from functools import partial
from pathlib import Path
from typing import Any, Optional

try:
    from PySide6.QtCore import Qt, QTimer, QUrl
    from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon
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
        QTableView,
        QVBoxLayout,
        QWidget,
    )
except ImportError:  # 便于无 PySide6 环境下静态检查
    Qt = QApplication = QMainWindow = QWidget = QFrame = object  # type: ignore[assignment]
    QTimer = QUrl = QDesktopServices = QIcon = None  # type: ignore[assignment]

try:
    from .. import models
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
        STATS_STATE_LOADING,
        STATS_STATE_READY,
        STATS_STATE_UNAVAILABLE,
        PlayerTableModel,
    )
    from .widgets import DataStatusDialog, PlayerDetailPanel
except ImportError:  # 直接以脚本方式运行时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src import models
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
        STATS_STATE_LOADING,
        STATS_STATE_READY,
        STATS_STATE_UNAVAILABLE,
        PlayerTableModel,
    )
    from src.ui.widgets import DataStatusDialog, PlayerDetailPanel

SCAN_MODE_LABELS = {"latest": "最新完整录像", "recent": "最近 7 天", "all": "全部录像", "file": "手动选择"}

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


def default_window_size() -> tuple:
    """默认 1920×1080（16:9）；屏幕可用区域不足时等比收缩。"""
    width, height = DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT
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
        self._query_seq = 0
        self._scan_mode = "latest"
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
        """顶栏（单行）：标题 + 版本 + 操作按钮。

        v0.3 精简：原先的「数据更新 / 名单规模 / wgid 覆盖 / 缺失批次 / 上次检查」
        元信息行、「本地真实数据」徽标与「本机环境」路径信息行全部移除；名单库版本
        信息统一在「数据状态」页查看，本机环境路径只保留在按钮 tooltip 与状态页。
        """
        header = QFrame()
        header.setObjectName("Header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(18, 10, 18, 10)
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
        self.data_status_button = QPushButton("数据状态")
        self.data_status_button.setToolTip("查看名单库批次数 / 条数 / 更新时间 / wgid 覆盖 / 缺失批次")
        self.update_button = QPushButton("更新名单")
        self.update_button.setObjectName("Primary")
        self.update_button.setToolTip("检查官方名单接口并增量更新（启动时自动检查一次，运行期每 6 小时）")
        for button in (self.redetect_button, self.manual_game_button,
                       self.data_status_button, self.update_button):
            layout.addWidget(button)
        return header

    def _build_replay_bar(self) -> Any:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(7)

        row = QHBoxLayout()
        row.setSpacing(8)
        title = QLabel("录像区")
        title.setObjectName("SectionTitle")
        row.addWidget(title)
        self.scan_latest_button = QPushButton("最新完整录像")
        self.scan_latest_button.setObjectName("Primary")
        self.scan_recent_button = QPushButton("最近 7 天")
        self.scan_all_button = QPushButton("全部录像")
        self.manual_button = QPushButton("手动选择…")
        for button in (self.scan_latest_button, self.scan_recent_button, self.scan_all_button,
                       self.manual_button):
            row.addWidget(button)
        row.addSpacing(10)
        row.addWidget(QLabel("当前对局"))
        self.replay_combo = QComboBox()
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
        layout.setContentsMargins(12, 10, 12, 6)
        layout.setSpacing(0)
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(self._build_table_area())
        self._splitter.addWidget(self._build_detail_area())
        self._splitter.setStretchFactor(0, 1)
        self._splitter.setStretchFactor(1, 0)
        self._splitter.setSizes([1120, 480])
        layout.addWidget(self._splitter)
        return container

    def _build_table_area(self) -> Any:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(10)
        title = QLabel("查询结果")
        title.setObjectName("SectionTitle")
        filter_row.addWidget(title)
        self.id_only_check = QCheckBox("仅看 ID 命中")
        self.id_only_check.setToolTip("只显示完整录像 dbid 与名单 wgid 精确匹配的玩家（A2 口径下默认不启用）")
        self.hits_only_check = QCheckBox("只看命中")
        self.hits_only_check.setToolTip("只显示命中历史封禁名单的玩家")
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
            "请先点击「最新完整录像」或「最近 7 天」扫描录像；也可以「手动选择…」单个 .wotreplay 文件。"
        )
        self.empty_hint.setObjectName("EmptyHint")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_hint)
        empty_layout.addStretch(1)
        self.stack.addWidget(empty_widget)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        header.setSortIndicatorShown(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for index, column in enumerate(COLUMNS):
            self.table.setColumnWidth(index, column["width"])
        header.setSectionResizeMode(COLUMN_INDEX["reason"], QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSortIndicator(
            COLUMN_INDEX["ban_count"], Qt.SortOrder.DescendingOrder
        )
        self.stack.addWidget(self.table)
        layout.addWidget(self.stack, 1)

        self.summary_label = QLabel("—")
        self.summary_label.setObjectName("MetaHint")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        return card

    def _build_detail_area(self) -> Any:
        self.detail_panel = PlayerDetailPanel()
        self.detail_panel.setMinimumWidth(460)
        return self.detail_panel

    def _build_footer(self) -> Any:
        footer = QFrame()
        footer.setObjectName("Footer")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(18, 8, 18, 8)
        disclaimer = QLabel(theme.DISCLAIMER_TEXT)
        disclaimer.setObjectName("Disclaimer")
        disclaimer.setWordWrap(True)
        layout.addWidget(disclaimer)
        return footer

    def _apply_window_geometry(self) -> None:
        width, height = default_window_size()
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
        """按窗口实际宽度分配左右比例：结果表约 70%，明细面板约 30%（最小 460）。"""
        if not hasattr(self, "_splitter"):
            return
        total = self._splitter.width()
        if total <= 0:
            return
        right = max(460, int(total * 0.30))
        left = max(600, total - right - self._splitter.handleWidth())
        self._splitter.setSizes([left, right])

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_splitter_sizes)

    def _connect_signals(self) -> None:
        self.update_button.clicked.connect(self._on_update_clicked)
        self.data_status_button.clicked.connect(self._on_data_status_clicked)
        self.scan_latest_button.clicked.connect(partial(self.start_scan, "latest"))
        self.scan_recent_button.clicked.connect(partial(self.start_scan, "recent"))
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
        if self.detail_panel.player_stats_requested is not None:
            self.detail_panel.player_stats_requested.connect(self._on_player_stats_requested)

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
        self.statusBar().showMessage("操作已取消", 5000)
        self._set_scan_busy(False)
        self._set_query_busy(False)

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
        """读取名单库状态（v0.3 起顶栏不再显示版本条，仅供状态页与提示使用）。"""
        try:
            status = self.service.get_data_status()
        except ServiceError as exc:
            self.statusBar().showMessage("读取名单库状态失败：%s" % exc, 8000)
            return
        self._data_status = status
        self._refresh_status_dialog()

    def _on_data_status_clicked(self) -> None:
        self.open_data_status(modal=True)

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
        """定时器到点：静默检查一次（force=False，不打断本地查询）。"""
        if self._worker_running(self._update_worker):
            return
        self.statusBar().showMessage("已到自动检查间隔，正在后台检查名单更新…", 8000)
        self._start_update_check(silent=True, force=False)

    def trigger_scheduled_update(self) -> None:
        """手动触发一次「定时检查」逻辑（测试 / 截图脚本用）。"""
        self._on_scheduled_update()

    # ------------------------------------------------------------ 更新名单
    def _on_update_clicked(self) -> None:
        """手动「更新名单」按钮：force=True 强制检查官方接口。"""
        self._start_update_check(silent=False, force=True)

    def _start_update_check(self, silent: bool = False, force: bool = True) -> None:
        """启动名单更新检查（silent=True 时不弹结果框，仅状态栏提示）。

        force=False 用于启动检查与 6 小时定时检查（未到间隔则直接返回「未到检查间隔」）。
        """
        if self._worker_running(self._update_worker):
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("正在更新…")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_label.setText(
            "正在检查官方名单接口…（%s）" % ("强制更新" if force else "自动检查")
        )
        fn = lambda progress, should_cancel: self.service.update_ban_list(  # noqa: E731
            force=force, progress_cb=progress, cancel_cb=should_cancel
        )
        self._update_worker = self._start_worker(
            "更新名单", fn,
            on_success=partial(self._on_update_finished, silent),
            on_error=partial(self._on_update_failed, silent),
            on_progress=self._on_update_progress,
        )

    def _on_update_progress(self, percent: int, message: str) -> None:
        self.progress_bar.setValue(int(percent))
        self.progress_label.setText(message)

    def _on_update_finished(self, silent: bool, result: Any) -> None:
        self.update_button.setEnabled(True)
        self.update_button.setText("更新名单")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        message = getattr(result, "message", "") or "检查完成"
        errors = list(getattr(result, "errors", []) or [])
        if errors:
            message = "%s；异常：%s" % (message, "；".join(errors[:3]))
        failed = (not getattr(result, "ok", True)) or bool(errors)
        self.progress_label.setText(message)
        self.statusBar().showMessage("更新名单：%s" % message, 15000)
        self._refresh_data_status()
        if not silent:
            if failed:
                QMessageBox.warning(self, "更新名单失败", message)
            else:
                QMessageBox.information(self, "更新名单", message)

    def _on_update_failed(self, silent: bool, message: str) -> None:
        self.update_button.setEnabled(True)
        self.update_button.setText("更新名单")
        self.progress_label.setText(message)
        self.statusBar().showMessage(message, 15000)
        if not silent:
            QMessageBox.warning(self, "更新名单失败", message)

    # ------------------------------------------------------------ 录像扫描
    def start_scan(self, mode: str = "latest") -> None:
        """扫描录像（mode: latest / recent / all）。"""
        if self._worker_running(self._scan_worker):
            return
        if not self.service.is_mock and not self.service.replay_dir_available():
            self._apply_replay_dir_state()
            return
        self._scan_mode = mode
        self._set_scan_busy(True)
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

    def _set_scan_busy(self, busy: bool) -> None:
        self.scan_latest_button.setEnabled(not busy)
        self.scan_recent_button.setEnabled(not busy)
        self.scan_all_button.setEnabled(not busy)
        self.manual_button.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        if busy:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
        else:
            self.progress_bar.setRange(0, 100)

    def _on_scan_progress(self, percent: int, message: str) -> None:
        self.progress_bar.setValue(int(percent))
        self.progress_label.setText(message)

    def _on_scan_finished(self, mode: str, replays: list) -> None:
        self._set_scan_busy(False)
        self._current_replays = list(replays or [])
        self.replay_combo.blockSignals(True)
        self.replay_combo.clear()
        for replay in self._current_replays:
            self.replay_combo.addItem(replay_label(replay), replay.path)
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
            self.statusBar().showMessage("扫描完成：未找到录像文件", 8000)
            return
        complete_count = len([item for item in self._current_replays if not item.is_partial])
        partial_count = len(self._current_replays) - complete_count
        self.progress_label.setText(
            "扫描完成：%d 个录像（完整 %d / partial %d）"
            % (len(self._current_replays), complete_count, partial_count)
        )
        self.statusBar().showMessage(self.progress_label.text(), 8000)
        if target_index >= 0:
            self._on_replay_index_changed(target_index)

    def _on_scan_failed(self, message: str) -> None:
        self._set_scan_busy(False)
        self.progress_label.setText(message)
        self._show_empty("扫描录像失败", message)
        self._show_error("扫描录像失败", message)

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
        self._set_scan_busy(True)
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
                    self.replay_combo.insertItem(0, replay_label(replay), replay.path)
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
            self.statusBar().showMessage("正在解析录像并匹配名单…")
        else:
            self.progress_bar.setRange(0, 100)

    def _on_query_finished(self, seq: int, result: Any) -> None:
        if seq != self._query_seq:
            return
        self._set_query_busy(False)
        self._query_result = result
        self.model.set_rows(result.matches)
        self._apply_filters()
        self.stack.setCurrentIndex(1)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.progress_label.setText(result.message)
        self.statusBar().showMessage(result.message, 10000)
        if self.model.rowCount() > 0:
            self.table.selectRow(0)
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
        """按需请求当局战绩（A7：打开某局才请求；partial / 超期不请求）。"""
        if detail is None:
            return
        if detail.is_partial:
            self._set_battle_unavailable("partial 录像没有 arena_unique_id，无法取当局战绩（仅完整录像可查）")
            return
        fn = lambda progress, should_cancel: self.service.get_battle_stats(detail)  # noqa: E731
        self._battle_worker = self._start_worker(
            "获取当局战绩", fn,
            on_success=partial(self._on_battle_finished, seq),
            on_error=partial(self._on_battle_failed, seq),
        )

    def _on_battle_finished(self, seq: int, result: Any) -> None:
        if seq != self._query_seq:
            return
        if getattr(result, "available", False) and getattr(result, "stats", None) is not None:
            stats_map = {}
            for player in getattr(result.stats, "players", []) or []:
                if getattr(player, "dbid", None) is not None:
                    stats_map[("id", int(player.dbid))] = player
                if getattr(player, "nickname", None):
                    stats_map[("nick", player.nickname)] = player
            self.model.set_stats(STATS_STATE_READY, stats_map, getattr(result, "message", ""))
        else:
            self._set_battle_unavailable(getattr(result, "message", "当局战绩不可用"))
        self._refresh_selected_detail()

    def _on_battle_failed(self, seq: int, message: str) -> None:
        if seq != self._query_seq:
            return
        self._set_battle_unavailable(message)

    def _set_battle_unavailable(self, message: str) -> None:
        self.model.set_stats(STATS_STATE_UNAVAILABLE, {}, message)
        self.statusBar().showMessage(message, 10000)
        self._refresh_selected_detail()

    # ------------------------------------------------------------ 筛选 / 选中 / 汇总
    def _apply_filters(self) -> None:
        id_only = self.id_only_check.isChecked()
        hits_only = self.hits_only_check.isChecked()
        level_index = self.level_combo.currentIndex()
        level = "all" if level_index <= 0 else ("red" if level_index == 1 else "yellow")
        self.model.apply_filters(id_only=id_only, hits_only=hits_only, level=level)

    def _on_filter_changed(self) -> None:
        self._apply_filters()
        self._refresh_summary()
        if self.model.rowCount() > 0:
            self.table.selectRow(0)
        else:
            self.detail_panel.clear()

    def _on_current_row_changed(self, current: Any, previous: Any = None) -> None:
        if not getattr(current, "isValid", lambda: False)():
            self.detail_panel.clear()
            return
        player = self.model.row_object(current.row())
        self.detail_panel.set_player(
            player, self.model.stats_map(), self.model.stats_state(), self.model.stats_message()
        )

    def _refresh_selected_detail(self) -> None:
        current = self.table.currentIndex()
        if not current.isValid():
            return
        player = self.model.row_object(current.row())
        self.detail_panel.set_player(
            player, self.model.stats_map(), self.model.stats_state(), self.model.stats_message()
        )

    def _refresh_summary(self) -> None:
        self.count_label.setText("显示 %d / %d 人" % (self.model.rowCount(), len(self.model.total_rows())))
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
        if self.model.rowCount() != summary["total"]:
            text += " · 当前筛选显示 %d 人" % self.model.rowCount()
        text += " · 默认按封禁次数降序、同次数最近封禁日期降序"
        self.summary_label.setText(text)

    # ------------------------------------------------------------ 综合战绩点查
    def _on_player_stats_requested(self, player: Any) -> None:
        if player is None:
            return
        if self._worker_running(self._player_stats_worker):
            return
        self.detail_panel.set_player_stats_loading()
        fn = lambda progress, should_cancel: self.service.get_player_stats(player)  # noqa: E731
        self._player_stats_worker = self._start_worker(
            "点查综合战绩", fn,
            on_success=self._on_player_stats_finished,
            on_error=self._on_player_stats_failed,
        )

    def _on_player_stats_finished(self, result: Any) -> None:
        self.detail_panel.show_player_stats(result)
        self.statusBar().showMessage(getattr(result, "message", "综合战绩查询完成"), 10000)

    def _on_player_stats_failed(self, message: str) -> None:
        self.detail_panel.show_player_stats(None)
        self.statusBar().showMessage(message, 10000)

    # ------------------------------------------------------------ 空态 / 错误 / 启动
    def _show_empty(self, title: str, hint: str) -> None:
        self.empty_title.setText(title)
        self.empty_hint.setText(hint)
        self.stack.setCurrentIndex(0)
        self.summary_label.setText("—")
        self.count_label.setText("—")

    def _show_error(self, title: str, message: str) -> None:
        self.statusBar().showMessage("%s：%s" % (title, message), 15000)
        QMessageBox.warning(self, title, message)

    def auto_start(self) -> None:
        """窗口显示后的自动动作：刷新数据状态 + 探测本机环境（无需点击）+ 扫描 + A1 检查。"""
        self._refresh_data_status()
        self._env_seq += 1
        env = self.service.detect_environment(apply=True)
        self._apply_environment(env)
        if self.service.replay_dir_available():
            self.start_scan("latest")
        if QTimer is not None:
            # 启动检查延后 3 秒，让首次录像扫描先完成，避免两个 worker 同时打开 SQLite。
            QTimer.singleShot(3000, partial(self._start_update_check, True, False))

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
        for button in (self.scan_latest_button, self.scan_recent_button, self.scan_all_button):
            button.setEnabled(available)
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
