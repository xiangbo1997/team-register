# -*- coding: utf-8 -*-
"""账号池服务测试。覆盖 list / promote / abandon / generate_bind_link。"""

import unittest
from datetime import datetime, timezone
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

import csv as csv_mod
import io
import json

import src.db.engine as engine_mod
from src.db.engine import get_session
from src.db.models import CardActivation, MailAccount, Run, RunEvent
from src.services.account_pool_service import (
    FMT_CPA_JSON,
    FMT_CREDENTIALS_CSV,
    FMT_FULL_JSON,
    PLATFORM_GROK,
    PLATFORM_OPENAI,
    TIER_ABANDONED,
    TIER_PLUS,
    TIER_REGISTERED,
    TIER_TEAM,
    _redact_email,
    abandon,
    assign_card,
    export_pool,
    generate_bind_link,
    generate_link,
    get_access_token,
    get_account_detail,
    import_pool,
    list_pool,
    promote,
)


def _build_threadsafe_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _make_run(
    *,
    run_id: str,
    email: str,
    tier: str = TIER_REGISTERED,
    status: str = "success",
    snapshot: dict | None = None,
    card_key: str = "",
    password: str = "pw",
    mail_provider: str = "",
    tokens: dict | None = None,
    ip_address: str = "",
    ip_country: str = "",
    platform: str = PLATFORM_OPENAI,
) -> Run:
    now = datetime.now(timezone.utc)
    return Run(
        id=run_id,
        email=email,
        password=password,
        profile_id="prof-1",
        status=status,
        phase="token_extraction" if tier == TIER_REGISTERED else "payment",
        account_tier=tier,
        platform=platform,
        mail_provider=mail_provider,
        config_snapshot=snapshot or {},
        openai_tokens=tokens or {},
        card_key=card_key,
        ip_address=ip_address,
        ip_country=ip_country,
        created_at=now,
        updated_at=now,
    )


class RedactEmailTest(unittest.TestCase):
    def test_long_local(self):
        self.assertEqual(_redact_email("zhanghuayu@gitee.shop"), "zha***@gitee.shop")

    def test_short_local(self):
        self.assertEqual(_redact_email("ab@gitee.shop"), "a***@gitee.shop")

    def test_no_at(self):
        self.assertEqual(_redact_email("invalid"), "invalid")

    def test_empty(self):
        self.assertEqual(_redact_email(""), "")


class AccountPoolDBTest(unittest.TestCase):
    """需要 in-memory DB（StaticPool 多线程兼容）的集成测试。"""

    def setUp(self):
        self.engine = _build_threadsafe_engine()
        self._original = engine_mod._engine
        engine_mod._engine = self.engine

    def tearDown(self):
        engine_mod._engine = self._original

    # ── list_pool ────────────────────────────────────────
    def test_list_pool_only_returns_specified_tier(self):
        with get_session() as s:
            s.add(_make_run(run_id="r1" + "a" * 14, email="reg@x.com", tier=TIER_REGISTERED))
            s.add(_make_run(run_id="r2" + "a" * 14, email="plus@x.com", tier=TIER_PLUS))
            s.add(_make_run(run_id="r3" + "a" * 14, email="team@x.com", tier=TIER_TEAM))
            s.commit()

        regs = list_pool(tier=TIER_REGISTERED)
        self.assertEqual([r["email"] for r in regs], ["reg@x.com"])
        plus = list_pool(tier=TIER_PLUS)
        self.assertEqual([r["email"] for r in plus], ["plus@x.com"])
        team = list_pool(tier=TIER_TEAM)
        self.assertEqual([r["email"] for r in team], ["team@x.com"])

    def test_list_pool_excludes_failed_runs(self):
        with get_session() as s:
            s.add(_make_run(run_id="ok" + "a" * 14, email="ok@x.com", tier=TIER_REGISTERED, status="success"))
            s.add(_make_run(run_id="fail" + "a" * 12, email="fail@x.com", tier=TIER_REGISTERED, status="failed"))
            s.commit()
        rows = list_pool(tier=TIER_REGISTERED)
        self.assertEqual([r["email"] for r in rows], ["ok@x.com"])

    # ── list_pool platform 过滤 ──────────────────────────
    def _seed_two_platforms(self):
        with get_session() as s:
            s.add(_make_run(run_id="gpt" + "a" * 13, email="gpt@x.com", platform=PLATFORM_OPENAI))
            s.add(_make_run(run_id="grk" + "a" * 13, email="grok@x.com", platform=PLATFORM_GROK))
            s.commit()

    def test_list_pool_platform_filter_returns_only_matching(self):
        self._seed_two_platforms()
        grok_rows = list_pool(tier=TIER_REGISTERED, platform=PLATFORM_GROK)
        self.assertEqual([r["email"] for r in grok_rows], ["grok@x.com"])
        gpt_rows = list_pool(tier=TIER_REGISTERED, platform=PLATFORM_OPENAI)
        self.assertEqual([r["email"] for r in gpt_rows], ["gpt@x.com"])

    def test_list_pool_default_platform_returns_all(self):
        self._seed_two_platforms()
        rows = list_pool(tier=TIER_REGISTERED)
        self.assertEqual({r["email"] for r in rows}, {"gpt@x.com", "grok@x.com"})

    def test_list_pool_platform_all_keyword_returns_all(self):
        self._seed_two_platforms()
        rows = list_pool(tier=TIER_REGISTERED, platform="all")
        self.assertEqual({r["email"] for r in rows}, {"gpt@x.com", "grok@x.com"})

    def test_list_pool_invalid_platform_raises(self):
        with self.assertRaises(ValueError):
            list_pool(tier=TIER_REGISTERED, platform="meta")

    def test_account_dict_exposes_platform(self):
        self._seed_two_platforms()
        grok_rows = list_pool(tier=TIER_REGISTERED, platform=PLATFORM_GROK)
        self.assertEqual(grok_rows[0]["platform"], PLATFORM_GROK)
        # legacy 行（默认 openai）也应正确暴露
        gpt_rows = list_pool(tier=TIER_REGISTERED, platform=PLATFORM_OPENAI)
        self.assertEqual(gpt_rows[0]["platform"], PLATFORM_OPENAI)

    def test_list_pool_invalid_tier(self):
        with self.assertRaises(ValueError):
            list_pool(tier="weird")

    def test_list_pool_redacts_email(self):
        with get_session() as s:
            s.add(_make_run(run_id="x" * 16, email="zhanghuayu@gitee.shop", tier=TIER_REGISTERED))
            s.commit()
        rows = list_pool(tier=TIER_REGISTERED)
        self.assertEqual(rows[0]["email_redacted"], "zha***@gitee.shop")
        # 完整 email 也要透出（管理员需要看真实邮箱）
        self.assertEqual(rows[0]["email"], "zhanghuayu@gitee.shop")

    def test_list_pool_exposes_ip_fields(self):
        """新 IP 字段：账号池列表必须透出 ip_address + ip_country 给前端展示。"""
        with get_session() as s:
            s.add(_make_run(
                run_id="i" * 16, email="ipuser@x.com", tier=TIER_REGISTERED,
                ip_address="203.0.113.42", ip_country="US",
            ))
            s.commit()
        rows = list_pool(tier=TIER_REGISTERED)
        self.assertEqual(rows[0]["ip_address"], "203.0.113.42")
        self.assertEqual(rows[0]["ip_country"], "US")

    def test_list_pool_ip_fields_default_empty_for_legacy_rows(self):
        """老数据没抓 IP：字段必须是空字符串，便于前端模板 falsy 判断显示 '-'。"""
        with get_session() as s:
            s.add(_make_run(run_id="L" * 16, email="legacy@x.com", tier=TIER_REGISTERED))
            s.commit()
        rows = list_pool(tier=TIER_REGISTERED)
        self.assertEqual(rows[0]["ip_address"], "")
        self.assertEqual(rows[0]["ip_country"], "")

    # ── register_name（注册 GPT 的名字，来自 config_snapshot.identity）──
    def test_list_pool_exposes_register_name_from_full_name(self):
        """identity 含 full_name → register_name 直接取 full_name。"""
        with get_session() as s:
            s.add(_make_run(
                run_id="n" * 16, email="named@x.com", tier=TIER_REGISTERED,
                snapshot={"identity": {"first_name": "Laport", "last_name": "Willis",
                                       "full_name": "Laport Willis"}},
            ))
            s.commit()
        rows = list_pool(tier=TIER_REGISTERED)
        self.assertEqual(rows[0]["register_name"], "Laport Willis")

    def test_list_pool_register_name_falls_back_to_first_last(self):
        """identity 无 full_name → 用 first + last 兜底拼接。"""
        with get_session() as s:
            s.add(_make_run(
                run_id="f" * 16, email="fallback@x.com", tier=TIER_REGISTERED,
                snapshot={"identity": {"first_name": "Grom", "last_name": "Walburn"}},
            ))
            s.commit()
        rows = list_pool(tier=TIER_REGISTERED)
        self.assertEqual(rows[0]["register_name"], "Grom Walburn")

    def test_list_pool_register_name_empty_for_legacy_rows(self):
        """老数据 config_snapshot 无 identity：register_name 为空串，前端显示 —。"""
        with get_session() as s:
            s.add(_make_run(run_id="g" * 16, email="legacy2@x.com", tier=TIER_REGISTERED))
            s.commit()
        rows = list_pool(tier=TIER_REGISTERED)
        self.assertEqual(rows[0]["register_name"], "")

    # ── get_account_detail ───────────────────────────────
    def test_get_account_detail_not_found(self):
        self.assertIsNone(get_account_detail("nonexistent"))

    def test_get_account_detail_with_token_in_snapshot(self):
        with get_session() as s:
            s.add(_make_run(
                run_id="t" * 16, email="e@x.com", tier=TIER_REGISTERED,
                snapshot={"access_token": "tok-xyz"},
            ))
            s.commit()
        detail = get_account_detail("t" * 16)
        self.assertTrue(detail["has_access_token"])

    def test_get_account_detail_with_token_in_openai_tokens(self):
        """新路径：token 写在 Run.openai_tokens（worker.update_current_task_tokens 写入）。

        防回归：曾经 _resolve_access_token 只读 config_snapshot/csv，
        漏掉 openai_tokens，导致新号生成链接 400。
        """
        with get_session() as s:
            s.add(_make_run(
                run_id="o" * 16, email="o@x.com", tier=TIER_REGISTERED,
                tokens={"access_token": "ey-new-AT", "refresh_token": "rt-new"},
            ))
            s.commit()
        detail = get_account_detail("o" * 16)
        self.assertTrue(detail["has_access_token"])

    def test_get_account_detail_without_token(self):
        with get_session() as s:
            s.add(_make_run(run_id="n" * 16, email="e@x.com", tier=TIER_REGISTERED))
            s.commit()
        # 注意：会兜底查 accounts.csv，所以可能拿到 token
        # 我们 mock 让 csv 查询失败避免脏数据
        with mock.patch("src.services.account_pool_service.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            detail = get_account_detail("n" * 16)
        self.assertFalse(detail["has_access_token"])

    # ── promote ──────────────────────────────────────────
    def test_promote_to_plus(self):
        with get_session() as s:
            s.add(_make_run(run_id="p" * 16, email="e@x.com", tier=TIER_REGISTERED))
            s.commit()
        result = promote("p" * 16, TIER_PLUS)
        self.assertEqual(result["account_tier"], TIER_PLUS)
        # 留痕事件
        with get_session() as s:
            evs = s.query(RunEvent).filter_by(run_id="p" * 16, event_type="account_promoted").all()
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0].payload["from_tier"], TIER_REGISTERED)
            self.assertEqual(evs[0].payload["to_tier"], TIER_PLUS)

    def test_promote_to_team(self):
        with get_session() as s:
            s.add(_make_run(run_id="t" * 16, email="e@x.com", tier=TIER_REGISTERED))
            s.commit()
        result = promote("t" * 16, TIER_TEAM)
        self.assertEqual(result["account_tier"], TIER_TEAM)

    def test_promote_invalid_tier(self):
        with get_session() as s:
            s.add(_make_run(run_id="x" * 16, email="e@x.com", tier=TIER_REGISTERED))
            s.commit()
        with self.assertRaises(ValueError):
            promote("x" * 16, "weird")
        with self.assertRaises(ValueError):
            promote("x" * 16, TIER_ABANDONED)  # abandoned 不允许通过 promote 走

    def test_promote_unknown_run(self):
        with self.assertRaises(ValueError):
            promote("ghost", TIER_PLUS)

    # ── abandon ──────────────────────────────────────────
    def test_abandon_marks_account(self):
        with get_session() as s:
            s.add(_make_run(run_id="a" * 16, email="e@x.com", tier=TIER_REGISTERED))
            s.commit()
        result = abandon("a" * 16, "stripe declined")
        self.assertEqual(result["account_tier"], TIER_ABANDONED)
        self.assertEqual(result["error_reason"], "stripe declined")
        with get_session() as s:
            evs = s.query(RunEvent).filter_by(run_id="a" * 16, event_type="account_abandoned").all()
            self.assertEqual(len(evs), 1)

    def test_abandon_unknown_run(self):
        with self.assertRaises(ValueError):
            abandon("ghost", "reason")

    # ── generate_bind_link ───────────────────────────────
    def test_generate_bind_link_success(self):
        with get_session() as s:
            s.add(_make_run(
                run_id="b" * 16, email="e@x.com", tier=TIER_REGISTERED,
                snapshot={"access_token": "tok-xyz"},
            ))
            s.add(CardActivation(
                card_key="card-abc",
                card_provider="efuncard",
                card_number="4242424242424242",
                expiry_month="12",
                expiry_year="2030",
                cvv="123",
                bin_country="US",
                target_warmup_count=2,
                warmup_count=2,
            ))
            s.commit()
        with mock.patch(
            "src.payment_link.PaymentLinkGenerator.generate_checkout_link",
            return_value=(True, "https://checkout.example/abc"),
        ):
            result = generate_bind_link("b" * 16, card_key="card-abc", plan="team")
        self.assertEqual(result["link"], "https://checkout.example/abc")
        self.assertEqual(result["plan"], "team")
        self.assertEqual(result["card_last4"], "4242")
        # 留痕
        with get_session() as s:
            evs = s.query(RunEvent).filter_by(run_id="b" * 16, event_type="bind_link_generated").all()
            self.assertEqual(len(evs), 1)

    def test_generate_bind_link_unknown_run(self):
        with self.assertRaises(ValueError):
            generate_bind_link("ghost", card_key="x", plan="team")

    def test_generate_bind_link_wrong_tier(self):
        with get_session() as s:
            s.add(_make_run(run_id="w" * 16, email="e@x.com", tier=TIER_PLUS,
                             snapshot={"access_token": "tok"}))
            s.commit()
        with self.assertRaises(ValueError) as cm:
            generate_bind_link("w" * 16, card_key="x", plan="team")
        self.assertIn("registered", str(cm.exception))

    def test_generate_bind_link_no_token(self):
        with get_session() as s:
            s.add(_make_run(run_id="n" * 16, email="notoken@x.com", tier=TIER_REGISTERED))
            s.commit()
        # 让 csv 读不到
        with mock.patch("src.services.account_pool_service.Path") as mock_path:
            mock_path.return_value.exists.return_value = False
            with self.assertRaises(ValueError) as cm:
                generate_bind_link("n" * 16, card_key="x", plan="team")
        self.assertIn("access_token", str(cm.exception))

    def test_generate_bind_link_invalid_plan(self):
        with self.assertRaises(ValueError):
            generate_bind_link("any", card_key="x", plan="enterprise")

    def test_generate_bind_link_card_not_in_pool(self):
        with get_session() as s:
            s.add(_make_run(
                run_id="c" * 16, email="e@x.com", tier=TIER_REGISTERED,
                snapshot={"access_token": "tok"},
            ))
            s.commit()
        # generate_bind_link 现在委托给 generate_link + assign_card；
        # 必须 mock 让链接生成通过，否则会真实调 ChatGPT API
        with mock.patch(
            "src.payment_link.PaymentLinkGenerator.generate_checkout_link",
            return_value=(True, "https://checkout.example/x"),
        ):
            with self.assertRaises(ValueError) as cm:
                generate_bind_link("c" * 16, card_key="ghost-card", plan="team")
        self.assertIn("卡密不在卡池", str(cm.exception))

    def test_generate_bind_link_card_invalidated(self):
        with get_session() as s:
            s.add(_make_run(
                run_id="i" * 16, email="e@x.com", tier=TIER_REGISTERED,
                snapshot={"access_token": "tok"},
            ))
            s.add(CardActivation(
                card_key="dead-card",
                card_provider="efuncard",
                card_number="4242000000000001",
                cvv="000", expiry_month="01", expiry_year="2030",
                is_invalidated=True,
                invalidate_reason="ops manual kill",
            ))
            s.commit()
        with mock.patch(
            "src.payment_link.PaymentLinkGenerator.generate_checkout_link",
            return_value=(True, "https://checkout.example/x"),
        ):
            with self.assertRaises(ValueError) as cm:
                generate_bind_link("i" * 16, card_key="dead-card", plan="team")
        self.assertIn("作废", str(cm.exception))


def _parse_export_to_entries(content, content_type: str, filename: str) -> list[dict]:
    """统一把 export_pool 的输出解析成账号 dict 列表。

    自适应：
      - "[]" 空导出 → []
      - 单账号 JSON object → [obj]
      - zip（多账号）→ [obj1, obj2, ...]，按文件名排序
    """
    import zipfile as _zip
    import io as _io
    if "zip" in content_type:
        assert isinstance(content, (bytes, bytearray)), f"zip 应该是 bytes，实际 {type(content)}"
        out: list[dict] = []
        with _zip.ZipFile(_io.BytesIO(content)) as zf:
            for name in sorted(zf.namelist()):
                out.append(json.loads(zf.read(name)))
        return out
    # 单账号 / 空导出
    assert isinstance(content, str), f"JSON 应该是 str，实际 {type(content)}"
    data = json.loads(content)
    if isinstance(data, list):
        return data  # 兼容空数组
    return [data]  # 单账号 object → wrap


class GetAccessTokenTest(unittest.TestCase):
    """覆盖 get_access_token —— /accounts 行内"复制 token"按钮的取数函数。"""

    def setUp(self):
        self.engine = _build_threadsafe_engine()
        self._original = engine_mod._engine
        engine_mod._engine = self.engine

    def tearDown(self):
        engine_mod._engine = self._original

    def test_returns_token_from_openai_tokens(self):
        with get_session() as s:
            s.add(_make_run(run_id="t1" + "a" * 14, email="has@x.com",
                            tokens={"access_token": "ey-abc-123"}))
            s.commit()
        res = get_access_token("t1" + "a" * 14)
        self.assertEqual(res["access_token"], "ey-abc-123")
        self.assertEqual(res["email"], "has@x.com")

    def test_falls_back_to_config_snapshot(self):
        with get_session() as s:
            s.add(_make_run(run_id="t2" + "a" * 14, email="snap@x.com",
                            tokens={}, snapshot={"access_token": "ey-from-snapshot"}))
            s.commit()
        res = get_access_token("t2" + "a" * 14)
        self.assertEqual(res["access_token"], "ey-from-snapshot")

    def test_raises_when_no_token(self):
        with get_session() as s:
            s.add(_make_run(run_id="t3" + "a" * 14, email="empty@x.com", tokens={}))
            s.commit()
        with self.assertRaises(ValueError):
            get_access_token("t3" + "a" * 14)

    def test_raises_when_run_not_found(self):
        with self.assertRaises(ValueError):
            get_access_token("nonexistent-run-id")


class ExportPoolTest(unittest.TestCase):
    """覆盖 export_pool 两种格式 + JOIN MailAccount。"""

    def setUp(self):
        self.engine = _build_threadsafe_engine()
        self._original = engine_mod._engine
        engine_mod._engine = self.engine

    def tearDown(self):
        engine_mod._engine = self._original

    def test_export_full_json_with_mail_account_join(self):
        """full_json 含 password + 解密后的 OAuth client_id/refresh_token。"""
        from src.services.account_pool_service import FMT_FULL_JSON
        with get_session() as s:
            s.add(_make_run(
                run_id="r" + "1" * 15, email="alice@x.com",
                password="secret-pw", mail_provider="applemail",
            ))
            s.add(MailAccount(
                label="alice", provider_name="applemail", email="alice@x.com",
                client_id="cid-123", refresh_token="rt-xyz",
            ))
            s.commit()

        content, content_type, filename, skipped = export_pool(TIER_REGISTERED, FMT_FULL_JSON)
        # 单账号 → 单 JSON object，文件名 codex-{email}-{plan}.json
        self.assertIn("application/json", content_type)
        self.assertTrue(filename.startswith("codex-alice@x.com"), f"filename={filename}")
        self.assertTrue(filename.endswith(".json"))
        self.assertEqual(skipped, [])

        rows = _parse_export_to_entries(content, content_type, filename)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "alice@x.com")
        self.assertEqual(rows[0]["password"], "secret-pw")
        self.assertEqual(rows[0]["client_id"], "cid-123")
        self.assertEqual(rows[0]["refresh_token"], "rt-xyz")  # OAuth refresh
        self.assertEqual(rows[0]["mail_provider"], "applemail")

    def test_export_full_json_includes_operational_metadata(self):
        """full_json 必须补全运维元数据：平台 / profile / 卡商 / IP / 段位 / 注册名 / 创建时间。

        回归锁：导出曾经只含凭证 + token，丢了列表页展示的这些元数据（"导出内容不全"）。
        """
        with get_session() as s:
            s.add(_make_run(
                run_id="m" + "1" * 15, email="meta@x.com",
                platform=PLATFORM_GROK,
                ip_address="203.0.113.7", ip_country="JP",
                snapshot={"identity": {"full_name": "Taro Yamada"}},
            ))
            s.commit()

        content, content_type, filename, skipped = export_pool(TIER_REGISTERED, FMT_FULL_JSON)
        rows = _parse_export_to_entries(content, content_type, filename)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # 运维元数据组（与列表页 _account_to_dict 对齐）
        self.assertEqual(row["platform"], PLATFORM_GROK)
        self.assertEqual(row["profile_id"], "prof-1")
        self.assertEqual(row["ip_address"], "203.0.113.7")
        self.assertEqual(row["ip_country"], "JP")
        self.assertEqual(row["account_tier"], TIER_REGISTERED)
        self.assertEqual(row["register_name"], "Taro Yamada")
        self.assertEqual(row["run_id"], "m" + "1" * 15)
        self.assertTrue(row["created_at"], "created_at 不应为空")
        # 字段齐全（卡商/浏览器 provider 即使为空也应作为 key 存在，便于消费方稳定解析）
        for key in ("card_provider", "browser_provider"):
            self.assertIn(key, row)

    # ── Codex CLI 兼容 cpa_json 测试 ──────────────────────
    # 真实 access_token JWT 样本：含 chatgpt_account_id=1bf12f2f-7414-4885-8ee4-980f3fd9d779
    # 和 exp=1779861686 (= 2026-05-27T14:01:26+08:00)
    _SAMPLE_JWT = (
        "eyJhbGciOiJSUzI1NiIsImtpZCI6IjE5MzQ0ZTY1LWJiYzktNDRkMS1hOWQwLWY5NTdiMDc5YmQwZSIsInR5cCI6IkpXVCJ9"
        ".eyJhdWQiOlsiaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS92MSJdLCJjbGllbnRfaWQiOiJhcHBfRU1vYW1FRVo3M2YwQ2tYYV"
        "hwN2hyYW5uIiwiZXhwIjoxNzc5ODYxNjg2LCJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiYW1yIjpbIm90cCIsI"
        "nVybjpvcGVuYWk6YW1yOm90cF9lbWFpbCJdLCJjaGF0Z3B0X2FjY291bnRfaWQiOiIxYmYxMmYyZi03NDE0LTQ4ODUtOGVl"
        "NC05ODBmM2ZkOWQ3NzkiLCJjaGF0Z3B0X2FjY291bnRfdXNlcl9pZCI6InVzZXItOG1sMkRja0RzR3ZRT052bHBjc0xuVUx"
        "HX18xYmYxMmYyZi03NDE0LTQ4ODUtOGVlNC05ODBmM2ZkOWQ3NzkiLCJjaGF0Z3B0X2NvbXB1dGVfcmVzaWRlbmN5Ijoibm"
        "9fY29uc3RyYWludCIsImNoYXRncHRfcGxhbl90eXBlIjoiZnJlZSIsImNoYXRncHRfdXNlcl9pZCI6InVzZXItOG1sMkRja"
        "0RzR3ZRT052bHBjc0xuVUxHIiwibG9jYWxob3N0Ijp0cnVlLCJ1c2VyX2lkIjoidXNlci04bWwyRGNrRHNHdlFPTnZscGNz"
        "TG5VTEcifSwiaHR0cHM6Ly9hcGkub3BlbmFpLmNvbS9wcm9maWxlIjp7ImVtYWlsIjoiamVzc2ljYXBlcnJ5ODlAemhhbmd"
        "4Yi54eXoiLCJlbWFpbF92ZXJpZmllZCI6dHJ1ZX0sImlhdCI6MTc3ODk5NzY4NiwiaXNzIjoiaHR0cHM6Ly9hdXRoLm9wZW"
        "5haS5jb20iLCJqdGkiOiIyNDFmMTk4Mi02ZjNiLTQyNjYtYmMxMS0xMDQ4ZGMxN2M4MTgiLCJuYmYiOjE3Nzg5OTc2ODYsI"
        "nB3ZF9hdXRoX3RpbWUiOjE3Nzg5OTc2NzI5NjgsInNjcCI6WyJvcGVuaWQiLCJlbWFpbCIsInByb2ZpbGUiLCJvZmZsaW5l"
        "X2FjY2VzcyJdLCJzZXNzaW9uX2lkIjoiYXV0aHNlc3NfMEZ4c0FrcDVaRjNkMEZzUllPSmNJVU45Iiwic2wiOnRydWUsInN"
        "1YiI6ImF1dGgwfFJNS0ZBV25Cb2lkTGJPdTJlN0U5VmE2SCJ9"
        ".dummysig"  # 签名段不影响 base64 payload 解析
    )

    def test_export_cpa_json_codex_format_from_real_jwt(self):
        """真实 access_token JWT → 导出 Codex CLI 格式，account_id/expired 从 JWT 解出。"""
        with get_session() as s:
            s.add(_make_run(
                run_id="r" + "2" * 15, email="bob@x.com",
                tokens={
                    "access_token": self._SAMPLE_JWT,
                    "refresh_token": "rt-RT",
                    "id_token": "ey-IT",
                    "extracted_at": "2026-05-17T14:01:26+08:00",
                },
            ))
            s.commit()

        content, content_type, filename, skipped = export_pool(TIER_REGISTERED, FMT_CPA_JSON)
        self.assertIn("application/json", content_type)
        self.assertEqual(skipped, [])
        # 单账号文件名带 email + plan_type (JWT 解出 free)
        self.assertEqual(filename, "codex-bob@x.com-free.json")
        data = _parse_export_to_entries(content, content_type, filename)
        self.assertEqual(len(data), 1)
        item = data[0]
        # 与你贴的 Codex CLI 样本 1:1 对齐
        self.assertEqual(item["access_token"], self._SAMPLE_JWT)
        self.assertEqual(item["refresh_token"], "rt-RT")
        self.assertEqual(item["id_token"], "ey-IT")
        self.assertEqual(item["email"], "bob@x.com")
        # type 固定 codex（不是 account_tier）
        self.assertEqual(item["type"], "codex")
        # disabled 固定 false
        self.assertEqual(item["disabled"], False)
        # account_id 从 JWT chatgpt_account_id 解出（不是 Run.id）
        self.assertEqual(item["account_id"], "1bf12f2f-7414-4885-8ee4-980f3fd9d779")
        # expired 从 JWT exp 解出，UTC+8 北京时间格式
        self.assertEqual(item["expired"], "2026-05-27T14:01:26+08:00")
        self.assertEqual(item["last_refresh"], "2026-05-17T14:01:26+08:00")
        # _filename_* 内部字段不应暴露在 JSON 体里
        self.assertNotIn("_filename_email", item)
        self.assertNotIn("_filename_plan", item)

    def test_export_cpa_json_skips_run_without_access_token(self):
        """老 Run 没 access_token → 跳过不导出，记入 skipped 列表（不再空字符串占位）。"""
        with get_session() as s:
            s.add(_make_run(run_id="r" + "3" * 15, email="old@x.com"))
            s.commit()

        content, content_type, filename, skipped = export_pool(TIER_REGISTERED, FMT_CPA_JSON)
        # 0 条导出 → 空数组 + 兜底文件名 codex-empty-*
        self.assertEqual(content, "[]")
        self.assertTrue(filename.startswith("codex-empty-"), f"filename={filename}")
        # skipped 列表说明原因
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["email"], "old@x.com")
        self.assertIn("access_token", skipped[0]["reason"])

    def test_export_cpa_json_skips_run_with_invalid_jwt(self):
        """token 不是合法 JWT 时跳过该 Run，其他正常 Run 不受影响。"""
        with get_session() as s:
            s.add(_make_run(
                run_id="bad" + "0" * 13, email="bad@x.com",
                tokens={"access_token": "not-a-jwt-at-all"},
            ))
            s.add(_make_run(
                run_id="good" + "0" * 12, email="good@x.com",
                tokens={"access_token": self._SAMPLE_JWT},
            ))
            s.commit()

        content, content_type, filename, skipped = export_pool(TIER_REGISTERED, FMT_CPA_JSON)
        # 只剩 1 条好的 → 单 JSON object（不是 zip），文件名 codex-good@x.com-free.json
        self.assertEqual(filename, "codex-good@x.com-free.json")
        data = _parse_export_to_entries(content, content_type, filename)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["email"], "good@x.com")
        # 跳过列表里有坏的那条
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["email"], "bad@x.com")
        self.assertIn("JWT", skipped[0]["reason"])

    def test_export_invalid_tier_raises(self):
        with self.assertRaises(ValueError):
            export_pool("weird", FMT_CREDENTIALS_CSV)

    def test_export_invalid_fmt_raises(self):
        with self.assertRaises(ValueError):
            export_pool(TIER_REGISTERED, "xml")

    def test_export_credentials_csv_no_longer_allowed_for_export(self):
        """credentials_csv 已从导出菜单移除；调用时应抛 ValueError。

        这是有意的破坏性变更：UI 只暴露 full_json / cpa_json 两个 JSON 选项，
        CSV 仅在 import 路径保留（向后兼容旧文件）。
        """
        with get_session() as s:
            s.add(_make_run(run_id="r" + "1" * 15, email="a@x.com"))
            s.commit()
        with self.assertRaises(ValueError):
            export_pool(TIER_REGISTERED, FMT_CREDENTIALS_CSV)

    def test_export_full_json_with_real_jwt_includes_account_id_and_expired(self):
        """full_json 主路径：账号凭证组 + 令牌组 + JWT 解出 account_id/expired"""
        from src.services.account_pool_service import FMT_FULL_JSON
        with get_session() as s:
            s.add(_make_run(
                run_id="r" + "f" * 15, email="bob@x.com", password="pw-secret",
                mail_provider="applemail",
                tokens={
                    "access_token": self._SAMPLE_JWT,
                    "refresh_token": "openai-rt-XYZ",   # OpenAI session refresh
                    "id_token": "ey-IT",
                    "extracted_at": "2026-05-17T14:01:26+08:00",
                },
            ))
            s.add(MailAccount(
                label="bob", provider_name="applemail", email="bob@x.com",
                client_id="oauth-cid", refresh_token="oauth-rt",
            ))
            s.commit()

        content, ctype, filename, skipped = export_pool(TIER_REGISTERED, FMT_FULL_JSON)
        self.assertIn("application/json", ctype)
        self.assertEqual(skipped, [])
        # 单账号文件名带 email + plan_type
        self.assertEqual(filename, "codex-bob@x.com-free.json")

        data = _parse_export_to_entries(content, ctype, filename)
        self.assertEqual(len(data), 1)
        item = data[0]

        # 账号凭证组
        self.assertEqual(item["email"], "bob@x.com")
        self.assertEqual(item["password"], "pw-secret")
        self.assertEqual(item["mail_provider"], "applemail")
        self.assertEqual(item["client_id"], "oauth-cid")        # OAuth
        self.assertEqual(item["refresh_token"], "oauth-rt")     # OAuth refresh（注意：与 OpenAI 区分）

        # 令牌组
        self.assertEqual(item["access_token"], self._SAMPLE_JWT)
        self.assertEqual(item["openai_refresh_token"], "openai-rt-XYZ")  # OpenAI session refresh
        self.assertEqual(item["id_token"], "ey-IT")
        # JWT 解出
        self.assertEqual(item["account_id"], "1bf12f2f-7414-4885-8ee4-980f3fd9d779")
        self.assertEqual(item["expired"], "2026-05-27T14:01:26+08:00")
        self.assertEqual(item["last_refresh"], "2026-05-17T14:01:26+08:00")

    def test_export_full_json_tolerates_invalid_jwt_does_not_skip(self):
        """full_json 容忍 JWT 解析失败：account_id/expired 留空，但 Run 仍导出（不跳过）"""
        from src.services.account_pool_service import FMT_FULL_JSON
        with get_session() as s:
            s.add(_make_run(
                run_id="r" + "9" * 15, email="badjwt@x.com",
                tokens={"access_token": "not-a-jwt"},
            ))
            s.commit()

        content, ctype, filename, skipped = export_pool(TIER_REGISTERED, FMT_FULL_JSON)
        # 关键差异：full_json 不跳过！
        self.assertEqual(skipped, [])
        # JWT 解不出时 plan_type 留空 → 文件名为 codex-{email}.json（不带 -plan 段）
        self.assertEqual(filename, "codex-badjwt@x.com.json")
        data = _parse_export_to_entries(content, ctype, filename)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["email"], "badjwt@x.com")
        self.assertEqual(data[0]["access_token"], "not-a-jwt")
        self.assertEqual(data[0]["account_id"], "")  # 留空
        self.assertEqual(data[0]["expired"], "")     # 留空

    def test_export_full_json_without_mail_account_keeps_oauth_empty(self):
        """没有匹配 MailAccount 的 Run：client_id / refresh_token 留空，不报错"""
        from src.services.account_pool_service import FMT_FULL_JSON
        with get_session() as s:
            s.add(_make_run(
                run_id="r" + "n" * 15, email="orphan@x.com", password="pw",
            ))
            s.commit()

        content, ctype, filename, _ = export_pool(TIER_REGISTERED, FMT_FULL_JSON)
        data = _parse_export_to_entries(content, ctype, filename)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["client_id"], "")
        self.assertEqual(data[0]["refresh_token"], "")  # OAuth 留空（不是 OpenAI）

    def test_import_full_json_roundtrip(self):
        """端到端：先用 full_json 导出，再用相同 fmt 导入回库，字段保真"""
        from src.services.account_pool_service import FMT_FULL_JSON, import_pool
        with get_session() as s:
            s.add(_make_run(
                run_id="r" + "t" * 15, email="round@x.com", password="pw",
                mail_provider="applemail",
                tokens={
                    "access_token": self._SAMPLE_JWT,
                    "refresh_token": "openai-rt",
                    "id_token": "ey-id",
                    "extracted_at": "2026-05-17T14:01:26+08:00",
                },
            ))
            s.add(MailAccount(
                label="round", provider_name="applemail", email="round@x.com",
                client_id="cid", refresh_token="oauth-rt",
            ))
            s.commit()

        content, _, _, _ = export_pool(TIER_REGISTERED, FMT_FULL_JSON)

        # 清库（删 Run，保留 MailAccount 以测 OAuth 写入分支）
        from src.db.models import Run as RunModel
        with get_session() as s:
            for r in s.exec(RunModel.__table__.select()).all():
                s.delete(s.get(RunModel, r.id))
            s.commit()

        # 导入回来
        result = import_pool(content.encode("utf-8"), FMT_FULL_JSON)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 0)

        # 验证 token 字段被正确写入 openai_tokens
        with get_session() as s:
            stmt = RunModel.__table__.select().where(RunModel.email == "round@x.com")
            row = s.exec(stmt).first()
            self.assertIsNotNone(row)
            self.assertEqual(row.email, "round@x.com")
            tokens = dict(row.openai_tokens or {})
            self.assertEqual(tokens.get("access_token"), self._SAMPLE_JWT)
            # full_json 导入应把 openai_refresh_token 写到 openai_tokens.refresh_token
            self.assertEqual(tokens.get("refresh_token"), "openai-rt")
            self.assertEqual(tokens.get("id_token"), "ey-id")

    def test_export_with_run_ids_filter(self):
        """传 run_ids → 仅导出选中行；文件名带 _xN 后缀。"""
        with get_session() as s:
            s.add(_make_run(run_id="aa" + "0" * 14, email="a@x.com"))
            s.add(_make_run(run_id="bb" + "0" * 14, email="b@x.com"))
            s.add(_make_run(run_id="cc" + "0" * 14, email="c@x.com"))
            s.commit()

        from src.services.account_pool_service import FMT_FULL_JSON
        content, ctype, filename, _skipped = export_pool(
            TIER_REGISTERED, FMT_FULL_JSON,
            run_ids=["aa" + "0" * 14, "cc" + "0" * 14],
        )
        # 2 条 → zip 打包
        self.assertEqual(ctype, "application/zip")
        self.assertTrue(filename.startswith("codex-registered-2-"), f"filename={filename}")
        self.assertTrue(filename.endswith(".zip"))
        rows = _parse_export_to_entries(content, ctype, filename)
        self.assertEqual(sorted(r["email"] for r in rows), ["a@x.com", "c@x.com"])

    def test_export_with_empty_run_ids_returns_empty(self):
        """传空列表（用户清空选中）→ 不应导出整个 tier，返回空。"""
        from src.services.account_pool_service import FMT_FULL_JSON
        with get_session() as s:
            s.add(_make_run(run_id="zz" + "0" * 14, email="z@x.com"))
            s.commit()
        content, _, filename, _skipped = export_pool(TIER_REGISTERED, FMT_FULL_JSON, run_ids=[])
        # 0 条 → 空数组（不是 zip）+ 兜底文件名
        self.assertEqual(content, "[]")
        self.assertTrue(filename.startswith("codex-empty-"))

    def test_export_run_ids_none_exports_all(self):
        """run_ids=None 时退化为导出整个 tier（向后兼容）。"""
        from src.services.account_pool_service import FMT_FULL_JSON
        with get_session() as s:
            s.add(_make_run(run_id="11" + "0" * 14, email="x@x.com"))
            s.add(_make_run(run_id="22" + "0" * 14, email="y@x.com"))
            s.commit()
        content, ctype, filename, _skipped = export_pool(TIER_REGISTERED, FMT_FULL_JSON, run_ids=None)
        # 2 条 → zip
        self.assertEqual(ctype, "application/zip")
        rows = _parse_export_to_entries(content, ctype, filename)
        self.assertEqual(len(rows), 2)

    # ── export_pool platform 过滤 ────────────────────────
    def test_export_platform_filter_only_grok(self):
        """platform=grok → 只导出 grok 账号。"""
        with get_session() as s:
            s.add(_make_run(run_id="g1" + "0" * 14, email="g1@x.com", platform=PLATFORM_OPENAI))
            s.add(_make_run(run_id="g2" + "0" * 14, email="g2@x.com", platform=PLATFORM_OPENAI))
            s.add(_make_run(run_id="k1" + "0" * 14, email="k1@x.com", platform=PLATFORM_GROK))
            s.commit()
        content, ctype, filename, _skipped = export_pool(
            TIER_REGISTERED, FMT_FULL_JSON, platform=PLATFORM_GROK
        )
        rows = _parse_export_to_entries(content, ctype, filename)
        self.assertEqual([r["email"] for r in rows], ["k1@x.com"])

    def test_export_default_platform_exports_all(self):
        """不传 platform → 全平台导出（向后兼容）。"""
        with get_session() as s:
            s.add(_make_run(run_id="g3" + "0" * 14, email="g3@x.com", platform=PLATFORM_OPENAI))
            s.add(_make_run(run_id="k2" + "0" * 14, email="k2@x.com", platform=PLATFORM_GROK))
            s.commit()
        content, ctype, filename, _skipped = export_pool(TIER_REGISTERED, FMT_FULL_JSON)
        rows = _parse_export_to_entries(content, ctype, filename)
        self.assertEqual(sorted(r["email"] for r in rows), ["g3@x.com", "k2@x.com"])

    def test_export_platform_with_run_ids_intersect(self):
        """platform 与 run_ids 取交集：选中里只导出匹配平台的行。"""
        with get_session() as s:
            s.add(_make_run(run_id="g4" + "0" * 14, email="g4@x.com", platform=PLATFORM_OPENAI))
            s.add(_make_run(run_id="k3" + "0" * 14, email="k3@x.com", platform=PLATFORM_GROK))
            s.commit()
        # 选中两条，但限定 platform=openai → 只剩 g4
        content, ctype, filename, _skipped = export_pool(
            TIER_REGISTERED, FMT_FULL_JSON,
            run_ids=["g4" + "0" * 14, "k3" + "0" * 14],
            platform=PLATFORM_OPENAI,
        )
        rows = _parse_export_to_entries(content, ctype, filename)
        self.assertEqual([r["email"] for r in rows], ["g4@x.com"])

    def test_export_zip_contains_one_file_per_account(self):
        """多账号 zip：每条账号一个独立 codex-{email}-{plan}.json"""
        from src.services.account_pool_service import FMT_FULL_JSON
        import io as _io
        import zipfile
        with get_session() as s:
            s.add(_make_run(
                run_id="z" + "1" * 15, email="alice@x.com",
                tokens={"access_token": self._SAMPLE_JWT},
            ))
            s.add(_make_run(
                run_id="z" + "2" * 15, email="bob@x.com",
                tokens={"access_token": self._SAMPLE_JWT},
            ))
            s.commit()

        content, ctype, filename, _ = export_pool(TIER_REGISTERED, FMT_FULL_JSON)
        self.assertEqual(ctype, "application/zip")
        self.assertTrue(filename.endswith(".zip"))

        with zipfile.ZipFile(_io.BytesIO(content)) as zf:
            names = sorted(zf.namelist())
        # 两个文件名都按命名规范 codex-{email}-free.json（JWT 解出 plan=free）
        self.assertEqual(names, [
            "codex-alice@x.com-free.json",
            "codex-bob@x.com-free.json",
        ])

    def test_export_zip_handles_duplicate_filename_with_suffix(self):
        """同 email + 同 plan 出现两次时，第二个文件名加 -2 后缀避免覆盖"""
        from src.services.account_pool_service import FMT_FULL_JSON
        import io as _io
        import zipfile
        # 不该发生但理论可能：手工往 DB 塞两条同 email 的 Run
        with get_session() as s:
            s.add(_make_run(
                run_id="d" + "1" * 15, email="dup@x.com",
                tokens={"access_token": self._SAMPLE_JWT},
            ))
            s.add(_make_run(
                run_id="d" + "2" * 15, email="dup@x.com",
                tokens={"access_token": self._SAMPLE_JWT},
            ))
            s.commit()

        content, ctype, _, _ = export_pool(TIER_REGISTERED, FMT_FULL_JSON)
        with zipfile.ZipFile(_io.BytesIO(content)) as zf:
            names = sorted(zf.namelist())
        # 第二个加 -2 后缀避免 zip 内文件重名导致解压覆盖
        self.assertEqual(names, [
            "codex-dup@x.com-free-2.json",
            "codex-dup@x.com-free.json",
        ])

    def test_export_filename_omits_plan_when_jwt_invalid(self):
        """JWT 解不出 plan_type 时，文件名退化为 codex-{email}.json"""
        from src.services.account_pool_service import FMT_FULL_JSON
        with get_session() as s:
            s.add(_make_run(
                run_id="n" + "p" * 15, email="noplan@x.com",
                tokens={"access_token": "not-a-jwt"},
            ))
            s.commit()

        _, _, filename, _ = export_pool(TIER_REGISTERED, FMT_FULL_JSON)
        self.assertEqual(filename, "codex-noplan@x.com.json")

    def test_export_only_returns_success_runs(self):
        """failed 状态的号不导出。"""
        from src.services.account_pool_service import FMT_FULL_JSON
        with get_session() as s:
            s.add(_make_run(run_id="ok" + "0" * 14, email="ok@x.com", status="success"))
            s.add(_make_run(run_id="fl" + "0" * 14, email="fail@x.com", status="failed"))
            s.commit()

        content, ctype, filename, _skipped = export_pool(TIER_REGISTERED, FMT_FULL_JSON)
        # 只剩 1 条 success → 单 JSON object
        rows = _parse_export_to_entries(content, ctype, filename)
        self.assertEqual([r["email"] for r in rows], ["ok@x.com"])


class ImportPoolTest(unittest.TestCase):
    """覆盖 import_pool：补号池语义、判重、错误处理、MailAccount 联动。"""

    def setUp(self):
        self.engine = _build_threadsafe_engine()
        self._original = engine_mod._engine
        engine_mod._engine = self.engine

    def tearDown(self):
        engine_mod._engine = self._original

    def test_import_credentials_csv_creates_registered_run(self):
        """CSV 导入 → Run(status=success, tier=registered, imported=true)。"""
        csv_content = (
            "email,password,mail_provider,client_id,refresh_token,account_tier,created_at\n"
            "new1@x.com,pw1,applemail,cid-1,rt-1,registered,2026-05-12T00:00:00+00:00\n"
            "new2@x.com,pw2,applemail,cid-2,rt-2,registered,2026-05-12T00:00:00+00:00\n"
        ).encode("utf-8")

        result = import_pool(csv_content, FMT_CREDENTIALS_CSV)
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["errors"], [])

        with get_session() as s:
            runs = s.query(Run).order_by(Run.email).all()
            self.assertEqual([r.email for r in runs], ["new1@x.com", "new2@x.com"])
            for r in runs:
                self.assertEqual(r.status, "success")
                self.assertEqual(r.account_tier, TIER_REGISTERED)
                self.assertTrue(r.config_snapshot.get("imported"))
                self.assertEqual(r.config_snapshot.get("import_format"), FMT_CREDENTIALS_CSV)
            # 同时建了 MailAccount
            mails = s.query(MailAccount).all()
            emails = sorted(m.email for m in mails)
            self.assertEqual(emails, ["new1@x.com", "new2@x.com"])
            # 凭据解密后可读
            ma = next(m for m in mails if m.email == "new1@x.com")
            self.assertEqual(ma.client_id, "cid-1")
            self.assertEqual(ma.refresh_token, "rt-1")

    def test_import_skip_duplicate_email(self):
        """同 email 已存在 → 跳过，不重复创建。"""
        with get_session() as s:
            s.add(_make_run(run_id="e" * 16, email="exists@x.com"))
            s.commit()

        csv_content = (
            "email,password,mail_provider,client_id,refresh_token,account_tier,created_at\n"
            "exists@x.com,pw,applemail,cid,rt,registered,\n"
            "newcomer@x.com,pw,applemail,cid,rt,registered,\n"
        ).encode("utf-8")

        result = import_pool(csv_content, FMT_CREDENTIALS_CSV)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 1)

        with get_session() as s:
            emails = sorted(r.email for r in s.query(Run).all())
            self.assertEqual(emails, ["exists@x.com", "newcomer@x.com"])

    def test_import_missing_email_recorded_as_error(self):
        """缺 email 的行进 errors 列表，不入库。"""
        csv_content = (
            "email,password,mail_provider,client_id,refresh_token,account_tier,created_at\n"
            ",pw,applemail,cid,rt,registered,\n"
            "valid@x.com,pw,applemail,cid,rt,registered,\n"
        ).encode("utf-8")

        result = import_pool(csv_content, FMT_CREDENTIALS_CSV)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("缺少 email", result["errors"][0])

    def test_import_cpa_json_persists_tokens(self):
        """CPA JSON 导入 → openai_tokens 字段填充。"""
        payload = [{
            "email": "cpa@x.com", "access_token": "ey-AT", "refresh_token": "rt-RT",
            "id_token": "ey-IT", "last_refresh": "2026-05-12T00:00:00Z",
            "expired": "2026-06-12T00:00:00Z", "type": "registered",
        }]
        content = json.dumps(payload).encode("utf-8")

        result = import_pool(content, FMT_CPA_JSON)
        self.assertEqual(result["imported"], 1)

        with get_session() as s:
            run = s.query(Run).filter_by(email="cpa@x.com").one()
            self.assertEqual(run.openai_tokens["access_token"], "ey-AT")
            self.assertEqual(run.openai_tokens["refresh_token"], "rt-RT")
            self.assertEqual(run.openai_tokens["id_token"], "ey-IT")
            self.assertEqual(run.openai_tokens["extracted_at"], "2026-05-12T00:00:00Z")
            self.assertEqual(run.openai_tokens["expires_at"], "2026-06-12T00:00:00Z")

    def test_import_invalid_fmt_raises(self):
        with self.assertRaises(ValueError):
            import_pool(b"x", "xml")

    def test_import_malformed_json_raises(self):
        with self.assertRaises(ValueError):
            import_pool(b"{not json", FMT_CPA_JSON)

    def test_import_csv_handles_bom(self):
        """带 UTF-8 BOM 的 CSV 也能正确解析。"""
        csv_content = b"\xef\xbb\xbfemail,password\nbom@x.com,pw\n"
        result = import_pool(csv_content, FMT_CREDENTIALS_CSV)
        self.assertEqual(result["imported"], 1)


class GenerateLinkTest(unittest.TestCase):
    """新拆出的 generate_link：无卡、支持 Pro、promo 透传。"""

    def setUp(self):
        self.engine = _build_threadsafe_engine()
        self._original = engine_mod._engine
        engine_mod._engine = self.engine

    def tearDown(self):
        engine_mod._engine = self._original

    def _make_registered(self, run_id="g" * 16, token="tok-link"):
        with get_session() as s:
            s.add(_make_run(
                run_id=run_id, email="g@x.com", tier=TIER_REGISTERED,
                snapshot={"access_token": token},
            ))
            s.commit()

    def test_generate_link_no_card_required(self):
        """完全不传 card_key 也能生成链接。"""
        self._make_registered()
        with mock.patch(
            "src.payment_link.PaymentLinkGenerator.generate_checkout_link",
            return_value=(True, "https://checkout.example/abc"),
        ) as mock_gen:
            result = generate_link("g" * 16, plan="team", return_mode="long")

        self.assertEqual(result["link"], "https://checkout.example/abc")
        self.assertEqual(result["plan"], "team")
        self.assertEqual(result["return_mode"], "long")
        # 调用 PaymentLinkGenerator 时不应包含 card_key
        kwargs = mock_gen.call_args.kwargs
        self.assertEqual(kwargs["plan_type"], "team")
        self.assertEqual(kwargs["return_mode"], "long")
        self.assertNotIn("card_key", kwargs)

    def test_generate_link_team_with_promo_code_and_campaign(self):
        """promo_code 和 promo_campaign_id 都应透传给 PaymentLinkGenerator。"""
        self._make_registered()
        with mock.patch(
            "src.payment_link.PaymentLinkGenerator.generate_checkout_link",
            return_value=(True, "https://checkout.example/uk?promoCode=datroaiuk"),
        ) as mock_gen:
            result = generate_link(
                "g" * 16,
                plan="team",
                return_mode="long",
                seat_quantity=2,
                promo_code="datroaiuk",
                promo_campaign_id="custom-campaign",
                aimizy_country="GB",
                aimizy_currency="GBP",
                workspace_name="MyTeam",
            )

        kwargs = mock_gen.call_args.kwargs
        self.assertEqual(kwargs["plan_type"], "team")
        self.assertEqual(kwargs["promo_code"], "datroaiuk")
        self.assertEqual(kwargs["promo_campaign_id"], "custom-campaign")
        self.assertEqual(kwargs["seat_quantity"], 2)
        self.assertEqual(kwargs["aimizy_country"], "GB")
        self.assertEqual(kwargs["aimizy_currency"], "GBP")
        self.assertEqual(kwargs["workspace_name"], "MyTeam")
        self.assertEqual(result["link"], "https://checkout.example/uk?promoCode=datroaiuk")

    def test_generate_link_pro_passes_plan_type(self):
        """Pro / Pro Lite 应作为 plan_type 透传（实际 payload 字段由 PaymentLinkGenerator 决定）。"""
        self._make_registered()
        for plan in ("pro", "pro_lite"):
            with mock.patch(
                "src.payment_link.PaymentLinkGenerator.generate_checkout_link",
                return_value=(True, f"https://checkout.example/{plan}"),
            ) as mock_gen:
                generate_link("g" * 16, plan=plan, return_mode="app")
            self.assertEqual(mock_gen.call_args.kwargs["plan_type"], plan)

    def test_generate_link_invalid_plan(self):
        self._make_registered()
        with self.assertRaises(ValueError):
            generate_link("g" * 16, plan="enterprise")

    def test_generate_link_invalid_return_mode(self):
        self._make_registered()
        with self.assertRaises(ValueError):
            generate_link("g" * 16, plan="team", return_mode="weird")

    def test_generate_link_writes_link_generated_event(self):
        """生成链接成功应写一条 RunEvent(event_type='link_generated')。"""
        self._make_registered()
        with mock.patch(
            "src.payment_link.PaymentLinkGenerator.generate_checkout_link",
            return_value=(True, "https://checkout.example/x"),
        ):
            generate_link("g" * 16, plan="plus", return_mode="long", promo_code="datro")

        with get_session() as s:
            evs = s.query(RunEvent).filter_by(
                run_id="g" * 16, event_type="link_generated"
            ).all()
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0].payload["plan"], "plus")
            self.assertEqual(evs[0].payload["return_mode"], "long")
            self.assertTrue(evs[0].payload["has_promo_code"])

    def test_generate_link_wrong_tier_rejected(self):
        with get_session() as s:
            s.add(_make_run(run_id="x" * 16, email="e@x.com", tier=TIER_PLUS,
                            snapshot={"access_token": "tok"}))
            s.commit()
        with self.assertRaises(ValueError) as cm:
            generate_link("x" * 16, plan="team", return_mode="long")
        self.assertIn("registered", str(cm.exception))

    def test_generate_link_writes_failed_event_and_friendly_error(self):
        """生成失败（如 401）应写 RunEvent(link_generate_failed) 并抛中文友好原因。"""
        self._make_registered()
        with mock.patch(
            "src.payment_link.PaymentLinkGenerator.generate_checkout_link",
            return_value=(False, "Access Token 无效或已过期 (401 Unauthorized)"),
        ):
            with self.assertRaises(ValueError) as cm:
                generate_link("g" * 16, plan="plus", return_mode="long")
        # 抛出的是友好中文原因（前端 toast 直接显示）
        self.assertIn("失效", str(cm.exception))
        # 失败也留痕，payload 含原因 + 耗时
        with get_session() as s:
            evs = s.query(RunEvent).filter_by(
                run_id="g" * 16, event_type="link_generate_failed"
            ).all()
            self.assertEqual(len(evs), 1)
            self.assertIn("失效", evs[0].payload["reason"])
            self.assertIn("401", evs[0].payload["raw_error"])
            self.assertIn("elapsed_ms", evs[0].payload)


class AssignCardTest(unittest.TestCase):
    """新拆出的 assign_card：仅校验卡 + 写 RunEvent。"""

    def setUp(self):
        self.engine = _build_threadsafe_engine()
        self._original = engine_mod._engine
        engine_mod._engine = self.engine

    def tearDown(self):
        engine_mod._engine = self._original

    def test_assign_card_writes_event_no_link_returned(self):
        with get_session() as s:
            s.add(_make_run(run_id="a" * 16, email="e@x.com", tier=TIER_REGISTERED))
            s.add(CardActivation(
                card_key="card-ok",
                card_provider="efuncard",
                card_number="4111111111111111",
                expiry_month="06", expiry_year="2030", cvv="123",
                target_warmup_count=2, warmup_count=2,
            ))
            s.commit()

        result = assign_card("a" * 16, "card-ok", note="UK promo")
        self.assertEqual(result["card_key"], "card-ok")
        self.assertEqual(result["card_last4"], "1111")
        self.assertNotIn("link", result)  # 不返回链接

        with get_session() as s:
            evs = s.query(RunEvent).filter_by(
                run_id="a" * 16, event_type="card_assigned"
            ).all()
            self.assertEqual(len(evs), 1)
            self.assertEqual(evs[0].payload["card_last4"], "1111")
            self.assertEqual(evs[0].payload["note"], "UK promo")

    def test_assign_card_rejects_unknown_card(self):
        with get_session() as s:
            s.add(_make_run(run_id="b" * 16, email="e@x.com", tier=TIER_REGISTERED))
            s.commit()
        with self.assertRaises(ValueError) as cm:
            assign_card("b" * 16, "ghost-card")
        self.assertIn("卡密不在卡池", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
