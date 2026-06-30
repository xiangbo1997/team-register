# -*- coding: utf-8 -*-
"""动态代理响应解析。

把 fetch_one 拿到的供应商响应（txt / json）归一化为 [(host, port), ...] 列表，
供 adapter 模板方法消费。所有 adapter 共享同一套解析逻辑。

支持的 response_format：
  "txt_line"             — 每行 host:port，多 IP 时一行一个
  "json_array_host_port" — [{"host": "1.2.3.4", "port": "8080"}, ...]
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


def parse_proxy_response(text: str, response_format: str) -> list[tuple[str, str]]:
    """按 response_format 解析响应文本为 [(host, port), ...]。

    Args:
        text: HTTP 响应正文
        response_format: ProxyProvider.response_format 字段值

    Returns:
        非空 (host, port) 元组列表；空响应或解析失败返回空列表。

    Raises:
        ValueError: response_format 不在白名单
    """
    safe_text = (text or "").strip()
    if not safe_text:
        return []

    if response_format == "txt_line":
        return _parse_txt_line(safe_text)
    if response_format == "json_array_host_port":
        return _parse_json_array_host_port(safe_text)
    raise ValueError(
        f"不支持的 response_format={response_format!r}；"
        f"可选: 'txt_line' / 'json_array_host_port'"
    )


def _parse_txt_line(text: str) -> list[tuple[str, str]]:
    """每行 host:port。允许混入空行；port 含字母直接跳过。"""
    out: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            continue
        host, _, port = line.rpartition(":")
        host = host.strip()
        port = port.strip()
        if not host or not port:
            continue
        if not port.isdigit():
            # 防错配（1024 偶尔会返 HTML 错误页）
            logger.debug("txt_line 跳过非数字端口: %r", line)
            continue
        out.append((host, port))
    return out


def _parse_json_array_host_port(text: str) -> list[tuple[str, str]]:
    """[{"host":..., "port":...}, ...]。port 可能是 int 或 str，统一转 str。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("json_array_host_port 解析失败 err=%s text[:200]=%r", exc, text[:200])
        return []
    if not isinstance(data, list):
        logger.warning("json_array_host_port 期望数组得到 %s", type(data).__name__)
        return []
    out: list[tuple[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or item.get("ip") or "").strip()
        port = str(item.get("port") or "").strip()
        if host and port:
            out.append((host, port))
    return out
