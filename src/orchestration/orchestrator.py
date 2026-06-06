# -*- coding: utf-8 -*-
"""
Phase Orchestrator

将注册流程拆为 3 个可检查点、可恢复的阶段：
1. Registration — 使用现有 RegistrationStateMachine
2. Token Extraction — 提取 session token
3. Payment — 可选的订阅支付流程

替代 main.py 的 run_task()，提供断点续跑能力。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from src.automation import (
    ArtifactRecorder,
    AutomationRuntime,
    AutomationState,
    ExperienceStore,
    RegistrationStateMachine,
    extract_session_tokens_with_http,
)
from src.automation.captcha_solver import build_solver_from_config
from src.automation.triage import attach_log_buffer, build_triage_provider
from src.config import AppConfig
from src.db.engine import get_session
from src.db.models import Checkpoint, Run, RunEvent
from src.orchestration.handlers import (
    build_runtime_handlers,
    dismiss_home_welcome_modal,
    prepare_clean_start_page,
    resolve_card_with_retry,
)
from src.orchestration.preflight import (
    fetch_exit_ip_from_page,
    run_preflight,
    write_run_exit_ip,
)
from src.providers.browser import BrowserProvider
from src.providers.card import CardProvider
from src.providers.mail import MailProvider
from src.services.event_service import EventBroadcaster
from src.utils import human_delay

logger = logging.getLogger(__name__)

# 阶段定义
PHASE_REGISTRATION = "registration"
PHASE_TOKEN_EXTRACTION = "token_extraction"
PHASE_PAYMENT = "payment"
PHASE_ORDER = [PHASE_REGISTRATION, PHASE_TOKEN_EXTRACTION, PHASE_PAYMENT]


@dataclass
class RunResult:
    """编排器运行结果"""
    success: bool
    run_id: str
    phase: str = ""
    email: str = ""
    access_token: str = ""
    refresh_token: str = ""
    checkout_link: str = ""
    error_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class PhaseOrchestrator:
    """
    三阶段编排器。

    将 main.py 的 run_task() 拆为独立阶段，每个阶段完成后
    写入 Checkpoint，失败时可从最近检查点恢复。
    """

    def __init__(
        self,
        config: AppConfig,
        browser_provider: BrowserProvider,
        card_provider: Optional[CardProvider] = None,
        mail_provider: Optional[MailProvider] = None,
        event_broadcaster: Optional[EventBroadcaster] = None,
    ) -> None:
        self._config = config
        self._browser = browser_provider
        self._card = card_provider
        self._mail = mail_provider
        self._events = event_broadcaster or EventBroadcaster()

    def execute(
        self,
        *,
        profile_id: str,
        email: str,
        password: str,
        card_key: str = "",
        run_id: Optional[str] = None,
    ) -> RunResult:
        """
        执行完整的注册→提取→支付流程。

        Args:
            profile_id: 浏览器配置文件 ID
            email: 注册邮箱
            password: 注册密码
            card_key: 虚拟卡密钥
            run_id: 已有 Run ID（断点续跑时传入）

        Returns:
            RunResult
        """
        # 创建或加载 Run 记录
        run = self._create_or_load_run(
            run_id=run_id,
            email=email,
            password=password,
            profile_id=profile_id,
            card_key=card_key,
        )

        # 确定起始阶段
        start_phase = self._resolve_start_phase(run.id)

        self._update_run_status(run.id, "running", phase=start_phase)
        self._events.emit_sync(run.id, "orchestrator_start", payload={"start_phase": start_phase})

        result = RunResult(success=False, run_id=run.id, email=email)

        # Preflight（数据层一致性；指纹评分需要 page，延后到 _phase_registration 里做）
        try:
            card_bin_country = ""
            proxy_country = ""
            if self._card is not None:
                card_bin_country = str(getattr(self._card, "bin_country", "") or "").upper()
            if self._browser is not None:
                proxy_country = str(getattr(self._browser, "proxy_country", "") or "").upper()

            preflight = run_preflight(
                config=self._config,
                card_bin_country=card_bin_country,
                proxy_country=proxy_country,
                page=None,  # 浏览器此时还没连上；指纹检查在 _phase_registration 里单独触发
                emit_event=lambda t, p: self._events.emit_sync(run.id, t, payload=p),
            )
            if preflight.should_abort:
                result.error_reason = f"preflight_block: {', '.join(preflight.issues)}"
                result.phase = "preflight"
                self._update_run_status(run.id, "failed", error_reason=result.error_reason)
                return result
        except Exception as exc:
            # preflight 自身故障不能阻塞主流程（降级为 warn 模式行为）
            logger.warning("Preflight 执行异常，继续运行: %s", exc)

        # 出口 IP 抓取已移至 _phase_registration 浏览器连接后（PREFLIGHT_IP），
        # 因为只有浏览器内才能拿到 AdsPower 注入的住宅代理实际出口。

        try:
            # Phase 1: Registration
            if self._should_run_phase(start_phase, PHASE_REGISTRATION):
                self._events.emit_sync(run.id, "phase_start", state=PHASE_REGISTRATION)
                reg_ok = self._phase_registration(run.id, profile_id, email, password)
                if not reg_ok:
                    result.error_reason = "registration_failed"
                    result.phase = PHASE_REGISTRATION
                    self._update_run_status(run.id, "failed", error_reason=result.error_reason)
                    return result
                self._save_checkpoint(run.id, PHASE_REGISTRATION, "completed")
                self._events.emit_sync(run.id, "phase_complete", state=PHASE_REGISTRATION)

            # Phase 2: Token Extraction
            if self._should_run_phase(start_phase, PHASE_TOKEN_EXTRACTION):
                self._events.emit_sync(run.id, "phase_start", state=PHASE_TOKEN_EXTRACTION)
                access_token, refresh_token = self._phase_token_extraction(run.id, profile_id)
                if not access_token:
                    result.error_reason = "token_extraction_failed"
                    result.phase = PHASE_TOKEN_EXTRACTION
                    self._update_run_status(run.id, "failed", error_reason=result.error_reason)
                    return result
                result.access_token = access_token
                result.refresh_token = refresh_token
                self._save_checkpoint(
                    run.id, PHASE_TOKEN_EXTRACTION, "completed",
                    resumable_data={"access_token_present": True},
                )
                self._events.emit_sync(run.id, "phase_complete", state=PHASE_TOKEN_EXTRACTION)

                # 导出 CSV
                from main import export_success
                export_success(email, password, access_token, refresh_token)

                # 把 token 持久化到 Run.openai_tokens，供号池 cpa 格式导出读取。
                # 失败不应影响主流程（CSV 已经落盘了）。
                try:
                    with get_session() as session:
                        db_run = session.get(Run, run.id)
                        if db_run is not None:
                            db_run.openai_tokens = {
                                "access_token": access_token or "",
                                "refresh_token": refresh_token or "",
                                "id_token": "",
                                "extracted_at": datetime.now(timezone.utc).isoformat(),
                                "expires_at": "",
                            }
                            db_run.updated_at = datetime.now(timezone.utc)
                            session.add(db_run)
                            session.commit()
                except Exception as exc:
                    logger.warning("token 写库失败 run=%s: %s", run.id[:12], exc)

            # Phase 3: Payment（可选）
            if self._should_run_phase(start_phase, PHASE_PAYMENT) and self._config.enable_payment_flow:
                self._events.emit_sync(run.id, "phase_start", state=PHASE_PAYMENT)
                checkout_link = self._phase_payment(
                    run.id, profile_id, card_key, email,
                    access_token=result.access_token,
                )
                result.checkout_link = checkout_link
                self._save_checkpoint(run.id, PHASE_PAYMENT, "completed")
                self._events.emit_sync(run.id, "phase_complete", state=PHASE_PAYMENT)

            result.success = True
            result.phase = PHASE_PAYMENT if self._config.enable_payment_flow else PHASE_TOKEN_EXTRACTION
            self._update_run_status(run.id, "success")
            self._events.emit_sync(run.id, "orchestrator_complete", payload={"success": True})

        except Exception as exc:
            result.error_reason = str(exc)
            logger.error("编排器异常: %s", exc)
            self._update_run_status(run.id, "failed", error_reason=str(exc))
            self._events.emit_sync(run.id, "orchestrator_error", payload={"error": str(exc)})

        return result

    def resume(self, run_id: str) -> RunResult:
        """从最近检查点恢复执行。"""
        with get_session() as session:
            run = session.get(Run, run_id)
            if not run:
                return RunResult(success=False, run_id=run_id, error_reason="run_not_found")

        return self.execute(
            profile_id=run.profile_id,
            email=run.email,
            password=run.password,
            card_key=run.card_key,
            run_id=run_id,
        )

    # ── Phase 实现 ────────────────────────────────

    def _phase_registration(self, run_id: str, profile_id: str, email: str, password: str) -> bool:
        """Phase 1: 使用现有状态机完成注册。"""
        from src.automation import LLMDecisionProvider, OpenAICompatibleLLMClient
        from playwright.sync_api import sync_playwright

        llm_provider = self._build_llm_provider()
        connection = self._browser.connect(profile_id, proxy=None)

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(connection.ws_url)
            context = browser.contexts[0]
            page = prepare_clean_start_page(context)

            # 浏览器已就绪：从 page 内查询真实出口 IP（AdsPower 注入的住宅代理）。
            # 失败仅 warning，不阻塞主流程。
            try:
                ip_addr, ip_country = fetch_exit_ip_from_page(page, timeout_ms=8000)
                write_run_exit_ip(run_id, ip_addr, ip_country)
                if ip_addr or ip_country:
                    self._events.emit_sync(
                        run_id,
                        "state_change",
                        state="PREFLIGHT_IP",
                        payload={"ip_address": ip_addr, "ip_country": ip_country},
                    )
            except Exception as exc:
                logger.warning("PREFLIGHT_IP 抓取失败（继续主流程） run=%s: %s", run_id[:12], exc)

            recorder = ArtifactRecorder(self._config.run_artifacts_dir)
            art_run_id = recorder.start_run(email)
            # 经验库 + DB 双写（自进化可视化）；assist_store 仅在 assist_fallback_enabled 时非 None。
            from src.orchestration.experience_factory import build_assist_store, build_experience_store

            experience_store = build_experience_store(self._config)
            assist_store = build_assist_store(self._config)

            runtime = AutomationRuntime(
                page=page,
                context=context,
                config=self._config,
                email=email,
                password=password,
                mail_api=self._mail,
                sms_api=None,  # PhaseOrchestrator 路径不走 phone 模式；phone 必经 worker.py
                logger=logger,
                handlers=build_runtime_handlers(email=email, password=password),
                artifact_recorder=recorder,
                run_id=art_run_id,
                llm_provider=llm_provider,
                experience_store=experience_store,
                captcha_solver=build_solver_from_config(self._config),
                triage_provider=build_triage_provider(self._config),
                assist_enabled=bool(getattr(self._config, "assist_fallback_enabled", False)),
                assist_experience=assist_store,
            )
            attach_log_buffer(runtime)

            machine = RegistrationStateMachine()
            result = machine.run(runtime)

            self._events.emit_sync(run_id, "state_change", state=result.final_state.value, payload={
                "success": result.success,
                "steps": result.steps_taken,
            })

            return result.success

    def _phase_token_extraction(self, run_id: str, profile_id: str) -> tuple[str, str]:
        """Phase 2: 提取 session token。"""
        # token 提取需要复用已有浏览器上下文
        # 此处简化为使用 HTTP 方式提取（如 main.py 中的逻辑）
        self._events.emit_sync(run_id, "log", payload={"message": "Token 提取阶段（需浏览器上下文）"})
        # 实际实现需要在 Phase 1 结束后传递 context
        # 当前返回占位，由 execute() 中的完整浏览器会话处理
        return "", ""

    def _phase_payment(
        self,
        run_id: str,
        profile_id: str,
        card_key: str,
        email: str,
        access_token: str = "",
    ) -> str:
        """Phase 3: 支付流程。"""
        if not access_token:
            logger.error("无 AccessToken，跳过支付。")
            return ""

        # 卡预热（Pro 账号代刷模式）—— 仅在 ENABLE_CARD_WARMUP=true 时执行。
        # 调度逻辑：每次只跑一笔，跑完写 next_action_at = now + interval_hours，
        # 然后 return 让 worker 调度循环到时再恢复任务到 _phase_payment。
        # 当 warmup_pro_attempts >= 配置序列长度时跳过预热进入正式 Team 绑卡。
        # 关键：如果 card_key 来自卡池且已成熟（warmup_count >= target），
        # **跳过现场预热**避免重复浪费 Stripe attempt（违反 24h velocity）
        if self._config.enable_card_warmup:
            if self._card_already_warmed_from_pool(card_key):
                self._events.emit_sync(
                    run_id, "log",
                    payload={
                        "message": "卡来自卡池且已成熟（warmup_count >= target），跳过现场预热进入正式绑卡"
                    },
                )
            else:
                preheat_outcome = self._maybe_run_card_preheat(run_id, card_key, email)
                if preheat_outcome == "scheduled_next":
                    # 已写下 next_action_at，让 worker 调度循环按时唤醒任务
                    self._events.emit_sync(
                        run_id, "log",
                        payload={"message": "卡预热已排程下一笔 attempt，任务挂起等待调度"},
                    )
                    return ""
                # preheat_outcome == "completed" / "skipped" → 继续走正式 Team 绑卡

        if self._config.payment_link_only:
            from src.payment_link import PaymentLinkGenerator
            success, link = PaymentLinkGenerator.generate_checkout_link(
                access_token,
                plan_type=self._config.payment_plan,
                proxy=self._config.proxy or None,
                return_mode=self._config.payment_link_return_mode,
                aimizy_country=self._config.aimizy_country,
                aimizy_currency=self._config.aimizy_currency,
            )
            if success:
                self._events.emit_sync(run_id, "log", payload={"message": f"支付链接: {link}"})
                return link
            logger.error("支付链接生成失败: %s", link)
            return ""

        # 完整支付流程需要 card_provider + 浏览器上下文
        if not self._card:
            logger.error("未配置虚拟卡 Provider，跳过支付。")
            return ""

        self._events.emit_sync(run_id, "log", payload={"message": "完整支付流程（需浏览器上下文）"})
        return ""

    # ── 卡预热辅助 ──────────────────────────────────

    def _card_already_warmed_from_pool(self, card_key: str) -> bool:
        """检查卡是否来自卡池且已成熟。

        判定：CardActivation 记录存在 + 未作废 + warmup_count >= target_warmup_count。
        target_warmup_count == 0 视为"未启用卡池预热"（老数据），不跳过。
        DB 异常一律返回 False（保守，让现场预热兜底）。
        """
        if not card_key:
            return False
        try:
            with get_session() as session:
                from src.db.models import CardActivation
                rec = session.get(CardActivation, card_key)
                if rec is None or rec.is_invalidated:
                    return False
                target = int(rec.target_warmup_count or 0)
                if target <= 0:
                    return False  # 未启用池预热的老卡，正常走现场预热
                return int(rec.warmup_count or 0) >= target
        except Exception:
            return False

    def _maybe_run_card_preheat(self, run_id: str, card_key: str, target_email: str) -> str:
        """走真实卡预热路径（垫脚石账号 + access_token + 3DS）。

        调用 ``src/orchestration/warmup.py:execute_card_warmup`` —— 该函数已实现
        完整流程：起 AdsPower 垫脚石环境 → 用 access_token 生成支付链接 → 填卡 →
        触发 3DS → 期望 Stripe 回 ``too frequently`` 报错（预热成功信号）。

        预热账号来源：``WARMUP_ACCOUNT_POOL`` 环境变量（控制台 /config 支付 tab 设置），
        格式 ``[{"profile_id": "k1b9945d", "access_token": "eyJ..."}]``。

        返回值：
          - "completed": 预热成功 / 已 warmed_up / 配置缺失跳过 → 继续走正式绑卡
          - "scheduled_next": （目前不返回；execute_card_warmup 是同步单笔，不需要排程）
          - "skipped": 卡 / Pro 账号池 / 预热未启用 → 直接走正式绑卡
        """
        from src.orchestration.warmup import execute_card_warmup

        with get_session() as session:
            run = session.get(Run, run_id)
            if run is None:
                return "skipped"
            if run.is_card_warmed_up:
                logger.info("Run %s 卡已预热完成，跳过", run_id)
                return "completed"

        # 拿待绑卡的 CardInfo（带 bounded retry，吸收 provider API 偶发抖动）
        card_info = None
        try:
            if self._card is not None:
                card_info = resolve_card_with_retry(self._card, card_key)
        except Exception as exc:
            logger.warning("卡预热: 获取 CardInfo 失败 %s", exc)
        if card_info is None:
            self._events.emit_sync(
                run_id, "warning",
                payload={"message": "卡预热: 无法获取 CardInfo，跳过预热进入正式绑卡"},
            )
            return "skipped"

        self._events.emit_sync(
            run_id, "action",
            payload={"message": "开始卡预热（垫脚石账号 + 3DS 触发模式）"},
        )

        # execute_card_warmup 内部已处理：号池为空 / 未启用 / 登录失败 / AdsPower 失败 等所有降级
        # ConfigService 提供号池调度（select_warmup_account / record_warmup_outcome）
        from src.services.config_service import ConfigService
        svc = ConfigService()
        warmed = False
        try:
            warmed = execute_card_warmup(
                config=self._config,
                card_info=card_info,
                card_api=self._card,
                card_key=card_key,
                svc=svc,
                proxy_url=self._config.proxy or "",
            )
        except Exception as exc:
            logger.error("execute_card_warmup 异常: %s", exc)
            self._events.emit_sync(
                run_id, "error",
                payload={"message": f"卡预热执行异常: {exc}"},
            )

        # 写库：标记 warmed_up（无论结果如何都标，避免重试浪费）
        with get_session() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.is_card_warmed_up = True
                run.warmup_pro_attempts = int(run.warmup_pro_attempts or 0) + 1
                run.next_action_at = None
                session.add(run)
                session.commit()

        self._events.emit_sync(
            run_id, "log",
            payload={"message": f"卡预热结束: warmed={warmed}，进入正式绑卡"},
        )
        return "completed"

    # ── 辅助方法 ──────────────────────────────────

    def _create_or_load_run(
        self,
        *,
        run_id: Optional[str],
        email: str,
        password: str,
        profile_id: str,
        card_key: str,
    ) -> Run:
        """创建新 Run 或加载已有 Run。"""
        with get_session() as session:
            if run_id:
                existing = session.get(Run, run_id)
                if existing:
                    return existing

            from src.services.config_service import ConfigService
            svc = ConfigService()

            run = Run(
                email=email,
                password=password,
                profile_id=profile_id,
                card_key=card_key,
                status="pending",
                phase=PHASE_REGISTRATION,
                config_snapshot=svc.get_config_snapshot(),
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            return run

    def _resolve_start_phase(self, run_id: str) -> str:
        """确定断点续跑的起始阶段。"""
        with get_session() as session:
            from sqlmodel import select
            stmt = (
                select(Checkpoint)
                .where(Checkpoint.run_id == run_id, Checkpoint.state == "completed")
                .order_by(Checkpoint.created_at.desc())  # type: ignore
            )
            latest = session.exec(stmt).first()
            if not latest:
                return PHASE_REGISTRATION

            try:
                idx = PHASE_ORDER.index(latest.phase)
                if idx + 1 < len(PHASE_ORDER):
                    return PHASE_ORDER[idx + 1]
            except ValueError:
                pass
            return PHASE_REGISTRATION

    def _should_run_phase(self, start_phase: str, current_phase: str) -> bool:
        """判断当前阶段是否应该执行。"""
        try:
            return PHASE_ORDER.index(current_phase) >= PHASE_ORDER.index(start_phase)
        except ValueError:
            return True

    def _save_checkpoint(
        self,
        run_id: str,
        phase: str,
        state: str,
        resumable_data: Optional[dict[str, Any]] = None,
    ) -> None:
        """保存检查点。"""
        with get_session() as session:
            cp = Checkpoint(
                run_id=run_id,
                phase=phase,
                state=state,
                resumable_data=resumable_data or {},
            )
            session.add(cp)
            session.commit()

    def _update_run_status(
        self,
        run_id: str,
        status: str,
        *,
        phase: Optional[str] = None,
        error_reason: Optional[str] = None,
    ) -> None:
        """更新 Run 状态。"""
        with get_session() as session:
            run = session.get(Run, run_id)
            if run:
                run.status = status
                if phase:
                    run.phase = phase
                if error_reason:
                    run.error_reason = error_reason
                run.updated_at = datetime.now(timezone.utc)
                session.add(run)
                session.commit()

    def _persist_exit_ip(self, run_id: str, ip_address: str, ip_country: str) -> None:
        """把本次抓到的出口 IP 写到 Run（薄包装，复用 preflight.write_run_exit_ip）。

        保留方法以兼容历史调用方；新代码请直接用 ``write_run_exit_ip``。
        """
        write_run_exit_ip(run_id, ip_address, ip_country)

    def _build_llm_provider(self):
        """按配置构造 LLM 决策器。"""
        from src.automation import LLMDecisionProvider, OpenAICompatibleLLMClient

        if not self._config.llm_enabled:
            return None
        missing = self._config.validate(required_modules=["llm"])
        if missing:
            logger.warning("LLM 配置不完整，降级为纯规则模式: %s", ", ".join(missing))
            return None
        client = OpenAICompatibleLLMClient(
            base_url=self._config.llm_base_url,
            api_key=self._config.llm_api_key,
            model=self._config.llm_model,
            timeout_ms=self._config.llm_timeout_ms,
        )
        return LLMDecisionProvider(
            client=client,
            confidence_threshold=self._config.llm_confidence_threshold,
            vision_enabled=bool(getattr(self._config, "llm_vision_enabled", False)),
        )
