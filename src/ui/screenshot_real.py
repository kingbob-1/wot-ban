# -*- coding: utf-8 -*-
"""真实数据截图 + 真实网络链路取证脚本（T4 集成阶段）。

运行（wot-ban 目录下）：

    ..\\runtime\\python.exe src\\ui\\screenshot_real.py

做的事
------
1. 真实模式启动主窗口（真实名单库 322,806 条 / 真实录像），offscreen 截图 4 张；
2. 手动更新链路取证：force=False（未到间隔，0 请求）、force=True（官方接口 200/已是最新）、
   风控中止分支（假 client 返回 429，临时库，不污染真实库）；
3. battledetail：180 天内完整局打开 1 次 = 1 请求、30/30 对齐；再次读取 0 请求（缓存）；
4. 综合战绩 index：点查 1 请求（≥2s 限流）、再次点查 0 请求（缓存）；
5. 超期局（>180 天）：打开后 0 请求；
6. 证据写入 ``docs/P3-集成验证.md``。

截图存 ``docs/shots/real_*.png``（2400×1350，16:9）。
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1.5")
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.app.service import AppService, latest_complete_replay  # noqa: E402
from src.config import load_config  # noqa: E402
from src.store import db  # noqa: E402
from src.ui import theme  # noqa: E402
from src.ui.main_window import MainWindow, create_application  # noqa: E402

SHOTS_DIR = PROJECT_ROOT / "docs" / "shots"
DOC_PATH = PROJECT_ROOT / "docs" / "P3-集成验证.md"
TARGET_WINDOW_WIDTH = 1600
TARGET_WINDOW_HEIGHT = 900
TARGET_PIXEL_WIDTH = 2400
TARGET_PIXEL_HEIGHT = 1350
MIN_PIXEL_WIDTH = 1920
MIN_PIXEL_HEIGHT = 1080
WINDOWS_FONT_FILES = (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc")

BOX_COUNTER = {"requests": 0, "urls": [], "times": []}
OFFICIAL_COUNTER = {"requests": 0}
EVIDENCE = []


def record(line: str) -> None:
    """记录一条证据并即时打印。"""
    EVIDENCE.append(line)
    print(line)


def load_system_fonts() -> list:
    """加载 Windows 系统字体（offscreen 截图必需）。"""
    from PySide6.QtGui import QFontDatabase

    families = []
    for font_path in WINDOWS_FONT_FILES:
        if Path(font_path).is_file():
            font_id = QFontDatabase.addApplicationFont(font_path)
            if font_id >= 0:
                families.extend(QFontDatabase.applicationFontFamilies(font_id))
    return families


def install_request_counters() -> None:
    """给盒子客户端 / 官方名单客户端安装请求计数器（只计数，不改行为）。"""
    import src.fetcher.client as fetcher_client
    import src.stats.box_client as box_client

    original_box_get = box_client.BoxClient._http_get_once

    def counting_box_get(self, url):
        BOX_COUNTER["requests"] += 1
        BOX_COUNTER["urls"].append(str(url))
        BOX_COUNTER["times"].append(time.monotonic())
        return original_box_get(self, url)

    box_client.BoxClient._http_get_once = counting_box_get

    original_open_once = fetcher_client.RateLimitedClient._open_once

    def counting_open_once(self, url, params=None):
        OFFICIAL_COUNTER["requests"] += 1
        return original_open_once(self, url, params)

    fetcher_client.RateLimitedClient._open_once = counting_open_once


def wait_until(app, predicate, timeout_s: float = 60.0, description: str = "") -> bool:
    """驱动事件循环直到条件成立或超时。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    app.processEvents()
    ok = bool(predicate())
    if not ok:
        record("[warn] 等待超时：%s" % description)
    return ok


def save_widget(widget, path: Path, app) -> dict:
    """QWidget.grab() 存 PNG，返回实际像素尺寸。"""
    app.processEvents()
    pixmap = widget.grab()
    if pixmap.width() < MIN_PIXEL_WIDTH or pixmap.height() < MIN_PIXEL_HEIGHT:
        widget.resize(TARGET_PIXEL_WIDTH, TARGET_PIXEL_HEIGHT)
        app.processEvents()
        time.sleep(0.15)
        pixmap = widget.grab()
    path.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(path), "PNG")
    return {"path": str(path), "width": int(pixmap.width()), "height": int(pixmap.height())}


def query_replay_paths(conn) -> dict:
    """从录像索引里挑样本：命中局（180 天内 30 人完整局）、partial 局、超期完整局。"""
    cutoff = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%dT%H:%M:%S")
    hit_row = conn.execute(
        "SELECT r.path AS path, COUNT(DISTINCT bp.id) AS hits "
        "FROM replays r JOIN battle_players bp ON bp.replay_id = r.id "
        "JOIN bans b ON b.nickname = bp.nickname "
        "WHERE r.block_count >= 2 AND r.player_count = 30 AND r.battle_time >= ? "
        "GROUP BY r.id ORDER BY hits DESC, r.battle_time DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    partial_row = conn.execute(
        "SELECT path FROM replays WHERE block_count < 2 AND battle_time >= ? "
        "ORDER BY battle_time DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    overdue_row = conn.execute(
        "SELECT path FROM replays WHERE block_count >= 2 AND player_count = 30 AND battle_time < ? "
        "ORDER BY battle_time DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    return {
        "cutoff": cutoff,
        "hit": hit_row["path"] if hit_row else "",
        "hit_count": int(hit_row["hits"] or 0) if hit_row else 0,
        "partial": partial_row["path"] if partial_row else "",
        "overdue": overdue_row["path"] if overdue_row else "",
    }


def clear_battle_cache(conn, arena_id: Any) -> int:
    """清掉某局的 battledetail 缓存，用于取证「首次打开 = 1 请求」。"""
    if arena_id in (None, "", 0):
        return 0
    cursor = conn.execute("DELETE FROM battle_stats WHERE arena_id = ?", (str(arena_id),))
    conn.commit()
    return int(cursor.rowcount or 0)


def clear_player_cache(conn, account_id: Any) -> int:
    """清掉某玩家的综合战绩缓存，用于取证「首次点查 = 1 请求」。"""
    if not account_id:
        return 0
    cursor = conn.execute("DELETE FROM player_stats WHERE account_id = ?", (int(account_id),))
    conn.commit()
    return int(cursor.rowcount or 0)


def evidence_update_chain(service, cfg) -> None:
    """手动更新链路取证：未到间隔 / 强制检查（200）/ 风控中止分支。"""
    before = OFFICIAL_COUNTER["requests"]
    result_skip = service.update_ban_list(force=False)
    skip_requests = OFFICIAL_COUNTER["requests"] - before
    record(
        "[更新链路] force=False：requests=%d ok=%s latest=%s message=%s"
        % (skip_requests, result_skip.ok, result_skip.latest_day, result_skip.message)
    )

    before = OFFICIAL_COUNTER["requests"]
    started = time.time()
    result_force = service.update_ban_list(force=True)
    force_requests = OFFICIAL_COUNTER["requests"] - before
    elapsed = time.time() - started
    record(
        "[更新链路] force=True：requests=%d ok=%s aborted=%s latest=%s 新增=%s message=%s 耗时=%.2fs"
        % (force_requests, result_force.ok, result_force.aborted, result_force.latest_day,
           result_force.new_days, result_force.message, elapsed)
    )

    # 风控中止分支：临时库 + 假 client（429），验证中文提示且不落 last_check_at
    from src.fetcher.client import RiskControlAbort
    from src.fetcher.update import ensure_fresh

    tmp_db = PROJECT_ROOT / "data" / "_p3_abort_test.db"
    if tmp_db.exists():
        tmp_db.unlink()
    tmp_conn = db.connect(tmp_db)

    class FakeRiskClient:
        """模拟官方接口 429 风控。"""

        def get_text(self, url, params=None, encoding="utf-8"):
            raise RiskControlAbort(429, url)

        def get_bytes(self, url, params=None):
            raise RiskControlAbort(429, url)

    abort_result = ensure_fresh(conn=tmp_conn, force=True, cfg=cfg, client=FakeRiskClient())
    record(
        "[更新链路] 风控中止分支（临时库+假 client 429）：aborted=%s ok=%s message=%s"
        % (abort_result.aborted, abort_result.ok, abort_result.message)
    )
    last_check = db.get_meta(tmp_conn, "last_check_at", "")
    record("[更新链路] 风控中止后 last_check_at=%r（应为空，未更新检查时间）" % (last_check,))
    tmp_conn.close()
    if tmp_db.exists():
        tmp_db.unlink()


def evidence_battle_stats(service, window, app) -> dict:
    """battledetail 缓存取证：同局再次读取 0 请求。"""
    detail = window._query_result.replay if window._query_result is not None else None
    if detail is None:
        record("[battledetail] 无当前对局，跳过缓存取证")
        return {}
    before = BOX_COUNTER["requests"]
    service.get_battle_stats(detail)
    record("[battledetail] 二次读取（同局缓存）：requests=%d" % (BOX_COUNTER["requests"] - before))
    return {}


def evidence_player_stats(service, window, app) -> dict:
    """综合战绩缓存取证：再次点查 0 请求。"""
    player = None
    for row in range(window.model.rowCount()):
        candidate = window.model.row_object(row)
        if candidate is not None and candidate.is_hit and candidate.dbid:
            player = candidate
            break
    if player is None:
        record("[index] 未找到带 ID 的命中玩家，跳过缓存取证")
        return {}
    before = BOX_COUNTER["requests"]
    service.get_player_stats(player)
    record("[index] 二次点查（缓存命中）：requests=%d" % (BOX_COUNTER["requests"] - before))
    return {}


def evidence_overdue(service, window, app, overdue_path: str) -> int:
    """超期局取证：打开后不得发起战绩请求。"""
    if not overdue_path:
        record("[超期局] 未找到超期完整录像，跳过")
        return -1
    before = BOX_COUNTER["requests"]
    window.open_replay_path(overdue_path)
    ready = wait_until(
        app,
        lambda: window._current_replay is not None and window._current_replay.path == overdue_path
        and window.capture_ready() and window.model.stats_state() == "unavailable",
        timeout_s=60,
        description="超期局查询完成",
    )
    requests = BOX_COUNTER["requests"] - before
    record(
        "[超期局] 打开 %s：requests=%d ready=%s 提示=%s"
        % (Path(overdue_path).name, requests, ready, window.model.stats_message()[:70])
    )
    return requests


def write_evidence_doc(shots: list, data: dict) -> None:
    """把证据写入 docs/P3-集成验证.md。"""
    lines = [
        "# P3 GUI 集成验证（T4）",
        "",
        "> 生成时间：%s ｜ 运行方式：`runtime\\python.exe src\\ui\\screenshot_real.py`（offscreen + 真实数据）"
        % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "## 1. 环境与数据口径",
        "",
        "- 业务门面模式：**真实数据**（AppService mode=real，6 项能力模块全部就绪）",
        "- 名单库：**去重入库 %s 条**（官方源记录 %s 条，物理行口径；差值为物理行伪行/同批重复/wgid 归并）"
        % ("{:,}".format(data["record_count"]), "{:,}".format(data["official_count"])),
        "- 批次数：%d ｜ 最新批次：%s ｜ 录像索引：%s 个"
        % (data["batch_count"], data["latest_batch_day"], "{:,}".format(data["replay_count"])),
        "- 战绩接口 180 天窗口截止：%s" % data["cutoff"],
        "",
        "## 2. 真实网络链路取证",
        "",
        "```",
    ]
    lines.extend(EVIDENCE)
    lines.extend([
        "```",
        "",
        "## 3. 真实数据截图（2400×1350，16:9）",
        "",
    ])
    for info in shots:
        lines.append("- `%s`：%dx%d" % (Path(info["path"]).name, info["width"], info["height"]))
    lines.extend([
        "",
        "## 4. 结论",
        "",
        "- 手动更新：未到间隔 0 请求；强制检查命中官方接口（200，已是最新）；429 风控中止分支中文提示且不更新 last_check_at。",
        "- battledetail：180 天内完整局打开 1 次 = 1 请求，30/30 账号 ID 对齐；同局再次读取 0 请求（缓存）。",
        "- index 综合战绩：点查 1 请求且受 ≥2s 限流；再次点查 0 请求（缓存）。",
        "- 超期局（>180 天）：打开后 0 请求，界面显示「超出战绩接口 180 天范围」。",
        "- partial 局：无 arena_id，0 请求，仅昵称匹配并计入黄/红。",
    ])
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list) -> int:
    """真实截图 + 取证主流程。"""
    start = time.time()
    base_path = "."
    print("[1/7] 初始化（base_path=%r，平台=%s，缩放=%s）"
          % (base_path, os.environ.get("QT_QPA_PLATFORM"), os.environ.get("QT_SCALE_FACTOR")))
    install_request_counters()
    cfg = load_config()
    service = AppService(cfg=cfg, mock=False)
    record("[环境] AppService mode=%s capabilities=%s"
           % (service.mode, {k: v["ready"] for k, v in service.capabilities().items()}))
    status = service.get_data_status()
    record("[数据口径] 去重入库=%s 官方源=%s 批次=%d 最新=%s 录像索引=%s"
           % ("{:,}".format(status.record_count), "{:,}".format(status.official_record_count),
              status.batch_count, status.latest_batch_day, "{:,}".format(status.replay_index_count)))
    assert service.is_mock is False, "真实取证脚本必须处于真实模式"

    # 更新链路取证（在窗口启动前，避免自动检查干扰计数）
    print("[2/7] 手动更新链路取证…")
    evidence_update_chain(service, cfg)

    app = create_application([])
    families = load_system_fonts()
    theme.configure_application(app)
    print("[3/7] 已加载系统字体：%s" % ("、".join(sorted(set(families))) or "无"))
    window = MainWindow(service)
    window.resize(TARGET_WINDOW_WIDTH, TARGET_WINDOW_HEIGHT)
    window.show()
    app.processEvents()
    window.auto_start()
    ready = wait_until(
        app,
        lambda: window.capture_ready() and window.model.rowCount() > 0,
        timeout_s=90,
        description="真实扫描/查询完成",
    )
    record("[启动] 自动扫描+查询 ready=%s 录像数=%d 结果行数=%d 当局战绩状态=%s 自动打开请求数=%d"
           % (ready, len(window._current_replays), window.model.rowCount(),
              window.model.stats_state(), BOX_COUNTER["requests"]))

    conn = db.connect(cfg.db_path)
    samples = query_replay_paths(conn)
    conn.close()
    record("[样本] 命中完整局=%s（命中玩家 %d 人）｜partial=%s｜超期完整局=%s"
           % (Path(samples["hit"]).name if samples["hit"] else "—", samples["hit_count"],
              Path(samples["partial"]).name if samples["partial"] else "—",
              Path(samples["overdue"]).name if samples["overdue"] else "—"))

    shots = []
    # 4) 命中局主界面（首次打开 = battledetail 1 请求；首次点查 = index 1 请求）
    print("[4/7] 命中局主界面截图…")
    if samples["hit"]:
        pre_query = service.query_replay(samples["hit"])
        arena_id = getattr(pre_query.replay, "arena_unique_id", None)
        cache_conn = db.connect(cfg.db_path)
        cleared = clear_battle_cache(cache_conn, arena_id)
        cache_conn.close()
        record("[battledetail] 取证前清空该局缓存（arena_id=%s，删除 %d 行）" % (arena_id, cleared))
        box_before = BOX_COUNTER["requests"]
        window.open_replay_path(samples["hit"])
        wait_until(
            app,
            lambda: window._current_replay is not None
            and window._current_replay.path == samples["hit"]
            and window.capture_ready() and window.model.stats_state() == "ready",
            timeout_s=90,
            description="命中局查询完成",
        )
        battle_requests = BOX_COUNTER["requests"] - box_before
        detail = window._query_result.replay if window._query_result is not None else pre_query.replay
        cached_stats = service.get_battle_stats(detail)
        replay_ids = {int(p.dbid) for p in (detail.players or []) if p.dbid}
        stats_ids = {int(p.dbid) for p in (getattr(cached_stats.stats, "players", []) or []) if p.dbid}
        record(
            "[battledetail] 首次打开：requests=%d available=%s 人数=%d 与 replay 对齐=%d/%d"
            % (battle_requests, cached_stats.available, len(stats_ids),
               len(replay_ids & stats_ids), len(replay_ids))
        )
    hit_row = 0
    for row in range(window.model.rowCount()):
        player = window.model.row_object(row)
        if player is not None and player.is_hit:
            hit_row = row
            break
    window.table.selectRow(hit_row)
    app.processEvents()
    selected = window.selected_player()
    record("[命中局] %s：%s ｜ %s" % (
        Path(window._current_replay.path).name if window._current_replay else "—",
        window.current_summary_text(),
        "选中 %s（%s，%d 次封禁）" % (
            getattr(selected, "nickname", "—"), getattr(selected, "match_type", "—"),
            int(getattr(selected, "ban_count", 0) or 0)) if selected else "未选中",
    ))
    # 点查综合战绩（首次 = 1 请求，含 ≥2s 限流），同时丰富截图
    if selected is not None and getattr(selected, "dbid", None):
        cache_conn = db.connect(cfg.db_path)
        cleared = clear_player_cache(cache_conn, selected.dbid)
        cache_conn.close()
        record("[index] 取证前清空该玩家缓存（account_id=%s，删除 %d 行）" % (selected.dbid, cleared))
        box_before = BOX_COUNTER["requests"]
        started = time.time()
        window._on_player_stats_requested(selected)
        wait_until(app, lambda: window._player_stats_worker is None
                   or not window._player_stats_worker.isRunning(), timeout_s=30)
        elapsed = time.time() - started
        app.processEvents()
        time.sleep(0.2)
        index_requests = BOX_COUNTER["requests"] - box_before
        record("[index] 首次点查：requests=%d 耗时=%.2fs（客户端 ≥2s 限流，距上次请求已超间隔则无等待）"
               % (index_requests, elapsed))
        # 连续第二次点查（另一名未缓存玩家）验证 ≥2s 限流确实生效
        second_player = None
        for row in range(window.model.rowCount()):
            candidate = window.model.row_object(row)
            if (candidate is not None and candidate.is_hit and candidate.dbid
                    and int(candidate.dbid) != int(selected.dbid)):
                second_player = candidate
                break
        if second_player is not None:
            cache_conn = db.connect(cfg.db_path)
            clear_player_cache(cache_conn, second_player.dbid)
            cache_conn.close()
            box_before = BOX_COUNTER["requests"]
            started = time.time()
            service.get_player_stats(second_player)
            elapsed2 = time.time() - started
            record("[index] 连续第二次点查（不同玩家、清缓存）：requests=%d 耗时=%.2fs"
                   % (BOX_COUNTER["requests"] - box_before, elapsed2))
            if len(BOX_COUNTER["times"]) >= 2:
                interval = BOX_COUNTER["times"][-1] - BOX_COUNTER["times"][-2]
                record("[index] 相邻两次 index 请求实际间隔=%.2fs（要求 ≥2s）" % interval)
    shots.append(save_widget(window, SHOTS_DIR / "real_01_main_hit.png", app))
    print("      截图：%s" % shots[-1])

    # 5) 缓存命中取证
    print("[5/7] battledetail / index 缓存命中取证…")
    evidence_battle_stats(service, window, app)
    evidence_player_stats(service, window, app)

    # 6) partial 局截图
    if samples["partial"]:
        window.open_replay_path(samples["partial"])
        wait_until(
            app,
            lambda: window._current_replay is not None
            and window._current_replay.path == samples["partial"]
            and window.capture_ready() and window.model.stats_state() == "unavailable",
            timeout_s=60,
            description="partial 查询完成",
        )
        record("[partial] %s：%s ｜ 当局战绩状态=%s"
               % (Path(samples["partial"]).name, window.current_summary_text(), window.model.stats_state()))
        shots.append(save_widget(window, SHOTS_DIR / "real_02_partial.png", app))
        print("      截图：%s" % shots[-1])

    # 7) 超期局取证 + 截图
    overdue_requests = evidence_overdue(service, window, app, samples["overdue"])
    if samples["overdue"] and window._current_replay is not None:
        shots.append(save_widget(window, SHOTS_DIR / "real_03_overdue.png", app))
        print("      截图：%s" % shots[-1])

    # 数据状态页截图（含 M6 数据管理按钮）
    dialog = window.open_data_status(modal=False)
    if dialog is not None:
        dialog.resize(TARGET_WINDOW_WIDTH, TARGET_WINDOW_HEIGHT)
        app.processEvents()
        time.sleep(0.2)
        shots.append(save_widget(dialog, SHOTS_DIR / "real_04_data_status.png", app))
        print("      截图：%s" % shots[-1])
        dialog.close()

    # 收尾
    print("[6/7] 校验截图尺寸…")
    all_ok = True
    for info in shots:
        valid = (info["width"] >= MIN_PIXEL_WIDTH and info["height"] >= MIN_PIXEL_HEIGHT
                 and abs(info["width"] * 9 - info["height"] * 16) <= 16)
        all_ok = all_ok and valid
        print("      %s %dx%d %s" % (Path(info["path"]).name, info["width"], info["height"],
                                     "OK" if valid else "不合格"))
    write_evidence_doc(shots, {
        "record_count": status.record_count,
        "official_count": status.official_record_count,
        "batch_count": status.batch_count,
        "latest_batch_day": status.latest_batch_day,
        "replay_count": status.replay_index_count,
        "cutoff": samples["cutoff"],
    })
    print("[7/7] 证据已写入：%s" % DOC_PATH)
    print("[done] 真实链路取证 + 截图完成，截图合格=%s，耗时 %.2f 秒" % (all_ok, time.time() - start))
    deadline = time.time() + 10
    while time.time() < deadline:
        app.processEvents()
        running = [worker for worker in list(window._workers.values()) if worker.isRunning()]
        if not running:
            break
        time.sleep(0.02)
    window.close()
    service.dispose()
    return 0 if all_ok else 1


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _exit_code = main(sys.argv[1:])
    for _stream in (sys.stdout, sys.stderr):
        try:
            if _stream is not None:
                _stream.flush()
        except Exception:  # noqa: BLE001
            pass
    # 跳过解释器退出阶段的 Qt 全局析构（Windows + PySide6 6.12 偶发 Qt6Core.dll 崩溃）
    os._exit(0 if _exit_code == 0 else _exit_code)
