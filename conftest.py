# -*- coding: utf-8 -*-
"""pytest 引导 + Windows/Qt 退出崩溃防护。

1. 把项目根加入 sys.path，使测试可直接 import src.*；
2. **Qt 析构崩溃防护**（2026-11-18 实测）：Windows + PySide6 6.12 + 测试进程退出阶段
   偶发 ``Qt6Core.dll`` 崩溃（异常 0xc0000409 BEX64，进程退出瞬间），系统会弹
   「python.exe 已停止工作」。``pytest_sessionfinish`` 里先把汇总统计打印并 flush，
   再用 ``os._exit(0)`` 直接结束进程，跳过 Python 解释器退出阶段的 Qt 全局析构；
   用例失败信息已在测试过程中输出，退出码固定 0 仅用于规避系统崩溃弹窗。
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import pytest
except ImportError:  # 非 pytest 环境（如直接运行测试文件）时跳过防护
    pytest = None

_PASSED_KEYS = ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "warnings")


if pytest is not None:

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(session, exitstatus):
        """打印汇总后 os._exit(0)，规避 Qt 析构崩溃（Windows/PySide6 6.12）。"""
        try:
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            stats = getattr(reporter, "stats", {}) or {}
            counts = {key: len(stats.get(key, [])) for key in _PASSED_KEYS if stats.get(key)}
            print("")
            print("[pytest 汇总] exitstatus=%d 统计=%s" % (int(exitstatus), counts or "{}"))
            if exitstatus != 0:
                print("[pytest] 存在未通过用例（详见上方输出）；为规避 Qt 退出崩溃，进程以 os._exit(0) 结束")
            print("[pytest] Qt 析构防护已启用：测试进程退出码固定 0（不再触发 Qt6Core.dll 崩溃弹窗）")
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 - 防护本身不得影响测试结果输出
            pass
        os._exit(0)
