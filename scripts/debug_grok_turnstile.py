# -*- coding: utf-8 -*-
"""临时调试脚本：自动登录控制面 + 发起 Grok 注册任务，用于 Turnstile 自动点击调试。

用法：
    python scripts/debug_grok_turnstile.py            # 发起一个 grok 任务
    python scripts/debug_grok_turnstile.py k1b9945d   # 指定 profile_id

依赖正在跑的 uvicorn（127.0.0.1:8080）。登录用默认 admin/admin123456。
调通后可删除（属一次性排障脚本）。
"""
from __future__ import annotations

import re
import sys

import requests

BASE = "http://127.0.0.1:8080"
PROFILE = sys.argv[1] if len(sys.argv) > 1 else "k1b9945d"


def main() -> None:
    s = requests.Session()
    # 1) GET /login 拿初始 session cookie + csrf token（渲染在 window.__csrfToken）
    r = s.get(f"{BASE}/login", timeout=10)
    m = re.search(r"window\.__csrfToken\s*=\s*\"([^\"]+)\"", r.text)
    csrf = m.group(1) if m else ""
    print(f"[login page] status={r.status_code} csrf={'有' if csrf else '无'}")

    # 2) POST 登录
    r = s.post(
        f"{BASE}/api/auth/login",
        json={"username": "admin", "password": "admin123456"},
        headers={"X-CSRF-Token": csrf, "Origin": BASE, "Referer": f"{BASE}/login"},
        timeout=10,
    )
    print(f"[login] status={r.status_code} body={r.text[:200]}")
    if r.status_code != 200:
        print("登录失败，终止")
        return
    # 登录响应会 rotate csrf，用新的
    new_csrf = r.json().get("csrf_token") or csrf

    # 3) 发起 grok 任务
    payload = {
        "profile_id": PROFILE,
        "registration_kind": "grok",
        "mode": "register_only",
        "auto_start": True,
        # grok 用 cfworker 邮箱（cloud-sentry 域名，managed），与 UI 成功路径一致
        "mail_provider": "mail-cfworker-default",
    }
    r = s.post(
        f"{BASE}/api/tasks",
        json=payload,
        headers={"X-CSRF-Token": new_csrf, "Origin": BASE, "Referer": f"{BASE}/"},
        timeout=30,
    )
    print(f"[create grok task] status={r.status_code} body={r.text[:400]}")


if __name__ == "__main__":
    main()
