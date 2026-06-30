# -*- coding: utf-8 -*-
"""HttpMailProvider 5xx / 网络错误自动重试单元测试

覆盖：
  - 5xx 触发重试，最终成功
  - 5xx 三次全失败，按现有错误分类抛出
  - 4xx 不重试，立即抛出
  - ConnectionError 触发重试
  - 退避时间符合 _RETRY_BACKOFF_SECONDS 配置

设计原则：
  - 用 sleep_fn=lambda 注入跳过真实等待，单元测试保持 <1s 完成
  - 直接调用 _request_with_retry 验证内部行为（白盒）
  - 也保留一个端到端 test 验证 create_session 路径
"""

import unittest
from unittest import mock

import requests

from src.providers.mail import (
    HttpMailProvider,
    MailRuntimeIncompatibleError,
    MailServiceError,
    _RETRY_BACKOFF_SECONDS,
    _RETRY_MAX_ATTEMPTS,
)


def _http_error(status_code: int) -> requests.HTTPError:
    """构造带 response.status_code 的 HTTPError，模拟 raise_for_status() 抛出。"""
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status_code
    err = requests.HTTPError(f"{status_code} Server Error", response=resp)
    return err


class TestRequestWithRetry(unittest.TestCase):
    """_request_with_retry 白盒测试"""

    def setUp(self):
        self.provider = HttpMailProvider(base_url="http://example.test", api_key="k")
        self.sleep_calls: list[float] = []

    def _record_sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)

    def test_502_then_success_returns_response(self):
        """5xx → 重试一次 → 成功，应返回 ok 响应"""
        ok_resp = mock.MagicMock(spec=requests.Response)
        ok_resp.status_code = 200
        request_fn = mock.MagicMock(side_effect=[_http_error(502), ok_resp])

        result = self.provider._request_with_retry(
            request_fn, operation="test", sleep_fn=self._record_sleep,
        )

        self.assertIs(result, ok_resp)
        self.assertEqual(request_fn.call_count, 2)
        self.assertEqual(self.sleep_calls, [_RETRY_BACKOFF_SECONDS[0]])

    def test_all_5xx_raises_after_all_attempts(self):
        """N 次 5xx 全失败 → 抛出最后一次异常，不再继续。

        N == _RETRY_MAX_ATTEMPTS（参数化，常量改了不用动测试）。
        """
        request_fn = mock.MagicMock(side_effect=[_http_error(503)] * _RETRY_MAX_ATTEMPTS)

        with self.assertRaises(requests.HTTPError) as ctx:
            self.provider._request_with_retry(
                request_fn, operation="test", sleep_fn=self._record_sleep,
            )

        self.assertEqual(ctx.exception.response.status_code, 503)
        self.assertEqual(request_fn.call_count, _RETRY_MAX_ATTEMPTS)
        # 重试前的 sleep 调用 = attempts - 1，恰好覆盖整个 _RETRY_BACKOFF_SECONDS
        self.assertEqual(self.sleep_calls, list(_RETRY_BACKOFF_SECONDS))

    def test_401_not_retried(self):
        """4xx → 不重试，立即抛出"""
        request_fn = mock.MagicMock(side_effect=_http_error(401))

        with self.assertRaises(requests.HTTPError):
            self.provider._request_with_retry(
                request_fn, operation="test", sleep_fn=self._record_sleep,
            )

        self.assertEqual(request_fn.call_count, 1)
        self.assertEqual(self.sleep_calls, [])

    def test_404_not_retried(self):
        """404 也不重试（端点缺失，重试无意义）"""
        request_fn = mock.MagicMock(side_effect=_http_error(404))

        with self.assertRaises(requests.HTTPError):
            self.provider._request_with_retry(
                request_fn, operation="test", sleep_fn=self._record_sleep,
            )

        self.assertEqual(request_fn.call_count, 1)

    def test_connection_error_retried(self):
        """ConnectionError 视为网络抖动，触发重试"""
        ok_resp = mock.MagicMock(spec=requests.Response)
        ok_resp.status_code = 200
        request_fn = mock.MagicMock(side_effect=[
            requests.ConnectionError("dns fail"), ok_resp,
        ])

        result = self.provider._request_with_retry(
            request_fn, operation="test", sleep_fn=self._record_sleep,
        )

        self.assertIs(result, ok_resp)
        self.assertEqual(request_fn.call_count, 2)
        self.assertEqual(self.sleep_calls, [_RETRY_BACKOFF_SECONDS[0]])

    def test_timeout_retried(self):
        """Timeout 也视为网络抖动"""
        ok_resp = mock.MagicMock(spec=requests.Response)
        ok_resp.status_code = 200
        request_fn = mock.MagicMock(side_effect=[
            requests.Timeout("read timeout"), ok_resp,
        ])

        result = self.provider._request_with_retry(
            request_fn, operation="test", sleep_fn=self._record_sleep,
        )

        self.assertIs(result, ok_resp)
        self.assertEqual(request_fn.call_count, 2)


class TestCreateSessionRetry(unittest.TestCase):
    """端到端：create_session 在 5xx 后能恢复"""

    def setUp(self):
        self.provider = HttpMailProvider(base_url="http://example.test", api_key="k")

    @mock.patch("time.sleep", new=lambda *_: None)
    @mock.patch("src.providers.mail.requests.post")
    def test_managed_session_recovers_from_502(self, mock_post):
        """managed-sessions 路径：第一次 502，第二次成功"""
        # 第一次：502
        bad_resp = mock.MagicMock(spec=requests.Response)
        bad_resp.status_code = 502
        bad_resp.raise_for_status.side_effect = _http_error(502)

        # 第二次：200 + 正常 payload
        good_resp = mock.MagicMock(spec=requests.Response)
        good_resp.status_code = 200
        good_resp.raise_for_status.return_value = None
        good_resp.json.return_value = {
            "session_id": "s1",
            "lease_token": "t1",
            "email": "a@b.c",
            "provider": "applemail",
            "before_ids": [],
        }
        mock_post.side_effect = [bad_resp, good_resp]

        session = self.provider.create_session("applemail", session_mode="managed")

        self.assertEqual(session.session_id, "s1")
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("time.sleep", new=lambda *_: None)
    @mock.patch("src.providers.mail.requests.post")
    def test_managed_session_all_502s_raises_runtime_incompatible(self, mock_post):
        """N 次 502 全失败 → 抛 MailRuntimeIncompatibleError。

        架构变更：managed 路径的失败也走统一的 _raise_runtime_request_error，
        与 credentialed 路径对齐。5xx 仍归类成"运行态不兼容"（语义正确）。

        N == _RETRY_MAX_ATTEMPTS（参数化，常量改了不用动测试）。
        """
        from src.providers.mail import MailRuntimeIncompatibleError

        bad_resp = mock.MagicMock(spec=requests.Response)
        bad_resp.status_code = 502
        bad_resp.raise_for_status.side_effect = _http_error(502)
        mock_post.side_effect = [bad_resp] * _RETRY_MAX_ATTEMPTS

        with self.assertRaises(MailRuntimeIncompatibleError):
            self.provider.create_session("applemail", session_mode="managed")

        self.assertEqual(mock_post.call_count, _RETRY_MAX_ATTEMPTS)

    @mock.patch("time.sleep", new=lambda *_: None)
    @mock.patch("src.providers.mail.requests.post")
    def test_managed_session_400_not_retried_classified_as_provider_upstream(self, mock_post):
        """managed 路径 + 400 → 不重试，归 ProviderUpstreamError（**可恢复**）。

        架构变更（2026-04-28）：400 = 上游 provider（CFWorker / Stripe / Apple backend）
        客户端语义错误，应让 triage 走"换邮箱/换 provider"可恢复路径，而不是
        MailRuntimeIncompatibleError 那种 fatal。详见 src/providers/mail.py:_raise_runtime_request_error
        和 docs/research/chatgpt2api-vs-team-register.md 的相关讨论。

        4xx 仍然 not retried —— 这条契约不变。
        """
        from src.providers.mail import ProviderUpstreamError

        bad_resp = mock.MagicMock(spec=requests.Response)
        bad_resp.status_code = 400
        bad_resp.text = "Failed to create address: Address already exists"
        bad_resp.json.side_effect = ValueError("not json")
        bad_resp.raise_for_status.side_effect = _http_error(400)
        mock_post.return_value = bad_resp

        with self.assertRaises(ProviderUpstreamError) as ctx:
            self.provider.create_session("applemail", session_mode="managed")

        # 4xx 不重试
        self.assertEqual(mock_post.call_count, 1)
        # upstream_status 透传
        self.assertEqual(ctx.exception.upstream_status, 400)


class TestPollCodeRetry(unittest.TestCase):
    """poll_code 路径在 5xx 后能恢复"""

    def setUp(self):
        self.provider = HttpMailProvider(base_url="http://example.test", api_key="k")
        from src.providers.mail import MailSession
        self.session = MailSession(
            session_id="s1", lease_token="t1", email="a@b.c", provider="applemail",
        )

    @mock.patch("time.sleep", new=lambda *_: None)
    @mock.patch("src.providers.mail.requests.post")
    def test_poll_code_recovers_from_503(self, mock_post):
        bad_resp = mock.MagicMock(spec=requests.Response)
        bad_resp.status_code = 503
        bad_resp.raise_for_status.side_effect = _http_error(503)

        good_resp = mock.MagicMock(spec=requests.Response)
        good_resp.raise_for_status.return_value = None
        good_resp.json.return_value = {"status": "ready", "code": "123456"}
        mock_post.side_effect = [bad_resp, good_resp]

        code = self.provider.poll_code(self.session, timeout_seconds=5)

        self.assertEqual(code, "123456")
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("time.sleep", new=lambda *_: None)
    @mock.patch("src.providers.mail.requests.post")
    def test_poll_code_all_503s_raises_mail_service_error(self, mock_post):
        """N 次 503 全失败 → 抛 MailServiceError。

        N == _RETRY_MAX_ATTEMPTS（参数化，常量改了不用动测试）。
        """
        bad_resp = mock.MagicMock(spec=requests.Response)
        bad_resp.status_code = 503
        bad_resp.raise_for_status.side_effect = _http_error(503)
        mock_post.side_effect = [bad_resp] * _RETRY_MAX_ATTEMPTS

        with self.assertRaises(MailServiceError):
            self.provider.poll_code(self.session, timeout_seconds=5)

        self.assertEqual(mock_post.call_count, _RETRY_MAX_ATTEMPTS)


class TestRetryConstants(unittest.TestCase):
    """重试常量配置自身的契约测试。

    锁住"重试总等待时间足以跨过 cfworker 短窗故障"（实测 ~30s+）。
    历史：v1 是 (5,15) 共 20s 不够；v2 改成 (5,15,30,60) 共 110s。
    如果以后有人想缩短，必须先想清楚是否会让 warmup 重新撞 5xx 失败。
    """

    def test_max_attempts_at_least_5(self):
        self.assertGreaterEqual(
            _RETRY_MAX_ATTEMPTS, 5,
            "至少 5 次 attempts 才能跨过 cfworker / 上游 ~30s+ 短窗故障",
        )

    def test_backoff_length_matches_max_attempts(self):
        """退避节奏长度 = max_attempts - 1（最后一次失败不 sleep）。"""
        self.assertEqual(
            len(_RETRY_BACKOFF_SECONDS), _RETRY_MAX_ATTEMPTS - 1,
            "退避节奏数量必须正好覆盖 attempts-1 次重试间隔",
        )

    def test_total_backoff_window_covers_short_outage(self):
        """累计退避时长应覆盖 90s+ 的常见短窗故障。"""
        total = sum(_RETRY_BACKOFF_SECONDS)
        self.assertGreaterEqual(
            total, 90.0,
            f"累计退避 {total}s 不足 90s，无法跨过常见 cfworker 短窗故障",
        )


if __name__ == "__main__":
    unittest.main()
