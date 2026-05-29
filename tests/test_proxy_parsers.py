# -*- coding: utf-8 -*-
"""parse_proxy_response 单元测试（src/proxy_clients/parsers.py）。

覆盖：
  - txt_line：单行 / 多行 / 空响应 / 含空行 / 非数字端口跳过 / IPv6 边界
  - json_array_host_port：标准格式 / port 为 int / 缺字段过滤 / 非数组 / 非 JSON
  - 未支持格式抛 ValueError
"""

from __future__ import annotations

import unittest

from src.proxy_clients.parsers import parse_proxy_response


class TestParseProxyResponseTxtLine(unittest.TestCase):
    def test_single_line(self):
        out = parse_proxy_response("1.2.3.4:8080", "txt_line")
        self.assertEqual(out, [("1.2.3.4", "8080")])

    def test_multi_line(self):
        out = parse_proxy_response("1.2.3.4:8080\n5.6.7.8:9090", "txt_line")
        self.assertEqual(out, [("1.2.3.4", "8080"), ("5.6.7.8", "9090")])

    def test_empty_string(self):
        self.assertEqual(parse_proxy_response("", "txt_line"), [])

    def test_whitespace_only(self):
        self.assertEqual(parse_proxy_response("   \n\n  \n", "txt_line"), [])

    def test_skip_blank_lines(self):
        out = parse_proxy_response("1.1.1.1:80\n\n  \n2.2.2.2:443", "txt_line")
        self.assertEqual(out, [("1.1.1.1", "80"), ("2.2.2.2", "443")])

    def test_skip_no_colon(self):
        out = parse_proxy_response("garbage_line\n1.1.1.1:80", "txt_line")
        self.assertEqual(out, [("1.1.1.1", "80")])

    def test_skip_non_numeric_port(self):
        # 防 1024 返 HTML 错误页时误抽
        out = parse_proxy_response("1.1.1.1:abc\n2.2.2.2:80", "txt_line")
        self.assertEqual(out, [("2.2.2.2", "80")])


class TestParseProxyResponseJsonArray(unittest.TestCase):
    def test_standard(self):
        out = parse_proxy_response(
            '[{"host": "1.2.3.4", "port": "8080"}]', "json_array_host_port",
        )
        self.assertEqual(out, [("1.2.3.4", "8080")])

    def test_port_as_int(self):
        out = parse_proxy_response(
            '[{"host": "1.2.3.4", "port": 8080}]', "json_array_host_port",
        )
        self.assertEqual(out, [("1.2.3.4", "8080")])

    def test_multiple_items(self):
        out = parse_proxy_response(
            '[{"host": "1.1.1.1", "port": 80}, {"host": "2.2.2.2", "port": "443"}]',
            "json_array_host_port",
        )
        self.assertEqual(out, [("1.1.1.1", "80"), ("2.2.2.2", "443")])

    def test_skip_missing_fields(self):
        out = parse_proxy_response(
            '[{"host": "ok", "port": "80"}, {"host": "no_port"}, {"port": 80}, {}]',
            "json_array_host_port",
        )
        self.assertEqual(out, [("ok", "80")])

    def test_accepts_ip_alias(self):
        # 部分供应商把 host 字段叫 ip
        out = parse_proxy_response(
            '[{"ip": "1.1.1.1", "port": 80}]', "json_array_host_port",
        )
        self.assertEqual(out, [("1.1.1.1", "80")])

    def test_non_json(self):
        self.assertEqual(
            parse_proxy_response("not json", "json_array_host_port"), [],
        )

    def test_non_array(self):
        self.assertEqual(
            parse_proxy_response('{"host": "1.1.1.1", "port": 80}', "json_array_host_port"),
            [],
        )

    def test_empty_array(self):
        self.assertEqual(
            parse_proxy_response("[]", "json_array_host_port"), [],
        )


class TestParseProxyResponseInvalidFormat(unittest.TestCase):
    def test_unsupported_format_raises(self):
        with self.assertRaises(ValueError) as ctx:
            parse_proxy_response("anything", "yaml_array")
        self.assertIn("不支持的 response_format", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
