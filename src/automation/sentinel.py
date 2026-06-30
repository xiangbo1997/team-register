# -*- coding: utf-8 -*-
"""
OpenAI Sentinel PoW token 生成框架

设计目标：
- 为 chatgpt.com 旁路 API 调用（payment_link / promo_eligibility / token 提取）
  提供 ``openai-sentinel-token`` header，避免被 OpenAI 风控扩面时直接拦截
- 框架与具体生成方案解耦：未来要接 Playwright sdk.js / QuickJS 方案时，
  只需新增一个 SentinelProvider 实现即可
- 当前默认 NoOpProvider（不做任何事），等价于"零行为变更"

使用方式：
    provider = build_provider_from_config(config)
    token = try_get_sentinel_token(provider, flow="authorize_continue")
    if token:
        headers["openai-sentinel-token"] = token

设计约束（与 captcha_solver.py / triage.py 一致）：
- 失败静默：任何异常降级为 ""（空字符串），绝不打断主流程
- 事件落盘：每次 generate 通过 emit_event 写入 SSE（暂不强制，初版可选）
- 不阻塞主线程：生成超时上限 10s（FNV-1a brute-force 单机 < 1s 即可完成）

实现参考：
- any-auto-register 项目 ``platforms/chatgpt/sentinel_token.py``（263 行）
- 算法：fetch challenge from sentinel.openai.com → PoW (FNV-1a brute-force)
- OpenAI 端点：POST https://sentinel.openai.com/backend-api/sentinel/req
"""

from __future__ import annotations

import base64
import json
import logging
import random
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# --- OpenAI Sentinel 端点常量 ----------------------------------------------

SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
SENTINEL_REFERER = "https://sentinel.openai.com/backend-api/sentinel/frame.html"

# Sentinel SDK 版本号：写进 token payload 的 _get_config()[6] 字段
# 注：这不是 sdk.js 下载路径，而是 OpenAI 内部用来识别 SDK 版本的 marker
# 升级方式：观察 chatgpt.com 页面里实际加载的 sentinel/<VERSION>/sdk.js 路径
DEFAULT_SDK_VERSION = "20260124ceb8"


# --- 数据模型 --------------------------------------------------------------

@dataclass(frozen=True)
class SentinelAttempt:
    """单次 Sentinel token 生成的结果。"""

    success: bool
    provider_name: str  # 例如 "noop" / "pure_python"
    duration_ms: int
    token: str = ""              # 成功时为有效 token，失败时为空串
    rationale: str = ""          # 失败原因或成功简述


# --- Provider 抽象 ---------------------------------------------------------

class SentinelProvider(ABC):
    """
    Sentinel token 生成器的抽象接口。

    实现要点：
    - ``generate()`` 必须在 10s 内返回（建议 < 3s）
    - 任何外部异常都要内部 catch，包装成 SentinelAttempt(success=False)
    - ``provider_name`` 用于事件日志和监控聚合
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """识别用名称，例如 'pure_python' / 'sdk_js'。"""

    @abstractmethod
    def generate(
        self,
        *,
        flow: str = "authorize_continue",
        user_agent: str = "",
        device_id: str = "",
        proxy: Optional[str] = None,
    ) -> SentinelAttempt:
        """
        生成一个 Sentinel token。

        参数：
          flow: OpenAI Sentinel flow name，例如 "authorize_continue" / "register" / "verify_email"
          user_agent: 与调用方一致的 UA（保证指纹一致性）；为空时用默认
          device_id: 持久化设备 ID（同一账号应复用，建议从 cookie 或 storage 取）
          proxy: 代理 URL（与调用方一致），形如 "socks5h://user:pass@host:port"

        返回：SentinelAttempt，``success=True`` 时 ``token`` 必须非空
        """


# --- NoOp Provider（默认）-------------------------------------------------

class NoOpProvider(SentinelProvider):
    """
    默认 provider：什么都不做，返回空 token。

    存在意义：让调用方永远不需要判 None，且与"未启用 sentinel"语义等价。
    """

    @property
    def provider_name(self) -> str:
        return "noop"

    def generate(
        self,
        *,
        flow: str = "authorize_continue",
        user_agent: str = "",
        device_id: str = "",
        proxy: Optional[str] = None,
    ) -> SentinelAttempt:
        return SentinelAttempt(
            success=False,
            provider_name=self.provider_name,
            duration_ms=0,
            token="",
            rationale="noop provider always declines",
        )


# --- PurePython Provider（FNV-1a brute-force PoW）-------------------------

class _PurePythonTokenBuilder:
    """
    内部 PoW 计算引擎。算法移植自 any-auto-register sentinel_token.py。

    工作流程：
      1. POST sentinel.openai.com/backend-api/sentinel/req 拿 challenge
      2. challenge.proofofwork.required=True 时本地跑 FNV-1a brute-force 找 nonce
      3. 拼出最终 token 字符串 {p, t, c, id, flow}
    """

    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(
        self,
        *,
        device_id: str = "",
        user_agent: str = "",
        sdk_version: str = DEFAULT_SDK_VERSION,
    ):
        self.device_id = device_id or str(uuid.uuid4())
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        )
        self.sdk_version = sdk_version
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        """32-bit FNV-1a 哈希，OpenAI Sentinel PoW 使用。"""
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        """生成 token payload 的核心数据。字段顺序与 OpenAI sdk.js 一致，勿改。"""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        date_str = now.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)")
        perf_now = random.uniform(1000, 50000)
        time_origin = time.time() * 1000 - perf_now
        nav_prop = random.choice([
            "vendorSub", "productSub", "vendor", "maxTouchPoints", "scheduling",
            "userActivation", "doNotTrack", "geolocation", "connection",
            "plugins", "mimeTypes", "pdfViewerEnabled", "webkitTemporaryStorage",
            "webkitPersistentStorage", "hardwareConcurrency", "cookieEnabled",
            "credentials", "mediaDevices", "permissions", "locks", "ink",
        ])
        return [
            "1920x1080",
            date_str,
            4294705152,
            random.random(),
            self.user_agent,
            f"https://sentinel.openai.com/sentinel/{self.sdk_version}/sdk.js",
            None,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            f"{nav_prop}−undefined",
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now,
            self.sid,
            "",
            "",
            random.choice([4, 8, 12, 16]),
            time_origin,
        ]

    @staticmethod
    def _base64_encode(data: Any) -> str:
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _run_check(self, start_time, seed, difficulty, config, nonce):
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        encoded = self._base64_encode(config)
        digest = self._fnv1a_32(seed + encoded)
        if digest[: len(difficulty)] <= difficulty:
            return encoded + "~S"
        return None

    def generate_pow_token(self, seed: Optional[str] = None, difficulty: Optional[str] = None) -> str:
        """跑 FNV-1a brute-force PoW，找到满足 difficulty 的 nonce。"""
        seed = seed or self.requirements_seed
        difficulty = difficulty or "0"
        start_time = time.time()
        config = self._get_config()
        for nonce in range(self.MAX_ATTEMPTS):
            value = self._run_check(start_time, seed, difficulty, config, nonce)
            if value:
                return "gAAAAAB" + value
        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))

    def generate_requirements_token(self) -> str:
        """生成 requirements token（首次握手用）。"""
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._base64_encode(config)


def _fetch_sentinel_challenge(
    session: Any,
    device_id: str,
    *,
    flow: str,
    user_agent: str,
    impersonate: str,
    request_p: str,
    timeout: float,
) -> Optional[dict]:
    """POST sentinel.openai.com 拿 challenge。失败返回 None。"""
    req_body = {
        "p": request_p,
        "id": device_id,
        "flow": flow,
    }
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Referer": SENTINEL_REFERER,
        "Origin": "https://sentinel.openai.com",
        "User-Agent": user_agent,
        "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    try:
        response = session.post(
            SENTINEL_REQ_URL,
            data=json.dumps(req_body),
            headers=headers,
            impersonate=impersonate,
            timeout=timeout,
        )
        if response.status_code == 200:
            return response.json()
        logger.warning("sentinel challenge HTTP %s: %s", response.status_code, response.text[:200])
    except Exception as exc:
        logger.warning("sentinel challenge fetch failed: %s", exc)
    return None


class PurePythonProvider(SentinelProvider):
    """
    纯 Python 实现：本地 PoW 计算 + 拿 OpenAI 服务端挑战。

    无浏览器依赖、单机 < 3s 完成、不需要预生成池。
    依赖：curl_cffi（与 payment_link.py / promo_eligibility.client 保持一致）

    缺陷：OpenAI 更新算法或 SDK 版本后需要同步跟进（参考 any-auto-register 项目）
    """

    def __init__(
        self,
        *,
        sdk_version: str = DEFAULT_SDK_VERSION,
        impersonate: str = "chrome120",
        timeout_seconds: float = 10.0,
    ):
        self.sdk_version = sdk_version
        self.impersonate = impersonate
        self.timeout_seconds = timeout_seconds

    @property
    def provider_name(self) -> str:
        return "pure_python"

    def generate(
        self,
        *,
        flow: str = "authorize_continue",
        user_agent: str = "",
        device_id: str = "",
        proxy: Optional[str] = None,
    ) -> SentinelAttempt:
        start = time.time()
        try:
            # curl_cffi 导入放在函数内：避免模块层 import 在测试 monkey-patch 时绕不开
            from curl_cffi import requests as cffi_requests

            session_kwargs: dict[str, Any] = {}
            if proxy:
                session_kwargs["proxies"] = {"http": proxy, "https": proxy}
            session = cffi_requests.Session(**session_kwargs)

            effective_device_id = device_id or str(uuid.uuid4())
            effective_ua = user_agent or (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )

            builder = _PurePythonTokenBuilder(
                device_id=effective_device_id,
                user_agent=effective_ua,
                sdk_version=self.sdk_version,
            )

            # 1. 首次握手：发 requirements_token 拿 challenge
            challenge = _fetch_sentinel_challenge(
                session,
                effective_device_id,
                flow=flow,
                user_agent=effective_ua,
                impersonate=self.impersonate,
                request_p=builder.generate_requirements_token(),
                timeout=self.timeout_seconds,
            )
            if not challenge:
                return SentinelAttempt(
                    success=False,
                    provider_name=self.provider_name,
                    duration_ms=int((time.time() - start) * 1000),
                    rationale="failed to fetch sentinel challenge",
                )

            c_value = str(challenge.get("token") or "").strip()
            if not c_value:
                return SentinelAttempt(
                    success=False,
                    provider_name=self.provider_name,
                    duration_ms=int((time.time() - start) * 1000),
                    rationale="challenge response missing 'token' field",
                )

            # 2. 跑 PoW（如果需要），否则用 requirements_token 作为 p
            pow_data = challenge.get("proofofwork") or {}
            if pow_data.get("required") and pow_data.get("seed"):
                p_value = builder.generate_pow_token(
                    seed=pow_data.get("seed"),
                    difficulty=pow_data.get("difficulty", "0"),
                )
            else:
                p_value = builder.generate_requirements_token()

            # 3. 拼最终 token（OpenAI 期望的 JSON 字符串格式）
            token_str = json.dumps(
                {
                    "p": p_value,
                    "t": "",
                    "c": c_value,
                    "id": effective_device_id,
                    "flow": flow,
                },
                separators=(",", ":"),
            )

            return SentinelAttempt(
                success=True,
                provider_name=self.provider_name,
                duration_ms=int((time.time() - start) * 1000),
                token=token_str,
                rationale=f"pow_required={bool(pow_data.get('required'))}",
            )
        except Exception as exc:
            logger.warning("PurePythonProvider.generate exception: %s", exc)
            return SentinelAttempt(
                success=False,
                provider_name=self.provider_name,
                duration_ms=int((time.time() - start) * 1000),
                rationale=f"exception: {type(exc).__name__}",
            )


# --- 工厂方法 + 顶层入口 ---------------------------------------------------

def build_provider_from_config(config: Any) -> SentinelProvider:
    """
    从 AppConfig 构造 SentinelProvider 实例。

    路由规则：
    - sentinel_strategy == "pure_python" → PurePythonProvider
    - 其他（含 "noop" 或缺省）→ NoOpProvider

    设计：失败降级为 NoOpProvider，绝不抛异常，避免 worker 启动崩溃。
    """
    kind = str(getattr(config, "sentinel_strategy", "noop") or "noop").strip().lower()
    if kind == "pure_python":
        sdk_version = str(
            getattr(config, "sentinel_sdk_version", DEFAULT_SDK_VERSION) or DEFAULT_SDK_VERSION
        ).strip()
        # 默认 impersonate 与 payment_link / promo_eligibility 锁死的 chrome120 一致，
        # 保证同一 access_token 对外的 TLS 指纹自洽
        impersonate = str(getattr(config, "sentinel_impersonate", "chrome120") or "chrome120").strip()
        timeout_ms = int(getattr(config, "sentinel_timeout_ms", 10000) or 10000)
        try:
            return PurePythonProvider(
                sdk_version=sdk_version,
                impersonate=impersonate,
                timeout_seconds=timeout_ms / 1000.0,
            )
        except Exception as exc:
            logger.warning("PurePythonProvider build failed: %s; degrading to noop", exc)
            return NoOpProvider()
    return NoOpProvider()


def try_get_sentinel_token(
    provider: Optional[SentinelProvider],
    *,
    flow: str = "authorize_continue",
    user_agent: str = "",
    device_id: str = "",
    proxy: Optional[str] = None,
) -> str:
    """
    顶层封装：从 provider 取一个 sentinel token，失败静默返回 ""。

    调用方典型用法：
        token = try_get_sentinel_token(runtime.sentinel_provider, flow="register")
        if token:
            headers["openai-sentinel-token"] = token

    本函数永不抛异常。
    """
    if provider is None:
        return ""
    try:
        attempt = provider.generate(
            flow=flow,
            user_agent=user_agent,
            device_id=device_id,
            proxy=proxy,
        )
        if attempt.success and attempt.token:
            logger.info(
                "sentinel token generated provider=%s duration_ms=%d rationale=%s",
                attempt.provider_name, attempt.duration_ms, attempt.rationale,
            )
            return attempt.token
        logger.info(
            "sentinel token unavailable provider=%s rationale=%s",
            attempt.provider_name, attempt.rationale,
        )
        return ""
    except Exception as exc:
        logger.warning("try_get_sentinel_token unexpected exception: %s", exc)
        return ""
