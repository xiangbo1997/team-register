# -*- coding: utf-8 -*-
"""BrowserBorrower 实战验证脚本（裸跑 vs borrow 对比）。

用法：
    # 1. 在 AdsPower profile 已登录 chatgpt.com 的页面，DevTools Network 标签
    #    找一个 /backend-api/payments/checkout 或 /backend-api/me 请求
    # 2. 右键 → Copy → Copy as cURL (bash)
    # 3. 把整段粘贴进 examples/curl_sample.txt（一行一段 cURL，--data-raw 等多行也支持）
    python scripts/diagnose_borrow.py --curl examples/curl_sample.txt

    # 或者直接通过 stdin 喂入
    pbpaste | python scripts/diagnose_borrow.py --curl -

输出：
    1. 解析摘要        — 从 cURL 抽出 access_token / borrow_headers / borrow_cookies
                          统计：x-oai-is 是否存在、cf_clearance 是否存在、cookie 总数
    2. 两路对照        — 同一个 access_token 跑两次 PaymentLinkGenerator.generate_checkout_link：
                          (a) 裸跑（不传 borrow）
                          (b) borrow 跑（传 borrow_headers + borrow_cookies）
                          对比 HTTP 状态 + 是否拿到 checkout URL
    3. 结论建议        — 根据对比结果给出 "borrow 必要 / 不必要 / 数据不足" 判断

零副作用：不写库、不调 OpenAI 注册端点、不消耗 promo 码。
仅调用 /backend-api/payments/checkout（Plus 的 plan_type，return_mode=long）。
即便 checkout 创建了 session，hosted checkout 链接需要用户手动打开才会扣费。

脱敏：
    打印的 access_token 仅显示头尾各 8 字符
    cookies 值仅显示前 16 字符 + 长度
    headers 中的 authorization / x-oai-is 同样脱敏
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Optional


logger = logging.getLogger("diagnose_borrow")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


# ---------------------------------------------------------------------------
# cURL 解析（DevTools "Copy as cURL (bash)" 格式）
# ---------------------------------------------------------------------------

def parse_curl(curl_text: str) -> dict:
    """从 cURL 文本抽取 headers / cookies / authorization。

    支持格式：
        curl 'URL' \\
          -H 'header-name: value' \\
          -b 'cookie1=v1; cookie2=v2' \\
          ...

    返回：
        {
            "url": str,
            "headers": dict[str, str],
            "cookies": dict[str, str],
            "access_token": str,  # 从 Authorization Bearer 抽出
        }
    """
    # 去掉行尾 backslash + 换行，合并成一行
    flat = re.sub(r"\\\s*\n\s*", " ", curl_text)

    # URL：第一个 'XXX' 或 "XXX" 在 curl 后
    url_match = re.search(r"curl\s+['\"]([^'\"]+)['\"]", flat)
    url = url_match.group(1) if url_match else ""

    # 所有 -H 'name: value'
    headers: dict[str, str] = {}
    for m in re.finditer(r"-H\s+\$?'([^']*?):\s*([^']*?)'", flat):
        name = m.group(1).strip().lower()
        value = m.group(2).strip()
        if name and value:
            headers[name] = value

    # cookies：-b 'k=v; k=v' 或 --cookie
    cookies: dict[str, str] = {}
    cookie_match = re.search(r"-b\s+\$?'([^']*)'", flat)
    if cookie_match:
        cookie_str = cookie_match.group(1)
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, _, v = pair.partition("=")
                k, v = k.strip(), v.strip()
                if k and v:
                    cookies[k] = v

    # access_token：从 authorization header 抠
    access_token = ""
    auth = headers.get("authorization", "")
    bearer_match = re.match(r"Bearer\s+(.+)", auth, re.IGNORECASE)
    if bearer_match:
        access_token = bearer_match.group(1).strip()

    return {
        "url": url,
        "headers": headers,
        "cookies": cookies,
        "access_token": access_token,
    }


# ---------------------------------------------------------------------------
# 脱敏 + 打印
# ---------------------------------------------------------------------------

def _mask(value: str, head: int = 8, tail: int = 8) -> str:
    if not value or len(value) <= head + tail:
        return value
    return f"{value[:head]}...{value[-tail:]} (len={len(value)})"


def print_parse_summary(parsed: dict) -> None:
    print("\n=== cURL 解析摘要 ===")
    print(f"URL: {parsed['url']}")
    print(f"access_token: {_mask(parsed['access_token'])}")
    print(f"headers 总数: {len(parsed['headers'])}")
    print(f"cookies 总数: {len(parsed['cookies'])}")

    critical_headers = [
        "x-oai-is", "oai-device-id", "oai-session-id",
        "user-agent", "sec-ch-ua", "sec-ch-ua-platform",
    ]
    print("\n关键 headers：")
    for h in critical_headers:
        v = parsed["headers"].get(h, "")
        print(f"  [{'✓' if v else '✗'}] {h}: {_mask(v, 12, 12) if v else '<missing>'}")

    critical_cookies = [
        "cf_clearance", "__Secure-next-auth.session-token",
        "oai-did", "oai-sc", "__cf_bm",
    ]
    print("\n关键 cookies：")
    for c in critical_cookies:
        v = parsed["cookies"].get(c, "")
        print(f"  [{'✓' if v else '✗'}] {c}: {_mask(v, 16, 0) if v else '<missing>'}")


# ---------------------------------------------------------------------------
# 两路对照测试
# ---------------------------------------------------------------------------

def run_comparison(parsed: dict, plan_type: str = "plus", return_mode: str = "long") -> dict:
    """同一 access_token 跑两次 generate_checkout_link：(a) 裸跑 (b) borrow 跑。"""
    from src.automation.browser_borrow import BrowserBorrower
    from src.payment_link import PaymentLinkGenerator

    access_token = parsed["access_token"]
    if not access_token:
        return {"error": "解析失败：未在 cURL 中找到 Authorization Bearer"}

    # 用 BrowserBorrower 白名单过滤 → 拿到干净的 borrow snapshot
    borrower = BrowserBorrower.from_dict(
        headers=parsed["headers"],
        cookies=parsed["cookies"],
        source_url=parsed["url"],
    )
    print(f"\nBrowserBorrower 白名单过滤后："
          f"headers={len(borrower.snapshot.headers)}, "
          f"cookies={len(borrower.snapshot.cookies)}, "
          f"is_usable={borrower.is_usable()}")

    # 路 A：裸跑
    print("\n=== 路 A：裸跑（不传 borrow_headers/borrow_cookies）===")
    try:
        ok_a, link_a = PaymentLinkGenerator.generate_checkout_link(
            access_token=access_token,
            plan_type=plan_type,
            return_mode=return_mode,
        )
        print(f"结果：{'成功' if ok_a else '失败'} | link/error: {link_a[:120]}")
    except Exception as exc:
        ok_a, link_a = False, f"异常: {exc}"
        print(f"异常：{exc}")

    # 路 B：borrow 跑
    print("\n=== 路 B：borrow 跑（传 borrow_headers + borrow_cookies）===")
    try:
        ok_b, link_b = PaymentLinkGenerator.generate_checkout_link(
            access_token=access_token,
            plan_type=plan_type,
            return_mode=return_mode,
            borrow_headers=dict(borrower.snapshot.headers),
            borrow_cookies=dict(borrower.snapshot.cookies),
        )
        print(f"结果：{'成功' if ok_b else '失败'} | link/error: {link_b[:120]}")
    except Exception as exc:
        ok_b, link_b = False, f"异常: {exc}"
        print(f"异常：{exc}")

    return {
        "bare": {"ok": ok_a, "result": link_a},
        "borrow": {"ok": ok_b, "result": link_b},
        "borrower_critical": borrower.is_usable(),
    }


# ---------------------------------------------------------------------------
# 结论建议
# ---------------------------------------------------------------------------

def print_verdict(comparison: dict) -> None:
    print("\n=== 结论建议 ===")
    if "error" in comparison:
        print(f"❌ 无法对比：{comparison['error']}")
        return

    bare_ok = comparison["bare"]["ok"]
    borrow_ok = comparison["borrow"]["ok"]
    critical = comparison["borrower_critical"]

    if bare_ok and borrow_ok:
        print("→ 两路都成功：当前 access_token 下 OpenAI 风控较松，borrow 暂时**不必要**。")
        print("  但建议定期重测；OpenAI 加严时 borrow 仍是兜底。")
    elif not bare_ok and borrow_ok:
        print("→ 裸跑失败、borrow 成功：✅ borrow **必要且有效**，应在调用方接入。")
        print("  下一步：把 borrow_headers/borrow_cookies 接到 orchestrator._phase_payment。")
    elif bare_ok and not borrow_ok:
        print("→ 裸跑成功、borrow 失败：⚠ 异常路径，borrow 可能引入了冲突 header/cookie。")
        print("  下一步：检查 BORROW_HEADER_NAMES 白名单是否过度，逐个 header 二分排查。")
    else:
        print("→ 两路都失败：access_token 可能已过期、proxy 错配、或新风控规则。")
        if not critical:
            print("  注意：borrower.is_usable=False，cURL 里缺 cf_clearance 或 session-token。")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_curl_source(curl_arg: str) -> str:
    """从文件 / stdin 读 cURL 文本。"""
    if curl_arg == "-":
        return sys.stdin.read()
    path = Path(curl_arg)
    if not path.exists():
        raise FileNotFoundError(f"cURL 文件不存在: {curl_arg}")
    return path.read_text(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="BrowserBorrower 验证脚本（裸跑 vs borrow 对比）"
    )
    parser.add_argument(
        "--curl", required=True,
        help="cURL 文本来源：文件路径 或 '-' 从 stdin 读",
    )
    parser.add_argument(
        "--plan-type", default="plus", choices=["plus", "team"],
        help="测试 plan_type（默认 plus，team 会绕道 aimizy 干扰诊断）",
    )
    parser.add_argument(
        "--return-mode", default="long", choices=["long", "app"],
        help="payment_link return_mode（默认 long 拿原始链接）",
    )
    parser.add_argument(
        "--skip-actual-call", action="store_true",
        help="只解析 cURL 不实际调用 OpenAI，用于 offline 验证脚本本身",
    )
    args = parser.parse_args()

    try:
        curl_text = _read_curl_source(args.curl)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 2

    if not curl_text.strip():
        logger.error("cURL 输入为空")
        return 2

    parsed = parse_curl(curl_text)
    print_parse_summary(parsed)

    if args.skip_actual_call:
        print("\n[--skip-actual-call] 已跳过实际调用阶段。")
        return 0

    if not parsed["access_token"]:
        logger.error("解析失败：cURL 中没有 Authorization Bearer header")
        return 1

    comparison = run_comparison(parsed, plan_type=args.plan_type, return_mode=args.return_mode)
    print_verdict(comparison)
    return 0


if __name__ == "__main__":
    sys.exit(main())
