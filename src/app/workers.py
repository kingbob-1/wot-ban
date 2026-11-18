# -*- coding: utf-8 -*-
"""后台线程封装：把 ``AppService`` 的同步调用放进 QThread，避免界面卡顿。

用法
----
界面层构造 ``ServiceWorker(lambda progress, should_cancel: service.xxx(...))``，
连接 ``succeeded`` / ``failed`` / ``progressChanged`` / ``cancelled`` 信号即可。
worker 由界面窗口持有引用（``self._workers``），结束后移除，避免被 GC。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Callable

try:
    from PySide6.QtCore import QThread, Signal
except ImportError:  # 便于在没有 PySide6 的环境里 import 本模块做静态检查
    QThread = object  # type: ignore[assignment]
    Signal = None  # type: ignore[assignment]

try:
    from .service import ServiceError
except ImportError:  # 直接以脚本方式运行时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.app.service import ServiceError


class ServiceWorker(QThread):
    """执行一个 ``fn(progress_cb, should_cancel)`` 调用的后台线程。"""

    progressChanged = Signal(int, str) if Signal is not None else None
    succeeded = Signal(object) if Signal is not None else None
    failed = Signal(str) if Signal is not None else None
    cancelled = Signal() if Signal is not None else None

    def __init__(self, fn: Callable[[Callable[[int, str], None], Callable[[], bool]], Any],
                 title: str = "操作", parent: Any = None) -> None:
        super().__init__(parent)
        self._fn = fn
        self._title = title
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """请求取消（协作式：由业务函数自行检查）。"""
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def _emit_progress(self, percent: int, message: str) -> None:
        try:
            self.progressChanged.emit(int(percent), str(message))
        except RuntimeError:  # 窗口已销毁
            pass

    def run(self) -> None:
        try:
            result = self._fn(self._emit_progress, self.is_cancelled)
        except ServiceError as exc:
            self._safe_emit(self.failed, "%s失败：%s" % (self._title, exc))
            return
        except Exception as exc:  # noqa: BLE001 - 后台线程必须兜底，异常转中文提示
            self._safe_emit(self.failed, "%s失败：%s（详细信息见日志）" % (self._title, exc))
            return
        if self._cancel_event.is_set():
            self._safe_emit(self.cancelled)
            return
        self._safe_emit(self.succeeded, result)

    def _safe_emit(self, signal: Any, *args: Any) -> None:
        """窗口销毁后发信号会抛 RuntimeError，这里吞掉保证线程正常退出。"""
        if signal is None:
            return
        try:
            signal.emit(*args)
        except RuntimeError:
            pass


def main(argv: list) -> int:
    """自检：无 GUI 环境下验证 worker 构造与取消标记（不启动事件循环）。"""
    import time

    start = time.time()
    base_path = "."
    print("[1/2] worker 构造自检（base_path=%r）" % base_path)
    worker = ServiceWorker(lambda progress, cancel: None, title="自检")
    print("      title=%s cancelled=%s" % (worker._title, worker.is_cancelled()))
    worker.cancel()
    print("      取消后 cancelled=%s" % worker.is_cancelled())
    print("[2/2] 自检完成，耗时 %.2f 秒" % (time.time() - start))
    return 0


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
