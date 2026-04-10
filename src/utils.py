# -*- coding: utf-8 -*-
"""
通用工具函数

包含日志配置、拟人化操作延迟等公共能力。
"""

import time
import random
import logging


def setup_logger(name: str = "GPT_Automation", level: int = logging.INFO) -> logging.Logger:
    """
    创建并配置日志记录器。

    Args:
        name: 日志器名称
        level: 日志级别

    Returns:
        配置好的 Logger 实例
    """
    log = logging.getLogger(name)

    # 避免重复添加 handler
    if not log.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        log.addHandler(handler)

    log.setLevel(level)
    return log


def human_delay(min_sec: float = 1.0, max_sec: float = 3.0) -> None:
    """
    模拟人类操作的随机延迟。

    Args:
        min_sec: 最小延迟秒数
        max_sec: 最大延迟秒数
    """
    delay = random.uniform(min_sec, max_sec)
    time.sleep(delay)
