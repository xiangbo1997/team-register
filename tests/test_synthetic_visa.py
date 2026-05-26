# -*- coding: utf-8 -*-
"""合成 Visa 卡生成器单测。

验证 Luhn 算法正确性 + BIN 段约束 + 确定性 seed 行为。
"""
import unittest

from src.fintech.synthetic_visa import (
    SyntheticCard,
    generate_synthetic_visa,
    is_luhn_valid,
)


class TestSyntheticVisa(unittest.TestCase):
    def test_card_number_is_16_digits(self):
        card = generate_synthetic_visa()
        self.assertEqual(len(card.card_number), 16)
        self.assertTrue(card.card_number.isdigit())

    def test_card_number_passes_luhn(self):
        """生成的 100 张卡都必须通过 Luhn 校验"""
        for _ in range(100):
            card = generate_synthetic_visa()
            self.assertTrue(
                is_luhn_valid(card.card_number),
                f"卡号 {card.card_number} 没过 Luhn 校验",
            )

    def test_bin_is_paypal_friendly(self):
        """所有生成的卡 BIN 必须是 4147 或 4100（PayPal 友好集合）"""
        for _ in range(50):
            card = generate_synthetic_visa()
            self.assertIn(card.bin_prefix, ("4147", "4100"))
            self.assertTrue(card.card_number.startswith(card.bin_prefix))

    def test_expiry_format(self):
        card = generate_synthetic_visa()
        self.assertEqual(len(card.expiry_month), 2)
        self.assertEqual(len(card.expiry_year), 2)
        self.assertTrue(1 <= int(card.expiry_month) <= 12)

    def test_expiry_at_least_2_years_in_future(self):
        from datetime import datetime
        current_yy = datetime.utcnow().year % 100
        for _ in range(20):
            card = generate_synthetic_visa()
            year_diff = (int(card.expiry_year) - current_yy) % 100
            self.assertGreaterEqual(year_diff, 2, f"过期年 {card.expiry_year} 太近")
            self.assertLessEqual(year_diff, 5, f"过期年 {card.expiry_year} 太远")

    def test_cvv_is_3_digits(self):
        card = generate_synthetic_visa()
        self.assertEqual(len(card.cvv), 3)
        self.assertTrue(card.cvv.isdigit())

    def test_explicit_bin_prefix(self):
        card = generate_synthetic_visa(bin_prefix=(4, 1, 4, 7))
        self.assertEqual(card.bin_prefix, "4147")
        self.assertTrue(card.card_number.startswith("4147"))

    def test_seed_produces_deterministic_card(self):
        """同一 seed 永远生成同一张卡（用于按 card_key 确定性映射）"""
        card1 = generate_synthetic_visa(seed="user-123")
        card2 = generate_synthetic_visa(seed="user-123")
        self.assertEqual(card1.card_number, card2.card_number)
        self.assertEqual(card1.expiry_month, card2.expiry_month)
        self.assertEqual(card1.expiry_year, card2.expiry_year)
        self.assertEqual(card1.cvv, card2.cvv)

    def test_different_seeds_produce_different_cards(self):
        card1 = generate_synthetic_visa(seed="user-A")
        card2 = generate_synthetic_visa(seed="user-B")
        self.assertNotEqual(card1.card_number, card2.card_number)

    def test_expiry_display_format(self):
        card = generate_synthetic_visa(seed="display-test")
        # 默认 MM/YY 格式（跟 src.models.CardInfo.expiry_display 一致）
        self.assertRegex(card.expiry_display, r"^\d{2}/\d{2}$")

    def test_last_four_property(self):
        card = generate_synthetic_visa(seed="last4-test")
        self.assertEqual(card.last_four, card.card_number[-4:])
        self.assertEqual(len(card.last_four), 4)

    def test_is_luhn_valid_rejects_invalid(self):
        # 已知合法 Visa 测试卡号
        self.assertTrue(is_luhn_valid("4242424242424242"))
        # 全 0 不合法
        self.assertFalse(is_luhn_valid("4147000000000000"))
        # 改一位让 Luhn 失败
        card = generate_synthetic_visa(seed="luhn-tamper-test")
        original = card.card_number
        tampered = original[:-1] + str((int(original[-1]) + 5) % 10)
        if tampered != original:
            self.assertFalse(is_luhn_valid(tampered))

    def test_known_paypal_friendly_card_passes_luhn(self):
        """已知 d4cc975 工程实测能在 PayPal 通过预校验的卡号"""
        # paypal-auto-config.json:cardNumber
        self.assertTrue(is_luhn_valid("4100557726067796"))


class TestSyntheticCardKit(unittest.TestCase):
    """完整套件（卡 + 地址 + 姓名 + 电话）一致性测试。"""

    def test_kit_returns_all_fields(self):
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit
        kit = generate_synthetic_visa_kit()
        # 卡字段
        self.assertEqual(len(kit.card.card_number), 16)
        self.assertTrue(is_luhn_valid(kit.card.card_number))
        # 姓名
        self.assertTrue(kit.first_name)
        self.assertTrue(kit.last_name)
        self.assertEqual(kit.full_name, f"{kit.first_name} {kit.last_name}")
        # 地址
        self.assertTrue(kit.address_line1)
        self.assertTrue(kit.address_city)
        self.assertEqual(len(kit.address_state), 2)
        self.assertEqual(len(kit.address_zip), 5)
        self.assertTrue(kit.address_zip.isdigit())
        # 电话
        self.assertIn("(", kit.phone)
        self.assertIn(")", kit.phone)
        self.assertIn("-", kit.phone)

    def test_phone_area_code_matches_address_state(self):
        """关键防关联：电话区号必须跟地址 state 一致（PayPal AVS 风控）"""
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit
        from src.fintech.billing_addresses import list_all_addresses

        # 构建 state→area_code 集合
        state_to_areas = {}
        for addr in list_all_addresses():
            state_to_areas.setdefault(addr.state, set()).add(addr.area_code)

        # 生成 30 个套件，每个都必须 state↔area_code 匹配
        for _ in range(30):
            kit = generate_synthetic_visa_kit()
            phone_area = kit.phone[1:4]  # "(212) 555-1234" → "212"
            valid_areas = state_to_areas[kit.address_state]
            self.assertIn(
                phone_area, valid_areas,
                f"电话区号 {phone_area} 不匹配 state {kit.address_state}（允许的区号: {valid_areas}）"
            )

    def test_zip_matches_state_in_pool(self):
        """地址池里每个 ZIP + state + city 都必须真实匹配（人工校验过）"""
        from src.fintech.billing_addresses import list_all_addresses
        # 这里只能做格式校验（不能跑 USPS API），但能防止数据漂移
        for addr in list_all_addresses():
            self.assertEqual(len(addr.state), 2)
            self.assertEqual(len(addr.zip_code), 5)
            self.assertTrue(addr.zip_code.isdigit())
            self.assertEqual(len(addr.area_code), 3)
            self.assertTrue(addr.area_code.isdigit())
            self.assertNotIn("PO Box", addr.line1)
            self.assertNotIn("P.O.", addr.line1)

    def test_kit_seed_deterministic(self):
        """同 seed 永远生成同一完整套件（卡 + 姓名 + 地址 + 电话全一致）"""
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit
        kit1 = generate_synthetic_visa_kit(seed="account-abc-123")
        kit2 = generate_synthetic_visa_kit(seed="account-abc-123")
        # 卡完全一致
        self.assertEqual(kit1.card.card_number, kit2.card.card_number)
        # 地址完全一致
        self.assertEqual(kit1.address_line1, kit2.address_line1)
        self.assertEqual(kit1.address_zip, kit2.address_zip)
        # 电话完全一致
        self.assertEqual(kit1.phone, kit2.phone)
        # 姓名注意：identity_generator 用 secrets.choice，不接受 seed
        # 所以姓名每次不同 —— 这是有意的（同卡不同号注册时姓名要变）

    def test_to_form_payload_has_all_required_fields(self):
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit
        kit = generate_synthetic_visa_kit()
        payload = kit.to_form_payload()
        required = {
            "card_number", "expiry_month", "expiry_year", "expiry_display",
            "cvv", "bin_prefix", "last_four",
            "first_name", "last_name", "full_name",
            "address_line1", "address_city", "address_state", "address_zip",
            "phone",
        }
        self.assertEqual(required - set(payload.keys()), set(), "缺少字段")

    def test_no_blocked_landmark_addresses(self):
        """反退化护栏：地址池绝对不能含 PayPal 已见过的网红地址"""
        from src.fintech.billing_addresses import list_all_addresses
        BLOCKED = {
            "350 5th Ave",      # Empire State Building
            "1600 Pennsylvania Ave",  # White House
            "1 Apple Park Way",  # Apple HQ
            "1 Hacker Way",      # Meta HQ
            "1600 Amphitheatre Pkwy",  # Google HQ
        }
        for addr in list_all_addresses():
            self.assertNotIn(addr.line1, BLOCKED, f"地址池含网红地址：{addr.line1}")


if __name__ == "__main__":
    unittest.main()
