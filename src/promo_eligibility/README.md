# promo_eligibility 子模块

只做一件事：**给定 promo 码，判定它在 ChatGPT 上是否可用**。

## 与原 gpt-promo-scanner 项目的关系

本模块是 [gpt-promo-scanner](https://github.com/JUk1-GH/gpt-promo-scanner) 项目核心能力的最小化集成版。**只搬了 eligibility 验证**，其他一律不搬：

| 原项目能力 | 本模块 | 复用的 team-register 现成实现 |
|---|---|---|
| `verify.py:check_code()` | ✅ 抄进 `client.py` | — |
| `verify.py:create_session()` | ❌ 不搬 | 改用 `payment_link.py:140` 的 curl_cffi 模式 |
| `auto_scan.py:_curl()`（Clash socket）| ❌ 不搬 | 用 `Proxy` 表 + `proxy_service.get_active_proxy_by_country()` |
| `auto_scan.py:fetch_exchange_rates()` | ❌ 不搬 | 不需要（dashboard 直接展示本地货币）|
| `auto_scan.py:REGIONS / BASE_2_SEAT_PRICES` | ❌ 不搬 | 已在 `LinkTemplate.aimizy_country` / `aimizy_currency` 字段里 |
| `discover_codes.py` 候选码矩阵扫描 | ❌ 不搬 | 用户手动新建 `LinkTemplate` 行（dashboard）|
| `open_stripe.py:get_stripe_url()` | ❌ 不搬 | 已是 `payment_link.py:PaymentLinkGenerator.generate_checkout_link()` |
| `config.toml` + `tomllib` | ❌ 不搬 | 复用 `src/config.py:AppConfig` + `.env` |

## 调用方式

```python
from src.promo_eligibility import check_eligibility, EligibilityResult

result = check_eligibility(
    access_token="eyJhbGc...",
    code="talentgeniusus",
    proxy_url="socks5h://user:pass@host:port",  # 可选；通常通过 proxy_service 按国家挑出来
)

if result.status == "eligible":
    # 当前 access_token + 当前 proxy 出口下，可以直接付款
    ...
elif result.status == "exists":
    # 码是真的，但当前 proxy 国家不匹配；换对应国家代理再试
    ...
elif result.status == "not_found":
    # 码不存在，或 access_token 已过期（**容易混淆**）
    ...
```

## 为什么 `not_found` 可能是误判

ChatGPT 在 access_token 过期时，对**任何** promo 码都返回 `invalid_code`。
所以 service 层应当：
1. 先用一个已知有效码（如 `talentgeniusus`）做 token 健康检查
2. 健康检查不通过时，跳过当批所有验证 + 提示用户刷新 token

这是 promo-scanner 原项目踩过的最大坑（README "踩坑 #9"）。

## 不在本模块的职责

- 选代理 → `src/services/proxy_service.py:get_active_proxy_by_country()`
- 写数据库 → `src/services/promo_eligibility_service.py`
- 节奏控制 / 限流 / 重试 → 上层 service 自行决定
- token 获取与存储 → 由 team-register 主流程托管，本模块只接受参数
