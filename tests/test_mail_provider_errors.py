# -*- coding: utf-8 -*-
"""HttpMailProvider 错误分类契约测试。

跨端 contract（见 docs/architecture/mail-provider-contract.md）：
  - 422 → MissingProviderConfigError（必填配置缺失，客户端修配置后重试）
  - 424 → ProviderUpstreamError（provider 上游 4xx，客户端换 provider）
  - 5xx → MailRuntimeIncompatibleError（服务端运行态异常，客户端升级/重启服务端）
  - 401/404 → MailRuntimeIncompatibleError（鉴权 / 端点缺失）

服务端响应体 contract：
  { "detail": { "code": "<MACHINE_READABLE>",
                "message": "<人类可读>",
                "missing_fields": [...],     # 仅 PROVIDER_NOT_CONFIGURED
                "upstream_status": <int>     # 仅 PROVIDER_UPSTREAM_ERROR
  }}

本测试钉死：
  1. 异常类型分类正确
  2. 服务端 detail.code / missing_fields / upstream_status 透传到异常属性
  3. 4xx 不触发 _request_with_retry（status 不在 _RETRYABLE_STATUS_CODES）
  4. 不符合 contract 的响应体（旧服务端 / 网络层）能容忍降级
"""
from __future__ import annotations

import unittest
from unittest import mock

import requests

from src.providers.mail import (
    HttpMailProvider,
    MailRuntimeIncompatibleError,
    MailServiceError,
    MissingProviderConfigError,
    ProviderUpstreamError,
    _RETRYABLE_STATUS_CODES,
)


def _http_error_with_payload(status: int, payload: dict | None) -> requests.HTTPError:
    """构造带 JSON 响应体的 HTTPError，模拟服务端 contract 响应。"""
    response = mock.MagicMock(spec=requests.Response)
    response.status_code = status
    if payload is None:
        response.json.side_effect = ValueError("no json")
    else:
        response.json.return_value = payload
    err = requests.HTTPError(f"{status} Server Error", response=response)
    return err


class TestExtractErrorPayload(unittest.TestCase):
    """_extract_error_payload 工具：从服务端 4xx 响应体提取 contract 字段。"""

    def test_full_contract_422(self):
        response = mock.MagicMock(spec=requests.Response)
        response.json.return_value = {
            "detail": {
                "code": "PROVIDER_NOT_CONFIGURED",
                "message": "managed session for cfworker requires config_name",
                "missing_fields": ["config_name", "cfworker_api_url"],
            }
        }
        code, msg, missing, upstream = HttpMailProvider._extract_error_payload(response)
        self.assertEqual(code, "PROVIDER_NOT_CONFIGURED")
        self.assertEqual(msg, "managed session for cfworker requires config_name")
        self.assertEqual(missing, ["config_name", "cfworker_api_url"])
        self.assertEqual(upstream, 0)

    def test_full_contract_424(self):
        response = mock.MagicMock(spec=requests.Response)
        response.json.return_value = {
            "detail": {
                "code": "PROVIDER_UPSTREAM_ERROR",
                "message": "CF Worker API returned 400: Invalid domain",
                "upstream_status": 400,
            }
        }
        code, msg, missing, upstream = HttpMailProvider._extract_error_payload(response)
        self.assertEqual(code, "PROVIDER_UPSTREAM_ERROR")
        self.assertEqual(upstream, 400)
        self.assertEqual(missing, [])

    def test_legacy_response_body_tolerated(self):
        """旧服务端裸 'Internal Server Error' 字符串不破坏分类。"""
        response = mock.MagicMock(spec=requests.Response)
        response.json.side_effect = ValueError("not json")
        code, msg, missing, upstream = HttpMailProvider._extract_error_payload(response)
        self.assertEqual(code, "")
        self.assertEqual(msg, "")
        self.assertEqual(missing, [])
        self.assertEqual(upstream, 0)

    def test_none_response_returns_empty(self):
        code, msg, missing, upstream = HttpMailProvider._extract_error_payload(None)
        self.assertEqual((code, msg, missing, upstream), ("", "", [], 0))

    def test_payload_not_dict_tolerated(self):
        response = mock.MagicMock(spec=requests.Response)
        response.json.return_value = ["array", "not", "object"]
        code, _, _, _ = HttpMailProvider._extract_error_payload(response)
        self.assertEqual(code, "")


class TestRaiseRuntimeRequestError(unittest.TestCase):
    """_raise_runtime_request_error 错误分类核心契约。"""

    def setUp(self):
        self.provider = HttpMailProvider(base_url="http://example.test", api_key="k")

    def _raise(self, status: int, payload: dict | None = None):
        exc = _http_error_with_payload(status, payload)
        self.provider._raise_runtime_request_error(
            exc=exc, endpoint="managed-sessions", operation="创建邮箱会话",
        )

    # ── 422 PROVIDER_NOT_CONFIGURED ──

    def test_422_with_full_contract_raises_MissingProviderConfigError(self):
        with self.assertRaises(MissingProviderConfigError) as ctx:
            self._raise(422, {
                "detail": {
                    "code": "PROVIDER_NOT_CONFIGURED",
                    "message": "managed session for cfworker requires config_name",
                    "missing_fields": ["config_name"],
                }
            })
        self.assertEqual(ctx.exception.error_code, "PROVIDER_NOT_CONFIGURED")
        self.assertEqual(ctx.exception.missing_fields, ["config_name"])
        self.assertIn("managed session for cfworker", str(ctx.exception))

    def test_422_legacy_no_contract_still_classified_correctly(self):
        """旧服务端 422 但响应体没 contract → 仍当 MissingProviderConfigError，但带兜底 message。"""
        with self.assertRaises(MissingProviderConfigError) as ctx:
            self._raise(422, None)
        # 兜底 message 应当包含"必填 provider 配置字段缺失"或类似
        self.assertIn("必填", str(ctx.exception))
        self.assertEqual(ctx.exception.error_code, "PROVIDER_NOT_CONFIGURED")
        self.assertEqual(ctx.exception.missing_fields, [])

    def test_422_is_subclass_of_MailServiceError(self):
        """MissingProviderConfigError 必须能被 except MailServiceError 接住，
        以便 src/mail.py:_poll_code_via_api 的 except RuntimeError 路径正确处理。"""
        try:
            self._raise(422, None)
        except MailServiceError:
            pass
        else:
            self.fail("MissingProviderConfigError 应当能被 MailServiceError catch")

    # ── 424 PROVIDER_UPSTREAM_ERROR ──

    def test_424_with_full_contract_raises_ProviderUpstreamError(self):
        with self.assertRaises(ProviderUpstreamError) as ctx:
            self._raise(424, {
                "detail": {
                    "code": "PROVIDER_UPSTREAM_ERROR",
                    "message": "CF Worker API returned 400: Invalid domain",
                    "upstream_status": 400,
                }
            })
        self.assertEqual(ctx.exception.error_code, "PROVIDER_UPSTREAM_ERROR")
        self.assertEqual(ctx.exception.upstream_status, 400)

    def test_424_no_contract_falls_back_to_default(self):
        with self.assertRaises(ProviderUpstreamError) as ctx:
            self._raise(424, None)
        self.assertEqual(ctx.exception.upstream_status, 0)
        self.assertIn("上游", str(ctx.exception))

    # ── 5xx 仍走 MailRuntimeIncompatibleError ──

    def test_500_raises_MailRuntimeIncompatibleError(self):
        with self.assertRaises(MailRuntimeIncompatibleError):
            self._raise(500, None)

    def test_503_raises_MailRuntimeIncompatibleError(self):
        with self.assertRaises(MailRuntimeIncompatibleError):
            self._raise(503, None)

    # ── 401 / 404 保留 ──

    def test_401_raises_MailRuntimeIncompatibleError(self):
        with self.assertRaises(MailRuntimeIncompatibleError) as ctx:
            self._raise(401, None)
        self.assertIn("鉴权", str(ctx.exception))

    def test_404_raises_MailRuntimeIncompatibleError(self):
        with self.assertRaises(MailRuntimeIncompatibleError) as ctx:
            self._raise(404, None)
        self.assertIn("缺少", str(ctx.exception))

    # ── 400 视为 ProviderUpstreamError（CFWorker / Stripe / 上游 4xx） ──

    def test_400_classified_as_ProviderUpstreamError(self):
        """400 = 上游 provider 客户端语义错误，可恢复，应归 ProviderUpstreamError 而非 fatal。

        历史背景：曾经 400 走兜底 MailRuntimeIncompatibleError 导致整个自动化任务死掉。
        实际场景：CFWorker /admin/new_address 在地址已存在时返回 400，应让 triage 走
        '换邮箱/换 provider' 可恢复路径。远端契约升级后会改为 424 + 结构化 detail，
        本测试覆盖兜底兼容路径。
        """
        with self.assertRaises(ProviderUpstreamError) as ctx:
            self._raise(400, None)
        self.assertEqual(ctx.exception.upstream_status, 400)

    def test_400_with_already_exists_body_preserves_message(self):
        """400 + 远端裸文本响应（无 detail contract）也应归 ProviderUpstreamError，
        且原始 message 应进 exception 文本，方便运维定位（不能因 server_message 为空就吞掉）。"""
        # 模拟 CFWorker 那种 raw text body
        exc = requests.RequestException("400 Client Error")
        response = mock.MagicMock(spec=requests.Response)
        response.status_code = 400
        response.json.side_effect = ValueError("not json")
        response.text = "Failed to create address: Address already exists"
        exc.response = response
        with self.assertRaises(ProviderUpstreamError) as ctx:
            self.provider._raise_runtime_request_error(
                exc=exc, endpoint="managed-sessions", operation="创建邮箱会话",
            )
        self.assertEqual(ctx.exception.upstream_status, 400)
        self.assertIn("Address already exists", str(ctx.exception))

    def test_400_is_subclass_of_MailServiceError(self):
        """ProviderUpstreamError 必须能被 except MailServiceError 接住，与 422/424 一致。"""
        try:
            self._raise(400, None)
        except MailServiceError:
            pass
        else:
            self.fail("ProviderUpstreamError 应当能被 MailServiceError catch")


class TestRetryRespectsNew4xxClassification(unittest.TestCase):
    """4xx 不触发重试（_RETRYABLE_STATUS_CODES 只含 5xx）。

    这是架构正确性关键 — 如果 4xx 也重试，客户端会拿稳态故障空转 110s。
    """

    def test_422_not_in_retryable(self):
        self.assertNotIn(422, _RETRYABLE_STATUS_CODES)

    def test_424_not_in_retryable(self):
        self.assertNotIn(424, _RETRYABLE_STATUS_CODES)

    def test_5xx_still_retryable(self):
        for code in (500, 502, 503, 504):
            self.assertIn(code, _RETRYABLE_STATUS_CODES)


class TestCreateSessionFailFast4xx(unittest.TestCase):
    """端到端：create_session 收到 4xx 时立即 fail-fast，不重试不空转。"""

    def setUp(self):
        self.provider = HttpMailProvider(base_url="http://example.test", api_key="k")

    @mock.patch("time.sleep", new=lambda *_: None)
    @mock.patch("src.providers.mail.requests.post")
    def test_managed_session_422_no_retry(self, mock_post):
        """managed-sessions 路径 422 → MissingProviderConfigError + 只调一次（不重试）。"""
        bad_resp = mock.MagicMock(spec=requests.Response)
        bad_resp.status_code = 422
        bad_resp.json.return_value = {
            "detail": {
                "code": "PROVIDER_NOT_CONFIGURED",
                "message": "config_name required",
                "missing_fields": ["config_name"],
            }
        }
        bad_resp.raise_for_status.side_effect = requests.HTTPError(
            "422 Unprocessable Entity", response=bad_resp,
        )
        mock_post.return_value = bad_resp

        with self.assertRaises(MissingProviderConfigError) as ctx:
            self.provider.create_session("cfworker", session_mode="managed")
        self.assertEqual(ctx.exception.missing_fields, ["config_name"])
        # 关键：4xx 不重试，只调一次
        self.assertEqual(mock_post.call_count, 1)

    @mock.patch("time.sleep", new=lambda *_: None)
    @mock.patch("src.providers.mail.requests.post")
    def test_managed_session_424_no_retry(self, mock_post):
        """managed-sessions 424 → ProviderUpstreamError + 不重试。"""
        bad_resp = mock.MagicMock(spec=requests.Response)
        bad_resp.status_code = 424
        bad_resp.json.return_value = {
            "detail": {
                "code": "PROVIDER_UPSTREAM_ERROR",
                "message": "CF Worker rejected: Invalid domain",
                "upstream_status": 400,
            }
        }
        bad_resp.raise_for_status.side_effect = requests.HTTPError(
            "424 Failed Dependency", response=bad_resp,
        )
        mock_post.return_value = bad_resp

        with self.assertRaises(ProviderUpstreamError) as ctx:
            self.provider.create_session("cfworker", session_mode="managed")
        self.assertEqual(ctx.exception.upstream_status, 400)
        self.assertEqual(mock_post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
