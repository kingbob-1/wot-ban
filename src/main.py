# -*- coding: utf-8 -*-
"""wot-ban 入口：参数解析 + 流程调度。

用法
----
    python src/main.py                 # 启动 GUI（PySide6 主窗口）
    python src/main.py --mock          # 用内置演示数据启动（数据层未就绪时自动启用）
    python src/main.py --init-db       # 初始化 / 升级本地 SQLite 数据库
    python src/main.py --version       # 打印版本后退出
    python src/main.py --config PATH   # 使用指定配置文件
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import load_config  # noqa: E402
from src.logger import get_logger, init_logging  # noqa: E402
from src.store.db import (  # noqa: E402
    EXPECTED_TABLES,
    connect,
    init_schema,
    list_tables,
    now_iso,
    set_meta,
)


def ensure_console_streams() -> None:
    """pythonw 启动时 sys.stdout/stderr 为 None，替换为 devnull 避免 print 崩溃。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def format_duration(seconds: float) -> str:
    """秒数格式化为 hh:mm:ss。"""
    total = int(round(seconds))
    return "%02d:%02d:%02d" % (total // 3600, total % 3600 // 60, total % 60)


def parse_args(argv: list) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="wot-ban", description="坦克世界国服封号案底查询工具（历史封禁记录，非官方）"
    )
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    parser.add_argument("--init-db", action="store_true", help="初始化 / 升级本地 SQLite 数据库后退出")
    parser.add_argument("--mock", action="store_true", help="使用内置演示数据启动 GUI（数据模块未就绪时自动启用）")
    parser.add_argument("--config", default=None, help="指定配置文件路径（默认项目内 config.json）")
    return parser.parse_args(argv)


def command_init_db(cfg) -> int:
    """--init-db：建库建表并写入版本信息。"""
    step_start = time.time()
    print("[1/2] 初始化数据库: %s" % cfg.db_path)
    conn = connect(cfg.db_path)
    version = init_schema(conn)
    tables = list_tables(conn)
    missing = [name for name in EXPECTED_TABLES if name not in tables]
    print(
        "      表数量=%d（期望 %d），schema_version=%s，耗时 %.2f 秒"
        % (len(tables), len(EXPECTED_TABLES), version, time.time() - step_start)
    )
    if missing:
        print("[error] 缺少表: %s" % ", ".join(missing))
        return 1

    step_start = time.time()
    print("[2/2] 写入 meta 自检标记")
    set_meta(conn, "last_init_db_at", now_iso())
    set_meta(conn, "db_path", str(cfg.db_path))
    print("      完成，耗时 %.2f 秒" % (time.time() - step_start))
    return 0


def command_launch_gui(cfg, mock: bool = False) -> int:
    """默认行为：启动 PySide6 主窗口（完整界面见 src/ui）。"""
    logger = get_logger("gui")
    try:
        from PySide6.QtWidgets import QApplication  # noqa: F401
    except ImportError as exc:
        print("[error] 缺少 PySide6：%s" % exc)
        print(
            '        安装命令: "%s" -m pip install PySide6 -i https://pypi.tuna.tsinghua.edu.cn/simple'
            % sys.executable
        )
        return 2

    from src.ui.main_window import run_app

    logger.info("GUI 主窗口启动（mock=%s）", mock)
    code = run_app(cfg, mock=mock)
    # Windows + PySide6 6.12：app.exec() 返回后跳过解释器退出阶段的 Qt 全局析构，
    # 否则偶发 Qt6Core.dll 崩溃（0xc0000409 BEX64）导致系统弹「python 已停止工作」。
    try:
        import logging

        logging.shutdown()
    except Exception:  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os._exit(0 if code == 0 else code)


def main(argv: list) -> int:
    """主入口：解析参数 -> 初始化日志 -> 按模式分派。"""
    start = time.time()
    base_path = "."
    ensure_console_streams()
    args = parse_args(argv)
    cfg = load_config(args.config) if args.config else load_config()

    if args.version:
        print("%s v%s" % (cfg.app_name, cfg.app_version))
        return 0

    cfg.ensure_dirs()
    logger = init_logging(script_name="wot-ban")
    logger.info("%s v%s 启动，base_path=%r，数据库=%s", cfg.app_name, cfg.app_version, base_path, cfg.db_path)

    try:
        if args.init_db:
            code = command_init_db(cfg)
        else:
            code = command_launch_gui(cfg, mock=args.mock)
    except Exception:  # noqa: BLE001 - 顶层兜底：未捕获异常必须落日志并给中文提示
        logger.exception("运行过程中出现未捕获异常")
        print("[error] 运行失败，详细信息见 log 目录中的日志文件")
        print(traceback.format_exc())
        return 1

    print("总耗时 %s" % format_duration(time.time() - start))
    return code


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
