# -*- coding: utf-8 -*-
"""以「防 Qt 退出崩溃」方式运行 QA 的 qa_gui.py（只读引用，不修改 QA 脚本）。

qa_gui.py 自身在进程退出阶段会走 Python 解释器清理 + Qt 全局析构，Windows +
PySide6 6.12 实测偶发 Qt6Core.dll 崩溃弹窗（0xc0000409）。本包装器用 runpy 执行
QA 脚本，捕获 SystemExit 后 flush 并以 ``os._exit`` 结束，退出码 0/非 0 由 QA 结果决定。

用法：

    ..\\runtime\\python.exe src\\ui\\run_qa_guarded.py
"""
from __future__ import annotations

import os
import runpy
import sys
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
QA_SCRIPT = PROJECT_ROOT / "tmp-qa" / "qa_gui.py"


def main(argv: list) -> int:
    """runpy 运行 QA 脚本，返回其退出码（异常转 1）。"""
    base_path = "."
    print("[guard] 以 Qt 退出崩溃防护方式运行：%s（base_path=%r）" % (QA_SCRIPT, base_path))
    if not QA_SCRIPT.is_file():
        print("[guard] 未找到 QA 脚本：%s" % QA_SCRIPT)
        return 2
    sys.argv = [str(QA_SCRIPT)]
    code = 0
    try:
        runpy.run_path(str(QA_SCRIPT), run_name="__main__")
    except SystemExit as exc:  # QA 脚本正常结束路径
        code = int(exc.code or 0)
    except BaseException:  # noqa: BLE001 - QA 脚本异常也要落输出，不能静默
        traceback.print_exc()
        code = 1
    print("[guard] QA 脚本退出码=%d（进程将以 os._exit 结束，避免 Qt 析构崩溃）" % code)
    sys.stdout.flush()
    sys.stderr.flush()
    return code


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
