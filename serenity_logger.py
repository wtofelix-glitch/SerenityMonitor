"""
SerenityMonitor 统一日志模块
用法:
    from serenity_logger import get_logger
    log = get_logger(__name__)
    log.info("message")
    log.warning("something", exc_info=True)
"""
import logging
import os
import sys
from datetime import datetime

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_loggers: dict[str, logging.Logger] = {}


def get_logger(name: str = "serenity") -> logging.Logger:
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    if logger.handlers:
        _loggers[name] = logger
        return logger

    logger.setLevel(logging.DEBUG)

    # 文件 handler — 按天轮转
    today = datetime.now().strftime("%Y-%m-%d")
    fh = logging.FileHandler(os.path.join(LOG_DIR, f"serenity_{today}.log"), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    # 控制台 handler — INFO+
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(levelname)-7s] %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    _loggers[name] = logger
    return logger
