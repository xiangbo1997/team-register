# -*- coding: utf-8 -*-
"""diagnose_borrow.py 的 cURL 解析逻辑测试。

不测试实际网络调用部分（那需要真 access_token + cf_clearance），
只覆盖 parse_curl 与脱敏函数：
- 单行 cURL
- 多行 backslash + 换行
- 缺失 Authorization
- header / cookie 大小写
- value 中带特殊字符
"""

import unittest

# scripts/ 不在 Python 包路径里，用动态加载
import sys
from pathlib import Path
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))
import diagnose_borrow  # noqa: E402


class ParseCurlTests(unittest.TestCase):
    def test_extract_url(self):
        text = "curl 'https://chatgpt.com/backend-api/me' -H 'a: b'"
        parsed = diagnose_borrow.parse_curl(text)
        self.assertEqual(parsed["url"], "https://chatgpt.com/backend-api/me")

    def test_extract_authorization_bearer(self):
        text = "curl 'https://x.com' -H 'authorization: Bearer eyJfake_token_xyz'"
        parsed = diagnose_borrow.parse_curl(text)
        self.assertEqual(parsed["access_token"], "eyJfake_token_xyz")

    def test_no_authorization_returns_empty_token(self):
        text = "curl 'https://x.com' -H 'accept: */*'"
        parsed = diagnose_borrow.parse_curl(text)
        self.assertEqual(parsed["access_token"], "")

    def test_header_names_lowercased(self):
        text = "curl 'https://x.com' -H 'X-OAI-IS: ois1.abc' -H 'Authorization: Bearer t'"
        parsed = diagnose_borrow.parse_curl(text)
        self.assertIn("x-oai-is", parsed["headers"])
        self.assertEqual(parsed["headers"]["x-oai-is"], "ois1.abc")
        # access_token 不受 header 名大小写影响
        self.assertEqual(parsed["access_token"], "t")

    def test_cookies_parsed_from_b_flag(self):
        text = (
            "curl 'https://x.com' "
            "-b 'cf_clearance=cfc-1; __Secure-next-auth.session-token=st-1; oai-did=did-1'"
        )
        parsed = diagnose_borrow.parse_curl(text)
        self.assertEqual(parsed["cookies"]["cf_clearance"], "cfc-1")
        self.assertEqual(parsed["cookies"]["__Secure-next-auth.session-token"], "st-1")
        self.assertEqual(parsed["cookies"]["oai-did"], "did-1")

    def test_multiline_backslash_format(self):
        """DevTools "Copy as cURL (bash)" 多行 \\ 换行格式"""
        text = """curl 'https://chatgpt.com/backend-api/me' \\
          -H 'accept: */*' \\
          -H 'authorization: Bearer fake-tok' \\
          -H 'x-oai-is: ois1.value' \\
          -b 'cf_clearance=cfc; oai-did=did-1'"""
        parsed = diagnose_borrow.parse_curl(text)
        self.assertEqual(parsed["url"], "https://chatgpt.com/backend-api/me")
        self.assertEqual(parsed["access_token"], "fake-tok")
        self.assertEqual(parsed["headers"]["x-oai-is"], "ois1.value")
        self.assertEqual(parsed["cookies"]["cf_clearance"], "cfc")

    def test_empty_string_input(self):
        parsed = diagnose_borrow.parse_curl("")
        self.assertEqual(parsed["url"], "")
        self.assertEqual(parsed["headers"], {})
        self.assertEqual(parsed["cookies"], {})
        self.assertEqual(parsed["access_token"], "")

    def test_cookies_value_with_equals(self):
        """cookie value 里包含 = 号（很常见）"""
        text = "curl 'https://x.com' -b 'token=abc=def=ghi; other=v'"
        parsed = diagnose_borrow.parse_curl(text)
        self.assertEqual(parsed["cookies"]["token"], "abc=def=ghi")
        self.assertEqual(parsed["cookies"]["other"], "v")


class MaskTests(unittest.TestCase):
    def test_short_value_not_masked(self):
        self.assertEqual(diagnose_borrow._mask("short", head=8, tail=8), "short")

    def test_long_value_masked(self):
        long_token = "a" * 50
        masked = diagnose_borrow._mask(long_token, head=8, tail=8)
        self.assertIn("...", masked)
        self.assertIn("(len=50)", masked)

    def test_empty_value_returns_empty(self):
        self.assertEqual(diagnose_borrow._mask(""), "")


if __name__ == "__main__":
    unittest.main()
