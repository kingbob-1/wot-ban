# -*- coding: utf-8 -*-
"""日志初始化：优先复用工作区 ``runtime\\common_logger``，失败回退标准 logging。

行为
----
- common_logger 可用时：由它接管 ``sys.stdout``（print 自动带时间戳落 ``log\\``），
  logging 只挂一个控制台 handler，避免同一行日志写两份文件。
- common_logger 不可用时：标准 logging 同时输出到控制台与 ``log\\<script>.log``。
- 日志目录来自 ``Config.log_dir``（默认项目内 ``log\\``），运行产物目录不入库。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

try:
    from .config import load_config
except ImportError:  # 直接以脚本方式运行（python src/logger.py）时的兜底
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.config import load_config

LOGGER_NAME = "wotban"
_initialized = False


def init_logging(script_name: str = "wot-ban", log_dir: Optional[Path] = None,
                 print_header: bool = True) -> logging.Logger:
    """初始化日志（幂等），返回项目根 logger。"""
    global _initialized

    cfg = load_config()
    target_dir = Path(log_dir) if log_dir is not None else cfg.log_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if _initialized:
        return logger

    stream = sys.stdout
    common_ready = False
    if stream is not None:  # pythonw 下 sys.stdout 为 None，不能交给 common_logger 包装
        try:
            from common_logger import init_logger as common_init

            common_init(
                script_name=script_name,
                log_dir=str(target_dir),
                print_header=print_header,
                capture_stderr=True,
            )
            common_ready = True
            stream = sys.stdout
        except Exception as exc:  # noqa: BLE001 - 任何异常都必须能回退，日志不能阻断主流程
            message = "[warn] common_logger 不可用，回退标准 logging：%s" % exc
            print(message)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    if stream is not None:
        console_handler = logging.StreamHandler(stream)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    if not common_ready:
        file_handler = logging.FileHandler(
            target_dir / ("%s.log" % script_name), encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _initialized = True
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取子 logger；未初始化时消息走 logging 默认链路（不报错）。"""
    if not name:
        return logging.getLogger(LOGGER_NAME)
    return logging.getLogger("%s.%s" % (LOGGER_NAME, name))


def main(argv: list) -> int:
    """自检：初始化日志并写一条记录（独立运行入口）。"""
    import time

    start = time.time()
    base_path = "."
    print("[1/2] 初始化日志（base_path=%r）" % base_path)
    logger = init_logging(script_name="wot-ban-logger-selftest", print_header=False)
    logger.info("logger self-check ok")
    print("[2/2] 日志初始化完成，耗时 %.2f 秒" % (time.time() - start))
    return 0


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
