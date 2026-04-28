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

    同时把 root logger 也配上同样的 handler，这样 src/services/、src/providers/ 等
    模块用 ``logging.getLogger(__name__)`` 拿的 logger 也能输出到 stderr。
    否则它们的 logger 没 handler，日志被吞掉，排错时看不到 CardActivation /
    号池调度等内部状态。
    """
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log = logging.getLogger(name)
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.setLevel(level)
    # 已经有自己的 handler，不要再让 root 重复输出一遍
    log.propagate = False

    # 同步配置 root logger，让所有模块 logger（src.services.* 等）都能输出
    root = logging.getLogger()
    if not any(getattr(h, "_team_register_marker", False) for h in root.handlers):
        root_handler = logging.StreamHandler()
        root_handler.setFormatter(formatter)
        # 标记一下，避免反复添加
        root_handler._team_register_marker = True  # type: ignore[attr-defined]
        root.addHandler(root_handler)
    root.setLevel(level)

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
