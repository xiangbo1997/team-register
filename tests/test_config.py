# -*- coding: utf-8 -*-
"""配置模块单元测试"""

import os
import unittest
from unittest.mock import patch

from src.config import AppConfig, load_config


class TestAppConfig(unittest.TestCase):
    """AppConfig 数据类测试"""

    def test_default_values(self):
        """默认值应正确设置"""
        cfg = AppConfig()
        self.assertEqual(cfg.ads_api, "http://local.adspower.net:50325")
        self.assertEqual(cfg.sms_country, "6")
        self.assertEqual(cfg.efuncard_token, "")
        self.assertEqual(cfg.payment_plan, "team")
        self.assertFalse(cfg.payment_link_only)
        self.assertEqual(cfg.payment_link_return_mode, "long")
        self.assertEqual(cfg.aimizy_country, "SG")
        self.assertEqual(cfg.aimizy_currency, "SGD")
        self.assertEqual(cfg.billing_country, "US")
        self.assertEqual(cfg.billing_line1, "350 5th Ave")
        self.assertEqual(cfg.billing_city, "New York")
        self.assertEqual(cfg.billing_state, "NY")
        self.assertEqual(cfg.billing_postal_code, "10118")

    def test_validate_no_modules(self):
        """不指定模块时应返回空列表"""
        cfg = AppConfig()
        self.assertEqual(cfg.validate(), [])

    def test_validate_missing_efuncard(self):
        """efuncard_token 为空时应报告缺失"""
        cfg = AppConfig(efuncard_token="")
        missing = cfg.validate(required_modules=["efuncard"])
        self.assertIn("EFUNCARD_TOKEN", missing)

    def test_validate_efuncard_present(self):
        """efuncard_token 有值时无缺失"""
        cfg = AppConfig(efuncard_token="test-token")
        missing = cfg.validate(required_modules=["efuncard"])
        self.assertEqual(missing, [])

    def test_validate_missing_mail(self):
        """邮件模块缺失 token/client_id 时应全部报告"""
        cfg = AppConfig()
        missing = cfg.validate(required_modules=["mail"])
        self.assertEqual(len(missing), 2)
        self.assertIn("MAIL_REFRESH_TOKEN", missing)
        self.assertIn("MAIL_CLIENT_ID", missing)

    def test_validate_partial_mail(self):
        """部分填写邮件配置应只报告缺失项"""
        cfg = AppConfig(mail_refresh_token="rt_abc")
        missing = cfg.validate(required_modules=["mail"])
        self.assertEqual(len(missing), 1)
        self.assertNotIn("MAIL_REFRESH_TOKEN", missing)
        self.assertIn("MAIL_CLIENT_ID", missing)

    def test_validate_mail_domain_optional(self):
        """MAIL_DOMAIN 为兼容字段，不应阻塞 mail 模块校验"""
        cfg = AppConfig(
            mail_refresh_token="rt_abc",
            mail_client_id="cid_xyz",
            mail_domain="",
        )
        missing = cfg.validate(required_modules=["mail"])
        self.assertEqual(missing, [])

    def test_validate_multiple_modules(self):
        """同时校验多个模块"""
        cfg = AppConfig()
        missing = cfg.validate(required_modules=["efuncard", "sms", "ads"])
        self.assertIn("EFUNCARD_TOKEN", missing)
        self.assertIn("SMS_API_KEY", missing)
        self.assertIn("ADS_API_KEY", missing)

    def test_validate_llm_requires_model_when_enabled(self):
        """启用 LLM 时，应校验 base_url/api_key/model。"""
        cfg = AppConfig(
            llm_enabled=True,
            llm_base_url="https://proxy.example.com/v1",
            llm_api_key="sk-test",
            llm_model="",
        )
        missing = cfg.validate(required_modules=["llm"])
        self.assertEqual(missing, ["LLM_MODEL"])

    def test_build_billing_profile_uses_configured_values(self):
        """应能从配置对象构造账单资料字典。"""
        cfg = AppConfig(
            billing_country="ES",
            billing_line1="Calle San Pablo, 1",
            billing_line2="2B",
            billing_city="Alicante (Alacant)",
            billing_state="A",
            billing_postal_code="03012",
        )

        self.assertEqual(
            cfg.build_billing_profile(),
            {
                "country": "ES",
                "line1": "Calle San Pablo, 1",
                "line2": "2B",
                "city": "Alicante (Alacant)",
                "state": "A",
                "postal_code": "03012",
            },
        )


class TestLoadConfig(unittest.TestCase):
    """load_config 加载测试"""

    @patch.dict(os.environ, {
        "ADS_API": "http://test:9999",
        "ADS_API_KEY": "key123",
        "EFUNCARD_TOKEN": "token456",
        "SMS_API_KEY": "sms789",
        "SMS_COUNTRY": "12",
        "MAIL_DOMAIN": "https://mail.test",
        "MAIL_REFRESH_TOKEN": "rt_abc",
        "MAIL_CLIENT_ID": "cid_xyz",
        "LLM_ENABLED": "true",
        "LLM_BASE_URL": "https://proxy.example.com/v1",
        "LLM_API_KEY": "llm-key",
        "LLM_MODEL": "gpt-4.1-mini",
        "ENABLE_PAYMENT_FLOW": "false",
        "PAYMENT_PLAN": "team",
        "PAYMENT_LINK_ONLY": "true",
        "PAYMENT_LINK_RETURN_MODE": "app",
        "AIMIZY_COUNTRY": "SG",
        "AIMIZY_CURRENCY": "SGD",
        "BILLING_COUNTRY": "ES",
        "BILLING_LINE1": "Calle San Pablo, 1",
        "BILLING_LINE2": "2B",
        "BILLING_CITY": "Alicante (Alacant)",
        "BILLING_STATE": "A",
        "BILLING_POSTAL_CODE": "03012",
    }, clear=False)
    def test_load_from_env(self):
        """从环境变量正确读取所有配置"""
        cfg = load_config()
        self.assertEqual(cfg.ads_api, "http://test:9999")
        self.assertEqual(cfg.ads_api_key, "key123")
        self.assertEqual(cfg.efuncard_token, "token456")
        self.assertEqual(cfg.sms_api_key, "sms789")
        self.assertEqual(cfg.sms_country, "12")
        self.assertEqual(cfg.mail_domain, "https://mail.test")
        self.assertTrue(cfg.llm_enabled)
        self.assertEqual(cfg.llm_base_url, "https://proxy.example.com/v1")
        self.assertEqual(cfg.llm_api_key, "llm-key")
        self.assertEqual(cfg.llm_model, "gpt-4.1-mini")
        self.assertFalse(cfg.enable_payment_flow)
        self.assertEqual(cfg.payment_plan, "team")
        self.assertTrue(cfg.payment_link_only)
        self.assertEqual(cfg.payment_link_return_mode, "app")
        self.assertEqual(cfg.aimizy_country, "SG")
        self.assertEqual(cfg.aimizy_currency, "SGD")
        self.assertEqual(cfg.billing_country, "ES")
        self.assertEqual(cfg.billing_line1, "Calle San Pablo, 1")
        self.assertEqual(cfg.billing_line2, "2B")
        self.assertEqual(cfg.billing_city, "Alicante (Alacant)")
        self.assertEqual(cfg.billing_state, "A")
        self.assertEqual(cfg.billing_postal_code, "03012")

    def test_load_defaults(self):
        """环境变量缺失时使用默认值"""
        # 临时移除可能残留的环境变量，避免 clear=True 影响系统变量
        keys_to_remove = [
            "ADS_API", "ADS_API_KEY", "EFUNCARD_TOKEN",
            "SMS_API_KEY", "SMS_COUNTRY",
            "MAIL_DOMAIN", "MAIL_REFRESH_TOKEN", "MAIL_CLIENT_ID",
            "TASK_ADS_ID", "TASK_CDK", "TASK_EMAIL", "TASK_PASSWORD"
        ]
        saved = {k: os.environ.pop(k, None) for k in keys_to_remove}
        try:
            # 传入不存在的路径阻止 load_dotenv 读取真实 .env 文件
            cfg = load_config(dotenv_path="/tmp/__nonexistent__.env")
            self.assertEqual(cfg.ads_api, "http://local.adspower.net:50325")
            self.assertEqual(cfg.sms_country, "6")
            self.assertEqual(cfg.efuncard_token, "")
        finally:
            # 恢复环境变量
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
