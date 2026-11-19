# -*- coding: utf-8 -*-
"""启动自动扫描验收自检（v0.2 / T19；1.0.1 起默认扫描「全部录像」）：offscreen 真实环境，不点击任何按钮。

验收目标
--------
- 启动后结果表出现行（自启动默认扫描「全部录像」，本机 12,851 个录像增量扫描约 9 秒、
  从 auto_start 到结果表出行约 11 秒，见 DEADLINE_SECONDS 余量说明）；
- 「重新检测」后再次扫描正常（该触发仍按原逻辑扫描「最新完整录像」）；
- 连续第二次启动窗口同样正常；
- 进程退出码 0（Qt 退出防护）。

运行（wot-ban 目录下）：

    ..\\runtime\\python.exe src\\ui\\selfcheck_autostart.py
    echo %errorlevel%

注意：等待循环必须用 ``app.processEvents() + time.sleep``（或真实 app.exec 事件循环）。
``QTest.qWait`` 在高频轮询下会干扰 worker 线程调度，不能作为自动扫描验收的等待方式。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.app.service import AppService  # noqa: E402
from src.config import load_config  # noqa: E402
from src.ui.main_window import MainWindow, create_application  # noqa: E402

# 1.0.1：启动默认扫描「全部录像」（12,851 个，增量扫描本机实测约 9 秒；
# 从 auto_start 到结果表出行约 11 秒）。上限留足 60 秒，避免磁盘缓存冷/机器繁忙时误报失败。
DEADLINE_SECONDS = 60.0


def wait_for_rows(app, window, timeout_s: float = DEADLINE_SECONDS) -> float:
    """用正确的事件循环等待结果表出现行，返回耗时（秒）。"""
    started = time.time()
    while time.time() - started < timeout_s:
        app.processEvents()
        if window.model.rowCount() > 0:
            return time.time() - started
        time.sleep(0.02)
    return -1.0


def wait_idle(app, window, timeout_s: float = DEADLINE_SECONDS) -> float:
    """等待扫描/查询 worker 全部结束，返回耗时（秒）。"""
    started = time.time()
    while time.time() - started < timeout_s:
        app.processEvents()
        scan_running = window._worker_running(window._scan_worker)
        query_running = window._worker_running(window._query_worker)
        if not scan_running and not query_running and window.model.rowCount() > 0:
            return time.time() - started
        time.sleep(0.02)
    return -1.0


def main(argv: list) -> int:
    """真实环境启动自动扫描验收主流程。"""
    start = time.time()
    base_path = "."
    print("[1/5] 启动自动扫描验收（base_path=%r，平台=%s）"
          % (base_path, os.environ.get("QT_QPA_PLATFORM")))
    cfg = load_config()
    app = create_application([])
    service = AppService(cfg=cfg, mock=False)
    print("[2/5] 模式=%s 配置版本=%s" % (service.mode, cfg.app_version))
    window = MainWindow(service)
    window.resize(2400, 1350)
    window.show()
    window.auto_start()
    first_elapsed = wait_for_rows(app, window)
    first_rows = window.model.rowCount()
    first_replays = len(window._current_replays)
    print("[3/5] 首次启动：耗时=%.2fs（要求 ≤%.0fs）结果行=%d 录像=%d 环境=%s"
          % (first_elapsed, DEADLINE_SECONDS, first_rows, first_replays,
             window.redetect_button.toolTip().splitlines()[-2:]))
    first_ok = 0 <= first_elapsed <= DEADLINE_SECONDS and first_rows > 0

    # 重新检测后再次扫描
    window._on_redetect_environment_clicked()
    redetect_elapsed = wait_idle(app, window)
    redetect_rows = window.model.rowCount()
    print("[4/5] 重新检测后：耗时=%.2fs 结果行=%d" % (redetect_elapsed, redetect_rows))
    redetect_ok = 0 <= redetect_elapsed <= DEADLINE_SECONDS and redetect_rows > 0

    # 连续第二次启动
    window.shutdown()
    window.close()
    window2 = MainWindow(service)
    window2.resize(2400, 1350)
    window2.show()
    window2.auto_start()
    second_elapsed = wait_for_rows(app, window2)
    second_rows = window2.model.rowCount()
    print("[5/5] 第二次启动：耗时=%.2fs 结果行=%d" % (second_elapsed, second_rows))
    second_ok = 0 <= second_elapsed <= DEADLINE_SECONDS and second_rows > 0

    all_ok = bool(first_ok and redetect_ok and second_ok)
    print("[done] 自动扫描验收：首次=%s 重新检测=%s 第二次启动=%s 全部通过=%s 总耗时=%.2fs"
          % (first_ok, redetect_ok, second_ok, all_ok, time.time() - start))
    window2.shutdown()
    window2.close()
    service.dispose()
    sys.stdout.flush()
    sys.stderr.flush()
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
    os._exit(0 if _exit_code == 0 else _exit_code)
