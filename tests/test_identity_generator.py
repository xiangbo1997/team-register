# -*- coding: utf-8 -*-
"""真实身份生成器测试。覆盖一致性 + 唯一性 + 边界。"""

import re
import unittest

from src.services.identity_generator import (
    Identity,
    generate_identity,
    generate_unique_identities,
)


class IdentityGeneratorTest(unittest.TestCase):
    def test_basic_format(self):
        """格式：first.last + 出生年后两位。"""
        for _ in range(50):
            ident = generate_identity()
            self.assertIsInstance(ident, Identity)
            # 格式：xxx.yyy + 2 位数字
            self.assertRegex(ident.email_local, r"^[a-z]+\.[a-z]+\d{2}$")
            # 全 ASCII 小写 + 点 + 数字
            self.assertEqual(ident.email_local, ident.email_local.lower())

    def test_consistency_first_name_in_email(self):
        for _ in range(20):
            ident = generate_identity()
            self.assertIn(ident.first_name.lower(), ident.email_local)
            self.assertIn(ident.last_name.lower(), ident.email_local)

    def test_consistency_birthdate_year_in_email(self):
        for _ in range(20):
            ident = generate_identity()
            yy_from_email = ident.email_local[-2:]
            yy_from_birth = ident.birthdate.split("-")[0][-2:]
            self.assertEqual(yy_from_email, yy_from_birth)

    def test_birthdate_iso_format(self):
        for _ in range(20):
            ident = generate_identity()
            self.assertRegex(ident.birthdate, r"^\d{4}-\d{2}-\d{2}$")

    def test_full_name_matches_parts(self):
        for _ in range(10):
            ident = generate_identity()
            self.assertEqual(ident.full_name, f"{ident.first_name} {ident.last_name}")

    def test_gender_male_only(self):
        # 男名集合（部分代表）
        male_set = {
            "James", "Michael", "William", "David", "Richard", "Joseph", "Thomas",
            "Charles", "Christopher", "Daniel", "Matthew", "Anthony",
        }
        for _ in range(20):
            ident = generate_identity(gender="m")
            # 只要在男名 pool 任意一个就 OK（不必所有名都在 male_set 这个小子集里）
            self.assertNotIn(ident.first_name, {"Mary", "Linda", "Susan", "Sarah"})

    def test_gender_female_only(self):
        for _ in range(20):
            ident = generate_identity(gender="f")
            self.assertNotIn(ident.first_name, {"James", "Michael", "William", "David"})

    def test_invalid_gender(self):
        with self.assertRaises(ValueError):
            generate_identity(gender="x")

    def test_year_range(self):
        for _ in range(50):
            ident = generate_identity(year_min=1990, year_max=1992)
            year = int(ident.birthdate.split("-")[0])
            self.assertGreaterEqual(year, 1990)
            self.assertLessEqual(year, 1992)

    def test_unique_identities_returns_n(self):
        ids = generate_unique_identities(50)
        self.assertEqual(len(ids), 50)

    def test_unique_identities_low_collision(self):
        """50 个生成应该无重复 email_local（统计 < 5% 碰撞概率）。"""
        ids = generate_unique_identities(50)
        emails = {i.email_local for i in ids}
        # 允许极低概率重复（max_attempts_per 兜底）
        self.assertGreaterEqual(len(emails), 48)

    def test_unique_identities_zero(self):
        self.assertEqual(generate_unique_identities(0), [])

    def test_unique_identities_negative(self):
        self.assertEqual(generate_unique_identities(-1), [])


if __name__ == "__main__":
    unittest.main()
