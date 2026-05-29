# -*- coding: utf-8 -*-
"""
代理池服务

仅供"生成 checkout 链接"按号选 IP 出口使用。注册流程 / token 提取 /
aimizy 中转**不读这张表**，继续走 .env 全局 PROXY。

支持两种 URL 输入格式（service 层自动归一化）：
1. 完整 URL：`socks5h://user:password@host:port` —— 直接存
2. 1024Proxy 4 段冒号格式：`host:port:user:password` —— 自动转 socks5h URL

UI 展示时 url 始终脱敏（前缀 + ***），admin 不允许直接读取明文 url。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import quote

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from src.db.engine import get_session
from src.db.models import LinkTemplate, Proxy

logger = logging.getLogger(__name__)


_VALID_PROTOCOLS = ("socks5h://", "socks5://", "http://", "https://")


def _normalize_proxy_url(raw: str) -> str:
    """把用户输入的代理字符串归一化为 requests 可用的完整 URL。

    支持：
    - 完整 URL（含 ://）→ 原样返回
    - 4 段冒号 `host:port:user:password` → 转 `socks5h://user:password@host:port`
      （1024Proxy 默认协议是 SOCKS5，用 socks5h 让 DNS 走代理避免本地解析泄露 IP）

    Args:
        raw: 用户输入的代理串

    Returns:
        归一化后的完整 URL；空输入抛 ValueError。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("代理 URL 不能为空")

    # 已经是完整 URL → 校验协议头并原样返回
    if any(text.lower().startswith(p) for p in _VALID_PROTOCOLS):
        return text

    # 4 段冒号格式：host:port:user:password
    # 注意 password 里可能含冒号，所以用 maxsplit=3
    parts = text.split(":", 3)
    if len(parts) == 4:
        host, port, user, password = (p.strip() for p in parts)
        if not host or not port or not user or not password:
            raise ValueError("4 段冒号代理格式不完整（host:port:user:password 都不可空）")
        if not port.isdigit():
            raise ValueError(f"端口必须是数字，得到 {port!r}")
        # 用户名/密码做 URL 编码（password 里有 @ / : 会破坏 URL）
        return f"socks5h://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"

    raise ValueError(
        "代理格式无法识别。支持："
        "① socks5h://user:password@host:port "
        "② http://user:password@host:port "
        "③ host:port:user:password（1024Proxy 4 段冒号格式，自动转 socks5h）"
    )


def _mask_proxy_url(url: str) -> str:
    """脱敏代理 URL（仅展示给前端，不暴露完整密码 / 用户名）。

    例：socks5h://wruz20033-region-CA-st-Alberta-city-Edmonton-sid-xxx:evrscsuk@us.1024proxy.io:3000
    →   socks5h://wruz20033-region-CA-***@us.1024proxy.io:3000
    """
    if not url:
        return ""
    # 用正则提取 protocol://user:password@host:port
    m = re.match(r"^(\w+://)([^:@]+)(?::[^@]+)?@(.+)$", url)
    if not m:
        # 没有用户名密码部分（如 http://host:port）→ 原样返回
        return url
    protocol, user, host = m.group(1), m.group(2), m.group(3)
    user_preview = user[:18] if len(user) > 18 else user
    return f"{protocol}{user_preview}-***@{host}"


def _proxy_to_dict(p: Proxy, *, include_full_url: bool = False) -> dict[str, Any]:
    """脱敏序列化。include_full_url=True 仅服务端内部使用，**禁止暴露给前端**。"""
    out: dict[str, Any] = {
        "id": p.id,
        "label": p.label,
        "country": p.country or "",
        "notes": p.notes or "",
        "is_active": bool(p.is_active),
        "created_at": p.created_at.isoformat() if p.created_at else "",
        "url_masked": _mask_proxy_url(p.url or ""),
    }
    if include_full_url:
        out["url"] = p.url or ""
    return out


def list_proxies(*, include_inactive: bool = True) -> list[dict[str, Any]]:
    """列出所有代理，按 label 升序。默认包含停用项（UI 自己用 is_active 标灰）。"""
    with get_session() as session:
        stmt = select(Proxy)
        if not include_inactive:
            stmt = stmt.where(Proxy.is_active == True)  # noqa: E712
        rows = list(session.exec(stmt.order_by(Proxy.label)).all())  # type: ignore
        return [_proxy_to_dict(p) for p in rows]


def get_proxy(proxy_id: int, *, with_url: bool = False) -> Optional[dict[str, Any]]:
    """按 id 取单条；不存在返回 None。

    `with_url=True` 仅供 generate-link 路由内部用（要拿真实 URL 调 ChatGPT）；
    HTTP 响应层永远不应回传 `url` 字段（前端只看 url_masked）。
    """
    with get_session() as session:
        p = session.get(Proxy, proxy_id)
        return _proxy_to_dict(p, include_full_url=with_url) if p else None


def create_proxy(
    *,
    label: str,
    url: str,
    country: str = "",
    notes: str = "",
    is_active: bool = True,
) -> dict[str, Any]:
    """新增代理。label 必须全局唯一；url 走归一化（支持 4 段冒号格式）。"""
    safe_label = (label or "").strip()
    if not safe_label:
        raise ValueError("label 不能为空")
    if len(safe_label) > 80:
        raise ValueError("label 过长（>80）")

    normalized_url = _normalize_proxy_url(url)
    safe_country = (country or "").strip()[:8].upper()
    safe_notes = (notes or "").strip()[:200]

    with get_session() as session:
        existing = session.exec(
            select(Proxy).where(Proxy.label == safe_label)
        ).first()
        if existing is not None:
            raise ValueError(f"代理标签已存在: {safe_label}")

        p = Proxy(
            label=safe_label,
            url=normalized_url,
            country=safe_country,
            notes=safe_notes,
            is_active=bool(is_active),
        )
        session.add(p)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise ValueError(f"代理标签已存在: {safe_label}") from exc
        session.refresh(p)
        logger.info("创建代理 id=%s label=%s country=%s", p.id, p.label, p.country)
        return _proxy_to_dict(p)


def update_proxy(
    proxy_id: int,
    *,
    is_active: Optional[bool] = None,
    notes: Optional[str] = None,
    country: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """局部更新。允许改 is_active / notes / country；**不允许改 url 和 label**。

    要换 URL → 删了重建（避免误改影响在用模板）。
    """
    with get_session() as session:
        p = session.get(Proxy, proxy_id)
        if p is None:
            return None
        if is_active is not None:
            p.is_active = bool(is_active)
        if notes is not None:
            p.notes = (notes or "").strip()[:200]
        if country is not None:
            p.country = (country or "").strip()[:8].upper()
        session.add(p)
        session.commit()
        session.refresh(p)
        logger.info("更新代理 id=%s is_active=%s", p.id, p.is_active)
        return _proxy_to_dict(p)


def delete_proxy(proxy_id: int) -> tuple[bool, str]:
    """硬删除。被 LinkTemplate 引用时拒绝（返回 False + 引用清单提示）。"""
    with get_session() as session:
        p = session.get(Proxy, proxy_id)
        if p is None:
            return False, f"代理不存在: id={proxy_id}"

        # 检查 LinkTemplate 引用
        refs = list(session.exec(
            select(LinkTemplate.name).where(LinkTemplate.proxy_id == proxy_id)
        ).all())
        if refs:
            sample = ", ".join(refs[:3])
            more = f" 等 {len(refs)} 个" if len(refs) > 3 else ""
            return False, f"被模板使用：{sample}{more}。请先在这些模板里改代理选择"

        session.delete(p)
        session.commit()
        logger.info("删除代理 id=%s label=%s", proxy_id, p.label)
        return True, ""


def count_templates_using_proxy(proxy_id: int) -> int:
    """返回使用该代理的模板数（前端展示用，避免运维直接点删除才知道）。"""
    with get_session() as session:
        return len(list(session.exec(
            select(LinkTemplate.id).where(LinkTemplate.proxy_id == proxy_id)
        ).all()))


def get_active_proxy_by_country(country: str, *, with_url: bool = False) -> Optional[dict[str, Any]]:
    """按国家选第一条可用代理（promo eligibility 验证用）。

    选择策略：is_active=True 且 country=XX 的多条里取 id 最小的（即"最早创建的"，最稳定）。
    返回 None 表示该国家没有可用代理 —— 调用方需要给出可读错误（提示用户去添加代理）。

    Args:
        country: ISO 国家码（大小写不敏感，内部 upper）
        with_url: True 时返回脱敏前的真实 url（仅服务端内部用，禁止暴露给前端）

    Returns:
        Proxy dict（同 get_proxy 的格式）或 None
    """
    safe_country = (country or "").strip().upper()
    if not safe_country:
        return None
    with get_session() as session:
        p = session.exec(
            select(Proxy)
            .where(Proxy.country == safe_country)
            .where(Proxy.is_active == True)  # noqa: E712
            .order_by(Proxy.id)
        ).first()
        return _proxy_to_dict(p, include_full_url=with_url) if p else None


__all__ = [
    "list_proxies",
    "get_proxy",
    "create_proxy",
    "update_proxy",
    "delete_proxy",
    "count_templates_using_proxy",
    "get_active_proxy_by_country",
]
