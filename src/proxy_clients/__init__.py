# -*- coding: utf-8 -*-
"""动态代理客户端包。

子模块：
  parsers       — 响应解析（txt_line / json_array_host_port）
  dynamic_pool  — DynamicProxyPool 轮换状态机
  adapters/     — 各供应商 SDK 适配器（base ABC + proxy1024 + generic_http + registry）

主要消费方：src/services/code_discovery_service.py
"""
