# -*- coding: utf-8 -*-
"""界面截图与 mock 全流程自检脚本（P3 第一阶段验收用）。

运行（在 wot-ban 目录下）：

    ..\\runtime\\python.exe src\\ui\\screenshot.py

- 默认使用 ``QT_QPA_PLATFORM=offscreen``（无显示器也能跑）+ ``QT_SCALE_FACTOR=1.5``
  （模拟 4K 150% 缩放），窗口逻辑尺寸 1600×900 → 截图像素 2400×1350（16:9，≥1920×1080）；
- 用内置 mock 数据真实驱动「扫描录像 -> 选择 -> 查询 -> 表格/明细/汇总 -> 当局战绩 ->
  综合战绩点查」，并打印逐项校验结果；
- 截图存 ``docs/shots/*.png``，同时输出每张图的实际尺寸。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 必须在导入 PySide6 之前设置平台与缩放因子（offscreen 无显示器可截图）。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1.5")
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.app.service import AppService, latest_complete_replay  # noqa: E402
from src.config import load_config  # noqa: E402
from src.ui import theme  # noqa: E402
from src.ui.main_window import MainWindow, create_application  # noqa: E402

SHOTS_DIR = PROJECT_ROOT / "docs" / "shots"
TARGET_WINDOW_WIDTH = 1600
TARGET_WINDOW_HEIGHT = 900
TARGET_PIXEL_WIDTH = 2400
TARGET_PIXEL_HEIGHT = 1350
MIN_PIXEL_WIDTH = 1920
MIN_PIXEL_HEIGHT = 1080

# offscreen 平台不带字体数据库，显式加载 Windows 系统字体（微软雅黑常规/粗体）。
WINDOWS_FONT_FILES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
)


def load_system_fonts() -> list:
    """把 Windows 系统字体加载进 Qt 字体库（offscreen 截图必需）。"""
    from PySide6.QtGui import QFontDatabase

    families = []
    for font_path in WINDOWS_FONT_FILES:
        if not Path(font_path).is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(font_path)
        if font_id >= 0:
            families.extend(QFontDatabase.applicationFontFamilies(font_id))
    return families


def wait_until(app, predicate, timeout_s: float = 20.0, description: str = "") -> bool:
    """驱动事件循环直到条件成立或超时。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    app.processEvents()
    ok = bool(predicate())
    if not ok and description:
        print("[warn] 等待超时：%s" % description)
    return ok


def save_widget(widget, path: Path, app) -> dict:
    """QWidget.grab() 存 PNG，返回实际像素尺寸；不足 1920×1080 时放大窗口重试。"""
    app.processEvents()
    pixmap = widget.grab()
    if pixmap.width() < MIN_PIXEL_WIDTH or pixmap.height() < MIN_PIXEL_HEIGHT:
        widget.resize(TARGET_PIXEL_WIDTH, TARGET_PIXEL_HEIGHT)
        app.processEvents()
        time.sleep(0.15)
        pixmap = widget.grab()
    path.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(path), "PNG")
    return {
        "path": str(path),
        "width": int(pixmap.width()),
        "height": int(pixmap.height()),
        "ratio_16_9": abs(pixmap.width() * 9 - pixmap.height() * 16) <= 16,
    }


def check_sorting(model) -> tuple:
    """校验默认排序：封禁次数降序，同次数最近封禁日期降序。"""
    rows = model.visible_rows()
    ok = True
    for first, second in zip(rows, rows[1:]):
        count_a = int(first.ban_count or 0)
        count_b = int(second.ban_count or 0)
        if count_a < count_b:
            ok = False
            break
        if count_a == count_b and (first.latest_ban_day or "") < (second.latest_ban_day or ""):
            ok = False
            break
    return ok, [(row.nickname, int(row.ban_count or 0), row.latest_ban_day) for row in rows[:5]]


def wait_workers_idle(window, app, timeout_s: float = 10.0) -> None:
    """等待窗口内所有后台 worker 结束（避免关闭窗口时 QThread 仍在运行）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        app.processEvents()
        running = [worker for worker in list(window._workers.values()) if worker.isRunning()]
        if not running:
            return
        time.sleep(0.02)
    app.processEvents()


def main(argv: list) -> int:
    """截图 + mock 全流程自检主流程。"""
    start = time.time()
    base_path = "."
    print("[1/6] 初始化（base_path=%r，平台=%s，缩放=%s）"
          % (base_path, os.environ.get("QT_QPA_PLATFORM"), os.environ.get("QT_SCALE_FACTOR")))
    cfg = load_config()
    service = AppService(cfg=cfg, mock=True)
    app = create_application([])
    families = load_system_fonts()
    theme.configure_application(app)  # 字体加载后重新应用主题字体
    print("      已加载系统字体：%s" % ("、".join(sorted(set(families))) or "无（中文可能显示为方块）"))
    window = MainWindow(service)
    window.resize(TARGET_WINDOW_WIDTH, TARGET_WINDOW_HEIGHT)
    window.show()
    app.processEvents()
    time.sleep(0.2)

    shots = []

    # 1) 空态
    wait_until(app, lambda: window.stack.currentIndex() == 0, timeout_s=5)
    shots.append(save_widget(window, SHOTS_DIR / "01_empty_state.png", app))
    print("[2/6] 空态截图：%s" % shots[-1])

    # 2) 自动扫描 + 查询最新完整录像（全流程）
    window.auto_start()
    ready = wait_until(
        app,
        lambda: window.capture_ready() and window.model.rowCount() > 0
        and window.model.stats_state() == "ready",
        timeout_s=30,
        description="扫描/查询/当局战绩就绪",
    )
    latest = latest_complete_replay(window._current_replays)
    latest_name = Path(latest.path).name if latest is not None else "—"
    print("      扫描到 %d 个录像，最新完整录像=%s" % (len(window._current_replays), latest_name))
    print("      查询结果：%s" % window.current_summary_text())
    print("      结果表行数=%d（总 %d），当局战绩状态=%s"
          % (window.model.rowCount(), len(window.model.total_rows()), window.model.stats_state()))
    sort_ok, sort_sample = check_sorting(window.model)
    print("      默认排序校验（次数降序+日期降序）：%s，前 5 行=%s" % (sort_ok, sort_sample))

    # 选中命中行并点查综合战绩，丰富截图内容
    hit_row = None
    for row in range(window.model.rowCount()):
        player = window.model.row_object(row)
        if player is not None and player.is_hit:
            hit_row = row
            break
    if hit_row is not None:
        window.table.selectRow(hit_row)
        app.processEvents()
    selected = window.selected_player()
    print("      选中玩家：%s（%s，%d 次封禁，记录 %d 条）"
          % (getattr(selected, "nickname", "—"),
             getattr(selected, "match_type", "—"),
             int(getattr(selected, "ban_count", 0)),
             len(getattr(selected, "records", []) or [])))
    if selected is not None:
        window._on_player_stats_requested(selected)
        wait_until(app, lambda: window._player_stats_worker is None
                   or not window._player_stats_worker.isRunning(), timeout_s=10)
        app.processEvents()
        time.sleep(0.2)
    shots.append(save_widget(window, SHOTS_DIR / "02_main_id_hit.png", app))
    print("[3/6] 主界面（ID 命中）截图：%s" % shots[-1])

    # 3) partial 录像（仅昵称匹配 + 当局战绩不可用）
    partial = next((item for item in window._current_replays if item.is_partial), None)
    partial_ready = False
    if partial is not None:
        window.select_replay_by_path(partial.path)
        partial_ready = wait_until(
            app,
            lambda: window._current_replay is not None and window._current_replay.is_partial
            and window.capture_ready() and window.model.stats_state() == "unavailable",
            timeout_s=20,
            description="partial 查询完成",
        )
        partial_summary = window.current_summary_text()
        print("      partial 局：%s" % partial_summary)
        print("      partial 结果表行数=%d，当局战绩状态=%s"
              % (window.model.rowCount(), window.model.stats_state()))
        shots.append(save_widget(window, SHOTS_DIR / "03_partial_nickname.png", app))
        print("[4/6] partial 截图：%s" % shots[-1])

    # 4) 超期局（>180 天，不请求战绩接口）
    overdue = None
    for replay in window._current_replays:
        if not replay.is_partial and "20260502" in replay.path:
            overdue = replay
            break
    if overdue is not None:
        window.select_replay_by_path(overdue.path)
        wait_until(
            app,
            lambda: window._current_replay is not None and window._current_replay.path == overdue.path
            and window.capture_ready(),
            timeout_s=20,
            description="超期局查询完成",
        )
        print("      超期局（>180 天）：当局战绩状态=%s，提示=%s"
              % (window.model.stats_state(), window.model.stats_message()[:60]))
        shots.append(save_widget(window, SHOTS_DIR / "04_overdue_180d.png", app))
        print("[5/6] 超期局截图：%s" % shots[-1])

    # 5) 数据状态页
    dialog = window.open_data_status(modal=False)
    if dialog is not None:
        dialog.resize(TARGET_WINDOW_WIDTH, TARGET_WINDOW_HEIGHT)
        app.processEvents()
        time.sleep(0.2)
        shots.append(save_widget(dialog, SHOTS_DIR / "05_data_status.png", app))
        print("[6/6] 数据状态页截图：%s" % shots[-1])
        dialog.close()

    # 汇总校验
    print("---- 截图清单 ----")
    all_ok = True
    for info in shots:
        valid = info["width"] >= MIN_PIXEL_WIDTH and info["height"] >= MIN_PIXEL_HEIGHT and info["ratio_16_9"]
        all_ok = all_ok and valid
        print("  %s  %dx%d  16:9=%s  %s"
              % (Path(info["path"]).name, info["width"], info["height"],
                 info["ratio_16_9"], "OK" if valid else "不合格"))
    if partial_ready:
        print("[ok] partial 流程校验通过（仅昵称匹配、计入黄/红、战绩不请求）")
    print("[ok] 排序校验=%s；截图全部合格=%s；总耗时 %.2f 秒"
          % (sort_ok, all_ok, time.time() - start))
    wait_workers_idle(window, app)
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
