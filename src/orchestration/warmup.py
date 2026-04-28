# -*- coding: utf-8 -*-
"""
卡片预热模块 (Card Warming Strategy)

实现 3DS 预热技巧（v2 - 直接走 ChatGPT UI Upgrade 路径）：
1. 从 mail_accounts 号池（role=pro_warmup）挑一个最久没用、未冷却的垫脚石账号。
2. 起 AdsPower 浏览器，用 email + magic link 现场登录该账号。
3. 登后**直接在同一个 BrowserContext 里点 ChatGPT UI 的 Claim offer 按钮**，
   让 OpenAI 自己跳到 hosted checkout 页（绕掉 token 提取 + PaymentLinkGenerator）。
4. 两轮刷卡循环：
     - 第 1 轮：select_pro_tier($200) → fill_card → submit → 等 3DS（best-effort）
     - 等 60s
     - 第 2 轮：select_pro_tier($100) → fill_card → submit → 等 3DS（best-effort）
     - 等 60s
5. 卡被 Stripe 拒是**预期行为**（账号余额不足），账号保持 Free 状态可重复用。
   两轮 navigate+select+fill+submit 都跑完 = 预热成功。
6. 把成功/失败结果写回号池（连续 3 次失败的账号自动 is_active=False）。

设计要点：
  - 不再依赖 access_token + PaymentLinkGenerator + aimizy 中转（容易被 OpenAI/IP 限流断链）
  - 直接走 ChatGPT UI 的 Claim offer 入口 → Configure your plan 页 → Stripe iframe
  - 号池存 mail_accounts 表（role=pro_warmup），UI 在 /mail-accounts 管理
  - 选中即占位（last_used_at + cooldown_until），多 worker 并发不会撞同一账号
  - 池空 / 全冷却 / 登录失败 → 直接 return False，不阻断主绑卡流程
"""

import logging
import re
from typing import Any, Optional

from playwright.sync_api import sync_playwright, BrowserContext, Page, Playwright

from src.config import AppConfig
from src.browser import get_browser_ws
from src.utils import human_delay
from src.orchestration.handlers import (
    wait_for_stripe_form,
    fill_checkout_card,
    handle_checkout_3ds_challenge,
    detect_checkout_error,
    is_chatgpt_logged_in,
    pro_account_login,
    prepare_clean_warmup_page,
    navigate_to_pro_checkout,
    select_pro_tier,
    WarmupUpgradeNotFound,
)
from src.models import CardInfo
from src.services.config_service import ConfigService

# 两轮预热配置：先 $200 Pro 后 $100 Pro，每轮间隔 60s
# 卡被 Stripe 拒是预期行为（账号余额不足），保留 Free 状态可重复用
WARMUP_ROUNDS = (
    {"amount_usd": 200, "wait_after_sec": 60},
    {"amount_usd": 100, "wait_after_sec": 60},
)

# 失败原因里命中以下任一关键词 → 视为外部依赖故障（不累计 consecutive_failures）。
# 命中规则用 re.IGNORECASE，所以大小写不敏感。
# 设计意图：保护 pro_warmup 号池在远程服务（邮件 / AdsPower）短期抖动时不被连带 disable。
_EXTERNAL_FAILURE_PATTERNS = (
    r"adspower",                     # AdsPower 启动 / WebSocket 失败
    r"5\d{2}\b",                     # HTTP 5xx
    r"\bservererror\b",
    r"server error",
    r"internal server error",
    r"\btimeout\b",                  # 网络/上游超时
    r"connection",                   # 连接错误
    r"mailbox-service",              # 邮件 provider 服务（managed-sessions/credentialed-sessions）
    r"managed-sessions",
    r"credentialed-sessions",
    r"mail_service",
    r"email-provider",
    r"\bdns\b",
    # mail provider 的 contract error_code（来自服务端 4xx/5xx 响应体 detail.code）：
    r"MAILBOX_RUNTIME_INCOMPAT",     # 5xx / 401 / 404 — 服务端运行态问题
    r"PROVIDER_UPSTREAM_ERROR",      # 424 — provider 上游 API 4xx
)
_EXTERNAL_FAILURE_RE = re.compile("|".join(_EXTERNAL_FAILURE_PATTERNS), re.IGNORECASE)

# 配置错（账号 / admin UI 配置不全）即便 reason 含 "mailbox-service" 也归
# account_failure — 这是运维配置问题，应当让 consecutive_failures 累计 + 触发自动
# disable，让运维注意修配置而不是把它当成外部抖动忽略。
_ACCOUNT_FAILURE_OVERRIDE_PATTERNS = (
    r"PROVIDER_NOT_CONFIGURED",
    r"missing_fields",
    r"必填.*配置",
)
_ACCOUNT_OVERRIDE_RE = re.compile("|".join(_ACCOUNT_FAILURE_OVERRIDE_PATTERNS), re.IGNORECASE)


def _classify_failure(reason: str) -> str:
    """根据失败 reason 字符串分类为 'external_failure' / 'account_failure'。

    分类规则（从强到弱）：
      1. reason 含 PROVIDER_NOT_CONFIGURED / missing_fields → account_failure
         （即便它通过 mail 链路报的，本质是配置错，要让运维注意）
      2. reason 含 5xx / mailbox-service / adspower / timeout 等 → external_failure
      3. 其它 → account_failure（默认）

    在 record_warmup_outcome 里 external_failure 不会累计 consecutive_failures，
    避免远程服务抖动把整个 pro_warmup 号池连带 disable。
    """
    if not reason:
        return "account_failure"
    # Override 规则优先：配置错就是配置错，无论它通过什么链路报上来
    if _ACCOUNT_OVERRIDE_RE.search(reason):
        return "account_failure"
    return "external_failure" if _EXTERNAL_FAILURE_RE.search(reason) else "account_failure"


logger = logging.getLogger(__name__)


def execute_card_warmup(
    config: AppConfig,
    card_info: CardInfo,
    card_api: Any,  # EfunCard / NodeCard / X988Card 实例
    card_key: str,
    *,
    svc: ConfigService,
    proxy_url: str = "",
    playwright: Optional[Playwright] = None,
) -> bool:
    """执行卡片预热流程。

    Args:
        playwright: 已存在的 sync Playwright 实例。**必须传**——main.py 在 worker 线程
            里已经持有一个 sync_playwright()，本函数不能再 nest 一个，否则会触发
            "Playwright Sync API inside the asyncio loop" 错误。caller 必须把外部
            的 ``p`` 传进来，例如：
                with sync_playwright() as p:
                    execute_card_warmup(..., playwright=p)
            为了兼容（测试 + 旧 caller），允许 None：此时回退到自己开
            ``sync_playwright()``，但只有在不嵌套 sync_playwright 的场景下才安全。

    Returns:
        True = 两轮 navigate+select+fill+submit 全部跑完 = 预热成功
        False = 任何环节失败 / 池空 / 未启用（主绑卡流程仍会继续）
    """
    if not config.enable_card_warmup:
        logger.info("卡片预热未启用，跳过。")
        return False

    # 1. 从号池挑账号（自动占位冷却，避免并发撞号）
    account = svc.select_warmup_account()
    if account is None:
        logger.warning("号池中无可用 pro_warmup 账号，跳过预热。")
        return False

    # 取登录凭据 + AdsPower profile。
    #
    # 历史 UI 设计假设 pro_warmup 账号一定走"ChatGPT 密码登录"路径，所以强制必填
    # adspower_profile_id + password。但 OAuth-only 邮箱（applemail / outlook + Microsoft
    # Graph）走的是 client_id + refresh_token 直接拉收件箱，profile_id / password
    # 反而是空。UI 已经放宽不强制必填这些字段（见 src/templates/pages/mail_accounts/index.html）。
    #
    # 运行时只强制 email（DB 唯一标识必填）+ adspower_profile_id（必须有 AdsPower 浏览器
    # 才能跑预热），不再强制 password — password 缺失时 pro_account_login 会自动尝试
    # magic link 路径。
    extra = dict(account.extra or {})
    profile_id = str(extra.get("adspower_profile_id") or "").strip()
    password = str(extra.get("password") or "").strip()
    email = str(account.email or "").strip()

    if not email or not profile_id:
        reason = f"missing_credentials(profile={bool(profile_id)},email={bool(email)})"
        logger.error("预热账号 %s 凭据不完整（必须 email + adspower_profile_id）: %s", account.id, reason)
        svc.record_warmup_outcome(
            account.id, success=False, reason=reason, failure_class="account_failure",
        )
        return False
    if not password:
        logger.info(
            "预热账号 %s 未设置 password，将依赖 magic link / OAuth 路径登录",
            account.id,
        )

    logger.info("开始预热：账号=%s profile=%s", email, profile_id)

    # 2. 启动 AdsPower 垫脚石浏览器
    try:
        ws_url = get_browser_ws(
            ads_api=config.ads_api,
            user_id=profile_id,
            api_key=config.ads_api_key,
        )
    except Exception as exc:
        logger.error("预热中止：无法连接 AdsPower 垫脚石环境 (%s)", exc)
        # AdsPower 抖动是外部依赖问题，不累计计数
        svc.record_warmup_outcome(
            account.id,
            success=False,
            reason=f"adspower_failed:{exc}"[:200],
            failure_class="external_failure",
        )
        return False

    # 关键：caller 是否已持有 sync_playwright()。
    # main.py:1983 已 with sync_playwright() as p，在那里嵌套再开一个会触发
    # "Playwright Sync API inside the asyncio loop" 错误。
    # 测试 / 旧 caller 没传时回退到自己开。
    if playwright is not None:
        return _run_warmup_inner(
            playwright, ws_url, account, email, password,
            config, card_info, card_api, card_key, svc, proxy_url,
        )
    with sync_playwright() as p:
        return _run_warmup_inner(
            p, ws_url, account, email, password,
            config, card_info, card_api, card_key, svc, proxy_url,
        )


def _run_warmup_inner(
    p: Playwright,
    ws_url: str,
    account,
    email: str,
    password: str,
    config: AppConfig,
    card_info: CardInfo,
    card_api: Any,
    card_key: str,
    svc: ConfigService,
    proxy_url: str,
) -> bool:
    """真正执行预热的核心逻辑，要求外部已提供活跃的 Playwright 实例。"""
    page: Optional[Page] = None
    warmed = False
    failure_reason = ""

    # mail_api 由 caller 通过 svc 上下文构造好；这里直接复用 svc.mail_api（如有）
    # 兼容旧 caller：mail_api 缺失时 pro_account_login 会自动降级到密码流尝试
    mail_api = getattr(svc, "mail_api", None)

    # used_quick_path / quick_path_fallback_done：跨"登录态预检"和"两轮循环"共享
    # 的状态机标记。仅当 used_quick_path=True 且 quick_path_fallback_done=False
    # 且失败发生在第 1 轮第 1 次 navigate 时，才允许降级回完整登录路径。
    used_quick_path = False
    quick_path_fallback_done = False

    def _switch_to_full_login_path(current_page: Optional[Page]) -> Optional[Page]:
        """关闭当前 page、清 auth 域 cookies、走完整 pro_account_login。

        返回新 page；登录失败返回 None（caller 应当 set failure_reason 后 return False）。
        """
        if current_page is not None:
            try:
                current_page.close()
            except Exception as close_exc:
                logger.debug("关闭快路径 page 失败（忽略）: %s", close_exc)
        new_page = prepare_clean_warmup_page(context)
        logger.info("调用 pro_account_login 现场登录 %s ...", email)
        if pro_account_login(new_page, email, password, mail_api=mail_api, timeout_sec=120):
            return new_page
        return None

    try:
        logger.info("连接到垫脚石浏览器实例...")
        browser = p.chromium.connect_over_cdp(ws_url)
        context: BrowserContext = browser.contexts[0]

        # 1. 登录态预检（不清 cookies）：先开新 page 看 ChatGPT 主页是否已登录。
        #    用户期望流程：① 打开浏览器 ② 看登录态 ③ 已登录直接绑卡 ④ 未登录走登录。
        peek_page = context.new_page()
        peek_page.set_default_timeout(60000)
        logged_in = False
        try:
            peek_page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=20000)
            logged_in = is_chatgpt_logged_in(peek_page)
        except Exception as exc:
            logger.warning("登录态预检 goto 失败（按未登录处理）: %s", exc)
            logged_in = False

        if logged_in:
            # 快路径：复用已登录 page，跳过 prepare_clean_warmup_page + pro_account_login。
            # 节省 1 次 magic link + ~30s 登录耗时 + 一次性风控暴露。
            logger.info("预热账号 %s 已登录，跳过登录流程（快路径）", email)
            page = peek_page
            used_quick_path = True
        else:
            # 慢路径（原始路径）：清 auth 域 cookies + storage + 完整登录。
            # 反复登录失败会污染 profile，所以慢路径的 prepare_clean_warmup_page 不能省。
            logger.info("预热账号 %s 未登录或登录态失效，走完整登录流程", email)
            try:
                peek_page.close()
            except Exception:
                pass
            page = _switch_to_full_login_path(None)
            if page is None:
                failure_reason = "login_failed"
                logger.error("预热中止：垫脚石账号登录失败")
                return False
            used_quick_path = False

        # 4. 两轮刷卡循环：直接走 ChatGPT UI Upgrade 路径，不再提 token / 不再调 PaymentLinkGenerator
        # 第 1 轮先 navigate_to_pro_checkout（从主页 → Configure plan 页）；
        # 第 2 轮如果还在 plan 页，select_pro_tier 直接切档位即可（幂等）
        first_round = True
        for round_idx, round_cfg in enumerate(WARMUP_ROUNDS, start=1):
            amount_usd = int(round_cfg["amount_usd"])
            wait_after_sec = int(round_cfg["wait_after_sec"])
            logger.info(
                "预热第 %d/%d 轮：amount=$%d wait_after=%ds",
                round_idx, len(WARMUP_ROUNDS), amount_usd, wait_after_sec,
            )

            # 4.1 navigate（仅第 1 轮）
            try:
                if first_round:
                    navigate_to_pro_checkout(page, timeout_sec=30)
                    first_round = False
                select_pro_tier(page, amount_usd=amount_usd)
            except WarmupUpgradeNotFound as exc:
                # 快路径下 navigate/select 失败的处理策略：
                #   ❌ 不清 cookies / 不调 pro_account_login
                #      （用户明确约束：两轮绑卡之间不能重新登录；快路径下已登录态有效，
                #      失败更可能是 SPA 渲染延迟 / UI 状态漂移，不是登录问题）
                #   ✅ 重试一次 navigate（给 SPA 多 5s 渲染时间）
                #   ❌ 仍失败 → 真实上报"主页找不到 Upgrade 入口"，由运维查 ChatGPT UI 变化
                #
                # 仅在第 1 轮第 1 次失败时重试（避免轮间反复挣扎），用 quick_path_fallback_done
                # flag 锁死最多重试 1 次。
                if used_quick_path and not quick_path_fallback_done and round_idx == 1:
                    logger.warning(
                        "快路径下 navigate/select 失败（%s），等 5s 重试 navigate（不清 token）",
                        exc,
                    )
                    quick_path_fallback_done = True
                    try:
                        page.wait_for_timeout(5000)
                    except Exception:
                        human_delay(5, 6)
                    try:
                        navigate_to_pro_checkout(page, timeout_sec=30)
                        first_round = False
                        select_pro_tier(page, amount_usd=amount_usd)
                    except WarmupUpgradeNotFound as retry_exc:
                        failure_reason = (
                            f"upgrade_failed_round1_after_retry:{str(retry_exc)[:100]}"
                        )
                        logger.error(
                            "预热中止：快路径下 navigate 重试仍失败 — 可能 ChatGPT UI 变化或账号无 Upgrade 入口（%s）",
                            retry_exc,
                        )
                        return False
                else:
                    failure_reason = f"upgrade_failed_round{round_idx}:{str(exc)[:120]}"
                    logger.error("预热第 %d 轮中止：%s", round_idx, exc)
                    return False

            # 4.2 等 Stripe iframe（best-effort，没出现就跳过本轮填卡）
            if not wait_for_stripe_form(page, timeout_sec=20):
                logger.warning("预热第 %d 轮 Stripe iframe 未出现，跳过本轮填卡", round_idx)
            else:
                # 4.3 填卡
                logger.info("预热第 %d 轮填写信用卡...", round_idx)
                variant = fill_checkout_card(
                    page,
                    card_info.card_number,
                    card_info.expiry_display,
                    card_info.cvv,
                )
                if not variant:
                    logger.warning("预热第 %d 轮填卡失败，跳过本轮 submit", round_idx)
                else:
                    # 4.4 提交（best-effort）
                    try:
                        submit_btn = page.locator(
                            'button[aria-label="Subscribe"], button[type="submit"]:has-text("Subscribe")'
                        ).first
                        if submit_btn.is_visible(timeout=3000):
                            submit_btn.click(timeout=5000)
                            logger.info("预热第 %d 轮 Subscribe 已点击", round_idx)
                        else:
                            page.keyboard.press("Enter")
                            logger.info("预热第 %d 轮 Subscribe 不可见，按 Enter 兜底", round_idx)
                        human_delay(2, 4)
                    except Exception as submit_exc:
                        logger.warning("预热第 %d 轮 submit 异常（视作正常）: %s", round_idx, submit_exc)

                    # 4.5 等 3DS（best-effort，没拿到也不算失败）
                    try:
                        otp = card_api.wait_for_3ds(card_key, timeout_sec=120)
                        if otp:
                            logger.info("预热第 %d 轮捕获 3DS 验证码: %s", round_idx, otp)
                            handle_checkout_3ds_challenge(page, otp)
                            human_delay(3, 5)
                        else:
                            logger.info("预热第 %d 轮未检测到 3DS（可能直接被拒，正常）", round_idx)
                    except Exception as otp_exc:
                        logger.info("预热第 %d 轮 3DS 处理异常（视作正常）: %s", round_idx, otp_exc)

                    # 4.6 记录拒卡原因（仅日志，不算失败）
                    err = detect_checkout_error(page) or ""
                    if err:
                        logger.info("预热第 %d 轮支付页返回错误（预期内）: %s", round_idx, err[:200])

            # 4.7 等间隔 + 准备下一轮
            if wait_after_sec > 0:
                logger.info("预热第 %d 轮结束，等待 %ds 进入下一轮", round_idx, wait_after_sec)
                try:
                    page.wait_for_timeout(wait_after_sec * 1000)
                except Exception:
                    human_delay(wait_after_sec, wait_after_sec + 1)

            # 第 2 轮过渡：用户明确约束 — **不 go_back、不 navigate、不刷新 page**，
            # 同一个 Configure your plan 页里：
            #   1. select_pro_tier 直接 click 另一档位 toggle（chatgptpro ↔ chatgptprolite radio group）
            #   2. 切档位会让 Stripe 重新初始化 iframe（金额变了），等 wait_for_stripe_form
            #   3. 重新 fill_checkout_card → submit Subscribe
            # 所以这里**什么都不做** — 让循环顶部的 select_pro_tier(amount_usd=下轮) 直接切档。
            # 注：first_round 仍是 False（不会重新 navigate），与"轮间不清 token"约束一致。
            if round_idx < len(WARMUP_ROUNDS):
                logger.info(
                    "预热第 %d 轮结束 → 第 %d 轮直接在同一 Configure plan 页切 tab（不 go_back / 不 navigate / 不清 token）",
                    round_idx, round_idx + 1,
                )

        # 两轮都跑完 = 成功（不依赖 3DS / 拒卡文案）
        logger.info("!!! 预热成功：%d 轮全部跑完", len(WARMUP_ROUNDS))
        warmed = True
        return True

    except Exception as exc:
        failure_reason = f"exception:{exc}"[:200]
        logger.error("预热执行过程中发生异常: %s", exc)
        return False
    finally:
        # 资源回收：page 必关
        if page is not None:
            try:
                page.close()
            except Exception as close_exc:
                logger.debug("关闭预热 page 失败（可忽略）: %s", close_exc)
        # 写回号池：
        #   - 成功 → 清失败计数
        #   - 账号自身原因失败（cookies / 风控）→ account_failure（累计 ≥3 自动禁用）
        #   - 外部依赖故障（AdsPower / 邮件 5xx / 网络 timeout）→ external_failure（不累计）
        try:
            if warmed:
                svc.record_warmup_outcome(account.id, success=True)
            else:
                reason_text = failure_reason or "unknown"
                fclass = _classify_failure(reason_text)
                svc.record_warmup_outcome(
                    account.id,
                    success=False,
                    reason=reason_text,
                    failure_class=fclass,
                )
        except Exception as rec_exc:
            logger.warning("record_warmup_outcome 失败: %s", rec_exc)
