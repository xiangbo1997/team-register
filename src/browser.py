# -*- coding: utf-8 -*-
"""
浏览器控制模块

负责 1024Proxy 动态代理提取和 AdsPower 浏览器 CDP 连接。
"""

import logging
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from src.models import ProxyInfo

logger = logging.getLogger(__name__)

# 1024Proxy 提取地址
_PROXY_URL = "https://white.1024proxy.com/white/api?region=Rand&num=1&time=10&format=1&type=txt"

# 请求超时（秒）
_REQUEST_TIMEOUT = 20

# 代理出口国家查询地址（免费，无需 API key）
_IP_GEO_URL = "https://ipapi.co/json/"


def _lookup_proxy_country(
    host: str,
    port: str,
    *,
    timeout: float = 5.0,
    http_get: Optional[Callable[[str, dict], Any]] = None,
) -> str:
    """
    通过给定代理主动发起 HTTPS 请求到 ip-api 类服务，验证真实出口国家。

    策略：
      - 使用 https://ipapi.co/json/ 作为主源（无需 API key，免费额度）
      - 返回 JSON 中的 `country_code` 字段（统一大写）
      - 超时 / 非 200 / JSON 错误 / 无字段 → 返回 ""
      - 不做重试：调用方（fetch_proxy）失败时仅记 warning，不影响代理本身可用性

    Args:
        host: 代理主机
        port: 代理端口
        timeout: 请求超时（秒），默认 5.0
        http_get: 可注入的 HTTP GET（签名 (url, proxies_dict) → response-like），用于测试

    Returns:
        ISO alpha-2 大写国家代码；任何失败返回 ""
    """
    proxies = {
        "http": f"http://{host}:{port}",
        "https": f"http://{host}:{port}",
    }

    def _default_http_get(url: str, proxies_dict: dict) -> Any:
        return requests.get(url, proxies=proxies_dict, timeout=timeout)

    getter = http_get or _default_http_get

    try:
        resp = getter(_IP_GEO_URL, proxies)
        status = getattr(resp, "status_code", 200)
        if status != 200:
            logger.warning("代理国家查询返回非 200 状态: %s", status)
            return ""

        data = resp.json()
        country = data.get("country_code")
        if not country or not isinstance(country, str):
            logger.warning("代理国家查询响应缺少 country_code 字段: %s", data)
            return ""
        return country.strip().upper()
    except requests.RequestException as exc:
        logger.warning("代理国家查询请求异常: %s", exc)
        return ""
    except ValueError as exc:
        # resp.json() 解析失败
        logger.warning("代理国家查询 JSON 解析失败: %s", exc)
        return ""
    except Exception as exc:  # noqa: BLE001 - 保底返回 ""
        logger.warning("代理国家查询未知异常: %s", exc)
        return ""


def _build_requests_proxies(proxy_url: str = "") -> Optional[dict[str, str]]:
    """构造 requests 代理配置。"""
    return {"http": proxy_url, "https": proxy_url} if proxy_url else None


def _candidate_ads_api_urls(ads_api: str) -> list[str]:
    """为本地 AdsPower 地址生成候选访问 URL。"""
    normalized = ads_api.rstrip("/")
    candidates = [normalized]
    if "local.adspower.net" in normalized:
        parts = urlsplit(normalized)
        fallback = urlunsplit((parts.scheme, f"127.0.0.1:{parts.port}" if parts.port else "127.0.0.1", parts.path, parts.query, parts.fragment))
        if fallback not in candidates:
            candidates.append(fallback.rstrip("/"))
    return candidates


def fetch_proxy(proxy_url: str = _PROXY_URL) -> Optional[ProxyInfo]:
    """
    从 1024Proxy 获取一个动态代理 IP。

    Args:
        proxy_url: 代理提取 API 地址

    Returns:
        成功返回 ProxyInfo，失败返回 None
    """
    logger.info("正在从 1024Proxy 提取代理 IP...")

    try:
        resp = requests.get(proxy_url, timeout=_REQUEST_TIMEOUT)
        text = resp.text.strip()

        # 白名单错误检测
        if "not added to whitelist" in text:
            logger.error("1024Proxy 白名单错误: %s", text)
            return None

        if ":" in text:
            host, port = text.split(":", 1)
            country = _lookup_proxy_country(host, port)
            if not country:
                logger.warning("未能确认代理出口国家，coherence 校验将按 block 处理")
            proxy = ProxyInfo(host=host, port=port, country=country)
            logger.info("成功提取代理: %s (country=%s)", proxy, country or "unknown")
            return proxy

        logger.error("1024Proxy 返回格式未知: %s", text)

    except requests.RequestException as exc:
        logger.error("访问 1024Proxy API 异常: %s", exc)

    return None


def get_browser_ws(
    ads_api: str,
    user_id: str,
    api_key: str = "",
    proxy: Optional[ProxyInfo] = None,
) -> str:
    """
    启动 AdsPower 浏览器并获取 CDP WebSocket 端点。

    Args:
        ads_api: AdsPower 本地 API 地址
        user_id: 浏览器配置文件 ID
        api_key: AdsPower API 密钥
        proxy: 可选的代理配置

    Returns:
        WebSocket URL 字符串

    Raises:
        ConnectionError: 无法连接或启动失败
    """
    logger.info("启动 AdsPower 浏览器 (用户: %s)...", user_id)

    params: dict[str, str] = {"user_id": user_id}

    # 注入代理参数
    if proxy:
        params.update({
            "proxy_type": "http",
            "proxy_host": proxy.host,
            "proxy_port": proxy.port,
            "proxy_soft": "other",
        })
        logger.info("已注入代理: %s", proxy)

    # 鉴权 header（兼容不同版本）
    headers: dict[str, str] = {}
    if api_key:
        headers["api-key"] = api_key
        headers["x-api-key"] = api_key

    last_exc: Exception | None = None
    for candidate_api in _candidate_ads_api_urls(ads_api):
        try:
            url = f"{candidate_api}/api/v1/browser/start"
            resp = requests.get(url, params=params, headers=headers, timeout=_REQUEST_TIMEOUT)
            data = resp.json()

            if data.get("code") == 0:
                ws_url = data["data"]["ws"]["puppeteer"]
                logger.info("AdsPower 浏览器启动成功。")
                return ws_url

            msg = data.get("msg", "未知错误")
            if "Require api-key" in msg:
                logger.error("鉴权失败，请检查 ADS_API_KEY 配置。")

            raise ConnectionError(f"AdsPower 启动失败: {msg}")

        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("AdsPower 启动接口访问失败，尝试下一个候选地址: %s", candidate_api)
            continue

    raise ConnectionError(f"请求 AdsPower 接口失败: {last_exc}")


def run_preflight_checks(
    ads_api: str,
    target_url: str = "https://chatgpt.com/",
    proxy_url: str = "",
    timeout_sec: int = _REQUEST_TIMEOUT,
    ads_retries: int = 3,
) -> None:
    """
    在真正启动浏览器前，做一轮轻量连通性检查。

    只验证 AdsPower API 与目标站点是否可达，不修改任何状态。
    """
    proxies = _build_requests_proxies(proxy_url)

    last_ads_exc: requests.RequestException | None = None
    for candidate_api in _candidate_ads_api_urls(ads_api):
        for attempt in range(1, max(ads_retries, 1) + 1):
            try:
                logger.info("执行 AdsPower 预检查: %s", candidate_api)
                requests.get(candidate_api, timeout=timeout_sec)
                last_ads_exc = None
                break
            except requests.RequestException as exc:
                last_ads_exc = exc
                if attempt >= max(ads_retries, 1):
                    break
                logger.warning("AdsPower 预检查失败，第 %d/%d 次重试前等待: %s", attempt, max(ads_retries, 1), exc)
                time.sleep(min(attempt, 2))
        if last_ads_exc is None:
            break

    if last_ads_exc is not None:
        raise ConnectionError(f"AdsPower 预检查失败: {last_ads_exc}") from last_ads_exc

    try:
        logger.info("执行目标站点预检查: %s", target_url)
        resp = requests.get(target_url, timeout=timeout_sec, proxies=proxies)
        if resp.status_code >= 500:
            raise ConnectionError(f"目标站点返回异常状态码: {resp.status_code}")
    except requests.RequestException as exc:
        raise ConnectionError(f"目标站点预检查失败: {exc}") from exc
