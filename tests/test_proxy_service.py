# -*- coding: utf-8 -*-
"""代理池服务测试：CRUD + URL 归一化 + 引用检查。"""

import unittest

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import src.db.engine as engine_mod
from src.db.engine import get_session
from src.db.models import LinkTemplate, Proxy
from src.services.proxy_service import (
    _normalize_proxy_url,
    count_templates_using_proxy,
    create_proxy,
    delete_proxy,
    get_proxy,
    list_proxies,
    update_proxy,
)


def _build_threadsafe_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


class NormalizeProxyUrlTest(unittest.TestCase):
    """URL 归一化：支持 socks5h:// / http:// / 4 段冒号"""

    def test_complete_socks5h_url_kept_as_is(self):
        url = "socks5h://user:pwd@host.com:1080"
        self.assertEqual(_normalize_proxy_url(url), url)

    def test_complete_http_url_kept_as_is(self):
        url = "http://pool_b:J0Xkr7AIka7@173.254.207.117:7921"
        self.assertEqual(_normalize_proxy_url(url), url)

    def test_complete_https_url_kept_as_is(self):
        url = "https://user:pwd@host.com:443"
        self.assertEqual(_normalize_proxy_url(url), url)

    def test_four_part_colon_converted_to_socks5h(self):
        raw = "us.1024proxy.io:3000:wruz20033-region-CA-st-Alberta-city-Edmonton-sid-K2kjHCzp-t-5:evrscsuk"
        out = _normalize_proxy_url(raw)
        self.assertTrue(out.startswith("socks5h://"))
        self.assertIn("@us.1024proxy.io:3000", out)
        # 用户名/密码做了 URL 编码（这里 ASCII 字母数字 + 短横保持原样）
        self.assertIn("wruz20033-region-CA-st-Alberta", out)
        self.assertIn("evrscsuk", out)

    def test_special_chars_in_password_url_encoded(self):
        """密码含 @ : / 等特殊字符要 URL 编码，否则会破坏 URL 结构"""
        raw = "host:1080:user:p@ss/word"
        out = _normalize_proxy_url(raw)
        # @ → %40, / → %2F
        self.assertIn("%40", out)
        self.assertIn("%2F", out)
        # host 部分保持完整
        self.assertIn("@host:1080", out)

    def test_empty_input_raises(self):
        with self.assertRaises(ValueError):
            _normalize_proxy_url("")
        with self.assertRaises(ValueError):
            _normalize_proxy_url("   ")

    def test_unrecognized_format_raises(self):
        with self.assertRaises(ValueError) as cm:
            _normalize_proxy_url("just-some-random-string")
        self.assertIn("代理格式无法识别", str(cm.exception))

    def test_four_part_with_non_numeric_port_raises(self):
        with self.assertRaises(ValueError) as cm:
            _normalize_proxy_url("host:abc:user:pwd")
        self.assertIn("端口必须是数字", str(cm.exception))

    def test_four_part_with_empty_segments_raises(self):
        with self.assertRaises(ValueError):
            _normalize_proxy_url("host:1080::pwd")


class ProxyServiceTest(unittest.TestCase):
    """CRUD + 引用检查"""

    def setUp(self):
        self.engine = _build_threadsafe_engine()
        self._original = engine_mod._engine
        engine_mod._engine = self.engine

    def tearDown(self):
        engine_mod._engine = self._original

    def test_create_and_list(self):
        created = create_proxy(
            label="1024Proxy CA #1",
            url="socks5h://user:pwd@host.com:1080",
            country="CA",
            notes="datroai CA promo 用",
        )
        self.assertIsNotNone(created["id"])
        self.assertEqual(created["label"], "1024Proxy CA #1")
        self.assertEqual(created["country"], "CA")
        # 列表返回脱敏 url（不含明文密码）
        rows = list_proxies()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("pwd", rows[0].get("url_masked", ""))
        # list 默认不含明文 url
        self.assertNotIn("url", rows[0])

    def test_create_label_unique(self):
        create_proxy(label="dup", url="socks5h://a:b@h:1")
        with self.assertRaises(ValueError) as cm:
            create_proxy(label="dup", url="socks5h://c:d@h:2")
        self.assertIn("已存在", str(cm.exception))

    def test_get_with_url_returns_plaintext_for_internal_use(self):
        """generate-link 路由内部要拿到完整 URL → with_url=True"""
        created = create_proxy(label="get-test", url="socks5h://u:p@h:1080")
        result = get_proxy(int(created["id"]), with_url=True)
        self.assertIsNotNone(result)
        self.assertIn("url", result)
        self.assertEqual(result["url"], "socks5h://u:p@h:1080")

    def test_get_without_url_default(self):
        """默认 get 不返回明文 url（API 响应默认走这条）"""
        created = create_proxy(label="get-default", url="socks5h://u:p@h:1080")
        result = get_proxy(int(created["id"]))
        self.assertIsNotNone(result)
        self.assertNotIn("url", result)
        self.assertIn("url_masked", result)

    def test_update_toggles_is_active(self):
        created = create_proxy(label="toggle", url="socks5h://u:p@h:1")
        updated = update_proxy(int(created["id"]), is_active=False)
        self.assertFalse(updated["is_active"])
        # 再切回
        updated = update_proxy(int(created["id"]), is_active=True)
        self.assertTrue(updated["is_active"])

    def test_update_cannot_change_url(self):
        """update 函数签名根本不含 url 参数 → 改 url 只能删了重建"""
        created = create_proxy(label="immut-url", url="socks5h://u:p@h:1")
        # 通过 with_url=True 拿到原 url
        before = get_proxy(int(created["id"]), with_url=True)["url"]
        update_proxy(int(created["id"]), notes="改改备注")
        after = get_proxy(int(created["id"]), with_url=True)["url"]
        self.assertEqual(before, after)

    def test_delete_proxy_no_refs(self):
        created = create_proxy(label="to-del", url="socks5h://u:p@h:1")
        ok, msg = delete_proxy(int(created["id"]))
        self.assertTrue(ok)
        self.assertEqual(msg, "")
        # 二次删 → False（不存在）
        ok, msg = delete_proxy(int(created["id"]))
        self.assertFalse(ok)
        self.assertIn("不存在", msg)

    def test_delete_proxy_rejected_when_referenced_by_template(self):
        """被 LinkTemplate.proxy_id 引用时拒绝删除，返回引用清单提示。"""
        created = create_proxy(label="busy", url="socks5h://u:p@h:1")
        # 直接造个 LinkTemplate 引用它
        with get_session() as s:
            s.add(LinkTemplate(name="tpl-using-proxy", plan="team", proxy_id=created["id"]))
            s.commit()

        ok, msg = delete_proxy(int(created["id"]))
        self.assertFalse(ok)
        self.assertIn("tpl-using-proxy", msg)

    def test_count_templates_using_proxy(self):
        created = create_proxy(label="ref-count", url="socks5h://u:p@h:1")
        self.assertEqual(count_templates_using_proxy(int(created["id"])), 0)
        with get_session() as s:
            s.add(LinkTemplate(name="t1", plan="team", proxy_id=created["id"]))
            s.add(LinkTemplate(name="t2", plan="plus", proxy_id=created["id"]))
            s.commit()
        self.assertEqual(count_templates_using_proxy(int(created["id"])), 2)


if __name__ == "__main__":
    unittest.main()
