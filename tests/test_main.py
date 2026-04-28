# -*- coding: utf-8 -*-
"""main.py 编排辅助函数测试"""

import unittest
from unittest.mock import MagicMock, patch

import main
from src.automation.models import AutomationState, MachineResult
from src.config import AppConfig
from src.models import CardInfo
from src.providers.mail import MailServiceError


class TestMainHelpers(unittest.TestCase):
    """main.py 内部辅助逻辑测试"""

    def test_is_home_page(self):
        self.assertTrue(main._is_home_page("https://chatgpt.com/"))
        self.assertTrue(main._is_home_page("https://chatgpt.com/?model=gpt-4"))
        self.assertFalse(main._is_home_page("https://auth.openai.com/u/login"))
        self.assertFalse(main._is_home_page("https://chatgpt.com/auth/login"))

    def test_find_auth_page_returns_current_page(self):
        current_page = MagicMock()
        current_page.url = "https://auth.openai.com/u/login"
        context = MagicMock()
        context.pages = [current_page]

        result = main._find_auth_page(context, current_page)

        self.assertIs(result, current_page)
        current_page.bring_to_front.assert_not_called()

    def test_find_auth_page_switches_to_other_page(self):
        current_page = MagicMock()
        current_page.url = "https://chatgpt.com/"

        other_page = MagicMock()
        other_page.url = "https://auth0.openai.com/u/signup"

        context = MagicMock()
        context.pages = [current_page, other_page]

        result = main._find_auth_page(context, current_page)

        self.assertIs(result, other_page)
        other_page.bring_to_front.assert_called_once_with()

    def test_extract_session_tokens_success(self):
        page = MagicMock()
        page.evaluate.return_value = {"accessToken": "access_123"}

        context = MagicMock()
        context.cookies.return_value = [
            {"name": "other-cookie", "value": "x"},
            {"name": "next-auth.session-token", "value": "refresh_456"},
        ]

        access_token, refresh_token = main._extract_session_tokens(page, context)

        self.assertEqual(access_token, "access_123")
        self.assertEqual(refresh_token, "refresh_456")

    def test_extract_session_tokens_handles_errors(self):
        page = MagicMock()
        page.evaluate.side_effect = RuntimeError("fetch failed")

        context = MagicMock()
        context.cookies.return_value = []

        access_token, refresh_token = main._extract_session_tokens(page, context)

        self.assertEqual(access_token, "")
        self.assertEqual(refresh_token, "")

    def test_click_first_visible_prefers_structural_selector(self):
        page = MagicMock()

        hidden = MagicMock()
        hidden.first = hidden
        hidden.is_visible.return_value = False

        visible = MagicMock()
        visible.first = visible
        visible.is_visible.return_value = True

        page.locator.side_effect = lambda selector: {
            'a[href*="screen_hint=signup"]': visible,
            'button:has-text("Sign up")': hidden,
        }[selector]

        clicked = main._click_first_visible(
            page,
            ('a[href*="screen_hint=signup"]', 'button:has-text("Sign up")'),
            description="点击首页注册入口",
        )

        self.assertTrue(clicked)
        visible.click.assert_called_once_with(timeout=5000)
        hidden.click.assert_not_called()

    def test_find_payment_input_frame_returns_matching_frame(self):
        page = MagicMock()
        hidden_frame = MagicMock()
        visible_frame = MagicMock()
        page.frames = [hidden_frame, visible_frame]

        hidden_locator = MagicMock()
        hidden_locator.first = hidden_locator
        hidden_locator.is_visible.return_value = False
        hidden_frame.locator.return_value = hidden_locator

        visible_locator = MagicMock()
        visible_locator.first = visible_locator
        visible_locator.is_visible.return_value = True
        visible_frame.locator.return_value = visible_locator

        result = main._find_payment_input_frame(page, ('input[name="cardnumber"]',))

        self.assertIs(result, visible_frame)

    def test_find_payment_input_target_returns_matching_selector(self):
        page = MagicMock()
        frame = MagicMock()
        page.frames = [frame]

        def _locator(selector):
            locator = MagicMock()
            locator.first = locator
            locator.is_visible.return_value = selector == 'input[autocomplete="cc-number"]'
            return locator

        frame.locator.side_effect = _locator

        matched_frame, matched_selector = main._find_payment_input_target(
            page,
            ('input[name="cardnumber"]', 'input[autocomplete="cc-number"]'),
        )

        self.assertIs(matched_frame, frame)
        self.assertEqual(matched_selector, 'input[autocomplete="cc-number"]')

    @patch("main._snapshot_checkout_billing_details")
    @patch("main.human_delay")
    def test_fill_checkout_contact_and_billing_details_populates_required_fields(self, _mock_human_delay, mock_snapshot):
        def _visible_locator(checked: bool = False):
            locator = MagicMock()
            locator.first = locator
            locator.is_visible.return_value = True
            locator.is_checked.return_value = checked
            return locator

        manual_btn = _visible_locator()
        email_input = _visible_locator()
        country_select = _visible_locator()
        address1_input = _visible_locator()
        address2_input = _visible_locator()
        city_input = _visible_locator()
        postal_input = _visible_locator()
        state_select = _visible_locator()
        terms_checkbox = _visible_locator(False)

        page = MagicMock()
        page.locator.side_effect = lambda selector: {
            'button:has-text("手动输入地址")': manual_btn,
            'input[name="email"]': email_input,
            'select[name="billingCountry"]': country_select,
            'input[name="billingAddressLine1"]': address1_input,
            'input[name="billingAddressLine2"]': address2_input,
            'input[name="billingLocality"]': city_input,
            'input[name="billingPostalCode"]': postal_input,
            'select[name="billingAdministrativeArea"]': state_select,
            'input[name="termsOfServiceConsentCheckbox"]': terms_checkbox,
        }[selector]

        mock_snapshot.return_value = {
            "email": "user@example.com",
            "billingCountry": "US",
            "billingAddressLine1": "350 5th Ave",
            "billingAddressLine2": "",
            "billingLocality": "New York",
            "billingPostalCode": "10118",
            "billingAdministrativeArea": "NY",
            "termsAccepted": True,
            "billingName": "",
            "decline_message": "",
        }

        ok, snapshot = main._fill_checkout_contact_and_billing_details(page, email="user@example.com")

        self.assertTrue(ok)
        self.assertEqual(snapshot["billingCountry"], "US")
        email_input.fill.assert_any_call("user@example.com")
        country_select.select_option.assert_called_once_with(value="US")
        address1_input.fill.assert_any_call("350 5th Ave")
        city_input.fill.assert_any_call("New York")
        postal_input.fill.assert_any_call("10118")
        state_select.select_option.assert_called_once_with(value="NY")
        terms_checkbox.check.assert_called_once_with(force=True)

    @patch("main._snapshot_checkout_billing_details")
    @patch("main.human_delay")
    def test_fill_checkout_contact_and_billing_details_retries_when_snapshot_mismatch(
        self,
        _mock_human_delay,
        mock_snapshot,
    ):
        def _visible_locator(checked: bool = False):
            locator = MagicMock()
            locator.first = locator
            locator.is_visible.return_value = True
            locator.is_checked.return_value = checked
            return locator

        manual_btn = _visible_locator()
        email_input = _visible_locator()
        country_select = _visible_locator()
        address1_input = _visible_locator()
        address2_input = _visible_locator()
        city_input = _visible_locator()
        postal_input = _visible_locator()
        state_select = _visible_locator()
        terms_checkbox = _visible_locator(False)

        page = MagicMock()
        page.locator.side_effect = lambda selector: {
            'button:has-text("手动输入地址")': manual_btn,
            'input[name="email"]': email_input,
            'select[name="billingCountry"]': country_select,
            'input[name="billingAddressLine1"]': address1_input,
            'input[name="billingAddressLine2"]': address2_input,
            'input[name="billingLocality"]': city_input,
            'input[name="billingPostalCode"]': postal_input,
            'select[name="billingAdministrativeArea"]': state_select,
            'input[name="termsOfServiceConsentCheckbox"]': terms_checkbox,
        }[selector]
        mock_snapshot.side_effect = [
            {
                "email": "user@example.com",
                "billingCountry": "ES",
                "billingAddressLine1": "Calle San Pablo, 1",
                "billingAddressLine2": "",
                "billingLocality": "Alicante (Alacant)",
                "billingPostalCode": "03012",
                "billingAdministrativeArea": "A",
                "termsAccepted": True,
                "billingName": "",
                "decline_message": "",
            },
            {
                "email": "user@example.com",
                "billingCountry": "US",
                "billingAddressLine1": "350 5th Ave",
                "billingAddressLine2": "",
                "billingLocality": "New York",
                "billingPostalCode": "10118",
                "billingAdministrativeArea": "NY",
                "termsAccepted": True,
                "billingName": "",
                "decline_message": "",
            },
        ]

        ok, snapshot = main._fill_checkout_contact_and_billing_details(page, email="user@example.com")

        self.assertTrue(ok)
        self.assertEqual(snapshot["billingCountry"], "US")
        self.assertEqual(country_select.select_option.call_count, 2)
        self.assertEqual(address1_input.fill.call_count, 4)
        self.assertEqual(city_input.fill.call_count, 4)
        self.assertEqual(postal_input.fill.call_count, 4)
        self.assertEqual(state_select.select_option.call_count, 2)

    @patch("main.human_delay")
    def test_wait_for_stripe_form_accepts_single_frame_variant(self, _mock_human_delay):
        page = MagicMock()
        page.frames = []

        stripe_frame = MagicMock()
        stripe_field = MagicMock()
        stripe_field.first = stripe_field
        stripe_field.is_visible.return_value = True
        stripe_frame.locator.return_value = stripe_field

        frame_locator = MagicMock()
        frame_locator.first = stripe_frame
        page.frame_locator.return_value = frame_locator

        main._wait_for_stripe_form(page, timeout_sec=1)

        page.frame_locator.assert_called()

    @patch("main.human_delay")
    @patch("main.extract_session_tokens_with_http")
    def test_extract_session_tokens_with_retry_retries_until_access_token_available(
        self,
        mock_extract_session,
        mock_human_delay,
    ):
        class StubExperienceStore:
            def __init__(self):
                self.events = []

            def record_event(self, **payload):
                self.events.append(payload)

        mock_extract_session.side_effect = [
            ("", "refresh_123"),
            ("access_456", "refresh_456"),
        ]

        page = MagicMock()
        page.evaluate.return_value = "Mozilla/5.0"
        context = MagicMock()
        context.cookies.return_value = [
            {"name": "__Secure-next-auth.session-token", "value": "refresh_456", "domain": "chatgpt.com"}
        ]
        store = StubExperienceStore()

        access_token, refresh_token = main._extract_session_tokens_with_retry(
            page,
            context,
            attempts=3,
            proxy_url="http://127.0.0.1:7897",
            experience_store=store,
        )

        self.assertEqual(access_token, "access_456")
        self.assertEqual(refresh_token, "refresh_456")
        self.assertEqual(mock_extract_session.call_count, 2)
        first_call = mock_extract_session.call_args_list[0]
        self.assertEqual(first_call.kwargs["proxy_url"], "http://127.0.0.1:7897")
        page.goto.assert_called_once_with("https://chatgpt.com/", wait_until="domcontentloaded")
        mock_human_delay.assert_called_once()
        self.assertEqual(len(store.events), 2)
        self.assertEqual(store.events[0]["category"], "session")

    def test_ordered_payment_variants_prefers_recent_success(self):
        class StubExperienceStore:
            def latest_event(self, **kwargs):
                return {"variant": "single_frame"}

        ordered = main._ordered_payment_variants(
            StubExperienceStore(),
            "https://pay.example.com/checkout/abc",
        )

        self.assertEqual(ordered, ["single_frame", "split_frames"])

    @patch("main.human_delay")
    @patch("main.random.choices")
    @patch("main.random.randint")
    def test_fill_about_you_form_types_full_birthdate_string(
        self,
        mock_randint,
        mock_choices,
        mock_human_delay,
    ):
        page = MagicMock()
        page.evaluate.return_value = {}

        name_input = MagicMock()
        name_input.first = name_input
        name_input.is_visible.return_value = True
        name_input.is_enabled.return_value = True

        age_input = MagicMock()
        age_input.first = age_input
        age_input.is_visible.return_value = False
        age_input.is_enabled.return_value = False

        birthdate_input = MagicMock()
        birthdate_input.is_visible.return_value = True

        segment_input = MagicMock()
        segment_input.is_visible.return_value = False

        submit_button = MagicMock()
        submit_button.is_visible.return_value = True

        birthdate_collection = MagicMock()
        birthdate_collection.first = birthdate_input

        segment_collection = MagicMock()
        segment_collection.first = segment_input

        submit_collection = MagicMock()
        submit_collection.first = submit_button

        selector_map = {
            'input[name="name"][type="text"], input[name="name"], input[autocomplete="name"]': name_input,
            'input[name="age"]': age_input,
            'input[name="birthdate"], input[name="birthday"], input[autocomplete="bday"], input[inputmode="numeric"]': birthdate_collection,
            '[role="spinbutton"]': segment_collection,
            'button[type="submit"]': submit_collection,
        }
        page.locator.side_effect = lambda selector: selector_map[selector]
        page.url = "https://auth.openai.com/about-you"

        mock_randint.side_effect = [4, 4, 20, 4, 1991]
        mock_choices.side_effect = [list("john"), list("doee")]

        main._fill_about_you_form(page)

        birthdate_input.fill.assert_called_once_with("20/04/1991")
        page.keyboard.type.assert_not_called()
        submit_button.click.assert_called_once_with(timeout=5000)
        page.keyboard.press.assert_not_called()

    @patch("main._click_first_visible", return_value=True)
    @patch("main.human_delay")
    def test_fill_about_you_form_handles_onboarding_prompt(self, mock_human_delay, mock_click_first):
        page = MagicMock()
        page.evaluate.side_effect = [
            {"prompt_present": True, "option_count": 5, "footer_button_count": 2},
            "Trabajo",
            {"prompt_present": False, "option_count": 0, "footer_button_count": 0},
        ]

        main._fill_about_you_form(page)

        self.assertEqual(page.evaluate.call_count, 3)
        mock_click_first.assert_called_once()
        mock_human_delay.assert_called_once()

    @patch("main.human_delay")
    @patch("main.random.choices")
    @patch("main.random.randint")
    def test_fill_about_you_form_tolerates_redirect_race(
        self,
        mock_randint,
        mock_choices,
        mock_human_delay,
    ):
        page = MagicMock()
        page.url = "https://chatgpt.com/"

        name_input = MagicMock()
        name_input.first = name_input
        name_input.is_visible.side_effect = RuntimeError("detached")
        name_input.is_enabled.return_value = True

        page.locator.return_value = name_input

        mock_randint.side_effect = [4, 4, 20, 4, 1991]
        mock_choices.side_effect = [list("john"), list("doee")]

        main._fill_about_you_form(page)

        mock_human_delay.assert_not_called()

    @patch("main.human_delay")
    def test_handle_email_verification_step_replaces_existing_code_before_typing(self, mock_human_delay):
        page = MagicMock()
        page.url = "https://auth.openai.com/email-verification"

        code_input = MagicMock()
        code_input.first = code_input
        code_input.is_visible.return_value = True

        submit_button = MagicMock()
        submit_button.first = submit_button
        submit_button.is_visible.return_value = True
        submit_button.click.side_effect = lambda timeout=5000: setattr(page, "url", "https://auth.openai.com/about-you")

        selector_map = {
            'input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]': code_input,
            'button[type="submit"]': submit_button,
        }
        page.locator.side_effect = lambda selector: selector_map.get(selector, MagicMock(first=MagicMock()))

        mail_api = MagicMock()
        mail_api.get_verification_code_via_browser.return_value = "123456"

        result = main._handle_email_verification_step(page, mail_api, "user@example.com")

        self.assertTrue(result)
        # 新逻辑：直接 fill(mail_code, timeout=5000)，不再 click + fill("") 预清空
        code_input.fill.assert_any_call("123456", timeout=5000)
        submit_button.click.assert_called_once_with(timeout=5000)
        self.assertEqual(
            mail_api.get_verification_code_via_browser.call_args.kwargs["wait_timeout"],
            main._EMAIL_CODE_POLL_TIMEOUT_SECONDS,
        )

    @patch("main._click_first_visible")
    @patch("main.human_delay")
    def test_handle_email_verification_step_retries_after_resend(
        self,
        mock_human_delay,
        mock_click_first_visible,
    ):
        page = MagicMock()
        page.url = "https://auth.openai.com/email-verification"

        code_input = MagicMock()
        code_input.first = code_input
        code_input.is_visible.return_value = True

        page.locator.return_value = code_input

        mail_api = MagicMock()
        mail_api.get_verification_code_via_browser.side_effect = [None, "654321"]

        def _click_side_effect(*args, **kwargs):
            description = kwargs.get("description", "")
            return "Resend" in description or "继续按钮" in description

        mock_click_first_visible.side_effect = _click_side_effect

        result = main._handle_email_verification_step(page, mail_api, "user@example.com")

        self.assertTrue(result)
        self.assertEqual(mail_api.get_verification_code_via_browser.call_count, 2)
        # 新逻辑：直接 fill(mail_code, timeout=5000)，不再 fill("") 预清空
        code_input.fill.assert_any_call("654321", timeout=5000)
        descriptions = [call.kwargs.get("description", "") for call in mock_click_first_visible.call_args_list]
        self.assertTrue(any("Resend email 按钮" in item for item in descriptions))
        for call in mail_api.get_verification_code_via_browser.call_args_list:
            self.assertEqual(call.kwargs["wait_timeout"], main._EMAIL_CODE_POLL_TIMEOUT_SECONDS)

    @patch("main.human_delay")
    @patch("main._click_first_visible", return_value=False)
    def test_handle_email_verification_step_uses_short_polling_and_quick_manual_handoff(
        self,
        mock_click_first_visible,
        mock_human_delay,
    ):
        page = MagicMock()
        page.url = "https://auth.openai.com/email-verification"
        page.locator.return_value = MagicMock(first=MagicMock())

        delay_calls = {"count": 0}

        def _delay_side_effect(*_args, **_kwargs):
            delay_calls["count"] += 1
            if delay_calls["count"] >= 5:
                page.url = "https://auth.openai.com/about-you"

        mock_human_delay.side_effect = _delay_side_effect

        mail_api = MagicMock()
        mail_api.get_verification_code_via_browser.return_value = None

        result = main._handle_email_verification_step(page, mail_api, "user@example.com")

        self.assertTrue(result)
        self.assertEqual(mail_api.get_verification_code_via_browser.call_count, main._EMAIL_CODE_MAX_POLL_ATTEMPTS)
        for call in mail_api.get_verification_code_via_browser.call_args_list:
            self.assertEqual(call.kwargs["wait_timeout"], main._EMAIL_CODE_POLL_TIMEOUT_SECONDS)

    @patch("main._emit_task_event")
    def test_handle_email_verification_step_reraises_mail_service_error(self, mock_emit_task_event):
        page = MagicMock()
        page.url = "https://auth.openai.com/email-verification"
        page.locator.return_value = MagicMock(first=MagicMock())

        mail_api = MagicMock()
        mail_api.get_verification_code_via_browser.side_effect = MailServiceError("provider failed")

        with self.assertRaises(MailServiceError):
            main._handle_email_verification_step(page, mail_api, "user@example.com")
        mail_api.get_verification_code_via_browser.assert_called_once()
        self.assertTrue(
            any(
                call.kwargs.get("payload", {}).get("action_id") == "verify_email"
                and call.kwargs.get("payload", {}).get("result") == "failed"
                and "provider failed" in call.kwargs.get("payload", {}).get("error", "")
                for call in mock_emit_task_event.call_args_list
            )
        )

    @patch("main.human_delay")
    @patch("main.human_typing")
    def test_submit_password_ignores_detach_when_page_already_verification(
        self,
        mock_human_typing,
        mock_human_delay,
    ):
        page = MagicMock()
        page.url = "https://auth.openai.com/create-account/password"

        def _typing_side_effect(*_args, **_kwargs):
            page.url = "https://auth.openai.com/email-verification"
            raise RuntimeError("element was detached from the DOM")

        mock_human_typing.side_effect = _typing_side_effect

        main._submit_password(page, "Password123!")

        page.keyboard.press.assert_not_called()

    @patch("main.human_delay")
    @patch("main.human_typing")
    def test_submit_password_ignores_detach_when_input_disappears_before_url_changes(
        self,
        mock_human_typing,
        mock_human_delay,
    ):
        page = MagicMock()
        page.url = "https://auth.openai.com/create-account/password"
        password_input = MagicMock()
        password_input.first = password_input
        password_input.is_visible.return_value = False
        page.locator.return_value = password_input

        mock_human_typing.side_effect = RuntimeError("element was detached from the DOM")

        main._submit_password(page, "Password123!")

        page.keyboard.press.assert_not_called()

    @patch("main.human_delay")
    @patch("main.random.choices")
    @patch("main.random.randint")
    def test_fill_about_you_form_ignores_submit_detach_when_page_already_home(
        self,
        mock_randint,
        mock_choices,
        mock_human_delay,
    ):
        page = MagicMock()
        page.url = "https://auth.openai.com/about-you"

        name_input = MagicMock()
        name_input.first = name_input
        name_input.is_visible.return_value = True
        name_input.is_enabled.return_value = True

        age_input = MagicMock()
        age_input.first = age_input
        age_input.is_visible.return_value = True
        age_input.is_enabled.return_value = True

        submit_button = MagicMock()
        submit_button.first = submit_button
        submit_button.is_visible.return_value = True
        submit_button.is_enabled.return_value = True

        def _submit_side_effect(*_args, **_kwargs):
            page.url = "https://chatgpt.com/"
            raise RuntimeError("element was detached from the DOM")

        submit_button.click.side_effect = _submit_side_effect

        selector_map = {
            'input[name="name"][type="text"], input[name="name"], input[autocomplete="name"]': name_input,
            'input[name="age"]': age_input,
            'button[type="submit"]': submit_button,
        }
        page.locator.side_effect = lambda selector: selector_map.get(selector, MagicMock(first=MagicMock()))

        mock_randint.side_effect = [4, 4, 30]
        mock_choices.side_effect = [list("john"), list("doee")]

        main._fill_about_you_form(page)

        submit_button.click.assert_called_once_with(timeout=5000)
        page.keyboard.press.assert_not_called()
        mock_human_delay.assert_called_once()


class TestMainOrchestration(unittest.TestCase):
    """main() 编排测试"""

    @patch("src.providers.card.EfunCardProvider")
    @patch("main.SMSManager")
    @patch("main.MailManager")
    def test_build_runtime_clients(self, mock_mail, mock_sms, mock_card):
        config = AppConfig(
            efuncard_token="card-token",
            sms_api_key="sms-key",
            sms_country="12",
            email_provider_base_url="http://127.0.0.1:8000",
            email_provider_api_key="test-api-key",
            email_provider_name="applemail",
            known_mail_accounts_json='{"applemail":[{"email":"known@example.com","client_id":"cid-known","refresh_token":"rt-known"}]}',
            mail_refresh_token="refresh-token",
            mail_client_id="client-id",
            proxy="socks5h://127.0.0.1:7890",
        )

        card_api, sms_api, mail_api = main._build_runtime_clients(config)

        # 现在 _build_card_client 走 EfunCardProvider 包装层（带 audit 写入）
        self.assertIs(card_api, mock_card.return_value)
        self.assertIs(sms_api, mock_sms.return_value)
        self.assertIs(mail_api, mock_mail.return_value)
        mock_card.assert_called_once_with(token="card-token")
        mock_sms.assert_called_once_with(
            api_key="sms-key",
            country="12",
            proxy="socks5h://127.0.0.1:7890",
        )
        mock_mail.assert_called_once_with(
            base_url="http://127.0.0.1:8000",
            api_key="test-api-key",
            provider_name="applemail",
            known_accounts={
                "applemail": [
                    {
                        "email": "known@example.com",
                        "client_id": "cid-known",
                        "refresh_token": "rt-known",
                    }
                ]
            },
            refresh_token="refresh-token",
            client_id="client-id",
            proxy="socks5h://127.0.0.1:7890",
            preferred_session_mode="",
            config_name="",
        )

    @patch("main.EfunCard")
    @patch("main.SMSManager")
    @patch("main.MailManager")
    def test_build_runtime_clients_allows_missing_card_token(self, mock_mail, mock_sms, mock_card):
        config = AppConfig(
            efuncard_token="",
            sms_api_key="sms-key",
            sms_country="12",
            email_provider_base_url="http://127.0.0.1:8000",
            email_provider_api_key="test-key",
            email_provider_name="luckmail",
            proxy="socks5h://127.0.0.1:7890",
        )

        card_api, sms_api, mail_api = main._build_runtime_clients(config)

        self.assertIsNone(card_api)
        self.assertIs(sms_api, mock_sms.return_value)
        self.assertIs(mail_api, mock_mail.return_value)
        mock_card.assert_not_called()
        mock_mail.assert_called_once_with(
            base_url="http://127.0.0.1:8000",
            api_key="test-key",
            provider_name="luckmail",
            known_accounts={},
            refresh_token="",
            client_id="",
            proxy="socks5h://127.0.0.1:7890",
            preferred_session_mode="",
            config_name="",
        )

    @patch("main.run_task")
    @patch("main._build_runtime_clients")
    @patch("main.load_config")
    def test_main_skips_task_without_task_vars(self, mock_load_config, mock_build_clients, mock_run_task):
        config = AppConfig(
            ads_api_key="ads-key",
            efuncard_token="card-token",
            sms_api_key="sms-key",
            email_provider_api_key="test-api-key",
            task_ads_id="",
            task_email="",
            task_password="",
        )
        mock_load_config.return_value = config
        mock_build_clients.return_value = (MagicMock(), MagicMock(), MagicMock())

        main.main()

        mock_build_clients.assert_called_once_with(config)
        mock_run_task.assert_not_called()

    @patch("main.run_task")
    @patch("main._build_runtime_clients")
    @patch("main.load_config")
    def test_main_runs_task_when_task_vars_present(self, mock_load_config, mock_build_clients, mock_run_task):
        config = AppConfig(
            ads_api_key="ads-key",
            efuncard_token="card-token",
            sms_api_key="sms-key",
            email_provider_api_key="test-api-key",
            task_ads_id="ads-001",
            task_cdk="cdk-001",
            task_email="user@example.com",
            task_password="Password123!",
        )
        mock_load_config.return_value = config
        clients = (MagicMock(), MagicMock(), MagicMock())
        mock_build_clients.return_value = clients

        main.main()

        mock_run_task.assert_called_once_with(
            config=config,
            card_api=clients[0],
            sms_api=clients[1],
            mail_api=clients[2],
            ads_id="ads-001",
            cdk="cdk-001",
            email="user@example.com",
            password="Password123!",
        )

    @patch("main.run_task")
    @patch("main._build_runtime_clients")
    @patch("main.load_config")
    def test_main_allows_link_only_without_efuncard_token(self, mock_load_config, mock_build_clients, mock_run_task):
        config = AppConfig(
            ads_api_key="ads-key",
            efuncard_token="",
            sms_api_key="sms-key",
            email_provider_api_key="test-api-key",
            task_ads_id="ads-001",
            task_cdk="",
            task_email="user@example.com",
            task_password="Password123!",
            enable_payment_flow=True,
            payment_link_only=True,
            payment_plan="team",
        )
        mock_load_config.return_value = config
        mock_build_clients.return_value = (None, MagicMock(), MagicMock())

        main.main()

        mock_build_clients.assert_called_once_with(config)
        mock_run_task.assert_called_once()

    @patch("main.get_browser_ws")
    @patch("main.run_preflight_checks")
    def test_run_task_stops_before_browser_when_mail_runtime_preflight_fails(
        self,
        mock_preflight,
        mock_get_browser_ws,
    ):
        config = AppConfig(
            ads_api="http://mock-ads",
            ads_api_key="ads-key",
            sms_api_key="sms-key",
            email_provider_api_key="test-api-key",
            task_ads_id="ads-001",
            task_email="user@example.com",
            task_password="Password123!",
            enable_payment_flow=False,
        )
        mail_api = MagicMock()
        mail_api.ensure_runtime_ready.side_effect = MailServiceError("old runtime")

        main.run_task(
            config=config,
            card_api=MagicMock(),
            sms_api=MagicMock(),
            mail_api=mail_api,
            ads_id="ads-001",
            cdk="",
            email="user@example.com",
            password="Password123!",
        )

        mail_api.ensure_runtime_ready.assert_called_once_with("user@example.com")
        mock_preflight.assert_not_called()
        mock_get_browser_ws.assert_not_called()

    @patch("main._complete_payment_flow")
    @patch("main.export_success")
    @patch("main.extract_session_tokens_with_http")
    @patch("main.RegistrationStateMachine")
    @patch("main.ArtifactRecorder")
    @patch("main.run_preflight_checks")
    @patch("main.get_browser_ws")
    @patch("main.sync_playwright")
    def test_run_task_uses_state_machine_and_skips_payment_when_disabled(
        self,
        mock_sync_playwright,
        mock_get_browser_ws,
        mock_preflight,
        mock_artifacts,
        mock_machine_cls,
        mock_extract_session,
        mock_export_success,
        mock_payment,
    ):
        config = AppConfig(
            ads_api="http://mock-ads",
            ads_api_key="ads-key",
            efuncard_token="card-token",
            sms_api_key="sms-key",
            email_provider_api_key="test-api-key",
            task_ads_id="ads-001",
            task_cdk="cdk-001",
            task_email="user@example.com",
            task_password="Password123!",
            enable_payment_flow=False,
            run_artifacts_dir="artifacts/test-runs",
        )

        mock_get_browser_ws.return_value = "ws://127.0.0.1:9222/devtools/browser/abc"
        mock_extract_session.return_value = ("access_123", "refresh_456")
        mock_machine = mock_machine_cls.return_value
        mock_machine.run.return_value = MachineResult(success=True, final_state=AutomationState.HOME)

        page = MagicMock()
        page.context = MagicMock()
        page.context.cookies.return_value = [
            {"name": "__Secure-next-auth.session-token", "value": "refresh_456", "domain": "chatgpt.com"}
        ]
        page.evaluate.return_value = "Mozilla/5.0"
        context = MagicMock()
        context.cookies.return_value = [
            {"name": "__Secure-next-auth.session-token", "value": "refresh_456", "domain": "chatgpt.com"}
        ]
        browser = MagicMock()
        browser.contexts = [context]
        chromium = MagicMock()
        chromium.connect_over_cdp.return_value = browser

        playwright_manager = MagicMock()
        playwright_manager.__enter__.return_value = MagicMock(chromium=chromium)
        playwright_manager.__exit__.return_value = False
        mock_sync_playwright.return_value = playwright_manager

        with patch("main._prepare_clean_start_page", return_value=page), patch("main._build_llm_provider", return_value=None):
            main.run_task(
                config=config,
                card_api=MagicMock(),
                sms_api=MagicMock(),
                mail_api=MagicMock(),
                ads_id="ads-001",
                cdk="cdk-001",
                email="user@example.com",
                password="Password123!",
            )

        mock_preflight.assert_called_once()
        mock_machine.run.assert_called_once()
        mock_extract_session.assert_called_once()
        mock_export_success.assert_called_once_with("user@example.com", "Password123!", "access_123", "refresh_456")
        mock_payment.assert_not_called()

    @patch("main._complete_payment_flow")
    @patch("main.export_success")
    @patch("main.extract_session_tokens_with_http")
    @patch("main.RegistrationStateMachine")
    @patch("main.ArtifactRecorder")
    @patch("main.run_preflight_checks")
    @patch("main.get_browser_ws")
    @patch("main.sync_playwright")
    def test_run_task_resume_mode_reuses_existing_page_for_registration(
        self,
        mock_sync_playwright,
        mock_get_browser_ws,
        mock_preflight,
        mock_artifacts,
        mock_machine_cls,
        mock_extract_session,
        mock_export_success,
        mock_payment,
    ):
        config = AppConfig(
            ads_api="http://mock-ads",
            ads_api_key="ads-key",
            efuncard_token="card-token",
            sms_api_key="sms-key",
            email_provider_api_key="test-api-key",
            task_ads_id="ads-001",
            task_cdk="cdk-001",
            task_email="user@example.com",
            task_password="Password123!",
            enable_payment_flow=False,
            run_artifacts_dir="artifacts/test-runs",
        )

        mock_get_browser_ws.return_value = "ws://127.0.0.1:9222/devtools/browser/abc"
        mock_extract_session.return_value = ("access_123", "refresh_456")
        mock_machine = mock_machine_cls.return_value
        mock_machine.run.return_value = MachineResult(success=True, final_state=AutomationState.HOME)

        page = MagicMock()
        page.evaluate.return_value = "Mozilla/5.0"
        context = MagicMock()
        context.cookies.return_value = [
            {"name": "__Secure-next-auth.session-token", "value": "refresh_456", "domain": "chatgpt.com"}
        ]
        browser = MagicMock()
        browser.contexts = [context]
        chromium = MagicMock()
        chromium.connect_over_cdp.return_value = browser

        playwright_manager = MagicMock()
        playwright_manager.__enter__.return_value = MagicMock(chromium=chromium)
        playwright_manager.__exit__.return_value = False
        mock_sync_playwright.return_value = playwright_manager

        with patch("main._prepare_retry_resume_page", return_value=page) as mock_resume_page, patch(
            "main._prepare_clean_start_page"
        ) as mock_clean_page, patch("main._build_llm_provider", return_value=None):
            main.run_task(
                config=config,
                card_api=MagicMock(),
                sms_api=MagicMock(),
                mail_api=MagicMock(),
                ads_id="ads-001",
                cdk="cdk-001",
                email="user@example.com",
                password="Password123!",
                retry_mode="resume",
                start_phase="registration",
            )

        mock_resume_page.assert_called_once_with(context)
        mock_clean_page.assert_not_called()
        mock_machine.run.assert_called_once()
        mock_export_success.assert_called_once()

    @patch("main._complete_payment_flow")
    @patch("main.export_success")
    @patch("main.extract_session_tokens_with_http")
    @patch("main.RegistrationStateMachine")
    @patch("main.ArtifactRecorder")
    @patch("main.run_preflight_checks")
    @patch("main.get_browser_ws")
    @patch("main.sync_playwright")
    def test_run_task_start_phase_token_extraction_skips_registration_state_machine(
        self,
        mock_sync_playwright,
        mock_get_browser_ws,
        mock_preflight,
        mock_artifacts,
        mock_machine_cls,
        mock_extract_session,
        mock_export_success,
        mock_payment,
    ):
        config = AppConfig(
            ads_api="http://mock-ads",
            ads_api_key="ads-key",
            efuncard_token="card-token",
            sms_api_key="sms-key",
            email_provider_api_key="test-api-key",
            task_ads_id="ads-001",
            task_cdk="cdk-001",
            task_email="user@example.com",
            task_password="Password123!",
            enable_payment_flow=False,
            run_artifacts_dir="artifacts/test-runs",
        )

        mock_get_browser_ws.return_value = "ws://127.0.0.1:9222/devtools/browser/abc"
        mock_extract_session.return_value = ("access_123", "refresh_456")

        page = MagicMock()
        page.evaluate.return_value = "Mozilla/5.0"
        context = MagicMock()
        context.cookies.return_value = [
            {"name": "__Secure-next-auth.session-token", "value": "refresh_456", "domain": "chatgpt.com"}
        ]
        browser = MagicMock()
        browser.contexts = [context]
        chromium = MagicMock()
        chromium.connect_over_cdp.return_value = browser

        playwright_manager = MagicMock()
        playwright_manager.__enter__.return_value = MagicMock(chromium=chromium)
        playwright_manager.__exit__.return_value = False
        mock_sync_playwright.return_value = playwright_manager

        with patch("main._prepare_retry_resume_page", return_value=page), patch(
            "main._build_llm_provider", return_value=None
        ):
            main.run_task(
                config=config,
                card_api=MagicMock(),
                sms_api=MagicMock(),
                mail_api=MagicMock(),
                ads_id="ads-001",
                cdk="cdk-001",
                email="user@example.com",
                password="Password123!",
                retry_mode="resume",
                start_phase="token_extraction",
            )

        mock_machine_cls.return_value.run.assert_not_called()
        mock_extract_session.assert_called_once()
        mock_export_success.assert_called_once()

    @patch("main._complete_payment_flow")
    @patch("main.export_success")
    @patch("main.extract_session_tokens_with_http")
    @patch("main.RegistrationStateMachine")
    @patch("main.ArtifactRecorder")
    @patch("main.run_preflight_checks")
    @patch("main.get_browser_ws")
    @patch("main.sync_playwright")
    def test_run_task_falls_back_to_direct_browser_start_when_preflight_fails(
        self,
        mock_sync_playwright,
        mock_get_browser_ws,
        mock_preflight,
        mock_artifacts,
        mock_machine_cls,
        mock_extract_session,
        mock_export_success,
        mock_payment,
    ):
        config = AppConfig(
            ads_api="http://mock-ads",
            ads_api_key="ads-key",
            efuncard_token="card-token",
            sms_api_key="sms-key",
            email_provider_api_key="test-api-key",
            task_ads_id="ads-001",
            task_cdk="cdk-001",
            task_email="user@example.com",
            task_password="Password123!",
            enable_payment_flow=False,
            run_artifacts_dir="artifacts/test-runs",
        )

        mock_preflight.side_effect = ConnectionError("preflight down")
        mock_get_browser_ws.return_value = "ws://127.0.0.1:9222/devtools/browser/abc"
        mock_extract_session.return_value = ("access_123", "refresh_456")
        mock_machine = mock_machine_cls.return_value
        mock_machine.run.return_value = MachineResult(success=True, final_state=AutomationState.HOME)

        page = MagicMock()
        page.evaluate.return_value = "Mozilla/5.0"
        context = MagicMock()
        context.cookies.return_value = [
            {"name": "__Secure-next-auth.session-token", "value": "refresh_456", "domain": "chatgpt.com"}
        ]
        browser = MagicMock()
        browser.contexts = [context]
        chromium = MagicMock()
        chromium.connect_over_cdp.return_value = browser

        playwright_manager = MagicMock()
        playwright_manager.__enter__.return_value = MagicMock(chromium=chromium)
        playwright_manager.__exit__.return_value = False
        mock_sync_playwright.return_value = playwright_manager

        with patch("main._prepare_clean_start_page", return_value=page), patch("main._build_llm_provider", return_value=None):
            main.run_task(
                config=config,
                card_api=MagicMock(),
                sms_api=MagicMock(),
                mail_api=MagicMock(),
                ads_id="ads-001",
                cdk="cdk-001",
                email="user@example.com",
                password="Password123!",
            )

        mock_preflight.assert_called_once()
        mock_get_browser_ws.assert_called_once()
        mock_export_success.assert_called_once()

    @patch("main._complete_payment_flow")
    @patch("main.export_success")
    @patch("main.extract_session_tokens_with_http")
    @patch("main.RegistrationStateMachine")
    @patch("main.ArtifactRecorder")
    @patch("main.run_preflight_checks")
    @patch("main.get_browser_ws")
    @patch("main.sync_playwright")
    def test_run_task_passes_team_plan_when_payment_enabled(
        self,
        mock_sync_playwright,
        mock_get_browser_ws,
        mock_preflight,
        mock_artifacts,
        mock_machine_cls,
        mock_extract_session,
        mock_export_success,
        mock_payment,
    ):
        config = AppConfig(
            ads_api="http://mock-ads",
            ads_api_key="ads-key",
            efuncard_token="card-token",
            sms_api_key="sms-key",
            email_provider_api_key="test-api-key",
            task_ads_id="ads-001",
            task_cdk="cdk-001",
            task_email="user@example.com",
            task_password="Password123!",
            enable_payment_flow=True,
            payment_plan="team",
            proxy="socks5h://127.0.0.1:7890",
            run_artifacts_dir="artifacts/test-runs",
        )

        mock_get_browser_ws.return_value = "ws://127.0.0.1:9222/devtools/browser/abc"
        mock_extract_session.return_value = ("access_123", "refresh_456")
        mock_machine = mock_machine_cls.return_value
        mock_machine.run.return_value = MachineResult(success=True, final_state=AutomationState.HOME)

        page = MagicMock()
        page.evaluate.return_value = "Mozilla/5.0"
        context = MagicMock()
        context.cookies.return_value = [
            {"name": "__Secure-next-auth.session-token", "value": "refresh_456", "domain": "chatgpt.com"}
        ]
        browser = MagicMock()
        browser.contexts = [context]
        chromium = MagicMock()
        chromium.connect_over_cdp.return_value = browser

        playwright_manager = MagicMock()
        playwright_manager.__enter__.return_value = MagicMock(chromium=chromium)
        playwright_manager.__exit__.return_value = False
        mock_sync_playwright.return_value = playwright_manager

        with patch("main._prepare_clean_start_page", return_value=page), patch("main._build_llm_provider", return_value=None):
            main.run_task(
                config=config,
                card_api=MagicMock(),
                sms_api=MagicMock(),
                mail_api=MagicMock(),
                ads_id="ads-001",
                cdk="cdk-001",
                email="user@example.com",
                password="Password123!",
            )

        mock_export_success.assert_called_once()
        mock_payment.assert_called_once_with(
            page,
            unittest.mock.ANY,
            "cdk-001",
            "access_123",
            plan_type="team",
            proxy_url="socks5h://127.0.0.1:7890",
            link_return_mode="long",
            aimizy_country="SG",
            aimizy_currency="SGD",
            email="user@example.com",
            billing_profile=config.build_billing_profile(),
            experience_store=unittest.mock.ANY,
        )

    @patch("main._generate_payment_link_only")
    @patch("main._complete_payment_flow")
    @patch("main.export_success")
    @patch("main.extract_session_tokens_with_http")
    @patch("main.RegistrationStateMachine")
    @patch("main.ArtifactRecorder")
    @patch("main.run_preflight_checks")
    @patch("main.get_browser_ws")
    @patch("main.sync_playwright")
    def test_run_task_uses_link_only_mode_when_enabled(
        self,
        mock_sync_playwright,
        mock_get_browser_ws,
        mock_preflight,
        mock_artifacts,
        mock_machine_cls,
        mock_extract_session,
        mock_export_success,
        mock_payment,
        mock_generate_link,
    ):
        config = AppConfig(
            ads_api="http://mock-ads",
            ads_api_key="ads-key",
            efuncard_token="",
            sms_api_key="sms-key",
            email_provider_api_key="test-api-key",
            task_ads_id="ads-001",
            task_cdk="",
            task_email="user@example.com",
            task_password="Password123!",
            enable_payment_flow=True,
            payment_plan="team",
            payment_link_only=True,
            payment_link_return_mode="long",
            proxy="socks5h://127.0.0.1:7890",
            run_artifacts_dir="artifacts/test-runs",
        )

        mock_get_browser_ws.return_value = "ws://127.0.0.1:9222/devtools/browser/abc"
        mock_extract_session.return_value = ("access_123", "refresh_456")
        mock_machine = mock_machine_cls.return_value
        mock_machine.run.return_value = MachineResult(success=True, final_state=AutomationState.HOME)

        page = MagicMock()
        page.evaluate.return_value = "Mozilla/5.0"
        context = MagicMock()
        context.cookies.return_value = [
            {"name": "__Secure-next-auth.session-token", "value": "refresh_456", "domain": "chatgpt.com"}
        ]
        browser = MagicMock()
        browser.contexts = [context]
        chromium = MagicMock()
        chromium.connect_over_cdp.return_value = browser

        playwright_manager = MagicMock()
        playwright_manager.__enter__.return_value = MagicMock(chromium=chromium)
        playwright_manager.__exit__.return_value = False
        mock_sync_playwright.return_value = playwright_manager

        with patch("main._prepare_clean_start_page", return_value=page), patch("main._build_llm_provider", return_value=None):
            main.run_task(
                config=config,
                card_api=None,
                sms_api=MagicMock(),
                mail_api=MagicMock(),
                ads_id="ads-001",
                cdk="",
                email="user@example.com",
                password="Password123!",
            )

        mock_export_success.assert_called_once()
        mock_generate_link.assert_called_once_with(
            "access_123",
            plan_type="team",
            proxy_url="socks5h://127.0.0.1:7890",
            return_mode="long",
            aimizy_country="SG",
            aimizy_currency="SGD",
            experience_store=unittest.mock.ANY,
        )
        mock_payment.assert_not_called()

    @patch("main.extract_session_tokens_with_http")
    @patch("main.RegistrationStateMachine")
    @patch("main.ArtifactRecorder")
    @patch("main.run_preflight_checks")
    @patch("main.get_browser_ws")
    @patch("main.sync_playwright")
    def test_run_task_stops_when_state_machine_fails(
        self,
        mock_sync_playwright,
        mock_get_browser_ws,
        mock_preflight,
        mock_artifacts,
        mock_machine_cls,
        mock_extract_session,
    ):
        config = AppConfig(
            ads_api="http://mock-ads",
            ads_api_key="ads-key",
            efuncard_token="card-token",
            sms_api_key="sms-key",
            email_provider_api_key="test-api-key",
            task_ads_id="ads-001",
            task_cdk="cdk-001",
            task_email="user@example.com",
            task_password="Password123!",
        )

        mock_get_browser_ws.return_value = "ws://127.0.0.1:9222/devtools/browser/abc"
        mock_machine = mock_machine_cls.return_value
        mock_machine.run.return_value = MachineResult(
            success=False,
            final_state=AutomationState.ERROR,
            failure_reason="AUTH_ERROR",
        )

        page = MagicMock()
        context = MagicMock()
        browser = MagicMock()
        browser.contexts = [context]
        chromium = MagicMock()
        chromium.connect_over_cdp.return_value = browser

        playwright_manager = MagicMock()
        playwright_manager.__enter__.return_value = MagicMock(chromium=chromium)
        playwright_manager.__exit__.return_value = False
        mock_sync_playwright.return_value = playwright_manager

        with patch("main._prepare_clean_start_page", return_value=page), patch("main._build_llm_provider", return_value=None):
            main.run_task(
                config=config,
                card_api=MagicMock(),
                sms_api=MagicMock(),
                mail_api=MagicMock(),
                ads_id="ads-001",
                cdk="cdk-001",
                email="user@example.com",
                password="Password123!",
            )

        mock_extract_session.assert_not_called()


class TestMainPaymentFlow(unittest.TestCase):
    """支付流关键顺序测试"""

    @patch("main.PaymentLinkGenerator.generate_checkout_link")
    def test_generate_payment_link_only_uses_checkout_link_generator(self, mock_generate_link):
        mock_generate_link.return_value = (True, "https://pay.openai.com/c/pay/abc")

        class StubExperienceStore:
            def __init__(self):
                self.events = []

            def record_event(self, **kwargs):
                self.events.append(kwargs)

        store = StubExperienceStore()
        result = main._generate_payment_link_only(
            "access_123",
            plan_type="team",
            proxy_url="socks5h://127.0.0.1:7890",
            return_mode="long",
            experience_store=store,
        )

        self.assertEqual(result, "https://pay.openai.com/c/pay/abc")
        mock_generate_link.assert_called_once_with(
            "access_123",
            plan_type="team",
            proxy="socks5h://127.0.0.1:7890",
            return_mode="long",
            aimizy_country="SG",
            aimizy_currency="SGD",
        )
        self.assertEqual(store.events[-1]["name"], "checkout_link_generated")

    @patch("main.PaymentLinkGenerator.generate_checkout_link")
    def test_generate_payment_link_only_records_failure(self, mock_generate_link):
        mock_generate_link.return_value = (False, "forbidden")

        class StubExperienceStore:
            def __init__(self):
                self.events = []

            def record_event(self, **kwargs):
                self.events.append(kwargs)

        store = StubExperienceStore()
        result = main._generate_payment_link_only(
            "access_123",
            plan_type="team",
            return_mode="long",
            experience_store=store,
        )

        self.assertEqual(result, "")
        self.assertEqual(store.events[-1]["name"], "checkout_link_failed")

    @patch("main.PaymentLinkGenerator.generate_checkout_link")
    def test_generate_payment_link_only_rejects_non_hosted_link_in_long_mode(self, mock_generate_link):
        mock_generate_link.return_value = (True, "https://chatgpt.com/checkout/openai_llc/cs_live_test")

        class StubExperienceStore:
            def __init__(self):
                self.events = []

            def record_event(self, **kwargs):
                self.events.append(kwargs)

        store = StubExperienceStore()
        result = main._generate_payment_link_only(
            "access_123",
            plan_type="team",
            return_mode="long",
            experience_store=store,
        )

        self.assertEqual(result, "")
        self.assertEqual(store.events[-1]["name"], "checkout_link_kind_mismatch")

    @patch("main._fill_checkout_contact_and_billing_details")
    @patch("main.human_typing")
    @patch("main.human_delay")
    @patch("main._ordered_payment_variants", return_value=["single_frame"])
    @patch("main.PaymentLinkGenerator.generate_short_link")
    def test_complete_payment_flow_opens_new_tab_before_card_lookup(
        self,
        mock_generate_short_link,
        _mock_variants,
        mock_human_delay,
        mock_human_typing,
        mock_fill_checkout_details,
    ):
        mock_fill_checkout_details.return_value = (
            True,
            {
                "email": "",
                "billingName": "JOHN DOE",
                "billingCountry": "US",
                "billingAddressLine1": "350 5th Ave",
                "billingAddressLine2": "",
                "billingLocality": "New York",
                "billingPostalCode": "10118",
                "billingAdministrativeArea": "NY",
                "termsAccepted": True,
                "decline_message": "",
            },
        )
        mock_generate_short_link.return_value = (
            True,
            "https://chatgpt.com/checkout/openai_llc/cs_live_test",
        )
        page = MagicMock()
        context = MagicMock()
        checkout_page = MagicMock()
        page.context = context
        context.new_page.return_value = checkout_page

        order: list[str] = []
        checkout_page.goto.side_effect = lambda *args, **kwargs: order.append("goto")

        stripe_frame = MagicMock()
        stripe_field = MagicMock()
        stripe_field.first = stripe_field
        stripe_field.is_visible.return_value = True
        stripe_frame.locator.return_value = stripe_field
        frame_locator = MagicMock()
        frame_locator.first = stripe_frame
        checkout_page.frame_locator.return_value = frame_locator

        name_input = MagicMock()
        name_input.first = name_input
        name_input.is_visible.return_value = True
        submit_btn = MagicMock()
        submit_btn.is_visible.return_value = True
        checkout_page.locator.side_effect = lambda selector: {
            'input[name="billingName"]': name_input,
            'button[type="submit"]': submit_btn,
        }[selector]

        card = CardInfo(
            card_number="4111111111111111",
            expiry_month="12",
            expiry_year="2028",
            cvv="123",
            name_on_card="JOHN DOE",
        )
        card_api = MagicMock()
        card_api.get_card.side_effect = lambda cdk: order.append("card") or card
        card_api.wait_for_3ds.return_value = None

        main._complete_payment_flow(page, card_api, "CDK-001", "access_123")

        self.assertEqual(order[:2], ["goto", "card"])
        mock_generate_short_link.assert_called_once_with(
            "access_123",
            "team",
            proxy=None,
            aimizy_country="SG",
            aimizy_currency="SGD",
        )
        context.new_page.assert_called_once_with()
        page.goto.assert_not_called()
        card_api.get_card.assert_called_once_with("CDK-001")
        submit_btn.click.assert_called_once_with()
        name_input.fill.assert_any_call("JOHN DOE")
        mock_fill_checkout_details.assert_called_once_with(checkout_page, email="", billing_profile=None)
        self.assertGreaterEqual(mock_human_delay.call_count, 1)

    @patch("main._fill_checkout_contact_and_billing_details")
    @patch("main.human_typing")
    @patch("main.human_delay")
    @patch("main._ordered_payment_variants", return_value=["single_frame"])
    @patch("main.PaymentLinkGenerator.generate_checkout_link")
    def test_complete_payment_flow_uses_long_return_mode_when_requested(
        self,
        mock_generate_checkout_link,
        _mock_variants,
        mock_human_delay,
        mock_human_typing,
        mock_fill_checkout_details,
    ):
        mock_fill_checkout_details.return_value = (
            True,
            {
                "email": "",
                "billingName": "JOHN DOE",
                "billingCountry": "US",
                "billingAddressLine1": "350 5th Ave",
                "billingAddressLine2": "",
                "billingLocality": "New York",
                "billingPostalCode": "10118",
                "billingAdministrativeArea": "NY",
                "termsAccepted": True,
                "decline_message": "",
            },
        )
        mock_generate_checkout_link.return_value = (
            True,
            "https://pay.openai.com/c/pay/cs_live_test",
        )
        page = MagicMock()
        context = MagicMock()
        checkout_page = MagicMock()
        page.context = context
        context.new_page.return_value = checkout_page

        stripe_frame = MagicMock()
        stripe_field = MagicMock()
        stripe_field.first = stripe_field
        stripe_field.is_visible.return_value = True
        stripe_frame.locator.return_value = stripe_field
        frame_locator = MagicMock()
        frame_locator.first = stripe_frame
        checkout_page.frame_locator.return_value = frame_locator

        name_input = MagicMock()
        name_input.first = name_input
        name_input.is_visible.return_value = True
        submit_btn = MagicMock()
        submit_btn.is_visible.return_value = True
        checkout_page.locator.side_effect = lambda selector: {
            'input[name="billingName"]': name_input,
            'button[type="submit"]': submit_btn,
        }[selector]

        card = CardInfo(
            card_number="4111111111111111",
            expiry_month="12",
            expiry_year="2028",
            cvv="123",
            name_on_card="JOHN DOE",
        )
        card_api = MagicMock()
        card_api.get_card.return_value = card
        card_api.wait_for_3ds.return_value = None

        main._complete_payment_flow(
            page,
            card_api,
            "CDK-001",
            "access_123",
            plan_type="team",
            link_return_mode="long",
        )

        mock_generate_checkout_link.assert_called_once_with(
            "access_123",
            plan_type="team",
            proxy=None,
            return_mode="long",
            aimizy_country="SG",
            aimizy_currency="SGD",
        )
        submit_btn.click.assert_called_once_with()
        name_input.fill.assert_any_call("JOHN DOE")
        mock_fill_checkout_details.assert_called_once_with(checkout_page, email="", billing_profile=None)
        self.assertGreaterEqual(mock_human_delay.call_count, 1)

    @patch("main.human_delay")
    @patch("main.PaymentLinkGenerator.generate_short_link")
    def test_complete_payment_flow_records_lookup_failure_with_last_lookup_meta(
        self,
        mock_generate_short_link,
        _mock_human_delay,
    ):
        mock_generate_short_link.return_value = (
            True,
            "https://chatgpt.com/checkout/openai_llc/cs_live_test",
        )
        page = MagicMock()
        context = MagicMock()
        checkout_page = MagicMock()
        page.context = context
        context.new_page.return_value = checkout_page

        class StubExperienceStore:
            def __init__(self):
                self.events = []

            def record_event(self, **kwargs):
                self.events.append(kwargs)

        store = StubExperienceStore()
        card_api = MagicMock()
        card_api.get_card.return_value = None
        card_api.last_lookup_meta = {"status": "failed", "source": "query"}

        main._complete_payment_flow(
            page,
            card_api,
            "CDK-001",
            "access_123",
            experience_store=store,
        )

        self.assertEqual(store.events[-1]["name"], "card_lookup_failed")
        self.assertEqual(store.events[-1]["payload"]["provider_meta"]["status"], "failed")

    @patch("main._fill_checkout_contact_and_billing_details")
    @patch("main.human_typing")
    @patch("main.human_delay")
    @patch("main._ordered_payment_variants", return_value=["split_frames"])
    @patch("main.PaymentLinkGenerator.generate_short_link")
    def test_complete_payment_flow_supports_split_frames_variant(
        self,
        mock_generate_short_link,
        _mock_variants,
        mock_human_delay,
        mock_human_typing,
        mock_fill_checkout_details,
    ):
        mock_fill_checkout_details.return_value = (
            True,
            {
                "email": "",
                "billingName": "JOHN DOE",
                "billingCountry": "US",
                "billingAddressLine1": "350 5th Ave",
                "billingAddressLine2": "",
                "billingLocality": "New York",
                "billingPostalCode": "10118",
                "billingAdministrativeArea": "NY",
                "termsAccepted": True,
                "decline_message": "",
            },
        )
        mock_generate_short_link.return_value = (
            True,
            "https://chatgpt.com/checkout/openai_llc/cs_live_test",
        )
        page = MagicMock()
        context = MagicMock()
        checkout_page = MagicMock()
        page.context = context
        context.new_page.return_value = checkout_page

        def _frame_for(selector_name: str):
            frame = MagicMock()

            def _locator(selector):
                locator = MagicMock()
                locator.first = locator
                locator.is_visible.return_value = selector == selector_name
                return locator

            frame.locator.side_effect = _locator
            return frame

        card_frame = _frame_for('input[autocomplete="cc-number"]')
        expiry_frame = _frame_for('input[autocomplete="cc-exp"]')
        cvc_frame = _frame_for('input[autocomplete="cc-csc"]')
        checkout_page.frames = [card_frame, expiry_frame, cvc_frame]

        name_input = MagicMock()
        name_input.first = name_input
        name_input.is_visible.return_value = True
        submit_btn = MagicMock()
        submit_btn.is_visible.return_value = True
        checkout_page.locator.side_effect = lambda selector: {
            'input[name="billingName"]': name_input,
            'button[type="submit"]': submit_btn,
        }[selector]

        card = CardInfo(
            card_number="4111111111111111",
            expiry_month="12",
            expiry_year="2028",
            cvv="123",
            name_on_card="JOHN DOE",
        )
        card_api = MagicMock()
        card_api.get_card.return_value = card
        card_api.wait_for_3ds.return_value = None

        main._complete_payment_flow(page, card_api, "CDK-001", "access_123")

        mock_human_typing.assert_any_call(card_frame, 'input[autocomplete="cc-number"]', "4111111111111111")
        mock_human_typing.assert_any_call(expiry_frame, 'input[autocomplete="cc-exp"]', "12/28")
        mock_human_typing.assert_any_call(cvc_frame, 'input[autocomplete="cc-csc"]', "123")
        submit_btn.click.assert_called_once_with()
        name_input.fill.assert_any_call("JOHN DOE")
        mock_fill_checkout_details.assert_called_once_with(checkout_page, email="", billing_profile=None)
        self.assertGreaterEqual(mock_human_delay.call_count, 1)

    @patch("main._fill_checkout_contact_and_billing_details")
    @patch("main.human_typing")
    @patch("main.human_delay")
    @patch("main._ordered_payment_variants", return_value=["single_frame"])
    @patch("main.PaymentLinkGenerator.generate_checkout_link")
    def test_complete_payment_flow_stops_before_submit_when_billing_profile_mismatch(
        self,
        mock_generate_checkout_link,
        _mock_variants,
        _mock_human_delay,
        _mock_human_typing,
        mock_fill_checkout_details,
    ):
        mock_generate_checkout_link.return_value = (
            True,
            "https://pay.openai.com/c/pay/cs_live_test",
        )
        mock_fill_checkout_details.return_value = (
            False,
            {
                "email": "user@example.com",
                "billingName": "JOHN DOE",
                "billingCountry": "ES",
                "billingAddressLine1": "Calle San Pablo, 1",
                "billingAddressLine2": "",
                "billingLocality": "Alicante (Alacant)",
                "billingPostalCode": "03012",
                "billingAdministrativeArea": "A",
                "termsAccepted": True,
                "decline_message": "",
            },
        )

        page = MagicMock()
        context = MagicMock()
        checkout_page = MagicMock()
        page.context = context
        context.new_page.return_value = checkout_page

        stripe_frame = MagicMock()
        stripe_field = MagicMock()
        stripe_field.first = stripe_field
        stripe_field.is_visible.return_value = True
        stripe_frame.locator.return_value = stripe_field
        frame_locator = MagicMock()
        frame_locator.first = stripe_frame
        checkout_page.frame_locator.return_value = frame_locator

        name_input = MagicMock()
        name_input.is_visible.return_value = True
        submit_btn = MagicMock()
        submit_btn.is_visible.return_value = True
        checkout_page.locator.side_effect = lambda selector: {
            'input[name="billingName"]': name_input,
            'button[type="submit"]': submit_btn,
        }[selector]

        class StubExperienceStore:
            def __init__(self):
                self.events = []

            def record_event(self, **kwargs):
                self.events.append(kwargs)

        store = StubExperienceStore()
        card = CardInfo(
            card_number="4111111111111111",
            expiry_month="12",
            expiry_year="2028",
            cvv="123",
            name_on_card="JOHN DOE",
        )
        card_api = MagicMock()
        card_api.get_card.return_value = card

        main._complete_payment_flow(
            page,
            card_api,
            "CDK-001",
            "access_123",
            link_return_mode="long",
            experience_store=store,
        )

        submit_btn.click.assert_not_called()
        card_api.wait_for_3ds.assert_not_called()
        self.assertEqual(store.events[-1]["name"], "billing_profile_mismatch")

    @patch("main._detect_checkout_decline_message")
    @patch("main._snapshot_checkout_billing_details")
    @patch("main._fill_checkout_contact_and_billing_details")
    @patch("main.human_typing")
    @patch("main.human_delay")
    @patch("main._ordered_payment_variants", return_value=["single_frame"])
    @patch("main.PaymentLinkGenerator.generate_checkout_link")
    def test_complete_payment_flow_records_card_declined_after_submit(
        self,
        mock_generate_checkout_link,
        _mock_variants,
        _mock_human_delay,
        _mock_human_typing,
        mock_fill_checkout_details,
        mock_snapshot,
        mock_detect_decline,
    ):
        mock_generate_checkout_link.return_value = (
            True,
            "https://pay.openai.com/c/pay/cs_live_test",
        )
        mock_fill_checkout_details.return_value = (
            True,
            {
                "email": "user@example.com",
                "billingName": "JOHN DOE",
                "billingCountry": "US",
                "billingAddressLine1": "350 5th Ave",
                "billingAddressLine2": "",
                "billingLocality": "New York",
                "billingPostalCode": "10118",
                "billingAdministrativeArea": "NY",
                "termsAccepted": True,
                "decline_message": "",
            },
        )
        mock_snapshot.return_value = {
            "email": "user@example.com",
            "billingName": "JOHN DOE",
            "billingCountry": "US",
            "billingAddressLine1": "350 5th Ave",
            "billingAddressLine2": "",
            "billingLocality": "New York",
            "billingPostalCode": "10118",
            "billingAdministrativeArea": "NY",
            "termsAccepted": True,
            "decline_message": "Your card was declined",
        }
        mock_detect_decline.return_value = "Your card was declined"

        page = MagicMock()
        context = MagicMock()
        checkout_page = MagicMock()
        page.context = context
        context.new_page.return_value = checkout_page

        stripe_frame = MagicMock()
        stripe_field = MagicMock()
        stripe_field.first = stripe_field
        stripe_field.is_visible.return_value = True
        stripe_frame.locator.return_value = stripe_field
        frame_locator = MagicMock()
        frame_locator.first = stripe_frame
        checkout_page.frame_locator.return_value = frame_locator

        name_input = MagicMock()
        name_input.is_visible.return_value = True
        submit_btn = MagicMock()
        submit_btn.is_visible.return_value = True
        checkout_page.locator.side_effect = lambda selector: {
            'input[name="billingName"]': name_input,
            'button[type="submit"]': submit_btn,
        }[selector]

        class StubExperienceStore:
            def __init__(self):
                self.events = []

            def record_event(self, **kwargs):
                self.events.append(kwargs)

        store = StubExperienceStore()
        card = CardInfo(
            card_number="4111111111111111",
            expiry_month="12",
            expiry_year="2028",
            cvv="123",
            name_on_card="JOHN DOE",
        )
        card_api = MagicMock()
        card_api.get_card.return_value = card

        main._complete_payment_flow(
            page,
            card_api,
            "CDK-001",
            "access_123",
            link_return_mode="long",
            experience_store=store,
        )

        submit_btn.click.assert_called_once_with()
        card_api.wait_for_3ds.assert_not_called()
        self.assertEqual(store.events[-1]["name"], "card_declined")

    @patch("main.PaymentLinkGenerator.generate_short_link")
    def test_complete_payment_flow_passes_proxy_to_short_link_generator(self, mock_generate_short_link):
        mock_generate_short_link.return_value = (False, "forbidden")
        page = MagicMock()

        main._complete_payment_flow(
            page,
            MagicMock(),
            "CDK-001",
            "access_123",
            proxy_url="socks5h://127.0.0.1:7890",
        )

        mock_generate_short_link.assert_called_once_with(
            "access_123",
            "team",
            proxy="socks5h://127.0.0.1:7890",
            aimizy_country="SG",
            aimizy_currency="SGD",
        )


if __name__ == "__main__":
    unittest.main()
