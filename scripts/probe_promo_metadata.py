"""探测 ChatGPT promo metadata API 的真实响应结构（一次性脚本，P0 用）。

用途：
    本计划 P4 要把 metadata 抽出 5 个结构化字段（percent_off / duration_months /
    expires_at / max_redemptions / applicable_plans），但项目里没有真实 ChatGPT
    响应样本（tests mock 的 schema 是猜的）。本脚本调一次真实 API，把响应原文
    dump 到 data/promo_metadata_sample.json 供后续抽字段参照。

依赖：
    - DB 里至少 1 个 status='success' 账号（用其 access_token 调 ChatGPT API）
    - 1 个已知 ELIGIBLE 的 promo_code（用户提供 / 命令行参数）
    - 有效的代理（避免 CF 403）；通过 --proxy-id 指定 Proxy 表 active 代理

使用：
    python scripts/probe_promo_metadata.py --code datroaiuk --country UK --proxy-id 1
    python scripts/probe_promo_metadata.py --code aff10off --country US
        （不指定 proxy-id 时直连，预计会被 CF 403）

输出：
    data/promo_metadata_sample.json — 完整原始响应 + 抽取候选字段路径

脚本本身是 P0 一次性产物，跑完 P4 后可删。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# 确保 src 在 sys.path（脚本独立运行场景）
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.promo_eligibility import check_eligibility
from src.services.promo_eligibility_service import (
    PromoVerifyError,
    _resolve_proxy_url_for_country,
    _resolve_token_for_verification,
)


def _candidate_paths(d: dict, prefix: str = "") -> list[tuple[str, object]]:
    """递归列出 dict 所有叶子路径，帮 P4 找字段。"""
    out: list[tuple[str, object]] = []
    if isinstance(d, dict):
        for k, v in d.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                out.extend(_candidate_paths(v, path))
            else:
                out.append((path, v))
    elif isinstance(d, list):
        for i, item in enumerate(d):
            out.extend(_candidate_paths(item, f"{prefix}[{i}]"))
    return out


def _guess_field_paths(metadata: dict) -> dict[str, list[str]]:
    """对常见字段名做模糊匹配，给 P4 抽字段做出初稿。"""
    paths = _candidate_paths(metadata)
    keyword_map = {
        "percent_off": ["percent", "off", "discount"],
        "duration_months": ["duration", "month", "period"],
        "expires_at": ["expir", "end", "until"],
        "max_redemptions": ["max", "redempt", "limit", "quota"],
        "applicable_plans": ["plan", "product", "tier"],
    }
    hits: dict[str, list[str]] = {}
    for field, keywords in keyword_map.items():
        candidates = []
        for path, value in paths:
            path_lower = path.lower()
            if any(kw in path_lower for kw in keywords):
                candidates.append(f"  {path} = {value!r}")
        hits[field] = candidates
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="探测 ChatGPT promo metadata schema")
    parser.add_argument("--code", required=True, help="已知 ELIGIBLE 的 promo code")
    parser.add_argument("--country", default="US", help="代理出口国家（ISO alpha-2）")
    parser.add_argument("--proxy-id", type=int, default=None, help="指定 Proxy 表 active 代理 id")
    parser.add_argument("--proxy-url", default=None, help="直接传完整代理 URL（如 http://1.2.3.4:7098），优先级高于 --proxy-id；适合用动态供应商现拉的干净 IP")
    parser.add_argument("--fresh-1024", action="store_true", help="从动态供应商现拉一个干净 1024 IP 用（忽略 --proxy-id/--proxy-url）")
    parser.add_argument("--run-id", default=None, help="借哪个 Run 的 access_token；默认最近 success")
    parser.add_argument(
        "--output",
        default="data/promo_metadata_sample.json",
        help="输出文件路径",
    )
    args = parser.parse_args()

    print(f"→ 解析 access_token...")
    try:
        token, used_run_id = _resolve_token_for_verification(args.run_id)
    except PromoVerifyError as exc:
        print(f"✗ token 解析失败: {exc.code}: {exc.message}")
        return 1
    print(f"  ✓ 使用 Run {used_run_id[:8]} 的 token")

    print(f"→ 解析代理...")
    if args.fresh_1024:
        # 从动态供应商现拉一个干净 IP（绕开可能已脏的静态池）
        from src.services.proxy_provider_service import list_providers, get_provider
        from src.proxy_clients.adapters.registry import get_adapter
        provs = [p for p in list_providers(include_inactive=False) if p["kind"] == "1024proxy"]
        if not provs:
            print("  ✗ 无活跃的 1024proxy 动态供应商，无法 --fresh-1024")
            return 1
        full = get_provider(provs[0]["id"], with_secrets=True)
        info = get_adapter(full["kind"]).fetch_one(full, country=args.country)
        if info is None:
            print("  ✗ 动态供应商拉 IP 失败")
            return 1
        proxy_url = f"http://{info.host}:{info.port}"
        matched = (info.country or "").upper() == args.country.upper()
        print(f"  ✓ 动态供应商现拉 IP: {proxy_url} (出口={info.country or '?'}, matched={matched})")
    elif args.proxy_url:
        proxy_url = args.proxy_url
        matched = True  # 手动传的视为已确认
        print(f"  ✓ 使用手动传入代理: {proxy_url}")
    else:
        proxy_url, matched = _resolve_proxy_url_for_country(
            args.country, template_proxy_id=args.proxy_id,
        )
        if proxy_url:
            print(f"  ✓ proxy_url 已设置 (country_matched={matched})")
        else:
            print("  ⚠ 无代理（直连），可能被 CF 403")

    print(f"→ 调 ChatGPT eligibility API code={args.code}...")
    result = check_eligibility(
        access_token=token,
        code=args.code,
        proxy_url=proxy_url,
    )
    print(f"  status={result.status} http={result.http_status} reason={result.reason_code}")
    if result.error:
        print(f"  ✗ error={result.error}")
        return 2

    if result.metadata_raw is None:
        print("  ✗ metadata_raw 为空（可能 status=not_found 跳过了 metadata 拉取）")
        return 3

    print(f"→ 分析字段路径...")
    hits = _guess_field_paths(result.metadata_raw)

    output_data = {
        "_meta": {
            "probed_at": datetime.now().isoformat(),
            "code": args.code,
            "country": args.country,
            "status": result.status,
            "reason_code": result.reason_code,
            "used_run_id": used_run_id,
            "proxy_used": bool(proxy_url),
        },
        "_field_path_guesses": hits,
        "raw_metadata": result.metadata_raw,
    }

    out_path = _REPO_ROOT / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output_data, indent=2, ensure_ascii=False))
    print(f"✓ 已 dump 到 {out_path}")
    print()
    print("=== 字段路径猜测（用于 P4 _extract_promo_fields 实现）===")
    for field, candidates in hits.items():
        print(f"\n{field}:")
        if candidates:
            for c in candidates[:5]:
                print(c)
        else:
            print("  （未找到匹配字段，需人工查看 raw_metadata）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
