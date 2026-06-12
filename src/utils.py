# -*- coding: utf-8 -*-
"""
通用工具函数

包含日志配置、拟人化操作延迟等公共能力。
"""

import os
import sys
import time
import random
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


def _resolve_log_dir() -> Optional[Path]:
    """解析控制面日志落盘目录（可写、稳定）。

    优先级：
      1. ``RUN_ARTIFACTS_DIR`` 的父目录（与注册流证据包同源，桌面 app 已锚定到
         ``~/Library/Application Support/team-register/artifacts``，父目录即数据目录）。
      2. 平台默认应用数据目录（与 ``desktop_app._app_data_dir`` 同款解析，避免循环 import）。
    任何一步失败都返回 None，让调用方降级为 stderr-only，绝不因日志路径问题让程序崩。
    """
    # ① RUN_ARTIFACTS_DIR 父目录
    artifacts = os.getenv("RUN_ARTIFACTS_DIR", "").strip()
    if artifacts:
        try:
            parent = Path(artifacts).expanduser().resolve().parent
            parent.mkdir(parents=True, exist_ok=True)
            return parent
        except Exception:
            pass

    # ② 平台默认应用数据目录（与 desktop_app 保持一致）
    try:
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support" / "team-register"
        elif sys.platform.startswith("win"):
            base = Path(os.getenv("APPDATA", str(Path.home()))) / "team-register"
        else:
            base = Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "team-register"
        base.mkdir(parents=True, exist_ok=True)
        return base
    except Exception:
        return None


def _attach_file_handler(target: logging.Logger, formatter: logging.Formatter, level: int) -> None:
    """给 logger 挂一个落盘 RotatingFileHandler（幂等：靠 marker 防重复）。

    写到 ``<数据目录>/control-plane.log``，单文件上限 5MB、保留 3 个备份
    （control-plane.log + .1/.2/.3，最多约 20MB），超过自动轮转，避免桌面 app
    长期运行把日志写到无限大占满磁盘。路径解析失败则静默跳过（保留 stream 输出），
    不影响主流程——日志能进文件是锦上添花，进不去也不能让程序起不来。
    """
    if any(getattr(h, "_team_register_file_marker", False) for h in target.handlers):
        return
    log_dir = _resolve_log_dir()
    if log_dir is None:
        return
    try:
        file_handler = RotatingFileHandler(
            log_dir / "control-plane.log",
            maxBytes=5 * 1024 * 1024,  # 单文件 5MB
            backupCount=3,             # 保留 3 个轮转备份
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        file_handler._team_register_file_marker = True  # type: ignore[attr-defined]
        target.addHandler(file_handler)
    except Exception:
        # 落盘失败不致命：保留 stderr 输出即可
        pass


def setup_logger(name: str = "GPT_Automation", level: int = logging.INFO) -> logging.Logger:
    """
    创建并配置日志记录器。

    同时把 root logger 也配上同样的 handler，这样 src/services/、src/providers/ 等
    模块用 ``logging.getLogger(__name__)`` 拿的 logger 也能输出到 stderr。
    否则它们的 logger 没 handler，日志被吞掉，排错时看不到 CardActivation /
    号池调度等内部状态。

    除 stderr 外，还把 root logger 落盘到 ``<数据目录>/control-plane.log``，
    便于桌面 app（stderr 飘走）场景下排查刷新 token / 核验 Plus / 号池调度等流程。
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

    # 落盘 handler（幂等）：桌面 app 下 stderr 读不到，靠这个文件排障
    _attach_file_handler(root, formatter, level)

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
