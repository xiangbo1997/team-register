# -*- coding: utf-8 -*-
"""i18n 国际化基础设施测试"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import src.api.i18n as i18n_mod
from src.api.i18n import (
    DEFAULT_LOCALE,
    SUPPORTED_LOCALES,
    _match_locale,
    _load_translations_cached,
    _resolve_accept_language,
    build_i18n_message_payload,
    get_text,
    localize_event_data,
    load_translations,
    normalize_locale,
)


_LOCALES_DIR = Path(__file__).resolve().parent.parent / "src" / "static" / "locales"


class TestLocaleMatching(unittest.TestCase):
    """Locale 匹配逻辑"""

    def test_match_zh_cn(self):
        self.assertEqual(_match_locale("zh-CN"), "zh-CN")
        self.assertEqual(_match_locale("zh_cn"), "zh-CN")
        self.assertEqual(_match_locale("zh"), "zh-CN")
        self.assertEqual(_match_locale("ZH-TW"), "zh-CN")

    def test_match_en(self):
        self.assertEqual(_match_locale("en"), "en")
        self.assertEqual(_match_locale("en-US"), "en")
        self.assertEqual(_match_locale("EN"), "en")

    def test_match_none(self):
        self.assertIsNone(_match_locale(None))
        self.assertIsNone(_match_locale(""))
        self.assertIsNone(_match_locale("fr"))
        self.assertIsNone(_match_locale("ja"))

    def test_normalize_locale(self):
        self.assertEqual(normalize_locale("en"), "en")
        self.assertEqual(normalize_locale("zh"), "zh-CN")
        self.assertEqual(normalize_locale(None), DEFAULT_LOCALE)
        self.assertEqual(normalize_locale("fr"), DEFAULT_LOCALE)


class TestAcceptLanguage(unittest.TestCase):
    """Accept-Language 头部解析"""

    def test_simple(self):
        self.assertEqual(_resolve_accept_language("en"), "en")
        self.assertEqual(_resolve_accept_language("zh-CN"), "zh-CN")

    def test_with_quality(self):
        self.assertEqual(
            _resolve_accept_language("en-US,en;q=0.9,zh-CN;q=0.8"), "en"
        )
        self.assertEqual(
            _resolve_accept_language("zh-CN,zh;q=0.9,en;q=0.8"), "zh-CN"
        )

    def test_unsupported_first(self):
        self.assertEqual(
            _resolve_accept_language("fr,en;q=0.8"), "en"
        )

    def test_none(self):
        self.assertIsNone(_resolve_accept_language(None))
        self.assertIsNone(_resolve_accept_language(""))
        self.assertIsNone(_resolve_accept_language("fr,ja"))


class TestTranslations(unittest.TestCase):
    """翻译文件加载与查询"""

    def test_load_zh_cn(self):
        data = load_translations("zh-CN")
        self.assertIn("app", data)
        self.assertIn("nav", data)

    def test_load_en(self):
        data = load_translations("en")
        self.assertIn("app", data)
        self.assertEqual(data["app"]["subtitle"], "Control Plane")

    def test_load_translations_refreshes_when_locale_file_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            locale_dir = Path(tmpdir)
            locale_file = locale_dir / "zh-CN.json"
            locale_file.write_text(json.dumps({"app": {"title": "V1"}}, ensure_ascii=False), encoding="utf-8")
            _load_translations_cached.cache_clear()
            with patch.object(i18n_mod, "_LOCALES_DIR", locale_dir):
                self.assertEqual(load_translations("zh-CN")["app"]["title"], "V1")
                locale_file.write_text(json.dumps({"app": {"title": "V2"}}, ensure_ascii=False), encoding="utf-8")
                self.assertEqual(load_translations("zh-CN")["app"]["title"], "V2")

    def test_get_text_simple(self):
        self.assertEqual(get_text("zh-CN", "app.title"), "Team Register")
        self.assertEqual(get_text("en", "nav.overview"), "Overview")

    def test_get_text_nested(self):
        self.assertEqual(get_text("zh-CN", "tasks.list.title"), "任务列表")
        self.assertEqual(get_text("en", "tasks.list.title"), "Tasks")

    def test_get_text_with_params(self):
        text = get_text("zh-CN", "api.task_not_retryable", status="running")
        self.assertIn("running", text)

    def test_get_text_missing_key(self):
        result = get_text("zh-CN", "nonexistent.key")
        self.assertEqual(result, "nonexistent.key")

    def test_get_text_fallback_locale(self):
        result = get_text("fr", "app.title")
        self.assertEqual(result, "Team Register")

    def test_localize_event_data_uses_message_i18n_key(self):
        event = {
            "event_type": "action",
            "payload": build_i18n_message_payload(
                "已收到取消请求，正在尽快停止任务",
                "task_events.cancel_requested",
                action_id="cancel_task",
                result="requested",
            ),
        }

        localized = localize_event_data("en", event)

        self.assertEqual(
            localized["payload"]["message"],
            "Cancellation requested. Stopping task as soon as possible",
        )
        self.assertEqual(localized["payload"]["action_id"], "cancel_task")


class TestTranslationCompleteness(unittest.TestCase):
    """两个 locale 文件 key 完整性校验"""

    def _flatten_keys(self, data: dict, prefix: str = "") -> set:
        keys = set()
        for k, v in data.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                keys.update(self._flatten_keys(v, full_key))
            else:
                keys.add(full_key)
        return keys

    def test_key_parity(self):
        zh_data = json.loads((_LOCALES_DIR / "zh-CN.json").read_text("utf-8"))
        en_data = json.loads((_LOCALES_DIR / "en.json").read_text("utf-8"))
        zh_keys = self._flatten_keys(zh_data)
        en_keys = self._flatten_keys(en_data)

        missing_in_en = zh_keys - en_keys
        missing_in_zh = en_keys - zh_keys

        self.assertEqual(
            missing_in_en, set(),
            f"Keys in zh-CN but missing in en: {missing_in_en}"
        )
        self.assertEqual(
            missing_in_zh, set(),
            f"Keys in en but missing in zh-CN: {missing_in_zh}"
        )

    def test_no_empty_values(self):
        for locale in SUPPORTED_LOCALES:
            data = load_translations(locale)
            empties = []
            self._check_empty(data, "", empties)
            self.assertEqual(
                empties, [],
                f"Empty values in {locale}: {empties}"
            )

    def _check_empty(self, data: dict, prefix: str, empties: list):
        for k, v in data.items():
            full_key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                self._check_empty(v, full_key, empties)
            elif isinstance(v, str) and not v.strip():
                empties.append(full_key)


if __name__ == "__main__":
    unittest.main()
