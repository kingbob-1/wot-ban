# -*- coding: utf-8 -*-
"""界面主题：字体、配色、全局 QSS 与常用小控件（徽标）。

PySide6 界面全部用代码构建（无 .ui 文件）；字体固定微软雅黑，高 DPI 交给
Qt 默认缩放（4K 下由系统缩放因子放大，界面尺寸按 16:9 计算）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont
    from PySide6.QtWidgets import QApplication, QLabel
except ImportError:  # 便于无 PySide6 环境下静态检查
    QColor = QFont = QLabel = QApplication = None  # type: ignore[assignment]
    Qt = None  # type: ignore[assignment]

# 字体：微软雅黑（中文渲染稳定），字号 10pt（4K 下由 Qt 缩放）。
FONT_FAMILY = "Microsoft YaHei"
BASE_FONT_POINT_SIZE = 10

# 语义色（与 QSS 保持一致）。
COLOR_RED_BG = "#ffd9d9"
COLOR_YELLOW_BG = "#fff3c4"
COLOR_RED_TEXT = "#b91c1c"
COLOR_YELLOW_TEXT = "#92400e"
COLOR_TEXT = "#1f2937"
COLOR_MUTED = "#6b7280"

# 固定免责声明（A4/M5：不可省，历史封禁 ≠ 当前开挂）。
DISCLAIMER_TEXT = (
    "免责声明：本工具仅呈现官方公开的历史封禁记录，历史封禁不代表当前违规，"
    "也不代表该玩家当前仍在开挂；数据为非官方整理、仅供参考，不得用于挂人、骚扰或任何处罚依据。"
    "partial 录像只能按昵称匹配，可能存在同名不同人；完整录像才能核对全部账号 ID。"
)

APP_QSS = """
QWidget {
    color: #1f2937;
    font-family: "Microsoft YaHei";
    font-size: 10pt;
}
QMainWindow, QDialog {
    background: #eef1f6;
}
QFrame#Card {
    background: #ffffff;
    border: 1px solid #d9dee8;
    border-radius: 8px;
}
QFrame#Header {
    background: #ffffff;
    border-bottom: 1px solid #d9dee8;
}
QFrame#Footer {
    background: #fff7ed;
    border-top: 1px solid #fed7aa;
}
QLabel#Title {
    font-size: 19pt;
    font-weight: 700;
    color: #0f172a;
}
QLabel#SubTitle {
    font-size: 9pt;
    color: #6b7280;
}
QLabel#SectionTitle {
    font-size: 11pt;
    font-weight: 700;
    color: #0f172a;
}
/* 「查询结果」标题行：本局胜负平（胜绿 / 平橙 / 负红），随异步战绩更新 */
QLabel#SectionTitle[outcome="victory"] {
    color: #15803d;
}
QLabel#SectionTitle[outcome="draw"] {
    color: #b45309;
}
QLabel#SectionTitle[outcome="defeat"] {
    color: #b91c1c;
}
QLabel#SectionTitle[outcome="loading"] {
    color: #6b7280;
}
QLabel#FieldName {
    color: #6b7280;
    font-size: 9pt;
}
QLabel#MetaValue {
    color: #111827;
    font-weight: 600;
}
QLabel#MetaHint {
    color: #6b7280;
    font-size: 9pt;
}
QLabel#Disclaimer {
    color: #9a3412;
    font-size: 9pt;
    font-weight: 600;
}
QLabel#EmptyState {
    color: #6b7280;
    font-size: 12pt;
}
QLabel#EmptyHint {
    color: #9ca3af;
    font-size: 9pt;
}
QLabel#DetailName {
    font-size: 15pt;
    font-weight: 700;
    color: #0f172a;
}
QLabel#DetailSub {
    color: #6b7280;
    font-size: 9pt;
}
QLabel#StatsValue {
    font-size: 9pt;
    font-weight: 600;
    color: #111827;
}
QLabel#StatsHint {
    color: #6b7280;
    font-size: 8.5pt;
}
QLabel#LoadingHint {
    color: #2563eb;
    font-size: 9pt;
}
QLabel#WarnHint {
    color: #b45309;
    font-size: 9pt;
}
QLabel#Badge {
    border-radius: 9px;
    padding: 2px 10px;
    font-size: 8.5pt;
    font-weight: 600;
}
QLabel#Badge[kind="mock"] {
    background: #fef3c7; color: #92400e; border: 1px solid #fcd34d;
}
QLabel#Badge[kind="real"] {
    background: #dcfce7; color: #166534; border: 1px solid #86efac;
}
QLabel#Badge[kind="partial"] {
    background: #fef3c7; color: #92400e; border: 1px solid #fcd34d;
}
QLabel#Badge[kind="complete"] {
    background: #e0f2fe; color: #075985; border: 1px solid #7dd3fc;
}
QLabel#Badge[kind="red"] {
    background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5;
}
QLabel#Badge[kind="yellow"] {
    background: #fef9c3; color: #854d0e; border: 1px solid #fde047;
}
QLabel#Badge[kind="id"] {
    background: #dbeafe; color: #1d4ed8; border: 1px solid #93c5fd;
}
QLabel#Badge[kind="nick"] {
    background: #f1f5f9; color: #475569; border: 1px solid #cbd5e1;
}
QLabel#Badge[kind="none"] {
    background: #f1f5f9; color: #94a3b8; border: 1px solid #e2e8f0;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    padding: 6px 14px;
    color: #1f2937;
}
QPushButton:hover {
    background: #f1f5f9;
    border-color: #94a3b8;
}
QPushButton:pressed {
    background: #e2e8f0;
}
QPushButton:disabled {
    color: #9ca3af;
    background: #f8fafc;
    border-color: #e2e8f0;
}
QPushButton#Primary {
    background: #2563eb;
    border-color: #2563eb;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#Primary:hover {
    background: #1d4ed8;
    border-color: #1d4ed8;
}
QPushButton#Primary:disabled {
    background: #93c5fd;
    border-color: #93c5fd;
    color: #eff6ff;
}
QPushButton#Danger {
    background: #fff5f5;
    border: 1px solid #fca5a5;
    color: #b91c1c;
    font-weight: 600;
}
QPushButton#Danger:hover {
    background: #fee2e2;
    border-color: #f87171;
}
QPushButton#Danger:disabled {
    background: #f8fafc;
    border-color: #e2e8f0;
    color: #9ca3af;
}
QPushButton#Ghost {
    background: transparent;
    border: 1px solid transparent;
    color: #2563eb;
    padding: 4px 8px;
}
QPushButton#Ghost:hover {
    background: #eff6ff;
    border-color: #bfdbfe;
}
QTableView {
    background: #ffffff;
    border: none;
    gridline-color: #e5e9f0;
    alternate-background-color: #fafbfd;
    selection-background-color: #dbeafe;
    selection-color: #111827;
}
/* 结果表：24 列 + 30/60 人，压缩字体 / 行高 / 内边距保证一屏 30 人（需求 6） */
QTableView#ResultTable {
    font-size: 7.5pt;
}
QTableView#ResultTable QWidget {
    font-size: 7.5pt;
}
QTableView#ResultTable QHeaderView {
    font-size: 7.5pt;
}
QTableView#ResultTable QHeaderView::section {
    padding: 2px 2px;
    font-size: 7.5pt;
}
QTableView#ResultTable::item {
    padding: 0px 2px;
}
QHeaderView::section {
    background: #f3f6fb;
    color: #374151;
    border: none;
    border-right: 1px solid #e5e9f0;
    border-bottom: 1px solid #d9dee8;
    padding: 7px 8px;
    font-weight: 600;
}
QTableCornerButton::section {
    background: #f3f6fb;
    border: none;
}
QProgressBar {
    border: 1px solid #d9dee8;
    border-radius: 4px;
    background: #f1f5f9;
    text-align: center;
    height: 16px;
    font-size: 8pt;
    color: #374151;
}
QProgressBar::chunk {
    background: #2563eb;
    border-radius: 3px;
}
QComboBox {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    padding: 5px 10px;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
/* 当前对局下拉框的自绘弹窗（最多 15 行 + 竖向滚动条，见 widgets.ScrollableComboBox） */
QFrame#ComboPopup {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
}
QFrame#ComboPopup QListView {
    background: transparent;
    border: none;
    outline: 0;
}
QFrame#ComboPopup QListView::item {
    padding: 5px 10px;
    color: #1f2937;
}
QFrame#ComboPopup QListView::item:hover {
    background: #eff6ff;
}
QFrame#ComboPopup QListView::item:selected {
    background: #dbeafe;
    color: #111827;
}
QCheckBox {
    spacing: 6px;
}
QStatusBar {
    background: #ffffff;
    border-top: 1px solid #e5e9f0;
    color: #4b5563;
}
QSplitter::handle {
    background: #e2e8f0;
    width: 3px;
}
QScrollArea {
    border: none;
}
QScrollBar:vertical {
    background: #f8fafc;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #cbd5e1;
    border-radius: 5px;
    min-height: 24px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QToolTip {
    background: #111827;
    color: #f9fafb;
    border: none;
    padding: 5px 8px;
}
"""


def configure_application(app: Any) -> None:
    """设置应用级字体、Fusion 样式与全局 QSS（幂等）。"""
    if app is None:
        return
    app.setStyle("Fusion")
    font = QFont(FONT_FAMILY, BASE_FONT_POINT_SIZE)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    app.setStyleSheet(APP_QSS)


def make_badge(text: str, kind: str = "none") -> Any:
    """构造圆角徽标标签（kind: mock/real/partial/complete/red/yellow/id/nick/none）。"""
    label = QLabel(text)
    label.setObjectName("Badge")
    label.setProperty("kind", kind)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setSizePolicy(label.sizePolicy().horizontalPolicy(), label.sizePolicy().verticalPolicy())
    return label


def set_badge(label: Any, text: str, kind: str) -> None:
    """更新徽标文字与语义色（改动态属性后需要重新 polish）。"""
    label.setText(text)
    label.setProperty("kind", kind)
    style = label.style()
    style.unpolish(label)
    style.polish(label)
    label.update()


def make_section_title(text: str) -> Any:
    """小节标题标签。"""
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    return label


def make_field(name: str, value: str = "—") -> tuple:
    """构造「字段名 + 值」两个标签，返回值标签供后续更新。"""
    name_label = QLabel(name)
    name_label.setObjectName("FieldName")
    value_label = QLabel(value)
    value_label.setObjectName("StatsValue")
    return name_label, value_label


def main(argv: list) -> int:
    """自检：打印主题常量与免责声明（不创建 QApplication）。"""
    import time

    start = time.time()
    base_path = "."
    print("[1/2] 主题自检（base_path=%r）：字体=%s 字号=%dpt" % (base_path, FONT_FAMILY, BASE_FONT_POINT_SIZE))
    print("      QSS 长度=%d 字符，免责声明=%d 字" % (len(APP_QSS), len(DISCLAIMER_TEXT)))
    print("[2/2] 主题自检完成，耗时 %.2f 秒" % (time.time() - start))
    return 0


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
