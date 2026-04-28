# -*- coding: utf-8 -*-
"""DB 字段加密层单元测试。"""

import os
import unittest
from unittest import mock

from src.db import crypto


class TestDbCrypto(unittest.TestCase):
    def setUp(self) -> None:
        crypto.reset_for_tests()

    def tearDown(self) -> None:
        crypto.reset_for_tests()

    def test_round_trip_with_key(self):
        key = crypto.generate_key()
        with mock.patch.dict(os.environ, {"DB_ENCRYPTION_KEY": key}):
            ciphertext = crypto.encrypt_value("hunter2")
            self.assertTrue(ciphertext.startswith("enc:v1:"))
            self.assertEqual(crypto.decrypt_value(ciphertext), "hunter2")

    def test_passthrough_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DB_ENCRYPTION_KEY", None)
            self.assertEqual(crypto.encrypt_value("hunter2"), "hunter2")
            self.assertEqual(crypto.decrypt_value("hunter2"), "hunter2")

    def test_decrypt_legacy_plaintext_returns_original(self):
        key = crypto.generate_key()
        with mock.patch.dict(os.environ, {"DB_ENCRYPTION_KEY": key}):
            # 旧明文（无 enc:v1: 前缀）应原样返回
            self.assertEqual(crypto.decrypt_value("legacy_plain"), "legacy_plain")

    def test_double_encrypt_is_idempotent(self):
        key = crypto.generate_key()
        with mock.patch.dict(os.environ, {"DB_ENCRYPTION_KEY": key}):
            once = crypto.encrypt_value("secret")
            twice = crypto.encrypt_value(once)
            self.assertEqual(once, twice)

    def test_empty_and_none(self):
        self.assertEqual(crypto.encrypt_value(""), "")
        self.assertEqual(crypto.encrypt_value(None), "")
        self.assertEqual(crypto.decrypt_value(""), "")
        self.assertEqual(crypto.decrypt_value(None), "")

    def test_is_ciphertext(self):
        key = crypto.generate_key()
        with mock.patch.dict(os.environ, {"DB_ENCRYPTION_KEY": key}):
            self.assertFalse(crypto.is_ciphertext("plain"))
            self.assertFalse(crypto.is_ciphertext(""))
            self.assertTrue(crypto.is_ciphertext(crypto.encrypt_value("secret")))

    def test_arbitrary_key_normalization(self):
        # 非标准 base64 字符串也能被规范化
        with mock.patch.dict(os.environ, {"DB_ENCRYPTION_KEY": "my-dev-key"}):
            cipher = crypto.encrypt_value("secret")
            self.assertEqual(crypto.decrypt_value(cipher), "secret")


if __name__ == "__main__":
    unittest.main()
