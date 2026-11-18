# -*- coding: utf-8 -*-
"""PySide6 界面：主窗口、结果表、明细面板、主题与截图脚本。

- ``main_window``：主窗口（顶栏 / 录像区 / 结果表 / 明细 / 汇总 / 免责声明）
- ``table_model``：结果表 QAbstractTableModel（黄红着色 / 排序 / 筛选 / 战绩列）
- ``widgets``：明细面板与名单库状态对话框
- ``theme``：字体、配色、全局 QSS、固定免责声明
- ``screenshot``：offscreen 截图 + mock 全流程自检脚本
"""

from .theme import DISCLAIMER_TEXT, configure_application  # noqa: F401

__all__ = ["DISCLAIMER_TEXT", "configure_application"]
