# -*- coding: utf-8 -*-
"""promo_eligibility_service 集成测试

使用 in-memory SQLite + StaticPool（沿用 tests/test_card_pool_service.py 的模式）。
mock 掉 check_eligibility 客户端函数，验证 service 编排逻辑：
  - 模板不存在 / promo_code 为空 → PromoVerifyError
  - 没有 status='success' 的 Run → PromoVerifyError(no_account)
  - 借到 token + 找到代理 → 调 client + 回写 last_eligibility_*
  - LinkTemplate.proxy_id 指定时优先用模板代理
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel

import src.db.engine as engine_mod
from src.db.engine import get_session
from src.db.models import EligibilityStatus, LinkTemplate, Proxy, Run
from src.promo_eligibility.client import EligibilityResult


def _make_engine():
    """in-memory SQLite + StaticPool（多线程共享同一份 DB）"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _make_run(session: Session, *, email: str, status: str, access_token: str) -> Run:
    """创建一条 Run，把 access_token 写到 openai_tokens 里（最高优先级路径）"""
    r = Run(
        email=email,
        status=status,
        openai_tokens={"access_token": access_token},
    )
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


def _make_template(
    session: Session,
    *,
    name: str,
    promo_code: str,
    country: str = "US",
    currency: str = "USD",
    proxy_id=None,
) -> LinkTemplate:
    t = LinkTemplate(
        name=name,
        plan="team",
        promo_code=promo_code,
        aimizy_country=country,
        aimizy_currency=currency,
        proxy_id=proxy_id,
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def _make_proxy(session: Session, *, label: str, country: str, url: str, is_active: bool = True) -> Proxy:
    p = Proxy(label=label, country=country, url=url, is_active=is_active)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


class PromoEligibilityServiceTests(unittest.TestCase):
    """verify_link_template 主路径"""

    def setUp(self):
        # 替换全局 engine 为内存版
        self._old_engine = engine_mod._engine
        engine_mod._engine = _make_engine()

    def tearDown(self):
        engine_mod._engine = self._old_engine

    def test_template_not_found_raises(self):
        from src.services.promo_eligibility_service import (
            PromoVerifyError,
            verify_link_template,
        )

        with self.assertRaises(PromoVerifyError) as ctx:
            verify_link_template(99999)
        self.assertEqual(ctx.exception.code, "template_not_found")

    def test_template_without_promo_code_raises(self):
        from src.services.promo_eligibility_service import (
            PromoVerifyError,
            verify_link_template,
        )

        with get_session() as session:
            tpl = _make_template(session, name="no-promo", promo_code="")
            tpl_id = tpl.id

        with self.assertRaises(PromoVerifyError) as ctx:
            verify_link_template(tpl_id)
        self.assertEqual(ctx.exception.code, "no_promo_code")

    def test_no_completed_run_raises_no_account(self):
        from src.services.promo_eligibility_service import (
            PromoVerifyError,
            verify_link_template,
        )

        with get_session() as session:
            tpl = _make_template(session, name="t1", promo_code="talentgeniusus")
            tpl_id = tpl.id
            # 创建一个 pending Run（不算 completed）
            _make_run(session, email="x@x.com", status="pending", access_token="ey.tok")

        with self.assertRaises(PromoVerifyError) as ctx:
            verify_link_template(tpl_id)
        self.assertEqual(ctx.exception.code, "no_account")

    def test_explicit_run_id_not_found_raises(self):
        from src.services.promo_eligibility_service import (
            PromoVerifyError,
            verify_link_template,
        )

        with get_session() as session:
            tpl = _make_template(session, name="t2", promo_code="talentgeniusus")
            tpl_id = tpl.id

        with self.assertRaises(PromoVerifyError) as ctx:
            verify_link_template(tpl_id, run_id="nonexistent")
        self.assertEqual(ctx.exception.code, "run_not_found")

    def test_run_without_token_raises_no_token(self):
        from src.services.promo_eligibility_service import (
            PromoVerifyError,
            verify_link_template,
        )

        with get_session() as session:
            tpl = _make_template(session, name="t3", promo_code="talentgeniusus")
            tpl_id = tpl.id
            run = Run(email="empty@x.com", status="success", openai_tokens={})
            session.add(run)
            session.commit()

        with self.assertRaises(PromoVerifyError) as ctx:
            verify_link_template(tpl_id)
        self.assertEqual(ctx.exception.code, "no_token")

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_eligible_writes_back_to_db(self, mock_check):
        """主路径：成功调用 client → 状态回写到 last_eligibility_*"""
        from src.services.promo_eligibility_service import verify_link_template

        mock_check.return_value = EligibilityResult(
            code="talentgeniusus",
            status=EligibilityStatus.ELIGIBLE,
            metadata_raw={"metadata": {"discount": {"value": 25}}},
            http_status=200,
        )

        with get_session() as session:
            tpl = _make_template(session, name="t-eligible", promo_code="talentgeniusus")
            tpl_id = tpl.id
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        result = verify_link_template(tpl_id)

        # 返回 dict 正确
        self.assertEqual(result["status"], EligibilityStatus.ELIGIBLE)
        self.assertEqual(result["promo_code"], "talentgeniusus")
        self.assertIn("checked_at", result)
        self.assertIn("metadata", result)

        # DB 已回写
        with get_session() as session:
            updated = session.get(LinkTemplate, tpl_id)
            self.assertEqual(updated.last_eligibility_status, EligibilityStatus.ELIGIBLE)
            self.assertIsNotNone(updated.last_eligibility_check_at)
            self.assertIn("metadata", updated.last_eligibility_metadata)

        # 调用 client 时传了正确的 token + 没有代理
        mock_check.assert_called_once()
        kwargs = mock_check.call_args.kwargs
        self.assertEqual(kwargs["access_token"], "ey.GOOD")
        self.assertEqual(kwargs["code"], "talentgeniusus")
        self.assertIsNone(kwargs["proxy_url"])  # 没建 Proxy 行

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_country_proxy_selected_automatically(self, mock_check):
        """模板有 country=US 且 DB 里有 country=US active Proxy → 自动选中"""
        from src.services.promo_eligibility_service import verify_link_template

        mock_check.return_value = EligibilityResult(
            code="talentgeniusus", status=EligibilityStatus.ELIGIBLE,
        )

        with get_session() as session:
            _make_proxy(session, label="US-1", country="US", url="socks5h://u:p@us-host:1080")
            tpl = _make_template(session, name="t-proxy", promo_code="talentgeniusus", country="US")
            tpl_id = tpl.id
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        verify_link_template(tpl_id)

        self.assertEqual(
            mock_check.call_args.kwargs["proxy_url"],
            "socks5h://u:p@us-host:1080",
        )

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_metadata_records_proxy_resolved_true_when_proxy_selected(self, mock_check):
        """metadata.proxy_resolved=True 当成功按国家选到代理"""
        from src.services.promo_eligibility_service import verify_link_template

        mock_check.return_value = EligibilityResult(
            code="talentgeniusus", status=EligibilityStatus.ELIGIBLE,
        )
        with get_session() as session:
            _make_proxy(session, label="US-1", country="US", url="socks5h://u:p@us:1080")
            tpl = _make_template(session, name="t-meta1", promo_code="talentgeniusus", country="US")
            tpl_id = tpl.id
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        ret = verify_link_template(tpl_id)
        self.assertTrue(ret["metadata"]["proxy_resolved"])

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_metadata_records_proxy_resolved_false_when_no_proxy(self, mock_check):
        """metadata.proxy_resolved=False 当 DB 完全没有 active 代理 → 直连诊断信号"""
        from src.services.promo_eligibility_service import verify_link_template

        mock_check.return_value = EligibilityResult(
            code="talentgeniusus", status=EligibilityStatus.NOT_FOUND,
        )
        with get_session() as session:
            # 故意不建任何代理（fallback 也找不到）
            tpl = _make_template(session, name="t-meta2", promo_code="talentgeniusus", country="SG")
            tpl_id = tpl.id
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        ret = verify_link_template(tpl_id)
        self.assertFalse(ret["metadata"]["proxy_resolved"])
        self.assertFalse(ret["metadata"]["proxy_country_matched"])
        # client 也应收到 proxy_url=None（已有的契约，再次确认未回归）
        self.assertIsNone(mock_check.call_args.kwargs["proxy_url"])

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_fallback_to_any_active_proxy_when_no_country_match(self, mock_check):
        """目标国家没对应代理但 DB 有其他 active 代理 → fallback 选一个，
        而不是直连必 403。这是修复 16/27 个 fallback=None 模板的核心逻辑。
        """
        from src.services.promo_eligibility_service import verify_link_template

        mock_check.return_value = EligibilityResult(
            code="thinkingmachinessg", status=EligibilityStatus.EXISTS,
        )
        with get_session() as session:
            # 只有 CA 代理，模板 country=SG → 应 fallback 到 CA
            _make_proxy(session, label="CA-1", country="CA", url="socks5h://u:p@ca:1080")
            tpl = _make_template(session, name="t-sg", promo_code="thinkingmachinessg", country="SG")
            tpl_id = tpl.id
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        ret = verify_link_template(tpl_id)
        # 用上代理了，但不是对应国家
        self.assertTrue(ret["metadata"]["proxy_resolved"])
        self.assertFalse(ret["metadata"]["proxy_country_matched"])
        # client 收到 CA 代理 URL
        self.assertEqual(mock_check.call_args.kwargs["proxy_url"], "socks5h://u:p@ca:1080")

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_metadata_country_matched_true_when_country_proxy_used(self, mock_check):
        """对应国家代理可用时 proxy_country_matched=True，与 fallback 路径区分"""
        from src.services.promo_eligibility_service import verify_link_template

        mock_check.return_value = EligibilityResult(
            code="datroaius", status=EligibilityStatus.ELIGIBLE,
        )
        with get_session() as session:
            # 同时建 CA 和 US；模板 country=US → 应选 US（不 fallback）
            _make_proxy(session, label="CA-1", country="CA", url="socks5h://u:p@ca:1080")
            _make_proxy(session, label="US-1", country="US", url="socks5h://u:p@us:1080")
            tpl = _make_template(session, name="t-us", promo_code="datroaius", country="US")
            tpl_id = tpl.id
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        ret = verify_link_template(tpl_id)
        self.assertTrue(ret["metadata"]["proxy_resolved"])
        self.assertTrue(ret["metadata"]["proxy_country_matched"])
        self.assertEqual(mock_check.call_args.kwargs["proxy_url"], "socks5h://u:p@us:1080")

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_template_proxy_id_overrides_country_lookup(self, mock_check):
        """LinkTemplate.proxy_id 显式指定时优先级高于按 country 选"""
        from src.services.promo_eligibility_service import verify_link_template

        mock_check.return_value = EligibilityResult(
            code="talentgeniusus", status=EligibilityStatus.ELIGIBLE,
        )

        with get_session() as session:
            # 故意建两个：一个 country=US 另一个 country=GB
            us_proxy = _make_proxy(session, label="US-1", country="US", url="socks5h://u:p@us:1080")
            gb_proxy = _make_proxy(session, label="GB-1", country="GB", url="socks5h://u:p@gb:1080")
            # 模板 country=US 但 proxy_id 绑了 GB 代理 → 应优先用 GB
            tpl = _make_template(
                session, name="t-override", promo_code="talentgeniusus",
                country="US", proxy_id=gb_proxy.id,
            )
            tpl_id = tpl.id
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        verify_link_template(tpl_id)

        self.assertEqual(
            mock_check.call_args.kwargs["proxy_url"],
            "socks5h://u:p@gb:1080",
        )

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_inactive_proxy_not_selected(self, mock_check):
        """is_active=False 的代理不能被 get_active_proxy_by_country 选中"""
        from src.services.promo_eligibility_service import verify_link_template

        mock_check.return_value = EligibilityResult(
            code="talentgeniusus", status=EligibilityStatus.ELIGIBLE,
        )

        with get_session() as session:
            _make_proxy(session, label="US-OFF", country="US", url="socks5h://u:p@off:1", is_active=False)
            tpl = _make_template(session, name="t-noproxy", promo_code="talentgeniusus", country="US")
            tpl_id = tpl.id
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        verify_link_template(tpl_id)

        self.assertIsNone(mock_check.call_args.kwargs["proxy_url"])

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_error_status_also_written_to_db(self, mock_check):
        """client 返回 error 状态时，error 信息也要进 DB（便于排查）"""
        from src.services.promo_eligibility_service import verify_link_template

        mock_check.return_value = EligibilityResult(
            code="talentgeniusus",
            status=EligibilityStatus.ERROR,
            http_status=403,
            error="代理被 Cloudflare 拦截",
        )

        with get_session() as session:
            tpl = _make_template(session, name="t-err", promo_code="talentgeniusus")
            tpl_id = tpl.id
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        result = verify_link_template(tpl_id)
        self.assertEqual(result["status"], EligibilityStatus.ERROR)

        with get_session() as session:
            tpl = session.get(LinkTemplate, tpl_id)
            self.assertEqual(tpl.last_eligibility_status, EligibilityStatus.ERROR)
            self.assertIn("Cloudflare", tpl.last_eligibility_metadata.get("error", ""))
            self.assertEqual(tpl.last_eligibility_metadata.get("http_status"), 403)

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_preserves_import_note_during_single_verify(self, mock_check):
        """回归测试：单条 verify_link_template 必须保留之前导入时写入的 import_note。

        历史 bug：原 verify_link_template 直接覆盖 last_eligibility_metadata，
        会把 promo_import_service 写入的 import_note 抹掉；提取
        _write_back_eligibility_result 后此行为应与 bulk_verify 一致。
        """
        from src.services.promo_eligibility_service import verify_link_template

        mock_check.return_value = EligibilityResult(
            code="talentgeniusus",
            status=EligibilityStatus.ELIGIBLE,
            metadata_raw={"metadata": {"discount": {"value": 25}}},
            http_status=200,
        )

        with get_session() as session:
            tpl = _make_template(session, name="t-import", promo_code="talentgeniusus")
            tpl_id = tpl.id
            # 模拟之前的导入：先写入 import_note
            tpl.last_eligibility_metadata = {"import_note": "source=valid | company=TalentGenius | price_usd=25"}
            session.add(tpl)
            session.commit()
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        verify_link_template(tpl_id)

        # 验证后 import_note 仍在
        with get_session() as session:
            updated = session.get(LinkTemplate, tpl_id)
            self.assertEqual(updated.last_eligibility_status, EligibilityStatus.ELIGIBLE)
            note = updated.last_eligibility_metadata.get("import_note", "")
            self.assertIn("source=valid", note)
            self.assertIn("TalentGenius", note)
            # 新字段也写进去了
            self.assertIn("metadata", updated.last_eligibility_metadata)

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_concurrent_delete_during_writeback_does_not_crash(self, mock_check):
        """边界：client 调用返回后，模板被并发删除 —— _write_back_eligibility_result 应优雅处理"""
        from src.services.promo_eligibility_service import verify_link_template

        mock_check.return_value = EligibilityResult(
            code="talentgeniusus", status=EligibilityStatus.ELIGIBLE,
        )

        with get_session() as session:
            tpl = _make_template(session, name="t-race", promo_code="talentgeniusus")
            tpl_id = tpl.id
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        # 模拟并发删除：在 verify_link_template 取完模板后但 client 调用返回前删模板
        # 简化做法：在 client mock 的 side_effect 里删模板
        def delete_then_return(*args, **kwargs):
            with get_session() as session:
                tpl = session.get(LinkTemplate, tpl_id)
                if tpl:
                    session.delete(tpl)
                    session.commit()
            return mock_check.return_value

        mock_check.side_effect = delete_then_return

        # 不应抛异常
        result = verify_link_template(tpl_id)
        self.assertEqual(result["status"], EligibilityStatus.ELIGIBLE)
        # 模板确实被删
        with get_session() as session:
            self.assertIsNone(session.get(LinkTemplate, tpl_id))


class BulkVerifyAllTemplatesTests(unittest.TestCase):
    """bulk_verify_all_templates 批量验证编排"""

    def setUp(self):
        self._old_engine = engine_mod._engine
        engine_mod._engine = _make_engine()

    def tearDown(self):
        engine_mod._engine = self._old_engine

    def test_no_templates_raises(self):
        """没有任何 promo_code 模板时报 no_promo_templates"""
        from src.services.promo_eligibility_service import (
            PromoVerifyError,
            bulk_verify_all_templates,
        )

        with get_session() as session:
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")
            # 故意建一个无 promo_code 的模板，确保不被算入
            _make_template(session, name="no-promo", promo_code="")

        with self.assertRaises(PromoVerifyError) as ctx:
            bulk_verify_all_templates(delay_sec=0)
        self.assertEqual(ctx.exception.code, "no_promo_templates")

    def test_no_token_raises_no_account(self):
        """没有 completed Run → no_account"""
        from src.services.promo_eligibility_service import (
            PromoVerifyError,
            bulk_verify_all_templates,
        )

        with get_session() as session:
            _make_template(session, name="t1", promo_code="talentgeniusus")

        with self.assertRaises(PromoVerifyError) as ctx:
            bulk_verify_all_templates(delay_sec=0)
        self.assertEqual(ctx.exception.code, "no_account")

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_bulk_verifies_all_and_aggregates(self, mock_check):
        """3 条模板 → 3 次调用 → by_status 正确聚合"""
        from src.services.promo_eligibility_service import bulk_verify_all_templates

        # 不同 promo_code 返回不同 status
        def fake_check(*, access_token, code, proxy_url):
            return {
                "talentgeniusus": EligibilityResult(code=code, status=EligibilityStatus.ELIGIBLE),
                "monicaius": EligibilityResult(code=code, status=EligibilityStatus.EXISTS),
                "deadcode": EligibilityResult(code=code, status=EligibilityStatus.NOT_FOUND),
            }[code]

        mock_check.side_effect = fake_check

        with get_session() as session:
            _make_template(session, name="t1", promo_code="talentgeniusus", country="US")
            _make_template(session, name="t2", promo_code="monicaius", country="US")
            _make_template(session, name="t3", promo_code="deadcode", country="US")
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        result = bulk_verify_all_templates(delay_sec=0)

        self.assertEqual(result["total"], 3)
        self.assertEqual(result["verified"], 3)
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["by_status"], {
            EligibilityStatus.ELIGIBLE: 1,
            EligibilityStatus.EXISTS: 1,
            EligibilityStatus.NOT_FOUND: 1,
        })
        self.assertFalse(result["stopped_early"])
        # 全部 DB 已回写
        with get_session() as session:
            from sqlmodel import select
            rows = list(session.exec(select(LinkTemplate)).all())
        statuses = sorted(r.last_eligibility_status for r in rows)
        self.assertEqual(statuses, sorted([
            EligibilityStatus.ELIGIBLE, EligibilityStatus.EXISTS, EligibilityStatus.NOT_FOUND,
        ]))

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_stops_on_401_when_stop_on_token_error_true(self, mock_check):
        """遇到 http_status=401 立即停止；后续模板不被处理"""
        from src.services.promo_eligibility_service import bulk_verify_all_templates

        # 第一条返回 401，后续不应再被调用
        mock_check.side_effect = [
            EligibilityResult(
                code="c1", status=EligibilityStatus.ERROR,
                http_status=401, error="access_token 无效或过期 (401)",
            ),
            EligibilityResult(code="c2", status=EligibilityStatus.ELIGIBLE),
        ]

        with get_session() as session:
            _make_template(session, name="t1", promo_code="c1", country="US")
            _make_template(session, name="t2", promo_code="c2", country="US")
            _make_run(session, email="x@x.com", status="success", access_token="ey.BAD")

        result = bulk_verify_all_templates(delay_sec=0, stop_on_token_error=True)

        self.assertTrue(result["stopped_early"])
        self.assertEqual(result["verified"], 1)
        self.assertEqual(mock_check.call_count, 1)  # 第二条没被调

    @mock.patch("src.services.promo_eligibility_service.check_eligibility")
    def test_preserves_import_note_during_bulk_verify(self, mock_check):
        """批量验证回写时保留 last_eligibility_metadata.import_note"""
        from src.services.promo_eligibility_service import bulk_verify_all_templates

        mock_check.return_value = EligibilityResult(
            code="talentgeniusus", status=EligibilityStatus.ELIGIBLE,
        )

        with get_session() as session:
            tpl = _make_template(session, name="t1", promo_code="talentgeniusus")
            tpl.last_eligibility_metadata = {"import_note": "source=valid | company=TalentGenius"}
            session.add(tpl)
            session.commit()
            tpl_id = tpl.id
            _make_run(session, email="x@x.com", status="success", access_token="ey.GOOD")

        bulk_verify_all_templates(delay_sec=0)

        with get_session() as session:
            updated = session.get(LinkTemplate, tpl_id)
        self.assertEqual(updated.last_eligibility_status, EligibilityStatus.ELIGIBLE)
        # import_note 没被验证结果覆盖掉
        self.assertEqual(
            updated.last_eligibility_metadata.get("import_note"),
            "source=valid | company=TalentGenius",
        )


if __name__ == "__main__":
    unittest.main()
