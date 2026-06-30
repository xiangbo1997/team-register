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
        # use_online=False 保证走本地 US 池（不打网络，断言稳定）
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit
        kit = generate_synthetic_visa_kit(use_online=False)
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
        # 电话（US NANP）
        self.assertIn("(", kit.phone)
        self.assertIn(")", kit.phone)
        self.assertIn("-", kit.phone)

    def test_phone_area_code_matches_address_state(self):
        """关键防关联：电话区号必须跟地址 state 一致（PayPal AVS 风控）。

        仅本地池（use_online=False）保证此不变量；在线地址源的电话另测。
        """
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit
        from src.fintech.billing_addresses import list_all_addresses

        # 构建 state→area_code 集合（US 池）
        state_to_areas = {}
        for addr in list_all_addresses("US"):
            state_to_areas.setdefault(addr.state, set()).add(addr.area_code)

        # 生成 30 个套件，每个都必须 state↔area_code 匹配
        for _ in range(30):
            kit = generate_synthetic_visa_kit(use_online=False)
            phone_area = kit.phone[1:4]  # "(212) 555-1234" → "212"
            valid_areas = state_to_areas[kit.address_state]
            self.assertIn(
                phone_area, valid_areas,
                f"电话区号 {phone_area} 不匹配 state {kit.address_state}（允许的区号: {valid_areas}）"
            )

    def test_zip_matches_state_in_pool(self):
        """US 地址池里每个 ZIP + state + area_code 格式校验（防数据漂移）"""
        from src.fintech.billing_addresses import list_all_addresses
        # 这里只能做格式校验（不能跑 USPS API），但能防止数据漂移
        for addr in list_all_addresses("US"):
            self.assertEqual(len(addr.state), 2)
            self.assertEqual(len(addr.zip_code), 5)
            self.assertTrue(addr.zip_code.isdigit())
            self.assertEqual(len(addr.area_code), 3)
            self.assertTrue(addr.area_code.isdigit())
            self.assertNotIn("PO Box", addr.line1)
            self.assertNotIn("P.O.", addr.line1)

    def test_kit_seed_deterministic(self):
        """同 seed 永远生成同一完整套件（卡 + 姓名 + 地址 + 电话全一致）。

        seed 模式强制本地池、不调在线（保确定性）。
        """
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
        kit = generate_synthetic_visa_kit(use_online=False)
        payload = kit.to_form_payload()
        required = {
            "card_number", "expiry_month", "expiry_year", "expiry_display",
            "cvv", "bin_prefix", "last_four",
            "first_name", "last_name", "full_name",
            "address_line1", "address_city", "address_state", "address_zip",
            "phone", "country", "postal_label", "source",
        }
        self.assertEqual(required - set(payload.keys()), set(), "缺少字段")

    def test_no_blocked_landmark_addresses(self):
        """反退化护栏：所有国家地址池都不能含已见过的网红地址"""
        from src.fintech.billing_addresses import list_all_addresses
        from src.fintech.country_profiles import SUPPORTED_COUNTRIES
        BLOCKED = {
            "350 5th Ave",      # Empire State Building
            "1600 Pennsylvania Ave",  # White House
            "1 Apple Park Way",  # Apple HQ
            "1 Hacker Way",      # Meta HQ
            "1600 Amphitheatre Pkwy",  # Google HQ
        }
        for cc in SUPPORTED_COUNTRIES:
            for addr in list_all_addresses(cc):
                self.assertNotIn(addr.line1, BLOCKED, f"{cc} 地址池含网红地址：{addr.line1}")


class TestCvvAndExpiryDistribution(unittest.TestCase):
    """B1 + B2：CVV 全空间 + 过期分布偏态 + 月份避开当月。"""

    def test_cvv_can_be_zero_prefix(self):
        """B1: 1000 次采样中至少出现一次 0XX（000-099），证明不再漏 10% 空间"""
        zero_prefix_seen = False
        for _ in range(2000):
            card = generate_synthetic_visa()
            if card.cvv.startswith("0"):
                zero_prefix_seen = True
                break
        self.assertTrue(zero_prefix_seen, "2000 次采样未出现 0XX CVV：B1 修正没生效")

    def test_cvv_always_3_digits_with_leading_zero(self):
        """B1: CVV 永远 3 位（补零正确）"""
        for _ in range(100):
            card = generate_synthetic_visa()
            self.assertEqual(len(card.cvv), 3)
            self.assertTrue(card.cvv.isdigit())

    def test_expiry_month_avoids_current_month(self):
        """B2: 过期月避开当月"""
        from datetime import datetime
        current_month = datetime.utcnow().month
        for _ in range(50):
            card = generate_synthetic_visa()
            self.assertNotEqual(
                int(card.expiry_month), current_month,
                f"过期月 {card.expiry_month} 不应等于当前月 {current_month}",
            )

    def test_expiry_year_offset_distribution_skewed(self):
        """B2: +3 / +4 年应明显多于 +2 / +5 年（偏态分布）"""
        from datetime import datetime
        current_yy = datetime.utcnow().year % 100
        offsets: dict[int, int] = {2: 0, 3: 0, 4: 0, 5: 0}
        N = 2000
        for _ in range(N):
            card = generate_synthetic_visa()
            offset = (int(card.expiry_year) - current_yy) % 100
            self.assertIn(offset, offsets, f"非法 offset: {offset}")
            offsets[offset] += 1
        # +3 / +4 各占 30%（共 60%），+2 / +5 各 20%（共 40%）
        # 用宽松边界容忍统计抖动：+3 + +4 应 > +2 + +5
        center = offsets[3] + offsets[4]
        edges = offsets[2] + offsets[5]
        self.assertGreater(center, edges, f"中心年份分布应高于边缘：{offsets}")


class TestAddressCooldown(unittest.TestCase):
    """A1 地址冷却：24h 内单地址最多 3 次，超过自动剔除。"""

    def setUp(self):
        from src.fintech.billing_addresses import reset_usage_log
        reset_usage_log()

    def tearDown(self):
        from src.fintech.billing_addresses import reset_usage_log
        reset_usage_log()

    def test_pick_address_with_cooldown_records_usage(self):
        from src.fintech.billing_addresses import (
            address_usage_snapshot,
            pick_address_with_cooldown,
        )
        pick_address_with_cooldown()
        snap = address_usage_snapshot()
        self.assertEqual(sum(snap.values()), 1, "应记录 1 次使用")

    def test_cooldown_excludes_overused_addresses(self):
        """同一地址 3 次后从可用池剔除"""
        from src.fintech.billing_addresses import (
            list_all_addresses,
            pick_address_with_cooldown,
            address_usage_snapshot,
        )
        pool_size = len(list_all_addresses())

        # 把每个地址各用满 3 次（用 seed 强制选同一地址不现实；改用反复随机直到全部 ≥ 3）
        # 用 max_uses=1 + 反复抽 N 次，比 max_uses=3 更快验证剔除逻辑
        seen_addresses = set()
        for _ in range(pool_size * 5):  # 5 倍冗余确保覆盖全部地址
            addr = pick_address_with_cooldown(max_uses=1)
            seen_addresses.add((addr.line1, addr.zip_code))

        # 验证：max_uses=1 下，重复抽 N 次后全部地址都被 seen 过（说明 cooldown 在剔除已用地址）
        self.assertEqual(len(seen_addresses), pool_size, "应轮转覆盖全部地址")

    def test_cooldown_degrades_to_random_when_pool_exhausted(self):
        """全池冷却后降级真随机，不抛异常"""
        from src.fintech.billing_addresses import (
            list_all_addresses,
            pick_address_with_cooldown,
        )
        pool_size = len(list_all_addresses())
        # 用 max_uses=1 抽 pool_size * 2 次：前 pool_size 次填满，后面强制走降级路径
        results = [pick_address_with_cooldown(max_uses=1) for _ in range(pool_size * 2)]
        self.assertEqual(len(results), pool_size * 2)
        # 全部结果必须是池内地址
        pool_lines = {a.line1 for a in list_all_addresses()}
        for addr in results:
            self.assertIn(addr.line1, pool_lines)

    def test_seed_mode_bypasses_cooldown(self):
        """seed != None 时跳过 cooldown 过滤，保持确定性"""
        from src.fintech.billing_addresses import pick_address_with_cooldown
        addr1 = pick_address_with_cooldown(seed="determ-seed-1")
        addr2 = pick_address_with_cooldown(seed="determ-seed-1")
        self.assertEqual(addr1.line1, addr2.line1, "同 seed 应返回同地址")
        self.assertEqual(addr1.zip_code, addr2.zip_code)

    def test_kit_no_seed_uses_cooldown(self):
        """generate_synthetic_visa_kit 在无 seed + 本地池时走 cooldown 路径"""
        from src.fintech.billing_addresses import address_usage_snapshot
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit
        generate_synthetic_visa_kit(use_online=False)
        snap = address_usage_snapshot()
        self.assertEqual(sum(snap.values()), 1, "kit 调用一次应记录一次地址使用")


class TestMultiCountry(unittest.TestCase):
    """多国合成卡：BIN / 电话 / 地址字段形态 + 向后兼容。"""

    def setUp(self):
        from src.fintech.billing_addresses import reset_usage_log
        reset_usage_log()

    def test_each_country_bin_and_luhn(self):
        from src.fintech.country_profiles import SUPPORTED_COUNTRIES, bin_prefix_strings
        from src.fintech.synthetic_visa import generate_synthetic_visa
        for cc in SUPPORTED_COUNTRIES:
            for _ in range(20):
                card = generate_synthetic_visa(country=cc)
                self.assertIn(card.bin_prefix, bin_prefix_strings(cc),
                              f"{cc} BIN {card.bin_prefix} 不在该国集合")
                self.assertTrue(is_luhn_valid(card.card_number), f"{cc} Luhn 失败")
                self.assertEqual(card.country, cc)

    def test_backward_compat_default_us(self):
        """不传 country 默认 US，BIN 仍是 4147/4100（行为不变）"""
        from src.fintech.synthetic_visa import generate_synthetic_visa
        for _ in range(20):
            card = generate_synthetic_visa()
            self.assertEqual(card.country, "US")
            self.assertIn(card.bin_prefix, ("4147", "4100"))

    def test_invalid_country_raises(self):
        from src.fintech.country_profiles import get_profile
        with self.assertRaises(ValueError):
            get_profile("ZZ")

    def test_phone_format_per_country(self):
        """各国电话格式：US/CA 为 (XXX)，GB/SG/HK 以国际区号开头"""
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit
        prefixes = {"GB": "+44", "SG": "+65", "HK": "+852", "JP": "+81"}
        for cc, pfx in prefixes.items():
            kit = generate_synthetic_visa_kit(country=cc, use_online=False)
            self.assertTrue(kit.phone.startswith(pfx),
                            f"{cc} 电话 {kit.phone} 应以 {pfx} 开头")
        for cc in ("US", "CA"):
            kit = generate_synthetic_visa_kit(country=cc, use_online=False)
            self.assertTrue(kit.phone.startswith("("), f"{cc} 电话应为 NANP 格式")

    def test_address_field_shape_per_country(self):
        """GB/SG/HK 无 state；CA postal 含字母；SG 6 位数字邮编；HK 无邮编"""
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit
        # GB：无 state，有 postcode
        gb = generate_synthetic_visa_kit(country="GB", use_online=False)
        self.assertEqual(gb.address_state, "")
        self.assertTrue(gb.address_zip)
        # CA：有省码 + 含字母 postal code
        ca = generate_synthetic_visa_kit(country="CA", use_online=False)
        self.assertTrue(ca.address_state)
        self.assertTrue(any(ch.isalpha() for ch in ca.address_zip))
        # SG：无 state + 6 位数字邮编
        sg = generate_synthetic_visa_kit(country="SG", use_online=False)
        self.assertEqual(sg.address_state, "")
        self.assertEqual(len(sg.address_zip), 6)
        self.assertTrue(sg.address_zip.isdigit())
        # HK：无 state + 无邮编
        hk = generate_synthetic_visa_kit(country="HK", use_online=False)
        self.assertEqual(hk.address_state, "")
        self.assertEqual(hk.address_zip, "")
        # JP：有都道府县(state) + 7 位邮编 NNN-NNNN
        jp = generate_synthetic_visa_kit(country="JP", use_online=False)
        self.assertTrue(jp.address_state)
        self.assertRegex(jp.address_zip, r"^\d{3}-\d{4}$")

    def test_postal_label_per_country(self):
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit
        self.assertEqual(generate_synthetic_visa_kit(country="US", use_online=False).postal_label, "ZIP")
        self.assertEqual(generate_synthetic_visa_kit(country="GB", use_online=False).postal_label, "Postcode")

    def test_cooldown_keyed_by_country(self):
        """同 line1 不同国家不互相占用冷却计数（key 含 country 前缀）"""
        from src.fintech.billing_addresses import (
            _address_key, _record_usage, _prune_usage, _USAGE_LOCK,
        )
        from src.fintech.country_profiles import get_profile
        us_addr = get_profile("US").fallback_addresses[0]
        with _USAGE_LOCK:
            _record_usage(_address_key(us_addr, "US"))
            us_count = _prune_usage(_address_key(us_addr, "US"), 86400.0)
            gb_count = _prune_usage(_address_key(us_addr, "GB"), 86400.0)
        self.assertEqual(us_count, 1)
        self.assertEqual(gb_count, 0, "不同 country key 不应共享计数")


class TestOnlineIdentityFallback(unittest.TestCase):
    """在线地址源：成功用在线 / 失败降级本地池 / seed 不调在线。"""

    def setUp(self):
        from src.fintech.billing_addresses import reset_usage_log
        reset_usage_log()

    def test_online_success_uses_online_address(self):
        """注入 mock 返回 randomuser 结构 → 用在线姓名+地址，source=online"""
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit

        def fake_get(url):
            return {
                "results": [{
                    "name": {"first": "Online", "last": "Tester"},
                    "location": {
                        "street": {"number": 742, "name": "Evergreen Terrace"},
                        "city": "Springfield", "state": "Oregon",
                        "postcode": "97403",
                    },
                    "phone": "(541) 754-3010",
                }]
            }

        kit = generate_synthetic_visa_kit(country="US", use_online=True, http_get=fake_get)
        self.assertEqual(kit.source, "online")
        self.assertEqual(kit.first_name, "Online")
        self.assertEqual(kit.last_name, "Tester")
        self.assertIn("Evergreen Terrace", kit.address_line1)
        self.assertEqual(kit.phone, "(541) 754-3010")

    def test_online_failure_falls_back_local(self):
        """在线 http_get 抛异常 → 静默回退本地池，source=fallback，不抛"""
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit

        def boom(url):
            raise RuntimeError("network down")

        kit = generate_synthetic_visa_kit(country="US", use_online=True, http_get=boom)
        self.assertEqual(kit.source, "fallback")
        self.assertTrue(kit.address_line1)  # 本地池有地址
        self.assertTrue(kit.first_name)

    def test_seed_mode_skips_online(self):
        """seed 模式不调在线（保确定性）：即使 http_get 会抛也不受影响"""
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit

        def boom(url):
            raise RuntimeError("should not be called")

        kit = generate_synthetic_visa_kit(
            country="US", use_online=True, http_get=boom, seed="determ-1"
        )
        self.assertEqual(kit.source, "fallback")

    def test_name_override_beats_online(self):
        """注入姓名优先级最高，覆盖在线姓名"""
        from src.fintech.synthetic_visa import generate_synthetic_visa_kit

        def fake_get(url):
            return {
                "results": [{
                    "name": {"first": "Online", "last": "Tester"},
                    "location": {
                        "street": {"number": 1, "name": "Main St"},
                        "city": "Town", "state": "TX", "postcode": "75001",
                    },
                    "phone": "(214) 555-0100",
                }]
            }

        kit = generate_synthetic_visa_kit(
            country="US", use_online=True, http_get=fake_get,
            override_first_name="Real", override_last_name="Name",
        )
        self.assertEqual(kit.first_name, "Real")
        self.assertEqual(kit.last_name, "Name")

    def test_fakerapi_country_mismatch_returns_none(self):
        """fakerapi 返回国家与请求不符 → 判无效（_parse_fakerapi 直接单测）。

        实测 fakerapi 无视 country_code（请求 JP 返回 BF 等），此校验是核心护栏。
        注：_FAKERAPI_COUNTRIES 当前为空（SG/HK/JP 走本地池），故直接单测解析函数。
        """
        from src.fintech.online_identity import _parse_fakerapi

        payload = {"data": [{"street": "x", "city": "y", "zipcode": "1", "country_code": "BF"}]}
        self.assertIsNone(_parse_fakerapi(payload, expected_cc="JP"))

    def test_sg_hk_jp_skip_online_use_fallback(self):
        """SG/HK/JP 当前不走在线源（fakerapi 实测无效），fetch 返回 None → 本地池"""
        from src.fintech.online_identity import fetch_online_identity

        def should_not_be_called(url):
            raise AssertionError("SG/HK/JP 不应发在线请求")

        for cc in ("SG", "HK", "JP"):
            self.assertIsNone(fetch_online_identity(cc, http_get=should_not_be_called))

    def test_unsupported_country_returns_none(self):
        from src.fintech.online_identity import fetch_online_identity
        self.assertIsNone(fetch_online_identity("ZZ", http_get=lambda u: {}))


if __name__ == "__main__":
    unittest.main()
