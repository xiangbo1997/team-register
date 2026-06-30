# -*- coding: utf-8 -*-
"""promo_eligibility 模块

只做一件事：给定 promo 码，调 ChatGPT 官方 promotions API 判断是否可用。

不负责：选代理、写数据库、并发控制、重试节奏 —— 那些由
src/services/promo_eligibility_service.py 编排。
"""

from src.promo_eligibility.client import EligibilityResult, check_eligibility

__all__ = ["check_eligibility", "EligibilityResult"]
