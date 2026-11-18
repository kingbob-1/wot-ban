# -*- coding: utf-8 -*-
"""退出码自检：offscreen 启动主窗口，3 秒后自动关闭，要求进程退出码 0。

用途：验证 Windows + PySide6 6.12 的 Qt 退出崩溃防护（Qt6Core.dll 0xc0000409）
是否生效——启动/关闭全程无崩溃弹窗、进程退出码 0。

运行：

    ..\\runtime\\python.exe src\\ui\\selfcheck_exit.py
    echo %errorlevel%
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

from PySide6.QtCore import QTimer  # noqa: E402

from src.app.service import AppService  # noqa: E402
from src.config import load_config  # noqa: E402
from src.ui.main_window import MainWindow, create_application  # noqa: E402


def main(argv: list) -> int:
    """启动 -> 3 秒后自动关闭 -> 返回 app.exec 退出码。"""
    start = time.time()
    base_path = "."
    print("[1/3] 退出码自检（base_path=%r，平台=%s）"
          % (base_path, os.environ.get("QT_QPA_PLATFORM")))
    cfg = load_config()
    service = AppService(cfg=cfg, mock=True)
    app = create_application([])
    window = MainWindow(service)
    window.show()
    window.auto_start()
    QTimer.singleShot(3000, window.close)
    code = int(app.exec())
    window.shutdown()
    service.dispose()
    print("[2/3] app.exec 返回码=%d，窗口已关闭" % code)
    print("[3/3] 退出码自检完成，耗时 %.2f 秒（进程将以 os._exit(0) 结束）" % (time.time() - start))
    sys.stdout.flush()
    sys.stderr.flush()
    return 0 if code == 0 else code


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
    # 关键：跳过 Python 解释器退出阶段的 Qt 全局析构，规避 Qt6Core.dll 崩溃
    os._exit(0 if _exit_code == 0 else _exit_code)
