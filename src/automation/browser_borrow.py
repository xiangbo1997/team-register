# -*- coding: utf-8 -*-
"""
浏览器 Header / Cookie 借用工具

设计动机：
- OpenAI 2026 chatgpt.com 实际反爬体系是 ``x-oai-is`` JWE 加密签名 + ``cf_clearance``
  + ``__Secure-next-auth.session-token`` 等 cookie 组合，**不是**之前推测的
  ``openai-sentinel-token`` PoW header
- 真实数据来源：DevTools "Copy as cURL" 的 ``/backend-api/payments/checkout``
  ``/backend-api/sentinel/chat-requirements/prepare`` ``/backend-api/me`` 等
- AdsPower CDP 浏览器已完成 cf_clearance + sentinel/chat-requirements 握手 →
  把它产生的整套 cookie + 关键 header 借给 curl_cffi 旁路调用即可绕开本地 PoW 重实现

使用方式（典型场景）：
    # 1. 上层有 Playwright Page（AdsPower CDP 连接已登录 chatgpt.com）
    borrower = BrowserBorrower.from_page(page, latest_request_url_pattern="backend-api")
    borrow_headers = borrower.build_borrow_headers(access_token="eyJ...")
    # 2. 把 borrow_headers 传给旁路 curl_cffi 调用
    ok, link = PaymentLinkGenerator.generate_short_link(
        access_token=token, borrow_headers=borrow_headers, ...
    )

设计约束：
- 失败静默：page 不可用/截不到时返回空 dict，调用方降级裸跑
- 不依赖 Playwright 强制 import：from_page 内部按需 import，单测可直接用 from_dict
- 不可变 dataclass：snapshot 一旦构造就不再变（避免并发污染）

与 sentinel.py 的区别：
- sentinel.py 处理 "本地算法生成 PoW token"（推测路线）
- browser_borrow.py 处理 "从已登录浏览器借现成 header"（实测路线，推荐主路径）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# OpenAI chatgpt.com 真实在用的"会话签名" header（来自 2026-05 DevTools 实测）
# 详见 docs/research/risk-control-insights.md 或 chat 历史
# 设为 frozenset 防止误改
BORROW_HEADER_NAMES: frozenset[str] = frozenset({
    "x-oai-is",                  # JWE 加密的客户端完整性签名（每请求不同）
    "oai-device-id",             # 设备 ID（同 oai-did cookie，长生命周期）
    "oai-session-id",            # 会话 ID（页面加载时生成）
    "oai-client-version",        # build hash，OpenAI 用于客户端版本风控
    "oai-client-build-number",
    "oai-language",              # ja-JP / en-US 等
    "sec-ch-ua",                 # Chrome 142 真实指纹
    "sec-ch-ua-arch",
    "sec-ch-ua-bitness",
    "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-mobile",
    "sec-ch-ua-model",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
    "user-agent",
})

# 借出去的 cookie 名单（chatgpt.com domain）：cf_clearance 必须、session 必须、
# 其他用于风控信号一致性
BORROW_COOKIE_NAMES: frozenset[str] = frozenset({
    "cf_clearance",                          # Cloudflare 通过证明（最关键）
    "__cf_bm",                               # Cloudflare bot manager
    "_cfuvid",                               # Cloudflare 访客 ID
    "__cflb",                                # Cloudflare load balancer 黏性
    "__Secure-next-auth.session-token",      # NextAuth 登录态
    "__Secure-next-auth.callback-url",
    "__Host-next-auth.csrf-token",
    "__Secure-oai-is",                       # 与 x-oai-is header 配套的 cookie 版
    "oai-did",                               # device id cookie 版
    "oai-sc",                                # 短期会话 cookie
    "oai-hlib",
    "oai-gn",                                # 用户姓
    "oai-client-auth-info",                  # 客户端登录元信息
    "_account_is_fedramp",
    "__stripe_mid",                          # Stripe 会话（绑卡场景需要）
})


@dataclass(frozen=True)
class BorrowSnapshot:
    """从浏览器借来的 headers + cookies 快照（不可变）。"""

    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    source_url: str = ""             # 借自哪个 page url，便于审计
    captured_at_ms: int = 0          # 借取时刻 epoch ms，用于过期判断（cf_clearance 通常 30min）

    def is_empty(self) -> bool:
        return not self.headers and not self.cookies

    def has_critical(self) -> bool:
        """是否包含至少 1 个关键凭据：cf_clearance 或 session-token。"""
        return any(
            name in self.cookies
            for name in ("cf_clearance", "__Secure-next-auth.session-token")
        )


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------

class BrowserBorrower:
    """
    从 Playwright Page / 原始 dict / curl 字符串借 headers + cookies。

    典型用法：
        borrower = BrowserBorrower.from_page(page)
        snap = borrower.snapshot()
        borrow_headers = snap.headers
        # 把 cookie 也拼进 curl_cffi session
    """

    def __init__(self, snapshot: BorrowSnapshot):
        self.snapshot = snapshot

    # --- 来源方法（多种入口）---------------------------------------------

    @classmethod
    def from_dict(
        cls,
        *,
        headers: Optional[dict[str, str]] = None,
        cookies: Optional[dict[str, str]] = None,
        source_url: str = "",
    ) -> "BrowserBorrower":
        """
        从纯 dict 构造。用于测试或手动粘贴 curl 数据时。

        会做两件清理：
        - 只保留 BORROW_HEADER_NAMES / BORROW_COOKIE_NAMES 白名单内的项
        - header 名统一 lowercase，避免大小写引起的合并冲突
        """
        clean_headers: dict[str, str] = {}
        for name, value in (headers or {}).items():
            lower = name.lower().strip()
            if lower in BORROW_HEADER_NAMES and value:
                clean_headers[lower] = str(value)

        clean_cookies: dict[str, str] = {}
        for name, value in (cookies or {}).items():
            if name in BORROW_COOKIE_NAMES and value:
                clean_cookies[name] = str(value)

        import time
        snap = BorrowSnapshot(
            headers=clean_headers,
            cookies=clean_cookies,
            source_url=source_url,
            captured_at_ms=int(time.time() * 1000),
        )
        return cls(snap)

    @classmethod
    def from_page(
        cls,
        page: Any,
        *,
        latest_request_url_pattern: str = "backend-api",
    ) -> "BrowserBorrower":
        """
        从 Playwright Page 借取。

        实现要点：
        - cookies：page.context.cookies('https://chatgpt.com') 拉所有 chatgpt.com domain
        - headers：监听最近一次匹配 ``latest_request_url_pattern`` 的请求，
          抠出其 request.headers()

        失败静默：page 无效 / 监听超时 / DevTools 协议错 → 返回 empty snapshot
        """
        try:
            # cookies：直接同步拉
            raw_cookies = []
            try:
                raw_cookies = page.context.cookies("https://chatgpt.com")
            except Exception as exc:
                logger.warning("BrowserBorrower: cookies dump failed: %s", exc)
            cookies = {c["name"]: c["value"] for c in raw_cookies if c.get("name")}

            # headers：用 page.evaluate 发一个真实请求，让浏览器自然生成 x-oai-is 等
            # 然后通过 request_finished 事件抓 request.allHeaders()
            captured_headers: dict[str, str] = {}

            def _on_request_finished(request: Any) -> None:
                if latest_request_url_pattern in (request.url or ""):
                    try:
                        for k, v in (request.all_headers() or {}).items():
                            captured_headers[k.lower()] = v
                    except Exception:
                        pass

            page.on("requestfinished", _on_request_finished)
            try:
                # 触发一个轻量请求：/backend-api/me 是只读 GET，sdk.js 会自动注入 x-oai-is
                page.evaluate(
                    "() => fetch('/backend-api/me', {credentials: 'include'}).catch(()=>null)"
                )
                # 等一小段时间让 requestfinished 触发
                page.wait_for_timeout(1500)
            finally:
                try:
                    page.remove_listener("requestfinished", _on_request_finished)
                except Exception:
                    pass

            source_url = ""
            try:
                source_url = page.url
            except Exception:
                pass

            return cls.from_dict(
                headers=captured_headers,
                cookies=cookies,
                source_url=source_url,
            )
        except Exception as exc:
            logger.warning("BrowserBorrower.from_page exception: %s", exc)
            return cls(BorrowSnapshot())

    # --- 输出方法 ---------------------------------------------------------

    def build_request_kwargs(
        self,
        *,
        access_token: str = "",
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """
        组合成可直接传给 curl_cffi.requests.post/get 的 kwargs。

        返回的字典含 ``headers`` 和 ``cookies`` 两个键。调用方写法：
            kwargs = borrower.build_request_kwargs(access_token=tok)
            response = requests.post(url, json=payload, **kwargs, impersonate="chrome120")

        参数：
          access_token: 显式覆盖 Authorization Bearer（旁路调用通常自己管 token）
          extra_headers: 额外自定义 header（如 Content-Type / Accept），优先级高于借来的

        失败处理：snapshot 为空时返回 {"headers": {}, "cookies": {}} 让调用方裸跑
        """
        headers: dict[str, str] = dict(self.snapshot.headers)
        if access_token:
            headers["authorization"] = f"Bearer {access_token}"
        if extra_headers:
            for k, v in extra_headers.items():
                headers[k.lower().strip()] = v

        cookies: dict[str, str] = dict(self.snapshot.cookies)

        return {"headers": headers, "cookies": cookies}

    def is_usable(self) -> bool:
        """快速检查：是否包含关键凭据。"""
        return self.snapshot.has_critical()
