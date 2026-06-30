# -*- coding: utf-8 -*-
"""
FastAPI 应用入口

启动命令：uvicorn src.api.app:app --reload --port 8080
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from src.api.deps import get_auth_service, get_knowledge_service
from src.api.i18n import setup_i18n
from src.api.routes import accounts, assistant, auth, cards, config, events, export, link_templates, promo_discovery, proxies, proxy_providers, registration_profiles, tasks, workflows
from src.api.security import (
    SESSION_COOKIE_NAME,
    get_or_create_csrf_token,
    get_current_user_from_request,
    redirect_to_login,
)
from src.db.engine import init_db

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _BASE_DIR / "templates"
_STATIC_DIR = _BASE_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库 + 启动 warmup 调度循环。"""
    init_db()
    get_auth_service().ensure_bootstrap_users()
    get_knowledge_service().rebuild()
    logger.info("数据库初始化完成，API 服务就绪。")

    # 启动 warmup 60s 调度循环（卡预热 + 账号养号），用 EventBroadcaster 单例广播
    from src.api.deps import get_event_broadcaster
    from src.api.worker import start_warmup_scheduler

    try:
        broadcaster = get_event_broadcaster()
        start_warmup_scheduler(broadcaster_factory=lambda: broadcaster)
        print("[lifespan] warmup 调度循环已启动 (60s tick)", flush=True)
        logger.info("warmup 调度循环已启动 (60s tick)")
    except Exception as exc:
        import traceback
        print(f"[lifespan] warmup 调度循环启动失败: {exc}", flush=True)
        traceback.print_exc()
        logger.error("warmup 调度循环启动失败: %s", exc)

    yield

    # 关闭顺序：先停调度循环再关 Worker 线程池
    from src.api.worker import shutdown_workers, stop_warmup_scheduler
    try:
        stop_warmup_scheduler(wait_sec=2.0)
    except Exception as exc:
        logger.warning("停止 warmup 调度循环失败: %s", exc)
    shutdown_workers(wait=False)
    logger.info("Worker 线程池已关闭。")


app = FastAPI(
    title="Team Register Control Plane",
    description="OpenAI 账号注册自动化管理面板",
    version="0.1.0",
    lifespan=lifespan,
)

session_secret = os.getenv("SESSION_SECRET", "dev-session-secret-change-me")
if session_secret == "dev-session-secret-change-me":
    logger.warning("SESSION_SECRET 未配置，当前仅适合本地开发。")
app.add_middleware(
    SessionMiddleware,
    secret_key=session_secret,
    session_cookie=SESSION_COOKIE_NAME,
    same_site="lax",
    max_age=60 * 60 * 12,
)

# 注册 API 路由
app.include_router(auth.router)
app.include_router(assistant.router)
app.include_router(config.router)
app.include_router(tasks.router)
app.include_router(cards.router)
app.include_router(accounts.router)
app.include_router(link_templates.router)
app.include_router(promo_discovery.router)
app.include_router(proxies.router)
app.include_router(proxy_providers.router)
app.include_router(registration_profiles.router)
app.include_router(events.router)
app.include_router(export.router)
app.include_router(workflows.router)

# 静态文件和模板
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR)) if _TEMPLATES_DIR.exists() else None

# i18n 必须在模块级初始化（确保 TestClient 也能使用）
setup_i18n(app, templates)


def _build_template_context(request: Request, **extra):
    current_user = get_current_user_from_request(request, get_auth_service())
    request.state.current_user = current_user
    base_context = {
        "request": request,
        "current_user": current_user,
        "assistant_enabled": bool(current_user),
        "assistant_backend_ready": True,
        "manual_path": "docs/usage-manual.md",
        "csrf_token": get_or_create_csrf_token(request),
    }
    base_context.update(extra)
    return base_context


def _protected_template_response(request: Request, template_name: str, **context):
    current_user = get_current_user_from_request(request, get_auth_service())
    request.state.current_user = current_user
    if current_user is None:
        return redirect_to_login(request)
    return templates.TemplateResponse(request, template_name, _build_template_context(request, **context))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """首页 — 渲染 Dashboard 或返回 API 信息。"""
    if templates and (_TEMPLATES_DIR / "pages" / "overview.html").exists():
        return _protected_template_response(request, "pages/overview.html")
    return HTMLResponse(
        "<h1>Team Register API</h1>"
        "<p>API 文档: <a href='/docs'>/docs</a></p>"
        "<p>前端模板尚未创建，请先访问 API。</p>"
    )


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_list(request: Request):
    """任务列表页。"""
    if templates:
        return _protected_template_response(request, "pages/tasks/list.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/tasks/create", response_class=HTMLResponse)
async def tasks_create(request: Request):
    """创建任务页。"""
    if templates:
        return _protected_template_response(request, "pages/tasks/create.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def tasks_detail(request: Request, task_id: str):
    """任务详情页。"""
    if templates:
        return _protected_template_response(request, "pages/tasks/detail.html", task_id=task_id)
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/grok", response_class=HTMLResponse)
async def grok_page(request: Request):
    """Grok (x.ai) 注册页（feat/grok-register）。"""
    if templates:
        return _protected_template_response(request, "pages/grok/index.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """配置管理页。"""
    if templates:
        return _protected_template_response(request, "pages/config/index.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/providers", response_class=HTMLResponse)
async def providers_page(request: Request):
    """Provider 管理页。"""
    if templates:
        return _protected_template_response(request, "pages/providers/index.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/registration-profiles", response_class=HTMLResponse)
async def registration_profiles_page(request: Request):
    """注册方式 × 供应商组合管理页。"""
    if templates and (_TEMPLATES_DIR / "pages" / "registration_profiles" / "index.html").exists():
        return _protected_template_response(request, "pages/registration_profiles/index.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/mail-accounts", response_class=HTMLResponse)
async def mail_accounts_page(request: Request):
    """邮箱账号管理页。"""
    if templates and (_TEMPLATES_DIR / "pages" / "mail_accounts" / "index.html").exists():
        return _protected_template_response(request, "pages/mail_accounts/index.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/cards", response_class=HTMLResponse)
async def cards_page(request: Request):
    """虚拟卡缓存管理页（X988 已激活卡列表 + 手动作废）。"""
    if templates and (_TEMPLATES_DIR / "pages" / "cards" / "index.html").exists():
        return _protected_template_response(request, "pages/cards/index.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/workflows", response_class=HTMLResponse)
async def workflows_page(request: Request):
    """自进化经验管理页（学到的工作流列表 + 成功率 + 启禁/删除）。"""
    if templates and (_TEMPLATES_DIR / "pages" / "workflows" / "index.html").exists():
        return _protected_template_response(request, "pages/workflows/index.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/proxies", response_class=HTMLResponse)
async def proxies_page(request: Request):
    """代理池管理页（仅用于"生成 checkout 链接"按号选 IP 出口）。"""
    if templates and (_TEMPLATES_DIR / "pages" / "proxies" / "index.html").exists():
        return _protected_template_response(request, "pages/proxies/index.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/proxy-providers", response_class=HTMLResponse)
async def proxy_providers_page(request: Request):
    """动态代理供应商管理页（1024Proxy 等按需拉 IP 的 API 配置）。"""
    if templates and (_TEMPLATES_DIR / "pages" / "proxy_providers" / "index.html").exists():
        return _protected_template_response(request, "pages/proxy_providers/index.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/promo-codes", response_class=HTMLResponse)
async def promo_codes_page(request: Request):
    """促销码列表 + 手动 eligibility 验证（promo_eligibility 模块）。"""
    if templates and (_TEMPLATES_DIR / "pages" / "promo_codes" / "index.html").exists():
        return _protected_template_response(request, "pages/promo_codes/index.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    """账号池管理页（普号池 / Plus 号池 / Team 号池 / 已放弃）。"""
    if templates and (_TEMPLATES_DIR / "pages" / "accounts" / "index.html").exists():
        return _protected_template_response(request, "pages/accounts/index.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/checkout-link", response_class=HTMLResponse)
async def checkout_link_page(request: Request):
    """独立 Checkout 链接生成器 —— 用户手动喂 access_token，不依赖号池上下文。

    与 ``/accounts`` 的弹窗共享 PaymentLinkGenerator 后端，仅 UI 入口不同；
    本页**不**会读写 DB（access_token 由前端临时输入，关闭即丢）。
    """
    if templates and (_TEMPLATES_DIR / "pages" / "checkout_link" / "index.html").exists():
        return _protected_template_response(request, "pages/checkout_link/index.html")
    return HTMLResponse("<p>Template not found</p>", status_code=404)


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    """帮助页。"""
    knowledge_service = get_knowledge_service()
    if templates and (_TEMPLATES_DIR / "pages" / "help.html").exists():
        return templates.TemplateResponse(
            request,
            "pages/help.html",
            _build_template_context(
                request,
                manual_html=knowledge_service.manual_html,
                manual_url="/manual",
            ),
        )
    return HTMLResponse(knowledge_service.manual_html or "<p>Usage manual not found.</p>")


@app.get("/manual", response_class=HTMLResponse)
async def manual_page(request: Request):
    """原样展示使用手册 HTML。"""
    knowledge_service = get_knowledge_service()
    title = "Usage Manual"
    html = knowledge_service.manual_html or "<p>Usage manual not found.</p>"
    return HTMLResponse(f"<html><head><title>{title}</title></head><body>{html}</body></html>")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """登录页。"""
    current_user = get_current_user_from_request(request, get_auth_service())
    next_url = request.query_params.get("next", "/")
    if current_user:
        return RedirectResponse(url=next_url or "/", status_code=303)
    if templates and (_TEMPLATES_DIR / "pages" / "login.html").exists():
        return templates.TemplateResponse(
            request,
            "pages/login.html",
            {
                "request": request,
                "next_url": next_url,
                "login_error": request.query_params.get("error", ""),
                "login_hint": "默认管理员账号可通过环境变量 ADMIN_USERNAME/ADMIN_PASSWORD 覆盖。",
                "help_url": "/help",
                "manual_url": "/manual",
                "csrf_token": get_or_create_csrf_token(request),
            },
        )
    return HTMLResponse(
        """
        <html><body style="font-family: sans-serif; max-width: 420px; margin: 40px auto;">
        <h1>Console Login</h1>
        <p>登录模板尚未生成，请直接调用 <code>POST /api/auth/login</code> 完成登录。</p>
        <p>帮助页：<a href="/help">/help</a> ｜ 使用手册：<a href="/manual">/manual</a></p>
        </body></html>
        """,
        status_code=200,
    )


@app.get("/health")
async def health():
    """健康检查。"""
    return {"ok": True, "service": "team-register-control-plane"}
