# -*- coding: utf-8 -*-
"""
FastAPI + Jinja2 国际化基础设施。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

SUPPORTED_LOCALES = ["zh-CN", "en"]
DEFAULT_LOCALE = "zh-CN"

_LOCALES_DIR = Path(__file__).resolve().parent.parent / "static" / "locales"


def _match_locale(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip().lower().replace("_", "-")
    if candidate.startswith("zh"):
        return "zh-CN"
    if candidate.startswith("en"):
        return "en"
    return None


def normalize_locale(value: str | None) -> str:
    return _match_locale(value) or DEFAULT_LOCALE


def _resolve_accept_language(header_value: str | None) -> str | None:
    if not header_value:
        return None
    for item in header_value.split(","):
        language = item.split(";", 1)[0].strip()
        matched = _match_locale(language)
        if matched:
            return matched
    return None


@lru_cache(maxsize=len(SUPPORTED_LOCALES) * 4)
def _load_translations_cached(locale_file: str, version: int) -> dict[str, Any]:
    path = Path(locale_file)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def load_translations(locale: str) -> dict[str, Any]:
    normalized = normalize_locale(locale)
    locale_file = _LOCALES_DIR / f"{normalized}.json"
    if not locale_file.exists():
        locale_file = _LOCALES_DIR / f"{DEFAULT_LOCALE}.json"
    if not locale_file.exists():
        return {}

    # 语言包文件更新后自动失效缓存，避免必须重启服务才能看到最新文案。
    version = int(locale_file.stat().st_mtime_ns)
    return _load_translations_cached(str(locale_file), version)


def _lookup_key(data: dict[str, Any], key: str) -> str:
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            return key
        current = current[part]
    return current if isinstance(current, str) else key


def get_text(locale: str, key: str, **params: Any) -> str:
    text = _lookup_key(load_translations(locale), key)
    if not params:
        return text
    try:
        return text.format(**params)
    except (KeyError, ValueError):
        return text


def _(request: Request, key: str, **params: Any) -> str:
    locale = getattr(request.state, "locale", DEFAULT_LOCALE)
    return get_text(locale, key, **params)


def build_i18n_message_payload(
    message: str,
    key: str,
    *,
    params: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造可在路由层按 locale 二次本地化的事件 payload。"""
    payload = dict(extra)
    payload["message"] = message
    payload["message_i18n_key"] = key
    if params:
        payload["message_i18n_params"] = params
    return payload


def localize_event_payload(locale: str, payload: Any) -> Any:
    """按请求 locale 本地化事件 payload 中的 message/error 文案。"""
    if not isinstance(payload, dict):
        return payload

    localized = dict(payload)
    message_key = str(localized.get("message_i18n_key") or "").strip()
    if message_key:
        params = localized.get("message_i18n_params")
        localized["message"] = get_text(locale, message_key, **(params if isinstance(params, dict) else {}))

    error_key = str(localized.get("error_i18n_key") or "").strip()
    if error_key:
        params = localized.get("error_i18n_params")
        localized["error"] = get_text(locale, error_key, **(params if isinstance(params, dict) else {}))

    return localized


def localize_event_data(locale: str, event_data: Any) -> Any:
    """本地化事件字典，供 SSE 与任务详情接口统一复用。"""
    if not isinstance(event_data, dict):
        return event_data

    localized = dict(event_data)
    localized["payload"] = localize_event_payload(locale, localized.get("payload"))
    return localized


class LocaleMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        locale = (
            _match_locale(request.cookies.get("lang"))
            or _resolve_accept_language(request.headers.get("accept-language"))
            or DEFAULT_LOCALE
        )
        request.state.locale = locale
        response = await call_next(request)
        response.headers.setdefault("Content-Language", locale)
        return response


@pass_context
def _template_translate(context, key: str, **params: Any) -> str:
    request = context.get("request")
    if request is None:
        return get_text(DEFAULT_LOCALE, key, **params)
    return _(request, key, **params)


@pass_context
def _template_current_locale(context) -> str:
    request = context.get("request")
    if request is None:
        return DEFAULT_LOCALE
    return getattr(request.state, "locale", DEFAULT_LOCALE)


@pass_context
def _template_translations(context) -> dict:
    request = context.get("request")
    locale = DEFAULT_LOCALE
    if request is not None:
        locale = getattr(request.state, "locale", DEFAULT_LOCALE)
    return load_translations(locale)


def setup_i18n(app: FastAPI, templates: Jinja2Templates | None) -> None:
    if getattr(app.state, "_i18n_ready", False):
        return
    app.add_middleware(LocaleMiddleware)
    if templates is not None:
        templates.env.globals["_"] = _template_translate
        templates.env.globals["current_locale"] = _template_current_locale
        templates.env.globals["_translations"] = _template_translations
    app.state._i18n_ready = True
