# -*- coding: utf-8 -*-
"""T4 GUI 层测试：业务门面（mock / 临时真实库）+ 主窗口（offscreen）。

运行：``pytest tests/test_gui.py -q``（自动使用 offscreen 平台，不会弹窗、不需要显示器）。

覆盖：
- 业务门面：数据状态口径（去重入库 322,806 / 官方源 322,941）、mock 全流程、真实库读取；
- **第一轮界面契约**（task-3）：
  1) 列顺序 / 标题逐字（24 列，删「命中率」「阵营」）；
  2) 本局效率四舍五入取整（half-up）；
  3) 红绿方上下分组（绿方 = 录像者本人所在队，不写死 team==1）、组头行跨列合并且不参与排序；
  4) 组内默认排序：封禁次数降序 -> 本局效率降序 -> 伤害降序（None 置后），表头点击只在组内改排序；
  5) 「查询结果」标题行胜负平 + 比分（含 partial 局异步到达）；
  6) 1920×1080 下 30 人（+2 组头）无垂直滚动条；60 人保留滚动条、视口约 30 名玩家；
  7) 明细面板当局战绩新字段（坦克车名 / 阵营按本人队）+ 综合战绩缓存展示；
  8) 过滤 / 底部「显示 X/Y 人」/ 数据状态等既有功能不回归；
- **第二轮 UI 微调**（task-6）：
  9) 查询结果区域（表格所有列 / 标题 / 筛选控件）无任何悬停提示；
  10) 本人（录像者）行浅蓝底 + 昵称加粗，封禁黄 / 红底色优先；
  11) 综合战绩只剩标题行右上角单个「刷新」按钮（force=True），非命中玩家也可点；
  12) 「当前对局」文案与下拉条目补本人本局车名（只显示名称、绝不显示代码）。
- **第三轮 1.0 定稿 UI 精简**（task-10）：
  13) 「最近 7 天」「数据状态」按钮移除、录像区扫描入口只剩 3 个、版本号定稿；
      DataStatusDialog / open_data_status() / get_data_status() 保留可用。
- **1.0.1 启动自动扫描**（task-14）：
  14) auto_start 自动扫描改为「全部录像」（mode="all"，不再仅最近 7 天窗口的 latest），
      扫描完成后选中最新一局（index 0）；其他触发（重新检测）仍保持 latest。版本号 1.0.1。
"""
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# 必须在导入 PySide6 之前设置（offscreen 无显示器也能跑 GUI 测试）。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from src import models  # noqa: E402
from src.config import Config, load_config  # noqa: E402
from src.store import db  # noqa: E402

# 列契约（顺序固定、标题逐字；task-3 / 需求 2）。
EXPECTED_COLUMN_TITLES = [
    "昵称", "账号 ID", "军团缩写", "封禁次数", "最近封禁日期", "原因", "时长", "匹配方式",
    "车型", "本局效率", "经验值", "射击", "击穿", "造成伤害", "被击中", "受到的潜在伤害",
    "装甲抵消伤害", "发现敌人", "击伤敌方战车", "击毁敌方战车", "协助盟友造成伤害",
    "占领敌方基地点数", "保卫基地防御点数", "行驶里程",
]

# 1920×1080 默认窗口（offscreen 下显式 resize，模拟用户默认窗口）。
TARGET_WINDOW_WIDTH = 1920
TARGET_WINDOW_HEIGHT = 1080


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


def settle(app, rounds: int = 4) -> None:
    """让布局 / 信号处理跑完（processEvents 若干轮 + 短睡眠）。"""
    for _ in range(rounds):
        app.processEvents()
        time.sleep(0.02)


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


def make_window(service=None):
    """构造主窗口并显示（默认 mock 服务）。"""
    from src.ui.main_window import MainWindow

    window = MainWindow(service or make_mock_service())
    window.resize(TARGET_WINDOW_WIDTH, TARGET_WINDOW_HEIGHT)
    window.show()
    return window


# ---------------------------------------------------------------- 测试数据构造
def make_synthetic_replay(player_count: int = 30, recorder_index: int = 0,
                          map_name: str = "99_poland", map_display_name=None):
    """构造可复现的合成对局（team 1 / team 2 各一半）+ 匹配行。

    玩家坦克统一用录像 tank 代码 ``japan:J20_Type_2605``，便于验证
    「车型 = 盒子 tank_title -> game_names 中文名 -> 原始值」的兜底链。
    """
    half = player_count // 2
    players = []
    for index in range(player_count):
        players.append(
            models.ReplayPlayer(
                nickname="测试玩家%02d" % index,
                dbid=7200000000 + index,
                clan_abbrev="T%02d" % (index % 7),
                team=1 if index < half else 2,
                tank="japan:J20_Type_2605",
            )
        )
    recorder = players[recorder_index]
    detail = models.ReplayDetail(
        file_path="X:/demo/synthetic-%d.wotreplay" % player_count,
        file_name="synthetic-%d.wotreplay" % player_count,
        block_count=2,
        battle_time="2026-11-15T01:08:00+08:00",
        map_name=map_name,
        map_display_name=map_display_name,
        winner_team=2,
        recorder_dbid=recorder.dbid,
        recorder_name=recorder.nickname,
        players=players,
    )
    matches = []
    for player in players:
        matches.append(
            models.MatchedPlayer(
                nickname=player.nickname,
                dbid=player.dbid,
                clan_abbrev=player.clan_abbrev,
                team=player.team,
                tank=player.tank,
                match_type=models.MATCH_TYPE_NONE,
                ban_count=0,
                level=models.LEVEL_NONE,
                records=[],
            )
        )
    return detail, matches


def make_stats_map(matches, values=None):
    """构造当局战绩映射：{("id", dbid): BattlePlayerStats}。

    values: {nickname: {"combat":..., "damage":..., ...}}，未给出的玩家无战绩对象。
    """
    values = values or {}
    stats_map = {}
    for player in matches:
        payload = values.get(player.nickname)
        if payload is None:
            continue
        fields = {
            "dbid": player.dbid,
            "nickname": player.nickname,
            "team": player.team,
            "tank": payload.get("tank", "五式重战"),
            "combat": payload.get("combat"),
            "damage": payload.get("damage"),
            "kills": payload.get("kills"),
            "xp": payload.get("xp"),
            "shots": payload.get("shots"),
            "piercings": payload.get("piercings"),
            "spotted": payload.get("spotted"),
            "capture_points": payload.get("capture_points"),
            "hits_received": payload.get("hits_received"),
            "potential_damage": payload.get("potential_damage"),
            "damage_blocked": payload.get("damage_blocked"),
            "vehicles_damaged": payload.get("vehicles_damaged"),
            "assisted_damage": payload.get("assisted_damage"),
            "defense_points": payload.get("defense_points"),
            "mileage": payload.get("mileage"),
        }
        stats_map[("id", int(player.dbid))] = models.BattlePlayerStats(**fields)
    return stats_map


def rows_grouped_by_team(model):
    """按模型显示顺序返回 [[组头行, 玩家...], ...]（含组头文案）。"""
    groups = []
    for row in range(model.rowCount()):
        if model.is_group_row(row):
            groups.append({"label": model.group_label(row), "players": []})
        elif groups:
            groups[-1]["players"].append(model.row_object(row))
    return groups


def make_player_stats_result(update_time="2026-11-18T12:05:00+08:00",
                             fetched_at="2026-11-18T15:30:00+08:00"):
    """构造可用的综合战绩结果（含数据更新时间 / 缓存时间）。"""
    from src.app.service import PlayerStatsResult

    return PlayerStatsResult(
        stats=models.PlayerStats(
            account_id=7048141181,
            source="box_index",
            fetched_at=fetched_at,
            update_time=update_time,
            combat_power=5100.0,
            win_rate=52.5,
            avg_damage=1800.0,
            battles_hint=32000,
        ),
        available=True,
        message="数据来自偶游盒子 index（最近 1500 场标准局口径，仅供参考）",
    )


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
    # 胜负平：完整录像即使盒子不可用也能用 winner_team 给出（需求 4 数据契约）。
    assert battle.outcome_label in ("胜利", "平局", "失败")

    hit = next(item for item in query.matches if item.is_hit)
    player_stats = service.get_player_stats(hit)
    assert player_stats.available and player_stats.stats.account_id > 0

    partial = next(item for item in replays if item.is_partial)
    partial_query = service.query_replay(partial.path)
    assert partial_query.is_partial
    assert all(item.partial for item in partial_query.matches if item.is_hit)
    assert partial_query.summary["id_hits"] == 0
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


# ---------------------------------------------------------------- 列契约 / 取整
def test_result_columns_contract(qapp):
    """列顺序与标题逐字契约：24 列、删「命中率」「阵营」、车型在本局效率前。"""
    from src.ui.table_model import COLUMNS, COLUMN_INDEX

    titles = [column["title"] for column in COLUMNS]
    assert titles == EXPECTED_COLUMN_TITLES
    assert len(COLUMNS) == 24
    assert "命中率" not in titles
    assert "阵营" not in titles
    # 车型列紧跟匹配方式、在本局效率之前（需求 3）。
    assert titles.index("车型") + 1 == titles.index("本局效率")
    # 表头取模型 headerData（视图展示值）。
    model = _make_model()
    headers = [model.headerData(index, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
               for index in range(model.columnCount())]
    assert headers == EXPECTED_COLUMN_TITLES
    assert COLUMN_INDEX["vehicle"] == 8


def _make_model():
    from src.ui.table_model import PlayerTableModel

    return PlayerTableModel()


def test_combat_rounding_half_up(qapp):
    """本局效率四舍五入取整（half-up）：868.46->868、216.3->216、216.5->217、868.5->869。"""
    from src.ui.table_model import COLUMN_INDEX, STATS_STATE_READY

    detail, matches = make_synthetic_replay(player_count=6)
    stats_map = make_stats_map(
        matches,
        {
            "测试玩家00": {"combat": 868.46},
            "测试玩家01": {"combat": 216.3},
            "测试玩家02": {"combat": 216.5},
            "测试玩家03": {"combat": 868.5},
            "测试玩家04": {"combat": None},
        },
    )
    model = _make_model()
    model.set_rows(matches, detail)
    model.set_stats(STATS_STATE_READY, stats_map)
    display = {}
    for row in model.player_rows():
        player = model.row_object(row)
        index = model.index(row, COLUMN_INDEX["combat"])
        display[player.nickname] = model.data(index, Qt.ItemDataRole.DisplayRole)
    assert display["测试玩家00"] == "868"
    assert display["测试玩家01"] == "216"
    assert display["测试玩家02"] == "217"
    assert display["测试玩家03"] == "869"
    assert display["测试玩家04"] == "—"
    # 无战绩对象的玩家同样显示 —（数据层缺失优雅降级）。
    assert display["测试玩家05"] == "—"


def test_vehicle_display_priority_and_fallback(qapp, monkeypatch):
    """车型：盒子 tank_title 优先；缺失时 game_names 中文名兜底；映射缺失显示原始值。"""
    from src.ui import table_model

    player = models.MatchedPlayer(nickname="甲", tank="japan:J20_Type_2605")
    # 1) 盒子 tank_title 优先
    stats = models.BattlePlayerStats(tank="五式重战", tank_code="japan-j20_type_2605")
    assert table_model.vehicle_display(player, stats) == "五式重战"
    # 2) 盒子缺失 -> game_names.vehicle_name（用假模块固定预期，避免依赖游戏目录）
    fake = SimpleNamespace(
        vehicle_name=lambda code: {"japan:J20_Type_2605": "五式重战"}.get(str(code)),
        map_name=lambda code: {"99_poland": "斯杜季昂奇"}.get(str(code)),
    )
    monkeypatch.setattr(table_model, "_game_names_module", lambda: fake)
    assert table_model.vehicle_display(player, None) == "五式重战"
    # 3) 映射无结果 -> 原始值（不崩、不显示空白）
    unknown = models.MatchedPlayer(nickname="乙", tank="ussr:R01_Object252_BF")
    assert table_model.vehicle_display(unknown, None) == "ussr:R01_Object252_BF"
    # 4) 映射模块缺失 -> 原始值（优雅降级）
    monkeypatch.setattr(table_model, "_game_names_module", lambda: None)
    assert table_model.vehicle_display(player, None) == "japan:J20_Type_2605"
    # 5) 完全没有车型信息 -> —
    assert table_model.vehicle_display(models.MatchedPlayer(nickname="丙"), None) == "—"


# ---------------------------------------------------------------- 分组 / 排序
def test_group_green_is_recorder_team_not_hardcoded(qapp):
    """绿方 = 录像者本人所在队（此处录像者在 team 2，绿方必须是 team 2），绿方组在上。"""
    from src.ui.table_model import COLUMN_INDEX, STATS_STATE_READY

    detail, matches = make_synthetic_replay(player_count=30, recorder_index=25)  # team 2
    assert detail.recorder_dbid == matches[25].dbid and matches[25].team == 2
    model = _make_model()
    model.set_rows(matches, detail)
    model.set_stats(STATS_STATE_READY, {})
    assert model.recorder_team() == 2
    groups = rows_grouped_by_team(model)
    assert len(groups) == 2
    assert groups[0]["label"] == "绿方 · 我方（15 人）"
    assert groups[1]["label"] == "红方 · 敌方（15 人）"
    assert all(player.team == 2 for player in groups[0]["players"])
    assert all(player.team == 1 for player in groups[1]["players"])
    # 组头行不参与排序：排序后组头仍在原位置、组顺序不变。
    model.sort(COLUMN_INDEX["nickname"], Qt.SortOrder.AscendingOrder)
    groups_sorted = rows_grouped_by_team(model)
    assert groups_sorted[0]["label"].startswith("绿方")
    assert all(player.team == 2 for player in groups_sorted[0]["players"])
    assert groups_sorted[1]["label"].startswith("红方")


def test_group_headers_span_all_columns(qapp):
    """组头行跨列合并（QTableView.setSpan 覆盖全部列），且不可选中。"""
    detail, matches = make_synthetic_replay(player_count=30)
    window = make_window()
    try:
        settle(qapp)
        window.model.set_rows(matches, detail)
        settle(qapp)
        group_rows = [row for row in range(window.model.rowCount())
                      if window.model.is_group_row(row)]
        assert len(group_rows) == 2
        for row in group_rows:
            assert window.table.columnSpan(row, 0) == len(EXPECTED_COLUMN_TITLES)
            index = window.model.index(row, 0)
            flags = window.model.flags(index)
            assert not bool(flags & Qt.ItemFlag.ItemIsSelectable)
    finally:
        window.close()


def test_default_sort_ban_combat_damage(qapp):
    """组内默认排序：封禁次数降序 -> 本局效率降序 -> 伤害降序（None 置后）。"""
    from src.ui.table_model import STATS_STATE_READY

    detail, matches = make_synthetic_replay(player_count=30, recorder_index=0)  # 绿方 = team 1
    # 绿方组（team 1）内构造排序用例；其余玩家 ban=0 且无战绩，排在后面。
    spec = {
        "测试玩家00": (2, 100.0, 500),   # 次数 2 / 效率 100 / 伤害 500
        "测试玩家01": (2, 200.0, 100),   # 次数 2 / 效率 200 / 伤害 100
        "测试玩家02": (2, 200.0, 300),   # 次数 2 / 效率 200 / 伤害 300 -> 应排第一
        "测试玩家03": (1, 999.0, 999),   # 次数 1 -> 排 2 次之后
        "测试玩家04": (0, None, None),   # 无战绩 -> None 置后
    }
    for player in matches:
        if player.nickname in spec:
            count = spec[player.nickname][0]
            player.ban_count = count
            player.level = (models.LEVEL_RED if count >= 2
                            else models.LEVEL_YELLOW if count == 1 else models.LEVEL_NONE)
            player.records = [
                models.BanRecord(batch_day="2026-11-06", nickname=player.nickname,
                                 reason="测试", duration="永久")
                for _ in range(count)
            ]
    stats_map = make_stats_map(matches, {
        name: {"combat": value[1], "damage": value[2]}
        for name, value in spec.items()
    })
    model = _make_model()
    model.set_rows(matches, detail)
    model.set_stats(STATS_STATE_READY, stats_map)
    groups = rows_grouped_by_team(model)
    green = [player.nickname for player in groups[0]["players"]]
    # 前 5 名 = 构造用例的期望顺序（次数降序 -> 效率降序 -> 伤害降序）。
    assert green[:5] == ["测试玩家02", "测试玩家01", "测试玩家00", "测试玩家03", "测试玩家04"]
    # 表头点击只在组内改排序：按昵称升序后组顺序 / 组头不动。
    from src.ui.table_model import COLUMN_INDEX
    model.sort(COLUMN_INDEX["nickname"], Qt.SortOrder.AscendingOrder)
    groups_sorted = rows_grouped_by_team(model)
    assert groups_sorted[0]["label"].startswith("绿方")
    assert groups_sorted[1]["label"].startswith("红方")
    names = [player.nickname for player in groups_sorted[0]["players"]]
    assert names == sorted(names, key=str.lower)
    # 恢复默认排序后回到 次数/效率/伤害 顺序。
    model.reset_sort()
    assert [player.nickname for player in rows_grouped_by_team(model)[0]["players"]][:5] == green[:5]


# ---------------------------------------------------------------- 布局 / 滚动条
def test_30_players_no_vertical_scrollbar_at_1920x1080(qapp):
    """30 人（+2 组头）在 1920×1080 默认窗口下一屏完整显示：verticalScrollBar().maximum()==0。"""
    detail, matches = make_synthetic_replay(player_count=30)
    window = make_window()
    try:
        settle(qapp)
        window.stack.setCurrentIndex(1)
        window.model.set_rows(matches, detail)
        window.model.set_stats("ready", make_stats_map(
            matches, {player.nickname: {"combat": 500 + index, "damage": 1000 + index}
                      for index, player in enumerate(matches)}))
        settle(qapp)
        assert window.width() == TARGET_WINDOW_WIDTH and window.height() == TARGET_WINDOW_HEIGHT
        assert window.model.rowCount() == 32  # 30 玩家 + 2 组头
        assert window.model.player_row_count() == 30
        assert window.table.verticalScrollBar().maximum() == 0, "30 人应一屏完整显示"
        assert window.table.horizontalScrollBar().maximum() == 0, "24 列在 1920 宽下应横向放下"
        assert window.table.verticalHeader().defaultSectionSize() <= 26
    finally:
        window.close()


def test_60_players_keep_vertical_scrollbar(qapp):
    """60 人（+2 组头）保留垂直滚动条，视口约 30 名玩家（不缩成蚂蚁字）。"""
    detail, matches = make_synthetic_replay(player_count=60)
    window = make_window()
    try:
        settle(qapp)
        window.stack.setCurrentIndex(1)
        window.model.set_rows(matches, detail)
        settle(qapp)
        assert window.model.rowCount() == 62
        assert window.model.player_row_count() == 60
        assert window.table.verticalScrollBar().maximum() > 0, "60 人应保留垂直滚动条"
        row_height = window.table.verticalHeader().defaultSectionSize()
        visible_rows = window.table.viewport().height() // max(1, row_height)
        # 视口约 30 名玩家（32 行左右：30 人局刚好一屏的行数）。
        assert 25 <= visible_rows <= 40, "视口应约 30 名玩家，实际行数=%d" % visible_rows
        # 分组仍为两组（60 人对局同样红绿分组）。
        groups = rows_grouped_by_team(window.model)
        assert len(groups) == 2
        assert groups[0]["label"].endswith("（30 人）")
    finally:
        window.close()


# ---------------------------------------------------------------- 胜负平标题
def test_outcome_title_draw_with_score(qapp, monkeypatch):
    """「查询结果」标题行显示胜负平 + 比分（平局 14v6），异步战绩到达后更新。"""
    detail, matches = make_synthetic_replay(player_count=30)
    window = make_window()
    try:
        settle(qapp)
        window.stack.setCurrentIndex(1)
        window.model.set_rows(matches, detail)
        window._apply_filters()
        settle(qapp)
        assert window.current_outcome_text() == "查询结果"

        fake = SimpleNamespace(available=True, stats=None, message="", cached=False,
                               outcome_label="平局", score="14v6")
        window._on_battle_finished(window._query_seq, fake)
        settle(qapp)
        assert window.current_outcome_text() == "查询结果 · 平局 14v6"
        assert window.result_title.property("outcome") == "draw"

        # 胜利 / 失败配色分支
        window._on_battle_finished(window._query_seq, SimpleNamespace(
            available=True, stats=None, message="", outcome_label="胜利", score="15v7"))
        assert window.current_outcome_text() == "查询结果 · 胜利 15v7"
        assert window.result_title.property("outcome") == "victory"
        window._on_battle_finished(window._query_seq, SimpleNamespace(
            available=True, stats=None, message="", outcome_label="失败", score="6v14"))
        assert window.current_outcome_text() == "查询结果 · 失败 6v14"
        assert window.result_title.property("outcome") == "defeat"
    finally:
        window.close()


def test_partial_replay_outcome_async(qapp, monkeypatch):
    """partial 局也会异步请求战绩并显示胜负平（不再被界面提前判定不可用）。"""
    from src.ui.main_window import MainWindow

    detail, matches = make_synthetic_replay(player_count=30)
    detail.block_count = 1  # partial：未打完
    detail.players = [models.ReplayPlayer(nickname=p.nickname, clan_abbrev=p.clan_abbrev,
                                          team=p.team, tank=p.tank) for p in detail.players]
    assert detail.is_partial

    service = make_mock_service()
    calls = []

    def fake_battle_stats(target_detail):
        calls.append(target_detail)
        return SimpleNamespace(available=False, stats=None, cached=False,
                               outcome_label="平局", score="14v6",
                               message="partial 局：盒子战斗日志匹配成功")

    monkeypatch.setattr(service, "get_battle_stats", fake_battle_stats)
    window = MainWindow(service)
    window.resize(TARGET_WINDOW_WIDTH, TARGET_WINDOW_HEIGHT)
    window.show()
    try:
        settle(qapp)
        window.stack.setCurrentIndex(1)
        window.model.set_rows(matches, detail)
        window._apply_filters()
        window._request_battle_stats(window._query_seq, detail)
        assert wait_until(qapp, lambda: window._battle_worker is None, timeout_s=10)
        assert calls and calls[0] is detail
        assert window.current_outcome_text() == "查询结果 · 平局 14v6"
    finally:
        window.close()
        service.dispose()


# ---------------------------------------------------------------- 地图中文名
def test_map_name_localized(qapp):
    """「当前对局」显示地图中文名：detail.map_display_name 优先，缺失用 game_names 兜底。"""
    from src.ui import main_window as main_window_module

    pytest.importorskip("src.game_names")
    window = make_window()
    try:
        settle(qapp)
        # 1) game_names 兜底：99_poland -> 斯杜季昂奇
        assert main_window_module.MainWindow._localized_map_name("99_poland") == "斯杜季昂奇"
        detail, _matches = make_synthetic_replay(player_count=30, map_name="99_poland",
                                                 map_display_name=None)
        window.replay_combo.clear()
        window.replay_combo.addItem("占位", detail.file_path)
        window.replay_combo.setCurrentIndex(0)
        window._refresh_current_replay_label(detail)
        text = window.replay_combo.currentText()
        assert "斯杜季昂奇" in text and "99_poland" not in text
        # 2) detail.map_display_name 优先（覆盖 game_names 结果）
        detail.map_display_name = "自定义地图名"
        window._refresh_current_replay_label(detail)
        assert "自定义地图名" in window.replay_combo.currentText()
    finally:
        window.close()


# ---------------------------------------------------------------- 明细面板
def test_detail_panel_new_fields_and_team(qapp):
    """明细面板：当局战绩 16 字段按盒子顺序展示、坦克显示车名、阵营按本人队。"""
    from src.ui.widgets import BATTLE_FIELDS, PlayerDetailPanel

    panel = PlayerDetailPanel()
    player = models.MatchedPlayer(nickname="kingbob_", dbid=7048141181, team=2,
                                  tank="japan:J20_Type_2605", match_type=models.MATCH_TYPE_ID,
                                  ban_count=1, level=models.LEVEL_YELLOW,
                                  records=[models.BanRecord(batch_day="2026-11-06",
                                                            nickname="kingbob_",
                                                            reason="违规", duration="永久")])
    stats = models.BattlePlayerStats(
        dbid=7048141181, nickname="kingbob_", team=2, tank="五式重战",
        combat=868.464, xp=438, shots=6, piercings=3, damage=2252,
        hits_received=12, potential_damage=4520, damage_blocked=1800,
        spotted=2, vehicles_damaged=2, kills=0, assisted_damage=941,
        capture_points=0, defense_points=0, mileage=1158,
    )
    panel.set_player(player, {("id", 7048141181): stats}, "ready", "", recorder_team=2)
    values = {key: label.text() for key, label in panel.battle_values.items()}
    assert [key for key, _label in BATTLE_FIELDS] == list(values.keys())
    assert values["tank"] == "五式重战"
    assert values["combat"] == "868"          # 868.464 half-up 取整
    assert values["xp"] == "438"
    assert values["shots"] == "6"
    assert values["piercings"] == "3"
    assert values["damage"] == "2252"
    assert values["hits_received"] == "12"
    assert values["potential_damage"] == "4520"
    assert values["damage_blocked"] == "1800"
    assert values["spotted"] == "2"
    assert values["vehicles_damaged"] == "2"
    assert values["kills"] == "0"
    assert values["assisted_damage"] == "941"
    assert values["capture_points"] == "0"
    assert values["defense_points"] == "0"
    assert values["mileage"] == "1158"
    # 阵营：绿方 = 录像者本人所在队（team 2）。
    assert "绿方 · 我方" in panel.sub_label.text()
    assert "五式重战" in panel.sub_label.text()
    # 换成敌对方（team 1）-> 红方 · 敌方
    player.team = 1
    panel.set_player(player, {("id", 7048141181): stats}, "ready", "", recorder_team=2)
    assert "红方 · 敌方" in panel.sub_label.text()
    # 缺失新字段 -> —（数据层未提供时优雅降级）
    bare = models.BattlePlayerStats(dbid=1, nickname="无数据", tank="五式重战")
    panel.set_player(models.MatchedPlayer(nickname="无数据", dbid=1, team=1),
                     {("id", 1): bare}, "ready", "", recorder_team=1)
    assert panel.battle_values["potential_damage"].text() == "—"
    assert panel.battle_values["mileage"].text() == "—"
    panel.deleteLater()


def test_detail_panel_single_refresh_button_and_cached_stats(qapp):
    """综合战绩：只剩标题行右上角单个「刷新」按钮；缓存直接展示；非命中玩家也可点。"""
    from src.ui.widgets import PlayerDetailPanel

    panel = PlayerDetailPanel()
    player = models.MatchedPlayer(nickname="kingbob_", dbid=7048141181, team=1,
                                  match_type=models.MATCH_TYPE_ID, ban_count=1,
                                  level=models.LEVEL_YELLOW,
                                  records=[models.BanRecord(batch_day="2026-11-06",
                                                            nickname="kingbob_")])
    panel.set_player(player, {}, "ready", "", recorder_team=1)
    # 第二轮反馈：合并为单个「刷新」按钮（放标题行右上角），旧两个按钮文案不再存在。
    assert panel.refresh_player_stats_button.text() == "刷新"
    assert panel.player_stats_button is panel.refresh_player_stats_button
    assert panel.refresh_player_stats_button.isEnabled()
    button_texts = [button.text() for button in panel.findChildren(QPushButton)]
    assert "查看综合战绩" not in button_texts
    assert "刷新综合战绩" not in button_texts
    # 有缓存直接展示：数据更新时间 / 缓存时间（标注「点刷新更新」）
    panel.show_player_stats(make_player_stats_result(), from_cache=True)
    meta = panel.player_stats_meta.text()
    assert "数据更新时间" in meta and "缓存于" in meta and "点刷新更新" in meta
    assert panel.player_values["combat_power"].text() == "5,100"
    # 第二轮反馈：未命中玩家同样可点「刷新」（不再限制必须命中封禁名单）。
    panel.set_player(models.MatchedPlayer(nickname="路人", dbid=2, team=2), {}, "ready", "")
    assert panel.refresh_player_stats_button.isEnabled()
    panel.deleteLater()


def test_refresh_player_stats_uses_force(qapp, monkeypatch):
    """「刷新」按钮点击走 force=True；选中玩家不自动请求；请求失败优雅提示。"""
    from src.ui.main_window import MainWindow

    service = make_mock_service()
    calls = []

    def fake_get_player_stats(player, force=False):
        calls.append({"player": player, "force": force})
        return make_player_stats_result()

    monkeypatch.setattr(service, "get_player_stats", fake_get_player_stats)
    detail, matches = make_synthetic_replay(player_count=30)
    window = MainWindow(service)
    window.resize(TARGET_WINDOW_WIDTH, TARGET_WINDOW_HEIGHT)
    window.show()
    try:
        settle(qapp)
        window.stack.setCurrentIndex(1)
        window.model.set_rows(matches, detail)
        window._apply_filters()
        settle(qapp)
        # 1) 选中玩家不自动请求综合战绩
        window._select_first_player()
        settle(qapp)
        assert calls == [], "选中玩家不应自动请求综合战绩"
        player = window.selected_player()
        assert player is not None
        assert window.detail_panel.refresh_player_stats_button.isEnabled()
        # 2) 点击单个「刷新」按钮 -> force=True
        window.detail_panel.refresh_player_stats_button.click()
        assert wait_until(qapp, lambda: window._player_stats_worker is None, timeout_s=10)
        assert calls and calls[-1]["force"] is True
        assert "缓存时间" in window.detail_panel.player_stats_meta.text()
        # 3) 再次选中该玩家：直接展示缓存（不发新请求）
        before = len(calls)
        window.table.selectRow(window.model.first_player_row())
        settle(qapp)
        assert len(calls) == before, "已有缓存时选中玩家不应再发请求"
        assert "缓存时间" in window.detail_panel.player_stats_meta.text()
    finally:
        window.close()
        service.dispose()


def test_refresh_button_enabled_for_non_hit_player(qapp, monkeypatch):
    """非命中玩家也可点「刷新」；数据层尚未放开时按返回中文提示优雅降级。"""
    from src.app.service import PlayerStatsResult
    from src.ui.main_window import MainWindow

    service = make_mock_service()
    calls = []

    def fake_get_player_stats(player, force=False):
        calls.append({"player": player, "force": force})
        return PlayerStatsResult(
            available=False,
            message="该玩家未命中封禁名单，暂无法点查综合战绩（数据层限制）",
        )

    monkeypatch.setattr(service, "get_player_stats", fake_get_player_stats)
    detail, matches = make_synthetic_replay(player_count=30)
    window = MainWindow(service)
    window.resize(TARGET_WINDOW_WIDTH, TARGET_WINDOW_HEIGHT)
    window.show()
    try:
        settle(qapp)
        window.stack.setCurrentIndex(1)
        window.model.set_rows(matches, detail)
        window._apply_filters()
        settle(qapp)
        # 选一名未命中玩家（构造数据全部 match_type=none）
        non_hit_row = None
        for row in window.model.player_rows():
            candidate = window.model.row_object(row)
            if candidate is not None and not candidate.is_hit:
                non_hit_row = row
                break
        assert non_hit_row is not None
        window.table.selectRow(non_hit_row)
        settle(qapp)
        button = window.detail_panel.refresh_player_stats_button
        assert button.isEnabled(), "非命中玩家的「刷新」按钮也应可点"
        button.click()
        assert wait_until(qapp, lambda: window._player_stats_worker is None, timeout_s=10)
        assert calls and calls[-1]["force"] is True
        assert "未命中" in window.detail_panel.player_stats_meta.text()
    finally:
        window.close()
        service.dispose()


def test_query_result_area_has_no_tooltips(qapp):
    """第二轮反馈：查询结果区域（表格所有列 + 标题 + 筛选控件）一律无悬停提示。"""
    from src.ui.main_window import MainWindow
    from src.ui.table_model import STATS_STATE_READY

    detail, matches = make_synthetic_replay(player_count=30)
    stats_map = make_stats_map(
        matches,
        {player.nickname: {"combat": 500 + index, "damage": 1000 + index}
         for index, player in enumerate(matches)},
    )
    model = _make_model()
    model.set_rows(matches, detail)
    model.set_stats(STATS_STATE_READY, stats_map)
    for row in range(model.rowCount()):
        for column in range(model.columnCount()):
            tooltip = model.data(model.index(row, column), Qt.ItemDataRole.ToolTipRole)
            assert tooltip in (None, ""), "第 %d 行第 %d 列仍有悬停提示：%r" % (
                row, column, tooltip)
    # 主窗口「查询结果」卡片内控件同样无 tooltip；顶栏按钮 / 明细面板 tooltip 保留。
    service = make_mock_service()
    window = MainWindow(service)
    window.resize(TARGET_WINDOW_WIDTH, TARGET_WINDOW_HEIGHT)
    window.show()
    try:
        settle(qapp)
        assert window.result_title.toolTip() == ""
        assert window.id_only_check.toolTip() == ""
        assert window.hits_only_check.toolTip() == ""
        assert window.level_combo.toolTip() == ""
        assert window.table.toolTip() == ""
        assert window.count_label.toolTip() == ""
        assert window.summary_label.toolTip() == ""
        # 顶栏按钮 tooltip 保留（不在「查询结果」区域内）；1.0 起顶栏只剩 3 个按钮
        assert window.update_button.toolTip()
        assert window.redetect_button.toolTip() and window.manual_game_button.toolTip()
        assert not hasattr(window, "data_status_button")
        # 明细面板 tooltip 保留
        assert window.detail_panel.player_title.toolTip()
        assert window.detail_panel.refresh_player_stats_button.toolTip()
    finally:
        window.close()
        service.dispose()


def test_recorder_row_highlight_and_nickname_bold(qapp):
    """本人（录像者）行浅蓝底 + 昵称加粗；封禁黄/红底色优先且不被盖掉。"""
    from src.ui import theme
    from src.ui.table_model import STATS_STATE_READY

    detail, matches = make_synthetic_replay(player_count=30, recorder_index=25)  # team 2
    # 构造：本人未命中 -> 浅蓝底；另两名命中玩家 -> 黄 / 红底优先。
    matches[0].ban_count = 1
    matches[0].level = models.LEVEL_YELLOW
    matches[0].records = [models.BanRecord(batch_day="2026-11-06", nickname=matches[0].nickname)]
    matches[1].ban_count = 2
    matches[1].level = models.LEVEL_RED
    matches[1].records = [models.BanRecord(batch_day="2026-11-06", nickname=matches[1].nickname)]
    model = _make_model()
    model.set_rows(matches, detail)
    model.set_stats(STATS_STATE_READY, {})
    recorder = matches[25]
    assert model.is_recorder(recorder) and model.recorder_team() == 2
    row_of = {model.row_object(row).nickname: row for row in model.player_rows()}
    recorder_row = row_of[recorder.nickname]
    assert model.is_recorder_row(recorder_row)
    bg = model.data(model.index(recorder_row, 0), Qt.ItemDataRole.BackgroundRole)
    assert bg is not None and bg.name().lower() == theme.COLOR_RECORDER_BG
    nickname_font = model.data(model.index(recorder_row, 0), Qt.ItemDataRole.FontRole)
    assert nickname_font is not None and nickname_font.bold()
    # 非本人、未命中玩家：无底色、昵称不加粗
    other_row = row_of[matches[2].nickname]
    assert not model.is_recorder_row(other_row)
    assert model.data(model.index(other_row, 0), Qt.ItemDataRole.BackgroundRole) is None
    assert model.data(model.index(other_row, 0), Qt.ItemDataRole.FontRole) is None
    # 封禁黄 / 红底优先级更高
    yellow_bg = model.data(model.index(row_of[matches[0].nickname], 0),
                           Qt.ItemDataRole.BackgroundRole)
    assert yellow_bg is not None and yellow_bg.name().lower() == theme.COLOR_YELLOW_BG
    red_bg = model.data(model.index(row_of[matches[1].nickname], 0),
                        Qt.ItemDataRole.BackgroundRole)
    assert red_bg is not None and red_bg.name().lower() == theme.COLOR_RED_BG
    # 本人若命中封禁：黄底优先于浅蓝底，但昵称仍加粗
    matches[25].ban_count = 1
    matches[25].level = models.LEVEL_YELLOW
    matches[25].records = [models.BanRecord(batch_day="2026-11-06",
                                            nickname=matches[25].nickname)]
    model.set_rows(matches, detail)
    row_of = {model.row_object(row).nickname: row for row in model.player_rows()}
    recorder_row = row_of[matches[25].nickname]
    bg = model.data(model.index(recorder_row, 0), Qt.ItemDataRole.BackgroundRole)
    assert bg is not None and bg.name().lower() == theme.COLOR_YELLOW_BG
    font = model.data(model.index(recorder_row, 0), Qt.ItemDataRole.FontRole)
    assert font is not None and font.bold()


def test_selection_does_not_cover_ban_background(qapp):
    """选中行用半透明叠加：封禁黄底在选中后仍可见（不被整块选中蓝盖掉）。"""
    from src.ui import theme
    from src.ui.main_window import ResultRowDelegate
    from src.ui.table_model import STATS_STATE_READY

    detail, matches = make_synthetic_replay(player_count=30, recorder_index=25)
    matches[0].ban_count = 1
    matches[0].level = models.LEVEL_YELLOW
    matches[0].records = [models.BanRecord(batch_day="2026-11-06", nickname=matches[0].nickname)]
    window = make_window()
    try:
        settle(qapp)
        window.stack.setCurrentIndex(1)
        window.model.set_rows(matches, detail)
        window.model.set_stats(STATS_STATE_READY, {})
        settle(qapp)
        assert isinstance(window.table.itemDelegate(), ResultRowDelegate)
        row_of = {window.model.row_object(row).nickname: row
                  for row in window.model.player_rows()}
        yellow_row = row_of[matches[0].nickname]
        window.table.selectRow(yellow_row)
        settle(qapp)
        rect = window.table.visualRect(window.model.index(yellow_row, 3))
        image = window.table.viewport().grab().toImage()
        color = image.pixelColor(rect.x() + rect.width() - 3, rect.y() + 3)
        # 半透明主色叠加在封禁黄 (#fff3c4) 上约为 (216,217,203)；不应变成纯选中蓝 #dbeafe。
        expected = (216, 217, 203)
        actual = (color.red(), color.green(), color.blue())
        assert all(abs(value - want) <= 10 for value, want in zip(actual, expected)), \
            "选中行盖掉了封禁黄底：%s %s" % (color.name(), actual)
        # 本人行未选中时保持浅蓝底
        recorder_row = row_of[matches[25].nickname]
        recorder_bg = window.model.data(window.model.index(recorder_row, 0),
                                        Qt.ItemDataRole.BackgroundRole)
        assert recorder_bg is not None
        assert recorder_bg.name().lower() == theme.COLOR_RECORDER_BG
    finally:
        window.close()


def test_current_replay_label_contains_recorder_vehicle(qapp):
    """「当前对局」文案在时间/地图后插入本人本局车名（只显示名称、绝不显示代码）。"""
    from src.ui.main_window import MainWindow

    pytest.importorskip("src.game_names")
    detail, _matches = make_synthetic_replay(player_count=30, map_name="99_poland",
                                             map_display_name=None)
    detail.block_count = 1  # partial 局
    label = MainWindow._localized_replay_label(detail)
    assert "斯杜季昂奇" in label and "五式重战" in label
    assert "partial（未打完）" in label
    assert label.index("斯杜季昂奇") < label.index("五式重战") < label.index("partial（未打完）")
    for code in ("99_poland", "japan", "J20"):
        assert code not in label, "当前对局不得显示车辆/地图代码：%r" % label
    # 主窗口下拉文案：查询完成后同样补车名
    window = make_window()
    try:
        settle(qapp)
        window.replay_combo.clear()
        window.replay_combo.addItem("占位", detail.file_path)
        window.replay_combo.setCurrentIndex(0)
        window._refresh_current_replay_label(detail)
        text = window.replay_combo.currentText()
        assert "斯杜季昂奇" in text and "五式重战" in text
        for code in ("99_poland", "japan", "J20"):
            assert code not in text
    finally:
        window.close()
    # 车名映射失败 -> 省略该项，绝不显示原始代码
    detail.players[0].tank = "ussr:NOT_A_REAL_TANK"
    label_failed = MainWindow._localized_replay_label(detail)
    assert "NOT_A_REAL_TANK" not in label_failed and "ussr" not in label_failed
    assert "五式重战" not in label_failed


def test_combo_items_include_vehicle_from_filename(qapp):
    """下拉列表条目：从录像文件名提取车辆代码映射车名；失败不加且绝不显示代码。"""
    from src.ui.main_window import MainWindow

    pytest.importorskip("src.game_names")
    info = models.ReplayInfo(
        path=r"X:\replays\20261115_0108_japan-J20_Type_2605_99_poland.wotreplay",
        battle_time="2026-11-15T01:08:00+08:00",
        map_name="99_poland",
        block_count=1,
        player_count=30,
    )
    label = MainWindow._localized_replay_label(info)
    assert "斯杜季昂奇" in label and "五式重战" in label
    assert "99_poland" not in label and "japan" not in label
    # 无法可靠提取（mock 风格文件名 / 非代码地图）时不加车名、不显示代码
    unknown = models.ReplayInfo(
        path=r"X:\tmp\20261117_201400_CN_锡默尔斯多夫.wotreplay",
        battle_time="2026-11-17T20:14:00+08:00",
        map_name="锡默尔斯多夫",
        block_count=2,
        player_count=30,
    )
    label_unknown = MainWindow._localized_replay_label(unknown)
    assert "五式重战" not in label_unknown
    assert "CN" not in label_unknown


def test_select_player_uses_peek_cache_without_request(qapp, monkeypatch):
    """数据层只读缓存接口（peek_player_stats_cache）可用时：选中即展示、零网络请求。"""
    from src.ui.main_window import MainWindow

    service = make_mock_service()
    calls = []
    cached = make_player_stats_result(update_time="2026-11-19T08:00:00+08:00",
                                      fetched_at="2026-11-19T09:30:00+08:00")

    def fake_peek(player):
        calls.append(player)
        return cached

    def fail_get(player, force=False):
        raise AssertionError("选中玩家不得自动请求综合战绩")

    monkeypatch.setattr(service, "peek_player_stats_cache", fake_peek, raising=False)
    monkeypatch.setattr(service, "get_player_stats", fail_get)
    detail, matches = make_synthetic_replay(player_count=30)
    window = MainWindow(service)
    window.resize(TARGET_WINDOW_WIDTH, TARGET_WINDOW_HEIGHT)
    window.show()
    try:
        settle(qapp)
        window.stack.setCurrentIndex(1)
        window.model.set_rows(matches, detail)
        window._apply_filters()
        window._select_first_player()
        settle(qapp)
        assert calls, "选中玩家时应只读探测数据层缓存"
        meta = window.detail_panel.player_stats_meta.text()
        assert "缓存于" in meta and "点刷新更新" in meta
        assert window.detail_panel.player_values["combat_power"].text() == "5,100"
    finally:
        window.close()
        service.dispose()


# ---------------------------------------------------------------- 主窗口既有功能
def test_main_window_mock_flow(qapp):
    """主窗口：扫描 -> 查询 -> 分组表 -> 明细 -> 筛选 -> 状态页 -> A1 定时器。"""
    from src.ui.main_window import MainWindow
    from src.ui.table_model import COLUMN_INDEX

    service = make_mock_service()
    window = MainWindow(service)
    window.resize(1600, 900)
    window.show()
    qapp.processEvents()

    # v0.3：顶栏不再显示数据版本条 / 模式徽标 / 本机环境路径；1.0 起「数据状态」按钮也移除
    # （数据状态页仅保留 open_data_status() 内部 / 脚本入口）
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
        and window.model.player_row_count() == 30
        and window.model.stats_state() in ("ready", "unavailable"),
        timeout_s=20,
    ), "mock 自动扫描/查询未在超时内完成"
    assert "命中 6" in window.summary_label.text()
    # 红绿分组 + 组头
    groups = rows_grouped_by_team(window.model)
    assert len(groups) == 2
    assert groups[0]["label"].startswith("绿方") and groups[1]["label"].startswith("红方")
    assert window.model.rowCount() == 32
    # 胜负平标题（mock 完整局 winner_team=2、录像者 team=1 -> 失败）
    assert "查询结果 ·" in window.current_outcome_text()

    # 选中第一行（组头下一行）-> 明细面板
    window._select_first_player()
    qapp.processEvents()
    assert window.detail_panel.name_label.text() != "未选择玩家"
    assert window.detail_panel.record_table.rowCount() >= 1
    assert window.detail_panel.battle_values["tank"].text() not in ("", "—")

    # 筛选：仅看 ID 命中（4 人命中：绿方 2 + 红方 2，组头仍保留 2 行）
    window.id_only_check.setChecked(True)
    qapp.processEvents()
    assert window.model.player_row_count() == 4
    assert window.model.rowCount() == 6
    assert "显示 4 / 30 人" in window.count_label.text()
    window.id_only_check.setChecked(False)
    qapp.processEvents()
    assert window.model.player_row_count() == 30

    # 只看命中 / 标色筛选
    window.hits_only_check.setChecked(True)
    qapp.processEvents()
    assert window.model.player_row_count() == 6
    window.hits_only_check.setChecked(False)
    window.level_combo.setCurrentIndex(1)  # 红标
    qapp.processEvents()
    assert window.model.player_row_count() == 2
    window.level_combo.setCurrentIndex(0)
    qapp.processEvents()
    assert window.model.player_row_count() == 30
    assert "显示 30 / 30 人" in window.count_label.text()

    # 表头排序：按昵称升序（组内）
    window.model.sort(COLUMN_INDEX["nickname"], Qt.SortOrder.AscendingOrder)
    groups_sorted = rows_grouped_by_team(window.model)
    assert groups_sorted[0]["label"].startswith("绿方")
    names = [player.nickname for player in groups_sorted[0]["players"]]
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
    # 第二轮反馈：两个按钮合并为标题行右上角单个「刷新」
    assert detail.refresh_player_stats_button.text() == "刷新"
    assert detail.player_stats_button is detail.refresh_player_stats_button
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
        lambda: window.model.player_row_count() > 0 and window.model.stats_state() in ("ready", "unavailable"),
        timeout_s=20,
    )
    window._select_first_player()
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
    """F2：QApplication 版本号读 config.app_version，不再写死旧版本号。"""
    from src.ui.main_window import create_application

    app = create_application([])
    assert app.applicationVersion() == load_config().app_version


def test_round3_final_ui_trim(qapp):
    """第三轮 1.0 定稿：「最近 7 天」「数据状态」按钮移除，扫描入口 3 个，版本 1.0.2。"""
    from src.ui.main_window import SCAN_MODE_LABELS, MainWindow
    from src.ui.widgets import DataStatusDialog

    # 1) 版本号 1.0.2（配置唯一真源；1.0.0 起的定稿版本串）
    assert load_config().app_version == "1.0.2"

    service = make_mock_service()
    window = MainWindow(service)
    window.resize(TARGET_WINDOW_WIDTH, TARGET_WINDOW_HEIGHT)
    window.show()
    try:
        settle(qapp)
        # 2) 窗口标题与顶栏版本标签显示 v1.0.2
        assert window.windowTitle().endswith("v1.0.2"), window.windowTitle()
        version_labels = [label.text() for label in window.findChildren(QLabel)
                          if label.objectName() == "SubTitle"]
        assert version_labels == ["v1.0.2"], version_labels

        # 3) 两个按钮彻底移除：属性不存在，且全窗口按钮文案里也不存在
        assert not hasattr(window, "scan_recent_button")
        assert not hasattr(window, "data_status_button")
        all_button_texts = [button.text() for button in window.findChildren(QPushButton)]
        assert "最近 7 天" not in all_button_texts, all_button_texts
        assert "数据状态" not in all_button_texts, all_button_texts

        # 4) 顶栏只剩 3 个按钮：重新检测 / 手动选择游戏目录 / 更新名单
        header = window.redetect_button.parentWidget()
        header_texts = [button.text() for button in header.findChildren(QPushButton)]
        assert header_texts == ["重新检测", "手动选择游戏目录", "更新名单"], header_texts

        # 5) 录像区扫描入口只剩 3 个（「取消」按钮仅扫描中显示，不是扫描入口）
        replay_bar = window.scan_latest_button.parentWidget()
        scan_texts = [button.text() for button in replay_bar.findChildren(QPushButton)
                      if button is not window.cancel_button]
        assert scan_texts == ["最新完整录像", "全部录像", "手动选择…"], scan_texts
        assert window.cancel_button.text() == "取消" and window.cancel_button.isHidden()
        assert window.scan_latest_button.isEnabled() and window.scan_all_button.isEnabled()
        assert window.manual_button.isEnabled()

        # 6) SCAN_MODE_LABELS 去掉 recent；空态提示文案不再出现「最近 7 天」
        assert list(SCAN_MODE_LABELS) == ["latest", "all", "file"], SCAN_MODE_LABELS
        assert "最近 7 天" not in SCAN_MODE_LABELS.values()
        hint = window.empty_hint.text()
        assert "最新完整录像" in hint and "手动选择" in hint
        assert "最近 7 天" not in hint, hint

        # 7) 保留项：DataStatusDialog / open_data_status() / get_data_status() 仍可用
        assert hasattr(window, "open_data_status") and hasattr(service, "get_data_status")
        dialog = window.open_data_status(modal=False)
        assert isinstance(dialog, DataStatusDialog)
        qapp.processEvents()
        dialog.close()
        assert window._data_status is not None
    finally:
        window.close()
        service.dispose()


def test_auto_start_scans_all_replays_and_selects_newest(qapp, monkeypatch):
    """1.0.1：启动自动扫描「全部录像」（mode="all"），完成后选中最新一局（index 0）。

    用户反馈：删了「最近 7 天」按钮后界面自动加载的仍是近 7 天窗口内的 latest；
    改为 all 后最新一局（本机 2026-11-15 01:08 partial）会排在列表第 1 个并被自动查询。
    其他触发（重新检测）保持原 latest 逻辑。
    """
    from src.app import mock_data
    from src.ui.main_window import MainWindow

    service = make_mock_service()
    window = MainWindow(service)
    window.show()
    try:
        settle(qapp)
        scan_modes = []
        monkeypatch.setattr(window, "start_scan",
                            lambda mode="latest": scan_modes.append(mode))
        # P0 修复后 _on_scan_finished 会调度启动名单检查；这里拦截，避免本用例引入后台更新。
        monkeypatch.setattr(window, "_start_update_check",
                            lambda *args, **kwargs: None)
        # 启动自动动作：扫描全部录像
        window.auto_start()
        assert scan_modes == ["all"], scan_modes
        # 其他触发保持原逻辑：重新检测仍扫描「最新完整录像」（latest）
        window._on_redetect_environment_clicked()
        assert scan_modes == ["all", "latest"], scan_modes
        # 模拟 all 扫描完成：target_index=0（最新一局），回填下拉并自动查询该局
        replays = mock_data.build_replays()
        window._on_scan_finished("all", replays)
        assert window.replay_combo.currentIndex() == 0
        assert window.replay_combo.itemData(0) == replays[0].path
        assert window._current_replays[0].path == replays[0].path
        assert window._current_replay is not None
        assert window._current_replay.path == replays[0].path
    finally:
        window.close()
        service.dispose()


def test_auto_start_real_mode_with_fake_scan(qapp, monkeypatch, tmp_path):
    """F1：真实模式 auto_start（fake detect + fake 扫描/查询）启动即出结果，且可重新检测。"""
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
    # P0 修复后启动检查在扫描结束后触发：测试里用假更新拦截官方请求（不发真实网络）。
    monkeypatch.setattr(
        service, "update_ban_list",
        lambda force=False, progress_cb=None, cancel_cb=None: models.UpdateResult(
            ok=True, message="测试：跳过官方更新"),
    )
    window = MainWindow(service)
    window.show()
    qapp.processEvents()
    # 本用例验证老用户自动流程；空库首启会走首次使用弹窗流程，这里显式跳过。
    monkeypatch.setattr(window, "_first_run_flow_state", lambda: {"active": False})
    window.auto_start()
    # 正确等待方式：processEvents + sleep（不能用 QTest.qWait 高频轮询）
    assert wait_until(qapp, lambda: window.model.player_row_count() > 0, timeout_s=15), "auto_start 未出结果"
    redetect_tip = window.redetect_button.toolTip()
    assert "FakeGame" in redetect_tip and "kingbob_" in redetect_tip
    rows_first = window.model.player_row_count()
    # 重新检测后再次扫描仍正常
    window._on_redetect_environment_clicked()
    assert wait_until(qapp, lambda: window.model.player_row_count() > 0, timeout_s=15)
    assert window.model.player_row_count() == rows_first
    window.close()
    service.dispose()


# ---------------------------------------------------------------- P0：启动名单检查改为「扫描完成后触发」
def test_auto_start_does_not_schedule_update_before_scan_finishes(qapp, monkeypatch):
    """P0 回归：auto_start 不再用固定 15s 定时器；扫描结束前不调度名单更新。"""
    from src.app import mock_data
    from src.ui.main_window import MainWindow

    service = make_mock_service()
    window = MainWindow(service)
    window.show()
    qapp.processEvents()
    if window._update_timer is not None:
        window._update_timer.stop()
    calls = []
    monkeypatch.setattr(window, "start_scan", lambda mode="latest": None)
    monkeypatch.setattr(
        window, "_start_update_check",
        lambda silent=False, force=True, _retry=0: calls.append((silent, force)))
    try:
        window.auto_start()
        settle(qapp)
        assert calls == [], "auto_start 不应立即调度名单更新（必须等扫描结束）"
        assert window._startup_update_pending is True
        assert window._startup_update_done is False
        # 扫描完成后才调度一次（silent=True, force=False）
        window._on_scan_finished("all", mock_data.build_replays())
        settle(qapp)
        assert calls == [(True, False)], calls
        assert window._startup_update_done is True
        # 再次扫描结束不会重复调度（不与 6h 定时器重复）
        window._on_scan_finished("all", mock_data.build_replays())
        settle(qapp)
        assert calls == [(True, False)], calls
    finally:
        window.close()
        service.dispose()


def test_auto_start_schedules_update_when_scan_not_started(qapp, monkeypatch):
    """录像目录不可用（扫描未启动）时：auto_start 尾部仍触发一次名单检查。"""
    from src.ui.main_window import MainWindow

    service = make_mock_service()
    monkeypatch.setattr(service, "replay_dir_available", lambda: False)
    window = MainWindow(service)
    window.show()
    qapp.processEvents()
    if window._update_timer is not None:
        window._update_timer.stop()
    calls = []
    scan_calls = []
    monkeypatch.setattr(window, "start_scan", lambda mode="latest": scan_calls.append(mode))
    monkeypatch.setattr(
        window, "_start_update_check",
        lambda silent=False, force=True, _retry=0: calls.append((silent, force)))
    try:
        window.auto_start()
        settle(qapp)
        assert scan_calls == [], "录像目录不可用不应启动扫描"
        assert calls == [(True, False)], calls
        assert window._startup_update_done is True
    finally:
        window.close()
        service.dispose()


def test_scan_finish_schedules_update_after_scan_worker_released(qapp, monkeypatch):
    """集成：mock 全量扫描结束（扫描 worker 已释放）后才触发启动名单检查。"""
    from src.ui.main_window import MainWindow

    service = make_mock_service()
    window = MainWindow(service)
    window.show()
    qapp.processEvents()
    if window._update_timer is not None:
        window._update_timer.stop()
    order = []

    def fake_start_update(silent=False, force=True, _retry=0):
        order.append({"silent": silent, "force": force,
                      "scan_worker": window._scan_worker})

    monkeypatch.setattr(window, "_start_update_check", fake_start_update)
    try:
        window.auto_start()
        assert order == [], "扫描未结束就调度了名单更新"
        assert wait_until(qapp, lambda: window._scan_worker is None, timeout_s=20), \
            "扫描 worker 未释放"
        assert wait_until(qapp, lambda: bool(order), timeout_s=10), "扫描结束后未调度名单更新"
        assert len(order) == 1, order
        assert order[0]["silent"] is True and order[0]["force"] is False
        assert order[0]["scan_worker"] is None, "调度更新时扫描 worker 必须已释放"
    finally:
        window.close()
        service.dispose()


def test_update_failure_stays_visible_and_not_overwritten_by_scan(qapp, monkeypatch):
    """更新失败信息在状态栏保留，不被扫描进度/扫描完成静默覆盖；可手动重试。"""
    from src.ui.main_window import MainWindow

    service = make_mock_service()
    window = MainWindow(service)
    window.show()
    qapp.processEvents()
    if window._update_timer is not None:
        window._update_timer.stop()
    try:
        window._on_update_failed(True, "更新名单失败：官方接口请求超时（网络不可用）")
        assert "官方接口请求超时" in window.statusBar().currentMessage()
        assert window.update_button.isEnabled()
        # 扫描进度 + 扫描完成（空结果）都不能覆盖失败信息
        window._on_scan_progress(50, "正在扫描录像：5000/12851")
        window._on_scan_finished("all", [])
        settle(qapp)
        assert "官方接口请求超时" in window.statusBar().currentMessage(), \
            window.statusBar().currentMessage()
        # 手动重试：点击「更新名单」清除失败保留标记并重新检查（假 service 不发网络）
        monkeypatch.setattr(
            service, "update_ban_list",
            lambda force=False, progress_cb=None, cancel_cb=None: models.UpdateResult(
                ok=True, message="检查完成：已是最新"))
        # 用 silent 启动避免弹出系统消息框（首次失败弹窗由首启流程覆盖）
        window._start_update_check(silent=True, force=True)
        assert window._update_failure_message == ""
        assert wait_until(qapp, lambda: window._update_worker is None, timeout_s=10)
        assert "更新名单" in window.update_button.text() or window.update_button.isEnabled()
    finally:
        window.close()
        service.dispose()


def test_update_running_blocks_duplicate_update_and_scan(qapp, monkeypatch):
    """更新检查进行中：重复点「更新名单」/ 启动扫描被拦截并有中文提示（防重入）。"""
    import threading

    from src.ui.main_window import MainWindow

    service = make_mock_service()
    window = MainWindow(service)
    window.show()
    qapp.processEvents()
    if window._update_timer is not None:
        window._update_timer.stop()
    started = threading.Event()
    release = threading.Event()
    update_calls = []

    def fake_update(force=False, progress_cb=None, cancel_cb=None):
        update_calls.append(force)
        started.set()
        release.wait(5)
        return models.UpdateResult(ok=True, message="检查完成")

    monkeypatch.setattr(service, "update_ban_list", fake_update)
    try:
        window._start_update_check(silent=True, force=False)
        assert started.wait(5), "更新 worker 未启动"
        # 更新进行中：重复点「更新名单」被拦（中文提示、不产生第二次更新）
        window._on_update_clicked()
        assert "正在更新名单" in window.statusBar().currentMessage()
        # 更新进行中：扫描启动被拦（不创建扫描 worker）
        window.start_scan("all")
        assert window._scan_worker is None
        assert "正在更新名单" in window.statusBar().currentMessage()
        assert update_calls == [False], update_calls
    finally:
        release.set()
        assert wait_until(qapp, lambda: window._update_worker is None, timeout_s=10)
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


# ---------------------------------------------------------------- P0：首次使用双弹窗确认流程（task-20）
# 用户拍板：空名单库首启**不自动扫描**，先弹软件内自绘弹窗①（开始扫描录像）；
# 扫描结束后弹窗②（开始更新名单）；成功且 bans>0 才写 meta.first_run_flow_done=true；
# 二次启动不再弹窗、走「自动扫描 + 扫描结束后串行更新检查」；老用户不弹窗。
# 坑：真实模式空库用例必须 monkeypatch `_ask_first_run_*` / `start_scan` / `_start_update_check`，
# 否则会真的弹模态对话框挂起测试，或真的发起扫描/官方请求（禁止）。
FIRST_RUN_FLOW_META_KEY = "first_run_flow_done"


def make_fake_environment():
    """构造假环境探测结果（测试不真的扫注册表/磁盘/录像目录）。"""
    from src.app.service import EnvironmentInfo

    return EnvironmentInfo(
        game_dir="X:/FakeGame",
        replay_dir="X:/FakeGame/replays",
        account_id=7048141181,
        account_name="kingbob_",
        game_dir_source="registry",
        account_source="replay_meta",
        detected=True,
        message="测试：假环境",
    )


def make_first_run_window(qapp, service, monkeypatch):
    """构造真实模式主窗口（假环境探测 + 录像目录可用），停掉 6h 更新定时器。"""
    from src.ui.main_window import MainWindow

    monkeypatch.setattr(service, "detect_environment", lambda **kwargs: make_fake_environment())
    monkeypatch.setattr(service, "replay_dir_available", lambda: True)
    window = MainWindow(service)
    window.resize(TARGET_WINDOW_WIDTH, TARGET_WINDOW_HEIGHT)
    window.show()
    qapp.processEvents()
    if window._update_timer is not None:
        window._update_timer.stop()
    return window


def test_first_run_empty_library_waits_for_scan_confirmation(qapp, monkeypatch, tmp_path):
    """空库首启：auto_start 不自动扫描，先弹弹窗①；拒绝时不产生扫描/更新（下次再提示）。"""
    service = make_real_service(tmp_path / "first-run-empty.db")
    window = make_first_run_window(qapp, service, monkeypatch)
    scan_modes, scan_asks, update_asks, update_calls = [], [], [], []

    def fake_scan(mode="latest"):
        scan_modes.append(mode)

    def fake_ask_scan():
        scan_asks.append(True)
        return False

    def fake_ask_update(error_message=""):
        update_asks.append(error_message)
        return False

    def fake_start_update(silent=False, force=True, _retry=0):
        update_calls.append((silent, force))

    monkeypatch.setattr(window, "start_scan", fake_scan)
    monkeypatch.setattr(window, "_ask_first_run_scan", fake_ask_scan)
    monkeypatch.setattr(window, "_ask_first_run_update", fake_ask_update)
    monkeypatch.setattr(window, "_start_update_check", fake_start_update)
    try:
        flow = window._first_run_flow_state()
        assert flow == {"active": True, "need_scan": True, "need_update": False}, flow
        window.auto_start()
        settle(qapp)
        assert scan_asks == [True], "空库首启必须先弹窗①询问是否扫描"
        assert scan_modes == [], "用户确认前不得自动扫描（P0：避免与名单更新并发写库）"
        assert update_asks == [], "扫描结束前不得弹窗②/启动名单更新"
        assert update_calls == [], "首启流程不得自动启动名单更新"
        assert window._first_run_flow_active is False
        assert window._startup_update_pending is False
        assert "已跳过扫描" in window.progress_label.text(), window.progress_label.text()
    finally:
        window.close()
        service.dispose()


def test_first_run_confirm_scan_then_update_dialog(qapp, monkeypatch, tmp_path):
    """空库首启：确认弹窗①后 start_scan("all")；扫描结束才弹窗②，确认后才启动更新。"""
    service = make_real_service(tmp_path / "first-run-scan.db")
    window = make_first_run_window(qapp, service, monkeypatch)
    scan_modes, scan_asks, update_asks, update_calls = [], [], [], []

    def fake_scan(mode="latest"):
        scan_modes.append(mode)

    def fake_ask_scan():
        scan_asks.append(True)
        return True

    def fake_ask_update(error_message=""):
        update_asks.append(error_message)
        return True

    def fake_start_update(silent=False, force=True, _retry=0):
        update_calls.append((silent, force))

    monkeypatch.setattr(window, "start_scan", fake_scan)
    monkeypatch.setattr(window, "_ask_first_run_scan", fake_ask_scan)
    monkeypatch.setattr(window, "_ask_first_run_update", fake_ask_update)
    monkeypatch.setattr(window, "_start_update_check", fake_start_update)
    try:
        window.auto_start()
        settle(qapp)
        assert scan_asks == [True]
        assert scan_modes == ["all"], "确认弹窗①后应扫描全部录像（mode='all'，非 latest）"
        assert update_asks == [], "扫描结束前不得弹窗②"
        assert update_calls == []
        assert window._first_run_flow_active is True
        assert window._scan_worker is None  # start_scan 已被拦截，不产生真实扫描线程
        # 模拟扫描结束（空结果也走同一结束入口）：出现弹窗②，确认后才启动更新。
        window._on_scan_finished("all", [])
        settle(qapp)
        assert update_asks == [""], "扫描完成后应弹窗②（首次提示无失败原因）"
        assert update_calls == [(True, True)], \
            "确认弹窗②后才启动强制更新，且 silent=True（不得弹系统消息框）"
        assert window._first_run_flow_active is False
        assert window._first_run_update_active is True
        assert "正在下载官方封禁名单" in window.progress_label.text(), window.progress_label.text()
    finally:
        window.close()
        service.dispose()


def test_first_run_update_failure_visible_and_retryable(qapp, monkeypatch, tmp_path):
    """首启更新失败：失败原因进自绘弹窗 + 状态栏保留，可重试；不静默、不弹系统框。"""
    service = make_real_service(tmp_path / "first-run-fail.db")
    window = make_first_run_window(qapp, service, monkeypatch)
    update_asks, update_calls = [], []
    ask_returns = []

    def fake_ask_update(error_message=""):
        update_asks.append(error_message)
        return ask_returns.pop(0) if ask_returns else False

    def fake_start_update(silent=False, force=True, _retry=0):
        update_calls.append((silent, force))

    monkeypatch.setattr(window, "_ask_first_run_update", fake_ask_update)
    monkeypatch.setattr(window, "_start_update_check", fake_start_update)
    failure = "更新名单失败：官方接口请求超时（网络不可用）"
    try:
        # 1) worker 抛错路径：失败原因必须出现在弹窗②的重试文案里 + 状态栏保留
        window._first_run_update_active = True
        window._on_update_failed(True, failure)
        settle(qapp)
        assert update_asks == [failure], "首启更新失败必须在软件内弹窗中显示失败原因"
        assert window._update_failure_message and "官方接口请求超时" in window._update_failure_message
        assert failure in window.statusBar().currentMessage(), "状态栏须保留失败信息"
        assert window._update_failure_visible() is True
        assert window.update_button.isEnabled(), "失败后必须允许用户重试"
        assert window._first_run_update_active is False
        # 2) 用户在失败弹窗里点「重试」：再次启动 silent 强制更新
        ask_returns.append(True)
        window._show_first_run_update_dialog(error_message=failure)
        assert update_asks == [failure, failure], update_asks
        assert update_calls == [(True, True)], update_calls
        assert window._first_run_update_active is True
        # 3) 更新返回 ok=False：同样弹失败/重试弹窗（silent 不弹系统消息框）
        ask_returns.append(False)
        window._first_run_update_active = True
        window._on_update_finished(
            True, models.UpdateResult(ok=False, message="官方接口返回 403 风控"))
        settle(qapp)
        assert update_asks[-1] == "官方接口返回 403 风控", update_asks
        assert "403" in window.statusBar().currentMessage(), window.statusBar().currentMessage()
    finally:
        window.close()
        service.dispose()


def test_first_run_update_success_persists_meta_and_second_start_autoscans(qapp, monkeypatch, tmp_path):
    """首启更新成功 → 持久化 meta.first_run_flow_done；二次启动不弹窗、自动扫描 + 扫描后串行检查。"""
    service = make_real_service(tmp_path / "first-run-done.db")
    window = make_first_run_window(qapp, service, monkeypatch)
    # 更新完成后名单库非空（真实官方下载不在测试里跑，按条数>0 模拟）。
    monkeypatch.setattr(window, "_record_count", lambda: 5)
    scan_modes, update_asks, update_calls = [], [], []

    def fake_scan(mode="latest"):
        scan_modes.append(mode)

    def fake_ask_update(error_message=""):
        update_asks.append(error_message)
        return True

    def fake_start_update(silent=False, force=True, _retry=0):
        update_calls.append((silent, force))

    monkeypatch.setattr(window, "start_scan", fake_scan)
    monkeypatch.setattr(window, "_ask_first_run_scan", lambda: True)
    monkeypatch.setattr(window, "_ask_first_run_update", fake_ask_update)
    monkeypatch.setattr(window, "_start_update_check", fake_start_update)
    second = None
    try:
        # 完整首启链路：弹窗①确认 → 扫描（假）→ 弹窗②确认 → 更新成功
        window.auto_start()
        settle(qapp)
        assert scan_modes == ["all"] and window._first_run_flow_active is True
        window._on_scan_finished("all", [])
        settle(qapp)
        assert update_asks == [""] and update_calls == [(True, True)]
        assert window._first_run_update_active is True
        window._on_update_finished(
            True, models.UpdateResult(ok=True, message="更新完成：新增 1 批"))
        settle(qapp)
        # 1) meta 持久化（内存 + 新连接读回，证明不是仅内存态）
        assert window._meta_get(FIRST_RUN_FLOW_META_KEY) is True
        conn = db.connect(service.cfg.db_path)
        try:
            assert db.get_meta(conn, FIRST_RUN_FLOW_META_KEY) is True
        finally:
            conn.close()
        assert window._first_run_update_active is False
        assert "首次使用完成" in window.statusBar().currentMessage(), \
            window.statusBar().currentMessage()

        # 2) 第二次启动（新窗口、同一 DB）：不再弹窗，自动扫描全部录像；
        #    扫描结束后才串行触发一次启动名单检查（P0 防御：不当场并发检查）。
        second = make_first_run_window(qapp, service, monkeypatch)
        second_scan, second_asks = [], []
        second_update_calls = []

        def second_fake_scan(mode="latest"):
            second_scan.append(mode)

        def second_fake_ask(name):
            def _ask(*args, **kwargs):
                second_asks.append(name)
                return False
            return _ask

        def second_fake_start_update(silent=False, force=True, _retry=0):
            second_update_calls.append((silent, force))

        monkeypatch.setattr(second, "start_scan", second_fake_scan)
        monkeypatch.setattr(second, "_ask_first_run_scan", second_fake_ask("scan"))
        monkeypatch.setattr(second, "_ask_first_run_update", second_fake_ask("update"))
        monkeypatch.setattr(second, "_start_update_check", second_fake_start_update)
        assert second._first_run_flow_state() == {"active": False}
        second.auto_start()
        settle(qapp)
        assert second_asks == [], "首启已完成：二次启动不得再弹窗"
        assert second_scan == ["all"], "二次启动应自动扫描全部录像"
        assert second_update_calls == [], "扫描结束前不得启动名单检查"
        second._on_scan_finished("all", [])
        assert wait_until(qapp, lambda: bool(second_update_calls), timeout_s=10), \
            "扫描结束后未串行触发启动名单检查"
        assert second_update_calls == [(True, False)], second_update_calls
        assert second._startup_update_done is True
    finally:
        if second is not None:
            second.close()
        window.close()
        service.dispose()


def test_first_run_skipped_for_old_user_with_bans(qapp, monkeypatch, tmp_path):
    """老用户（名单库已有 bans）：不弹首启弹窗，直接自动扫描 + 扫描结束后串行更新检查。"""
    db_path = tmp_path / "first-run-old.db"
    conn = db.connect(db_path)
    conn.execute(
        "INSERT INTO bans(batch_day, source_type, nickname, reason, duration, ban_times) "
        "VALUES(?, 'csv', ?, '违规使用脚本', '永久', 1)",
        ("2026-11-06", "老用户测试"),
    )
    conn.commit()
    conn.close()
    service = make_real_service(db_path)
    window = make_first_run_window(qapp, service, monkeypatch)
    scan_modes, asks, update_calls = [], [], []

    def fake_scan(mode="latest"):
        scan_modes.append(mode)

    def fake_ask_scan():
        asks.append("scan")
        return False

    def fake_ask_update(error_message=""):
        asks.append("update")
        return False

    def fake_start_update(silent=False, force=True, _retry=0):
        update_calls.append((silent, force))

    monkeypatch.setattr(window, "start_scan", fake_scan)
    monkeypatch.setattr(window, "_ask_first_run_scan", fake_ask_scan)
    monkeypatch.setattr(window, "_ask_first_run_update", fake_ask_update)
    monkeypatch.setattr(window, "_start_update_check", fake_start_update)
    try:
        assert window._data_status.record_count > 0
        assert window._first_run_flow_state() == {"active": False}
        window.auto_start()
        settle(qapp)
        assert asks == [], "已有名单数据的老用户不得弹任何首启弹窗"
        assert scan_modes == ["all"], "老用户保持自动扫描全部录像"
        assert update_calls == [], "扫描结束前不得启动名单检查"
        window._on_scan_finished("all", [])
        assert wait_until(qapp, lambda: bool(update_calls), timeout_s=10), "扫描结束后未触发名单检查"
        assert update_calls == [(True, False)], update_calls
    finally:
        window.close()
        service.dispose()


def test_first_run_existing_replay_index_skips_scan_dialog(qapp, monkeypatch, tmp_path):
    """上次已扫描出录像索引但名单仍空：跳过弹窗①，直接弹窗②下载官方名单。"""
    from src.app.service import DataStatus

    service = make_real_service(tmp_path / "first-run-index.db")
    window = make_first_run_window(qapp, service, monkeypatch)
    monkeypatch.setattr(
        window, "_safe_data_status",
        lambda: DataStatus(available=True, record_count=0, replay_index_count=12851),
    )
    scan_modes, asks, update_calls = [], [], []

    def fake_scan(mode="latest"):
        scan_modes.append(mode)

    def fake_ask_scan():
        asks.append("scan")
        return False

    def fake_ask_update(error_message=""):
        asks.append("update")
        return True

    def fake_start_update(silent=False, force=True, _retry=0):
        update_calls.append((silent, force))

    monkeypatch.setattr(window, "start_scan", fake_scan)
    monkeypatch.setattr(window, "_ask_first_run_scan", fake_ask_scan)
    monkeypatch.setattr(window, "_ask_first_run_update", fake_ask_update)
    monkeypatch.setattr(window, "_start_update_check", fake_start_update)
    try:
        flow = window._first_run_flow_state()
        assert flow == {"active": True, "need_scan": False, "need_update": True}, flow
        window.auto_start()
        settle(qapp)
        assert asks == ["update"], "已有录像索引时不应再问扫描，直接弹窗②"
        assert scan_modes == [], "已有录像索引时不得重复扫描"
        assert update_calls == [(True, True)], update_calls
    finally:
        window.close()
        service.dispose()


def test_first_run_dialogs_are_in_app_qdialogs_without_beep_or_system_box(qapp):
    """首启双弹窗 = 软件内自绘 QDialog；无 QApplication.beep 调用、无系统消息框。"""
    import ast
    import inspect

    from PySide6.QtWidgets import QDialog

    from src.ui import main_window as main_window_module
    from src.ui import widgets as widgets_module

    scan_dialog = widgets_module.FirstRunScanDialog()
    update_dialog = widgets_module.FirstRunUpdateDialog()
    retry_dialog = None
    try:
        # 1) 是软件内自绘 QDialog（不是 QMessageBox / 系统弹窗），模态且标题为中文
        for dialog in (scan_dialog, update_dialog):
            assert isinstance(dialog, QDialog), type(dialog)
            assert dialog.isModal(), "首启弹窗必须模态"
            assert dialog.windowTitle().startswith("首次使用"), dialog.windowTitle()
        assert scan_dialog.primary_button.text() == "开始扫描录像"
        assert update_dialog.primary_button.text() == "开始更新名单"
        assert scan_dialog.consent_label is not None
        assert "同意" in scan_dialog.consent_label.text(), scan_dialog.consent_label.text()
        # 2) 更新失败原因显示在自绘弹窗内（可重试；不依赖系统消息框/提示音）
        retry_dialog = widgets_module.FirstRunUpdateDialog(error_message="官方接口请求超时")
        assert not retry_dialog.error_label.isHidden(), "失败原因应显示在自绘弹窗内"
        assert "官方接口请求超时" in retry_dialog.error_label.text()
    finally:
        for dialog in (retry_dialog, scan_dialog, update_dialog):
            if dialog is not None:
                dialog.close()

    def ast_uses(source, name):
        import textwrap

        # inspect.getsource(方法) 带缩进：先 dedent 再 parse，否则 IndentationError。
        for node in ast.walk(ast.parse(textwrap.dedent(source))):
            if isinstance(node, ast.Name) and node.id == name:
                return True
            if isinstance(node, ast.Attribute) and node.attr == name:
                return True
        return False

    # 3) 首启弹窗类与询问方法源码里没有 QMessageBox / beep
    for obj in (widgets_module._FirstRunDialogBase,
                widgets_module.FirstRunScanDialog,
                widgets_module.FirstRunUpdateDialog):
        source = inspect.getsource(obj)
        assert not ast_uses(source, "QMessageBox"), obj.__name__
        assert not ast_uses(source, "beep"), obj.__name__
    for method in (main_window_module.MainWindow._ask_first_run_scan,
                   main_window_module.MainWindow._ask_first_run_update):
        source = inspect.getsource(method)
        assert not ast_uses(source, "QMessageBox"), method.__name__
        assert not ast_uses(source, "beep"), method.__name__
    # 4) 全 src/ui 无 QApplication.beep() 调用（无提示音；grep 仅剩注释/docstring 说明）
    ui_dir = PROJECT_ROOT / "src" / "ui"
    for path in sorted(ui_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        beep_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "beep"
        ]
        assert not beep_calls, "%s 含 QApplication.beep 调用（违反无提示音要求）" % path.name


# ---------------------------------------------------------------- 显示文案：「战力」→「效率」（task-25）
def test_user_visible_wording_uses_efficiency_not_combat_power(qapp):
    """用户口径：界面显示「效率」不再显示「战力」；内部字段/排序键保持不变。

    老玩家习惯叫「盒子效率」，没人说「战力」——只改显示文案：列标题、明细面板字段标签、
    综合战绩字段、底部默认排序提示；内部 ``combat`` / ``combat_power`` 字段名与排序键不动。
    """
    from src.ui.table_model import COLUMN_INDEX, COLUMNS, DEFAULT_SORT_CHAIN
    from src.ui.widgets import BATTLE_FIELDS, PlayerDetailPanel

    # 1) 24 列表头逐字：本局效率在、本局战力不在；内部 key 仍是 combat
    titles = [column["title"] for column in COLUMNS]
    assert titles[COLUMN_INDEX["combat"]] == "本局效率"
    assert "本局战力" not in titles
    assert all("战力" not in title for title in titles), titles
    # 2) 内部字段名 / 排序键保持不变（只改显示文案）
    assert COLUMN_INDEX["combat"] == 9
    assert ("combat", False) in DEFAULT_SORT_CHAIN
    # 3) 明细面板「当局战绩」字段标签 + 「综合战绩」字段标签
    battle_labels = dict(BATTLE_FIELDS)
    assert battle_labels["combat"] == "本局效率"
    assert all("战力" not in label for label in battle_labels.values())
    panel = PlayerDetailPanel()
    try:
        labels = [label.text() for label in panel.findChildren(QLabel)]
        assert "本局效率" in labels, labels
        assert "效率" in labels, labels
        assert not [text for text in labels if "战力" in text], labels
    finally:
        panel.close()
    # 4) 底部汇总（用户可见）的默认排序提示改为「本局效率降序」
    window = make_window()
    try:
        window._query_result = SimpleNamespace(
            summary={"total": 30, "hits": 6, "id_hits": 3, "nickname_hits": 3,
                     "yellow": 2, "red": 1},
            is_partial=False,
        )
        window._refresh_summary()
        summary = window.summary_label.text()
        assert "本局效率降序" in summary, summary
        assert "战力" not in summary, summary
    finally:
        window.close()
        window.service.dispose()
    # 5) src/ui 用户可见源码不再出现「战力」（grep 等价断言；models/service 内部不动）
    offenders = {}
    for path in sorted((PROJECT_ROOT / "src" / "ui").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "战力" in text:
            offenders[path.name] = [line.strip() for line in text.splitlines() if "战力" in line]
    assert not offenders, "用户可见文案仍含「战力」：%r" % offenders


def test_first_run_update_failure_dialog_no_duplicate_prefix(qapp):
    """首启更新失败弹窗：消息已带「更新失败：/更新名单失败：」前缀时不重复加前缀。"""
    from src.ui.widgets import FirstRunUpdateDialog

    for message in ("更新失败：官方接口请求超时",
                    "更新名单失败：官方接口请求超时"):
        dialog = FirstRunUpdateDialog(error_message=message)
        try:
            text = dialog.error_label.text()
            assert text == message, text
            assert "更新失败：更新失败" not in text, text
            assert "更新失败：更新名单失败" not in text, text
        finally:
            dialog.close()
    # 未带前缀的服务消息仍补「更新失败：」前缀（用户能看懂失败原因）
    dialog = FirstRunUpdateDialog(error_message="官方接口返回 403 风控")
    try:
        assert dialog.error_label.text() == "更新失败：官方接口返回 403 风控"
    finally:
        dialog.close()


# ---------------------------------------------------------------- 首启更新成功后自动刷新结果（task-29 P2）
def test_first_run_update_success_requeries_current_replay_and_refreshes_hits(
        qapp, monkeypatch, tmp_path):
    """首启：扫描结束（名单空，命中 0）→ 名单更新成功 → 自动重跑查询，「命中 0」变「命中 1」。

    BUG-1（P2）：扫描结束时已启动的那次查询发生在名单库仍为空时；首启更新成功后若
    不重跑，结果表/汇总会一直显示「命中 0 人」，新用户会以为工具坏了。修复要求：
    更新成功后用 ``singleShot(0)`` 重跑当前选中对局（等 worker 释放、不重复查询），
    普通（非首启）更新路径行为不变。
    """
    import threading

    from src.app import mock_data
    from src.app.service import BattleStatsResult, ReplayQueryResult

    replays = mock_data.build_replays()
    service = make_real_service(tmp_path / "first-run-requery.db")
    window = make_first_run_window(qapp, service, monkeypatch)
    state = {"bans": 0}                     # 0 = 更新前名单为空；1 = 更新成功后非空
    query_calls = []
    first_query_done = threading.Event()

    def build_query_result(hits):
        detail = mock_data.build_detail(replays[0])
        matches = mock_data.build_matches(detail)
        for player in matches:
            player.match_type = models.MATCH_TYPE_NONE
            player.ban_count = 0
            player.level = models.LEVEL_NONE
            player.records = []
        if hits:
            hit_player = matches[0]
            hit_player.match_type = models.MATCH_TYPE_ID
            hit_player.ban_count = 1
            hit_player.level = models.LEVEL_YELLOW
            hit_player.records = [models.BanRecord(
                batch_day="2026-11-06", nickname=hit_player.nickname,
                reason="测试", duration="永久")]
        yellow = sum(1 for player in matches if player.level == models.LEVEL_YELLOW)
        red = sum(1 for player in matches if player.level == models.LEVEL_RED)
        return ReplayQueryResult(
            replay=detail,
            matches=matches,
            summary={"total": len(matches), "hits": hits, "id_hits": hits,
                     "nickname_hits": 0, "yellow": yellow, "red": red},
            message="命中 %d 人" % hits,
        )

    def fake_query(path):
        # 在查询 worker 线程里执行：重跑时必须已释放更新 worker（不并发访问 SQLite）。
        query_calls.append({
            "path": str(path),
            "bans": state["bans"],
            "update_worker_running": window._worker_running(window._update_worker),
        })
        result = build_query_result(hits=1 if state["bans"] else 0)
        first_query_done.set()
        return result

    def fake_update(force=False, progress_cb=None, cancel_cb=None):
        # 首启真实的更新动作发生在用户看完弹窗②之后（此时扫描后的查询早已结束）；
        # 这里等首次查询跑完，保证「命中 0 → 命中 1」时序确定、不依赖竞态。
        assert first_query_done.wait(10), "首启更新前应先完成扫描后的首次查询"
        state["bans"] = 1
        return models.UpdateResult(ok=True, message="更新完成：新增 1 批")

    asks = []

    def fake_ask_update(error_message=""):
        asks.append(error_message)
        return True

    monkeypatch.setattr(window, "_record_count", lambda: state["bans"])
    monkeypatch.setattr(service, "query_replay", fake_query)
    monkeypatch.setattr(service, "update_ban_list", fake_update)
    monkeypatch.setattr(
        service, "get_battle_stats",
        lambda detail: BattleStatsResult(available=False, message="测试：跳过战绩请求"),
    )
    monkeypatch.setattr(service, "peek_player_stats_cache", lambda matched: None)
    monkeypatch.setattr(window, "_ask_first_run_update", fake_ask_update)
    try:
        # 1) 模拟扫描结束：自动查询当前对局（此刻名单空 → 命中 0）
        window._first_run_flow_active = False  # 先不让扫描结束入口弹窗②
        window._on_scan_finished("all", replays)
        assert wait_until(
            qapp, lambda: window._query_worker is None and window._query_result is not None,
            timeout_s=15), "扫描结束后的首次查询未完成"
        assert len(query_calls) == 1, query_calls
        assert "命中 0 人" in window.summary_label.text(), window.summary_label.text()

        # 2) 首启流程弹窗②确认 → 名单更新成功 → 自动重跑当前对局查询
        window._first_run_flow_active = True
        window._after_scan_completed()
        assert asks == [""], asks
        assert wait_until(qapp, lambda: window._update_worker is None, timeout_s=15), \
            "首启更新 worker 未结束"
        assert wait_until(
            qapp, lambda: len(query_calls) >= 2 and window._query_worker is None,
            timeout_s=20), "首启更新成功后未自动重跑当前对局查询"
        assert "命中 1 人" in window.summary_label.text(), window.summary_label.text()
        assert window._query_result.summary["hits"] == 1
        assert len(query_calls) == 2, "当前对局应只重跑一次（不重复/递归查询）"
        assert query_calls[1]["bans"] == 1, query_calls
        assert query_calls[1]["update_worker_running"] is False, \
            "重跑查询时更新 worker 必须已释放（不并发访问 SQLite）"
        assert window._first_run_update_active is False
        assert window._meta_get(FIRST_RUN_FLOW_META_KEY) is True

        # 3) 普通（非首启）更新成功不改变行为：不重跑对局查询（防回归）
        window._on_update_finished(
            True, models.UpdateResult(ok=True, message="更新完成：已是最新"))
        settle(qapp)
        assert len(query_calls) == 2, "普通更新不应重跑当前对局查询"
    finally:
        window.close()
        service.dispose()
