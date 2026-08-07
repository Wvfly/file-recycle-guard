"""
日志模块
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from .config import Config


def setup_logger(config: Config) -> logging.Logger:
    """初始化日志系统（重复调用不会叠加 handler）"""
    logger = logging.getLogger("FileRecycleGuard")

    # 避免重复初始化导致日志重复输出
    if getattr(logger, "_guard_initialized", False):
        return logger

    logger.setLevel(getattr(logging, config.log.level.upper(), logging.INFO))

    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    # 文件输出（按天轮转，保留 max_days 天）
    if config.log.file:
        log_dir = os.path.dirname(config.log.file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        file_handler = TimedRotatingFileHandler(
            config.log.file,
            when="midnight",
            backupCount=max(1, config.log.max_days),
            encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)s [%(funcName)s] - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        file_handler.setFormatter(file_fmt)
        logger.addHandler(file_handler)

    logger._guard_initialized = True
    return logger
