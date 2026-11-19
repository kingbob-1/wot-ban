# -*- coding: utf-8 -*-
"""pytest 引导 + Windows/Qt 退出崩溃防护（**保留真实退出码**）。

1. 把项目根加入 sys.path，使测试可直接 import src.*；
2. **Qt 析构崩溃防护**（2026-11-18 实测）：Windows + PySide6 6.12 + 测试进程退出阶段
   偶发 ``Qt6Core.dll`` 崩溃（异常 0xc0000409 BEX64，进程退出瞬间），系统会弹
   「python.exe 已停止工作」。``pytest_sessionfinish`` 里先把汇总统计打印并 flush，
   再用 ``os._exit(exitstatus)`` 直接结束进程，跳过 Python 解释器退出阶段的 Qt 全局析构。
3. **退出码保持 pytest 真实值**：全绿=0、有失败=1、内部错误=2/3…（由 ``session.exitstatus``
   透传），因此公开仓库 / CI 不会出现「失败也返回 0」的假绿；仅在 ``session.exitstatus``
   不可读时回退 0 并打印警告。
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import pytest
except ImportError:  # 非 pytest 环境（如直接运行测试文件）时跳过防护
    pytest = None

_PASSED_KEYS = ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "warnings")


def _resolve_exitstatus(session, exitstatus):
    """取 pytest 真实退出码；不可用时回退 0 并打印警告。"""
    try:
        value = getattr(session, "exitstatus", exitstatus)
        return int(value)
    except Exception:  # noqa: BLE001 - 退出码读取失败不得阻断收尾
        print("[pytest] 警告：无法读取 pytest 退出码，按 0 结束（Qt 析构防护）")
        return 0


if pytest is not None:

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(session, exitstatus):
        """打印汇总后按 pytest 真实退出码 os._exit，规避 Qt 析构崩溃。"""
        code = _resolve_exitstatus(session, exitstatus)
        try:
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            stats = getattr(reporter, "stats", {}) or {}
            counts = {key: len(stats.get(key, [])) for key in _PASSED_KEYS if stats.get(key)}
            print("")
            print("[pytest 汇总] exitstatus=%d 统计=%s" % (code, counts or "{}"))
            if code != 0:
                print("[pytest] 存在未通过用例：进程退出码=%d（真实退出码，非假绿）" % code)
            print("[pytest] Qt 析构防护已启用：跳过解释器退出阶段 Qt 全局析构，退出码保持 %d" % code)
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001 - 防护本身不得影响测试结果输出
            pass
        os._exit(code)
