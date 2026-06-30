# -*- coding: utf-8 -*-
"""ProviderRegistry 单元测试。

覆盖：
- discover() 自动扫描子目录并注册
- list_kinds / get_meta 基础查询
- validate 必填字段 + alias 校验
- build kind 未注册 / 缺必填字段 / 别名映射 / 多余字段过滤
- build_active 从 ProviderConfig 行构造
- build_all_active 多 active 行 + 失败行不阻塞其它
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from src.providers import (
    FieldSpec,
    ProviderConfigInvalidError,
    ProviderNotRegisteredError,
    ProviderRegistry,
    get_registry,
    register_provider,
)
from src.providers.card import CardProvider


class TestRegistryAutoDiscovery(unittest.TestCase):
    """启动时 discover() 应能自动找到内置三类 provider。"""

    def test_card_kinds_discovered(self):
        kinds = get_registry().list_kinds("card")
        self.assertIn("efuncard", kinds)
        self.assertIn("nodecard", kinds)
        self.assertIn("x988card", kinds)

    def test_mail_kinds_discovered(self):
        kinds = get_registry().list_kinds("mail")
        self.assertIn("cfworker", kinds)
        self.assertIn("outlook_email_plus", kinds)

    def test_browser_kinds_discovered(self):
        kinds = get_registry().list_kinds("browser")
        self.assertIn("adspower", kinds)

    def test_get_meta_returns_full_metadata(self):
        meta = get_registry().get_meta("card", "x988card")
        self.assertIsNotNone(meta)
        self.assertEqual(meta.provider_type, "card")
        self.assertEqual(meta.kind, "x988card")
        self.assertTrue(meta.display_name)


class TestRegistryValidate(unittest.TestCase):

    def test_validate_unknown_kind_returns_empty(self):
        result = get_registry().validate("card", "not-registered", {})
        self.assertEqual(result, [])

    def test_validate_missing_required_field(self):
        # efuncard.token 是 required
        result = get_registry().validate("card", "efuncard", {})
        self.assertIn("token", result)

    def test_validate_alias_satisfies_required(self):
        # config 用历史 alias efuncard_token，registry 应识别为满足 token 必填
        result = get_registry().validate("card", "efuncard", {"efuncard_token": "abc"})
        self.assertEqual(result, [])

    def test_validate_required_field_present(self):
        result = get_registry().validate("card", "efuncard", {"token": "abc"})
        self.assertEqual(result, [])


class TestRegistryBuild(unittest.TestCase):

    def test_build_unknown_kind_raises(self):
        with self.assertRaises(ProviderNotRegisteredError):
            get_registry().build("card", "nonexistent-kind", {})

    def test_build_missing_required_raises(self):
        with self.assertRaises(ProviderConfigInvalidError) as ctx:
            get_registry().build("card", "efuncard", {})
        self.assertIn("token", ctx.exception.missing_fields)

    def test_build_returns_correct_instance(self):
        instance = get_registry().build("card", "efuncard", {"token": "dummy"})
        from src.providers.cards.efuncard import EfunCardProvider
        self.assertIsInstance(instance, EfunCardProvider)

    def test_build_extra_kwargs_filtered(self):
        # 多余字段（如前端塞进来的 _csrf）应被自动过滤而不是 TypeError
        instance = get_registry().build(
            "card", "efuncard",
            {"token": "dummy", "_csrf": "xxx", "unknown_field": 1},
        )
        self.assertIsNotNone(instance)

    def test_build_alias_renamed(self):
        # 用 alias 名 efuncard_token 也能构造成功（registry 内部重命名为 token）
        instance = get_registry().build(
            "card", "efuncard",
            {"efuncard_token": "dummy"},
        )
        self.assertIsNotNone(instance)


class TestBuildActive(unittest.TestCase):

    def _mock_config_service(self, active_row=None, list_rows=None):
        cs = MagicMock()
        cs.get_active_provider.return_value = active_row
        cs.list_active_providers.return_value = list_rows or []
        return cs

    def _make_pc(self, provider_name: str, config: dict, provider_type: str = "card"):
        pc = MagicMock()
        pc.provider_name = provider_name
        pc.provider_type = provider_type
        pc.config = config
        return pc

    def test_build_active_none_when_no_row(self):
        cs = self._mock_config_service(active_row=None)
        result = get_registry().build_active("card", cs)
        self.assertIsNone(result)

    def test_build_active_extracts_kind_from_driver_key(self):
        # card 类约定 kind 在 config['driver']
        pc = self._make_pc("efuncard-default", {"driver": "efuncard", "token": "x"})
        cs = self._mock_config_service(active_row=pc)
        instance = get_registry().build_active("card", cs)
        from src.providers.cards.efuncard import EfunCardProvider
        self.assertIsInstance(instance, EfunCardProvider)

    def test_build_active_extras_merged_dont_override_db(self):
        # extras 字段只补 DB 没有的；DB 已有的优先
        pc = self._make_pc("efun-x", {"driver": "efuncard", "token": "db_token"})
        cs = self._mock_config_service(active_row=pc)
        instance = get_registry().build_active(
            "card", cs,
            extras={"token": "extras_should_lose", "base_url": "http://override"},
        )
        # token 用 DB 的（虽然没法直接验内部 _client.token，至少不抛错）
        self.assertIsNotNone(instance)

    def test_build_all_active_skips_invalid_rows(self):
        # 一行注册的 kind + 一行未注册 kind，registry 应跳过坏的不影响好的
        good = self._make_pc("good", {"driver": "efuncard", "token": "x"})
        bad = self._make_pc("bad", {"driver": "ghost-driver", "token": "y"})
        cs = self._mock_config_service(list_rows=[good, bad])
        instances = get_registry().build_all_active("card", cs)
        self.assertEqual(len(instances), 1)


class TestRegistryIdempotent(unittest.TestCase):
    """重复 import / 重复装饰器调用不应产生重复注册。"""

    def test_double_discover_no_growth(self):
        before = len(get_registry()._classes)
        get_registry().discover(force=True)
        after = len(get_registry()._classes)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
