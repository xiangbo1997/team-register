# OpenAI 反爬应对：借用 vs PoW —— 三模块设计决策

> **文档定位**：解释 `src/automation/sentinel.py` + `src/automation/browser_borrow.py` +
> `scripts/diagnose_borrow.py` 这三块为什么共存、什么时候用哪条路径、未来风控变化时如何应对。
> **不是**：算法逆向教程 / OpenAI 内部 API 文档复述。
> **修改前请读**：本文档"为什么暂不接入业务层"那节 —— 避免在没数据支撑时铺开 borrow 改动。

最后更新：2026-05-20

---

## 0. TL;DR（30 秒读懂）

`team-register` 调 `chatgpt.com/backend-api/*` 这种**旁路 API**（不在 AdsPower 浏览器内）时，
OpenAI 可能用反爬挡住。本仓库提供**两条应对路径**：

| 路径 | 模块 | 工作原理 | 状态 |
|------|------|---------|------|
| 🅰 **借用法**（主推） | `browser_borrow.py` | 从 AdsPower 浏览器借现成的 `x-oai-is` header + `cf_clearance` cookie | API 备好，**未在业务层接入** |
| 🅱 **PoW 法**（fallback） | `sentinel.py` | 本地 FNV-1a brute-force 算 sentinel token | API 备好，noop 默认 |
| 🔬 **诊断工具** | `diagnose_borrow.py` | 用真实 cURL 跑"裸跑 vs borrow"对比 | 可独立运行 |

**当前真实情况**：`/backend-api/me` 不带任何反爬 header 也能 200，说明 OpenAI **并未对所有
旁路 API 强制反爬**。是否需要接入 borrow，应在 `diagnose_borrow.py` 给出真实数据后再决定。

---

## 1. 问题背景

### 1.1 旁路 API 的反爬风险

`team-register` 的主流程在 **AdsPower 反检测浏览器**内跑（注册 / 登录 / 支付页交互），
浏览器里的请求自动带上 cf_clearance、sdk.js 生成的反爬 header，OpenAI 不会拦。

但**三处旁路调用**绕开浏览器、用 `curl_cffi` 直接打 chatgpt.com：

| 模块 | 入口 | 调用的端点 |
|------|------|-----------|
| `src/payment_link.py` | `PaymentLinkGenerator.generate_checkout_link` | `POST /backend-api/payments/checkout` |
| `src/promo_eligibility/client.py` | `check_eligibility` | `GET /backend-api/promotions/eligibility/{code}` |
| `src/automation/runtime.py:163` | `extract_session_tokens_with_http` | `GET /api/auth/session` |

这些 curl_cffi 调用**裸跑**（只带 Bearer access_token）—— OpenAI 2026+ 加严风控时，
这三处会同时失效，注册成功率与支付链接生成率会随风控扩面而崩塌。

### 1.2 OpenAI 反爬体系（2026-05 实测）

DevTools 抓 AdsPower profile 的真实请求，可以看到 OpenAI 实际依赖的几个层：

1. **`cf_clearance` cookie** — Cloudflare 通过证明，浏览器解完 Turnstile 后自动种
2. **`__Secure-next-auth.session-token` cookie** — NextAuth 登录态
3. **`x-oai-is` header** — JWE 加密的客户端完整性签名（每请求不同，sdk.js 生成）
4. **`oai-device-id` / `oai-session-id` header** — 设备 / 会话标识
5. **`POST /backend-api/sentinel/chat-requirements/prepare+finalize`** — 应用级握手
   （含 turnstile / proofofwork / so 三件套）

**关键观察**：`/backend-api/me` 这种"读自己信息"的请求**没带** `x-oai-is`、`openai-sentinel-token`
等任何反爬 header，但**HTTP 200 成功**。说明 OpenAI 对端点**分级强制反爬**，不是"所有 backend-api 都要"。

---

## 2. 三模块的角色分工

```
┌──────────────────────────────────────────────────────────┐
│  应用层（payment_link / promo_eligibility / runtime）       │
│                                                          │
│  接受可选参数：sentinel_provider, borrow_headers,           │
│              borrow_cookies                               │
└────────────┬─────────────────────────────────┬────────────┘
             │                                 │
             ▼                                 ▼
┌─────────────────────────┐     ┌──────────────────────────┐
│ src/automation/         │     │ src/automation/          │
│ sentinel.py             │     │ browser_borrow.py        │
│                         │     │                          │
│ PoW 法（推测路线）        │     │ 借用法（实测路线）         │
│ - NoOpProvider          │     │ - BorrowSnapshot         │
│ - PurePythonProvider    │     │ - BrowserBorrower        │
│   (FNV-1a brute-force)  │     │   .from_dict()           │
│ - try_get_sentinel_token│     │   .from_page()           │
│                         │     │ - 31 个白名单常量           │
└─────────────────────────┘     └──────────────────────────┘
             │                                 │
             └─────────────┬───────────────────┘
                           ▼
              ┌─────────────────────────┐
              │ scripts/                │
              │ diagnose_borrow.py      │
              │                         │
              │ 验证工具                  │
              │ - 解析 DevTools cURL     │
              │ - 裸跑 vs borrow 对比    │
              │ - 输出"必要/不必要"判断   │
              └─────────────────────────┘
```

### 2.1 `sentinel.py` — PoW 法（推测路线）

**位置**：`src/automation/sentinel.py:1-446`

**起源**：移植自 `any-auto-register` 项目（多平台注册系统）的 `platforms/chatgpt/sentinel_token.py`
（263 行），算法是 FNV-1a brute-force PoW + 拿 `sentinel.openai.com/backend-api/sentinel/req` 挑战。

**当时的假设**：以为 OpenAI 用 `openai-sentinel-token` header 注入 PoW 证明。

**实测后修正**（见 §3）：OpenAI 2026 实际用的是 `x-oai-is`，且 `sentinel.openai.com` 域已经退役 ——
真实端点是 `chatgpt.com/backend-api/sentinel/chat-requirements/prepare+finalize`。

**当前价值**：
- ✅ **配置默认 `SENTINEL_STRATEGY=noop`** —— 零行为变更，不影响现状
- ✅ **保留作 fallback** —— OpenAI 偶尔降难度（`turnstile.required=false` + `so.required=false`）
  时纯 Python PoW 也许能用
- ✅ **23 个单元测试** —— 算法层稳定，未来需要时可作起点重写

**为什么不删**：删除等于丢掉 263 行算法移植工作。留作 `pure_python` 选项不增加运行时成本（默认 noop）。

### 2.2 `browser_borrow.py` — 借用法（实测路线，主推）

**位置**：`src/automation/browser_borrow.py:1-261`

**核心思路**：AdsPower 浏览器**已经完成了** cf_clearance + sentinel/chat-requirements 全套握手 ——
直接从它的 page.context **借** cookies + headers 给 curl_cffi 旁路调用即可，**不需要自己重算**。

**两个数据来源**：

| 入口 | 用途 |
|------|------|
| `BrowserBorrower.from_page(page)` | 真实 Playwright Page（生产 / 集成） |
| `BrowserBorrower.from_dict(...)` | 测试 / 从 cURL 粘贴数据手动构造 |

**白名单设计**（防止"借多了"反而成指纹）：

| 类型 | 数量 | 关键项 |
|------|------|--------|
| `BORROW_HEADER_NAMES` | 16 | `x-oai-is`, `oai-device-id`, `oai-session-id`, `user-agent`, `sec-ch-ua-*` |
| `BORROW_COOKIE_NAMES` | 15 | `cf_clearance`, `__Secure-next-auth.session-token`, `__cf_bm`, `oai-sc`, `oai-did` |

**关键凭据检查**：
```python
def has_critical(self) -> bool:
    """是否包含至少 1 个关键凭据：cf_clearance 或 session-token。"""
```
这是判断"borrow 是否有用"的快速门 —— 没有 cf_clearance 等于没借成功。

**应用层接入**（已完成）：
- `payment_link.generate_checkout_link` / `generate_short_link` 加 `borrow_headers` + `borrow_cookies` 参数
- `promo_eligibility.check_eligibility` 同上 + `_fetch_metadata` 透传 cookies

**应用层接入**（未做，见 §4）：
- `runtime.py:extract_session_tokens_with_http` —— 注册主链路
- `orchestrator._phase_payment` —— 支付编排

### 2.3 `diagnose_borrow.py` — 验证工具

**位置**：`scripts/diagnose_borrow.py:1-302`

**为什么需要它**：sentinel 和 borrow 两条路径都是"备好但不知道何时必要"。
诊断脚本提供**用真实数据验证**的能力 —— 用户从 AdsPower DevTools 抓一段 cURL，
脚本自动跑两次 `payment_link.generate_checkout_link`（裸跑 vs borrow），对比 HTTP 结果。

**输出三段**：
1. **cURL 解析摘要** —— 关键 headers / cookies 命中清单（脱敏后）
2. **两路对照** —— 同 access_token 跑两次，分别成功 / 失败
3. **结论建议** —— `borrow 必要 / 不必要 / 数据不足`

**使用场景**：
- 当前：决定是否把 borrow 接入业务层
- 未来：OpenAI 加严风控时，30 秒重测验证 borrow 是否仍有效

---

## 3. 实测发现 vs 最初推测（决策记录）

| 维度 | 最初推测（基于 any-auto-register 源码） | 实测发现（2026-05-20 DevTools） | 影响 |
|------|----------------------------------|---------------------------|------|
| sentinel token 注入 header 名 | `openai-sentinel-token` | `x-oai-is`（JWE 加密） | sentinel.py 注入的 header 名**不对**，noop 默认才是安全选择 |
| sentinel token 生成端点 | `sentinel.openai.com/backend-api/sentinel/req` | `chatgpt.com/backend-api/sentinel/chat-requirements/prepare+finalize` | PurePython 调的 endpoint 已不再有效 |
| PoW 算法 | 纯 FNV-1a brute-force（500k 次循环 < 1s）| **FNV-1a + Turnstile dx + so collector/snapshot** 三件套 | 纯 Python 无法独立生成完整 token |
| token 寿命 | 推测 5 分钟 | 实测 `expire_after: 540` 秒 = 9 分钟 | 池化（预生成）意义不大 |
| `/backend-api/me` 是否强制反爬 | 是 | **否**（无 sentinel header 也 200） | 不是所有 backend-api 都要 borrow |

**结论**：PoW 路线在 2026 已经走不通（OpenAI 加了 Turnstile dx + so 两层），借用法是唯一可行路径。

**未删除 sentinel.py 的理由**：
1. 算法层无运行时成本（默认 noop）
2. 23 个测试覆盖完整，可作未来需要时的起点
3. OpenAI 偶尔降难度（如压力大时）也许 PurePython 能短暂可用

---

## 4. 为什么暂不接入业务层

最初规划是把 borrow 直接接到 `orchestrator._phase_payment`，让支付链路自动用浏览器借来的 header。
**没做**，原因：

### 4.1 接入难度

`_phase_payment(run_id, profile_id, card_key, email, access_token)` 签名里**没有 page 参数**。
要接入 borrow，必须改 phase 编排让 page 沿调用链传下来，影响多个层。

### 4.2 收益不明

如果 OpenAI 当前对 `/payments/checkout` 不强制反爬（实测 `/me` 不强制），borrow 接入纯属
overengineering。

### 4.3 正确顺序

**先测量、再行动**：
1. 用 `diagnose_borrow.py` 跑 5-10 次真实 cURL
2. 看裸跑成功率：
   - ≥ 80% → 不接入，保留 borrow API 但不使用
   - < 80% → 启动 task #14（接入 orchestrator）
3. 接入后再用 `diagnose_borrow.py` 持续监控

这是 [Karpathy 编程纪律 §3](https://karpathy.ai/karpathy/guidelines)
"verify before scaling" 原则的具体应用。

---

## 5. OpenAI 风控变化时的应对手册

未来某天发现 payment_link 或 promo_eligibility 调用大批失败时，按以下顺序排查：

### Step 1：跑诊断脚本

```bash
# 从 AdsPower DevTools 抓一个失败请求的 cURL，粘贴到剪贴板
pbpaste | python scripts/diagnose_borrow.py --curl -
```

### Step 2：根据诊断结果分支

| 结果 | 行动 |
|------|------|
| 裸跑成功 + borrow 成功 | OpenAI 风控未变，看代理 IP / access_token 是否过期 |
| 裸跑失败 + borrow 成功 | OpenAI 加严了 → **启动 task #14**，接入到 `orchestrator._phase_payment` |
| 裸跑成功 + borrow 失败 | BORROW 白名单引入冲突 → 看 `BORROW_HEADER_NAMES` 是否需要剔除某项 |
| 两路都失败 | 看 `BrowserBorrower.is_usable()`，缺 cf_clearance 说明 AdsPower 也被风控了，需要换 IP / profile |

### Step 3：白名单调整

如果 OpenAI 新增了某个 header（比如 2027 可能加 `x-oai-attestation`），把它加到
`browser_borrow.py:BORROW_HEADER_NAMES`。增量改动，不影响现有逻辑。

### Step 4：sentinel.py 是否启用

罕见情况下（OpenAI 临时降难度），可以 `SENTINEL_STRATEGY=pure_python` 启用 PoW 路径
作 fallback。但**不建议生产长期开启**（已实测在 2026 主流场景无效）。

---

## 6. 配置速查

### 环境变量（`.env`）

```bash
# Sentinel PoW 配置（实验路径，默认 noop 不工作）
SENTINEL_STRATEGY=noop                       # noop / pure_python
SENTINEL_SDK_VERSION=20260124ceb8            # 写进 token payload 的版本号
SENTINEL_IMPERSONATE=chrome120
SENTINEL_TIMEOUT_MS=10000
```

### Python 接口（业务层调用）

```python
from src.automation.browser_borrow import BrowserBorrower
from src.payment_link import PaymentLinkGenerator

# 1. 从 page 借取（需 Playwright 已连 AdsPower CDP）
borrower = BrowserBorrower.from_page(page)
if not borrower.is_usable():
    # cf_clearance 没借到 —— 浏览器未通过 Cloudflare
    return _fallback_path()

# 2. 调旁路 API，传入 borrow snapshot
ok, link = PaymentLinkGenerator.generate_checkout_link(
    access_token=access_token,
    plan_type="team",
    borrow_headers=dict(borrower.snapshot.headers),
    borrow_cookies=dict(borrower.snapshot.cookies),
)
```

---

## 7. 测试覆盖

| 测试文件 | 用例数 | 覆盖范围 |
|---------|--------|---------|
| `tests/test_sentinel.py` | 23 | NoOp + PurePython + 工厂 + PoW 算法层 |
| `tests/test_browser_borrow.py` | 18 | 白名单过滤 + 大小写规范化 + 关键凭据检测 + payment_link/promo 集成 |
| `tests/test_diagnose_borrow.py` | 11 | cURL 解析（单行/多行/缺 auth/大小写）+ 脱敏函数 |
| **合计** | **52** | 全部静默通过，无网络依赖（使用 mock） |

跑全套：
```bash
python -m pytest tests/test_sentinel.py tests/test_browser_borrow.py tests/test_diagnose_borrow.py -v
```

---

## 8. 文件清单（速查）

```
src/automation/sentinel.py              446 行  PoW 框架（noop / pure_python）
src/automation/browser_borrow.py        261 行  借用工具 + 白名单 + Snapshot
scripts/diagnose_borrow.py              302 行  CLI 验证脚本
src/payment_link.py                     +24 行  generate_*_link 加 borrow_* 参数
src/promo_eligibility/client.py         +20 行  check_eligibility 加 borrow_* 参数（未 commit）
src/config.py                           +16 行  SENTINEL_* 4 个字段
.env.example                            +9 行   SENTINEL_* 配置示例

tests/test_sentinel.py                  275 行
tests/test_browser_borrow.py            241 行
tests/test_diagnose_borrow.py           100 行
```

**Git 历史**（`feat/sentinel-pow` 分支）：

```
b324dec  chore: 添加 diagnose_borrow.py 脚本验证裸跑 vs borrow 对比
5962e41  feat: 新增 BrowserBorrower，从 AdsPower 浏览器借 header/cookie 给旁路 API
82fb29d  feat: 引入 Sentinel PoW 框架，旁路 API 注入 openai-sentinel-token
```

---

## 9. 相关参考

- **同问题域的兄弟项目**：`any-auto-register`（多平台注册系统）
  位置：远端 `root@173.254.207.117:54231:/software/any-auto-register`
  设计沉淀：`~/.claude/plans/any-auto-register-optimized-engelbart.md`（§13 Sentinel 子系统）

- **本仓库已有的风控文档**：
  - `docs/research/risk-control-insights.md`（probe 层 vs ban 层分层模型）
  - `docs/research/chatgpt2api-vs-team-register.md`
  - `docs/research/2026-04-25-card-preheat-stripe-radar.md`

- **本次决策的方案文档**：
  - `~/.claude/plans/team-register-followup-improvements.md`（B-P0 Sentinel 章节）
  - `~/.claude/plans/any-auto-register-followup-improvements.md`（A-P3 LLM 兜底章节）
