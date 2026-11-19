# -*- coding: utf-8 -*-
"""T4 GUI 层测试：业务门面（mock / 临时真实库）+ 主窗口（offscreen）。

运行：``pytest tests -q``（自动使用 offscreen 平台，不会弹窗、不需要显示器）。
覆盖：数据状态口径（去重入库 322,806 / 官方源 322,941）、mock 全流程、
真实库读取路径、主窗口扫描/查询/明细/筛选/状态页、A1 定时器间隔与触发。
"""
import os
import sys
import time
from pathlib import Path

# 必须在导入 PySide6 之前设置（offscreen 无显示器也能跑 GUI 测试）。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.config import Config, load_config  # noqa: E402
from src.store import db  # noqa: E402


def wait_until(app, predicate, timeout_s: float = 15.0) -> bool:
    """驱动 Qt 事件循环直到条件成立或超时（GUI 测试等待后台 worker 用）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


@pytest.fixture(scope="session")
def qapp():
    """会话级 QApplication（offscreen），并应用界面主题。"""
    app = QApplication.instance() or QApplication([])
    from src.ui import theme

    theme.configure_application(app)
    yield app


def make_mock_service():
    """构造 mock 模式业务门面。"""
    from src.app.service import AppService

    return AppService(cfg=load_config(), mock=True)


def make_real_service(db_path: Path):
    """构造真实模式业务门面（使用临时数据库）。"""
    from src.app.service import AppService

    cfg = Config(project_root=PROJECT_ROOT, db_path=db_path)
    return AppService(cfg=cfg, mock=False)


# ---------------------------------------------------------------- 业务门面
def test_mock_data_status_counts():
    """mock 状态口径：去重入库 322,806；官方源 322,941（物理行）。"""
    service = make_mock_service()
    status = service.get_data_status()
    assert status.record_count == 322806
    assert status.official_record_count == 322941
    assert status.record_count_text == "322,806 条（去重入库）"
    assert "322,941" in status.official_note and "物理行" in status.official_note
    assert status.batch_count == 114
    service.dispose()


def test_mock_full_flow():
    """mock 全流程：扫描 -> 查询 -> 匹配汇总 -> 当局战绩 -> 综合战绩。"""
    from src.app.service import latest_complete_replay

    service = make_mock_service()
    replays = service.list_replays(mode="recent")
    assert len(replays) == 4
    latest = latest_complete_replay(replays)
    assert latest is not None and not latest.is_partial

    query = service.query_replay(latest.path)
    assert query.summary["total"] == 30
    assert query.summary["hits"] == 6
    assert query.summary["id_hits"] == 4
    assert query.summary["nickname_hits"] == 2

    battle = service.get_battle_stats(query.replay)
    assert battle.available and len(battle.stats.players) == 30

    hit = next(item for item in query.matches if item.is_hit)
    player_stats = service.get_player_stats(hit)
    assert player_stats.available and player_stats.stats.account_id > 0

    partial = next(item for item in replays if item.is_partial)
    partial_query = service.query_replay(partial.path)
    assert partial_query.is_partial
    assert all(item.partial for item in partial_query.matches if item.is_hit)
    assert partial_query.summary["id_hits"] == 0
    assert service.get_battle_stats(partial_query.replay).available is False
    service.dispose()


def test_mock_clear_and_rebuild():
    """M6：清空名单库后条数归零，重建后恢复（mock 语义）。"""
    service = make_mock_service()
    cleared = service.clear_ban_library()
    assert cleared["deleted"] == 322806
    status = service.get_data_status()
    assert status.record_count == 0
    assert status.available is False
    rebuilt = service.rebuild_ban_library()
    assert rebuilt["imported"] == 322806
    status = service.get_data_status()
    assert status.record_count == 322806
    service.dispose()


def test_real_data_status_with_temp_db(tmp_path):
    """真实模式读取路径：临时库 3 条记录 + 官方源 meta。"""
    db_path = tmp_path / "wotban-gui-test.db"
    conn = db.connect(db_path)
    sql = (
        "INSERT INTO bans(batch_day, source_type, nickname, reason, duration, ban_times) "
        "VALUES(?, 'csv', ?, '违规使用脚本', '永久', 1)"
    )
    for nickname in ("测试玩家甲", "测试玩家乙", "测试玩家丙"):
        conn.execute(sql, ("2026-11-06", nickname))
    conn.commit()
    db.set_meta(conn, "days_json", ["2026-11-06"])
    db.set_meta(conn, "record_count", 3)
    db.set_meta(conn, "archive_import", {"source_rows_physical_target": 322941})
    db.set_meta(conn, "last_check_at", "2026-11-18T16:00:00+08:00")
    conn.close()

    service = make_real_service(db_path)
    assert service.is_mock is False
    status = service.get_data_status()
    assert status.batch_count == 1
    assert status.record_count == 3
    assert status.official_record_count == 322941
    assert status.record_count_text == "3 条（去重入库）"
    assert status.latest_batch_day == "2026-11-06"
    assert status.last_check_at.startswith("2026-11-18")
    service.dispose()


# ---------------------------------------------------------------- 主窗口
def test_main_window_mock_flow(qapp):
    """主窗口：扫描 -> 查询 -> 明细 -> 筛选 -> 状态页 -> A1 定时器。"""
    from src.ui.main_window import MainWindow
    from src.ui.table_model import COLUMN_INDEX

    service = make_mock_service()
    window = MainWindow(service)
    window.resize(1600, 900)
    window.show()
    qapp.processEvents()

    # v0.3：顶栏不再显示数据版本条 / 模式徽标 / 本机环境路径（信息统一在「数据状态」页）
    assert window._data_status is not None and window._data_status.record_count == 322806
    assert not hasattr(window, "meta_labels") and not hasattr(window, "mode_hint")
    assert not hasattr(window, "env_game_label") and not hasattr(window, "env_account_label")

    # 「当前对局」下拉：弹窗最多 15 行 + 滚动条（自绘弹窗，规避 QSS 菜单式弹窗）
    from src.ui.widgets import COMBO_VISIBLE_ROWS, ScrollableComboBox

    assert isinstance(window.replay_combo, ScrollableComboBox)
    assert window.replay_combo.visible_rows == COMBO_VISIBLE_ROWS == 15
    assert window.replay_combo.maxVisibleItems() == 15

    # A1：定时器间隔 = 配置 6 小时
    expected_ms = int(load_config().update_interval_hours * 3600 * 1000)
    assert window.update_timer_interval_ms() == expected_ms
    assert window._update_timer is not None and window._update_timer.isActive()

    # 自动启动：扫描 + 查询 + 当局战绩
    window.auto_start()
    assert wait_until(
        qapp,
        lambda: window.stack.currentIndex() == 1
        and window.model.rowCount() == 30
        and window.model.stats_state() in ("ready", "unavailable"),
        timeout_s=20,
    ), "mock 自动扫描/查询未在超时内完成"
    assert "命中 6" in window.summary_label.text()

    # 选中第一行 -> 明细面板
    window.table.selectRow(0)
    qapp.processEvents()
    assert window.detail_panel.name_label.text() != "未选择玩家"
    assert window.detail_panel.record_table.rowCount() >= 1

    # 筛选：仅看 ID 命中
    window.id_only_check.setChecked(True)
    qapp.processEvents()
    assert window.model.rowCount() == 4
    window.id_only_check.setChecked(False)
    qapp.processEvents()
    assert window.model.rowCount() == 30

    # 表头排序：按昵称升序
    window.model.sort(COLUMN_INDEX["nickname"], Qt.SortOrder.AscendingOrder)
    names = [window.model.row_object(i).nickname for i in range(window.model.rowCount())]
    assert names == sorted(names, key=str.lower)
    window.model.reset_sort()

    # 数据状态页（非模态）
    dialog = window.open_data_status(modal=False)
    assert dialog is not None
    qapp.processEvents()
    assert dialog.batch_table.rowCount() == 12
    assert dialog.values["record_count"].text().startswith("322,806")
    assert dialog.values["official"].text() == "322,941 条"
    assert dialog.clear_button.isEnabled() and dialog.rebuild_button.isEnabled()
    dialog.close()

    # F2：明细面板来源信息（来源批次 / 来源类型 / 抓取时间 / 公告链接）
    detail = window.detail_panel
    headers = [detail.record_table.horizontalHeaderItem(i).text()
               for i in range(detail.record_table.columnCount())]
    assert "来源类型" in headers
    assert detail.source_values["batch"].text() not in ("", "—")
    assert "官方" in detail.source_values["type"].text()
    assert detail.source_values["fetched"].text() not in ("", "—")
    assert "http" in detail.source_values["url"].text()
    tooltips = []
    for row in range(detail.record_table.rowCount()):
        for column in range(detail.record_table.columnCount()):
            item = detail.record_table.item(row, column)
            if item is not None:
                tooltips.append(item.toolTip() or "")
    assert any("抓取时间" in text and "http" in text for text in tooltips)

    # F4：综合战绩口径标注（偶游盒子 · 最近 1500 场标准局）
    assert "1500" in detail.player_title.text() and "偶游盒子" in detail.player_title.text()
    assert "1500" in detail.player_stats_button.toolTip()
    # v0.3：明细面板不再显示「点击口径 / 来源 battledetail」提示行
    assert not hasattr(detail, "player_hint") and not hasattr(detail, "battle_hint")

    # A1：手动触发一次「定时检查」（mock，静默不弹窗）
    window.trigger_scheduled_update()
    assert wait_until(
        qapp,
        lambda: window._update_worker is None or not window._update_worker.isRunning(),
        timeout_s=10,
    )
    assert "更新名单" in window.update_button.text() or window.update_button.isEnabled()

    window.close()
    service.dispose()


def test_worker_lifecycle_repeated_operations(qapp):
    """F1：连续两次扫描 / 更新 / 综合战绩点查 / 取消后重开，不得抛 C++ 对象已删除。"""
    from src.ui.main_window import MainWindow

    service = make_mock_service()
    window = MainWindow(service)
    window.show()
    qapp.processEvents()
    if window._update_timer is not None:
        window._update_timer.stop()  # 避免 6h 定时器在测试中干扰

    # 连续两次扫描（每次结束后属性必须为 None）
    for _ in range(2):
        window.start_scan("latest")
        assert wait_until(qapp, lambda: window._scan_worker is None, timeout_s=20), "扫描 worker 未释放"
    assert window.replay_combo.count() > 0

    # 连续两次「更新名单」等效调用（silent 避免弹窗）
    for _ in range(2):
        window._start_update_check(silent=True, force=True)
        assert wait_until(qapp, lambda: window._update_worker is None, timeout_s=20), "更新 worker 未释放"

    # 等待自动查询完成，连续两次综合战绩点查
    assert wait_until(
        qapp,
        lambda: window.model.rowCount() > 0 and window.model.stats_state() in ("ready", "unavailable"),
        timeout_s=20,
    )
    window.table.selectRow(0)
    qapp.processEvents()
    player = window.selected_player()
    assert player is not None
    for _ in range(2):
        window._on_player_stats_requested(player)
        assert wait_until(qapp, lambda: window._player_stats_worker is None, timeout_s=20), \
            "综合战绩 worker 未释放"

    # 取消后再次扫描（取消按钮走 isRunning 守卫，不得抛异常）
    window.start_scan("all")
    qapp.processEvents()
    window._on_cancel_clicked()
    assert wait_until(qapp, lambda: window._scan_worker is None, timeout_s=20)
    window.start_scan("latest")
    assert wait_until(qapp, lambda: window._scan_worker is None, timeout_s=20)

    window.close()
    service.dispose()


def test_environment_auto_display_with_fake_detect(qapp, monkeypatch, tmp_path):
    """v0.2：启动即显示环境（mock 合成路径）；fake detect 验证 T15 契约接线。"""
    from types import SimpleNamespace

    from src.ui.main_window import MainWindow

    # 1) mock 服务：合成环境（不依赖本机真实路径），启动即探测；v0.3 起路径只出现在按钮 tooltip
    service = make_mock_service()
    window = MainWindow(service)
    window.show()
    qapp.processEvents()
    window.auto_start()
    assert wait_until(qapp, lambda: window._environment is not None, timeout_s=15)
    redetect_tip = window.redetect_button.toolTip()
    assert "游戏目录" in redetect_tip and "wot-ban-demo" in redetect_tip
    assert "演示玩家" in redetect_tip
    assert window.redetect_button.isEnabled() and window.manual_game_button.isEnabled()
    window.close()
    service.dispose()

    # 2) 真实模式 + fake detect 模块：验证字段映射与录像目录缺失判定
    fake_module = SimpleNamespace(
        detect_environment=lambda **kwargs: SimpleNamespace(
            game_dir="X:/FakeGame",
            replay_dir="X:/FakeGame/replays",
            account_id=1234567,
            account_name="fake_user",
            game_dir_source="registry",
            account_source="replay_meta",
        ),
        apply_to_config=lambda *args, **kwargs: None,
    )
    real_service = make_real_service(tmp_path / "env-detect.db")
    monkeypatch.setattr(real_service, "_load_detect_module", lambda: fake_module)
    env = real_service.detect_environment(apply=True)
    assert env.game_dir == "X:/FakeGame"
    assert env.replay_dir == "X:/FakeGame/replays"
    assert env.account_name == "fake_user" and env.account_id == 1234567
    assert env.game_dir_source == "registry" and env.account_source == "replay_meta"
    assert env.detected is True
    assert real_service.replay_dir_available() is False  # 合成目录不存在 → 禁用扫描
    real_service.dispose()


def test_application_version_from_config(qapp):
    """F2：QApplication 版本号读 config.app_version（0.2.0），不再写死旧版本号。"""
    from src.ui.main_window import create_application

    app = create_application([])
    assert app.applicationVersion() == load_config().app_version
    assert app.applicationVersion() == "0.2.0"


def test_auto_start_real_mode_with_fake_scan(qapp, monkeypatch, tmp_path):
    """F1：真实模式 auto_start（fake detect + fake 扫描/查询）启动即出结果，且可重新检测。"""
    from types import SimpleNamespace

    from src.app import mock_data
    from src.app.service import BattleStatsResult
    from src.ui.main_window import MainWindow

    service = make_real_service(tmp_path / "auto-start.db")
    fake_module = SimpleNamespace(
        detect_environment=lambda **kwargs: SimpleNamespace(
            game_dir="X:/FakeGame",
            replay_dir="X:/FakeGame/replays",
            account_id=7048141181,
            account_name="kingbob_",
            game_dir_source="registry",
            account_source="replay_meta",
        ),
        apply_to_config=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(service, "_load_detect_module", lambda: fake_module)
    monkeypatch.setattr(service, "replay_dir_available", lambda: True)
    monkeypatch.setattr(service, "list_replays", lambda *a, **k: mock_data.build_replays())
    monkeypatch.setattr(service, "query_replay", lambda path: service._mock_query(str(path)))
    monkeypatch.setattr(
        service, "get_battle_stats",
        lambda detail: BattleStatsResult(available=False, message="测试：跳过战绩请求"),
    )
    window = MainWindow(service)
    window.show()
    qapp.processEvents()
    window.auto_start()
    # 正确等待方式：processEvents + sleep（不能用 QTest.qWait 高频轮询）
    assert wait_until(qapp, lambda: window.model.rowCount() > 0, timeout_s=15), "auto_start 未出结果"
    redetect_tip = window.redetect_button.toolTip()
    assert "FakeGame" in redetect_tip and "kingbob_" in redetect_tip
    rows_first = window.model.rowCount()
    # 重新检测后再次扫描仍正常
    window._on_redetect_environment_clicked()
    assert wait_until(qapp, lambda: window.model.rowCount() > 0, timeout_s=15)
    assert window.model.rowCount() == rows_first
    window.close()
    service.dispose()


def test_main_window_update_failure_is_chinese(qapp):
    """更新失败提示为中文且不阻塞界面（直接调用失败回调）。"""
    from src.ui.main_window import MainWindow

    service = make_mock_service()
    window = MainWindow(service)
    window.show()
    qapp.processEvents()
    window._on_update_failed(True, "更新名单失败：官方接口请求超时（网络不可用）")
    assert "官方接口请求超时" in window.progress_label.text()
    assert window.update_button.isEnabled()
    window.close()
    service.dispose()


def test_scrollable_combo_popup_15_rows_with_scrollbar(qapp):
    """「当前对局」下拉：弹窗最多 15 行，条目超过 15 时出现竖向滚动条，可反复开关。"""
    from src.ui.widgets import COMBO_VISIBLE_ROWS, ScrollableComboBox

    combo = ScrollableComboBox()
    assert combo.visible_rows == COMBO_VISIBLE_ROWS == 15
    assert combo.maxVisibleItems() == 15
    for index in range(20):
        combo.addItem("2026-11-15 00:00:%02d · 地图 · 完整 · 30 人" % index, index)
    combo.resize(600, 30)
    combo.show()
    qapp.processEvents()

    combo.showPopup()
    qapp.processEvents()
    popup = combo._popup
    view = combo._list
    assert popup is not None and popup.isVisible(), "下拉弹窗未显示"
    row_height = max(1, view.sizeHintForRow(0))
    # 弹窗高度 = 最多 15 行 + 边框余量（再多就被滚动条替代）
    assert popup.height() <= row_height * COMBO_VISIBLE_ROWS + 12
    # 20 条 > 15 行 ⇒ 需要滚动条（ScrollPerPixel 模式下 maximum 为像素值，> 0 即存在滚动）
    assert view.verticalScrollBar().maximum() > 0, "条目超过 15 行时应出现竖向滚动条"

    # 点击第二项：currentIndex 同步并收起弹窗
    combo._on_popup_index_activated(view.model().index(1, 0))
    qapp.processEvents()
    assert combo.currentIndex() == 1
    assert not popup.isVisible()
    # 再次打开 / 再次触发关闭（点击切换）
    combo.showPopup()
    qapp.processEvents()
    assert popup.isVisible()
    combo.showPopup()
    qapp.processEvents()
    assert not popup.isVisible()
    combo.close()
