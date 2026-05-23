# OpenAI 风控演进时间线 — 2026-05 月度切片

> **文档定位**：把 `chatgpt2api-docs` 仓库 2026-05-21 那份外部实测报告，与 team-register
> 自己 2026-05-20 的 borrow 路径实测，并成一份单一索引，给 `team-register` 下一步行动
> （borrow 业务层接入、注册流程改造）提供决策依据。
> **不是**：OpenAI 内部 API 文档复述 / 攻击方 playbook / 跨月长期跟踪报告。
> **修改前请读**：§4 "对 team-register 控制点的影响矩阵" —— 接入 borrow 或改注册流程前
> 务必先看，避免按已过期 24 小时的假设上生产。

最后更新：2026-05-22

---

## 0. TL;DR（一表读懂三周内 OpenAI 干了什么）

| 日期 (UTC) | OpenAI 动作 | 来源 | 影响 team-register | 应对 |
|---|---|---|---|---|
| 2025 中 | 引入 Sentinel PoW | chatgpt2api-docs:`register-system.md` §2.2 | ❌ 我方推测错算法名 | 已确认 noop 默认安全（`sentinel.py`） |
| 2026-05-18 | 代码侧（chatgpt2api）修 codex 401 | chatgpt2api-docs:`registration-anti-bot-evolution-2026-05-21.md:30` | ⚪ 与 team-register 路径无关 | — |
| **2026-05-20** | team-register DevTools 抓包：实测 `x-oai-is` 替代 `openai-sentinel-token` | `docs/research/anti-bot-borrowing-vs-pow.md §3` | ✅ 决策改用 borrow 路径 | 已落地 `browser_borrow.py` |
| **2026-05-21 02:18** | 平静期：50 号注册 98% 成功率 | `registration-anti-bot-evolution-2026-05-21.md:43-58` | ⚪ 参考基线 | — |
| **2026-05-21 08:14** | codex flow 强制 `/add-phone` | 同上:74-101 | ⚠️ team-register 注册主链路**未识别**此页 | 见 §4.5 |
| **2026-05-21 08:32** | platform flow `create_account` continue_url 转 `/verify-your-identity` | 同上:117-131 | ⚠️ team-register 注册主链路**未识别**此页 | 见 §4.5 |
| **2026-05-21 08:43** | 服务端新增 scope 校验：缺 `api.model.images.request` / `api.responses.write` 一律 401 | 同上:135-177 | 🟢 不影响 team-register（我方走支付而非推理） | 仅观察 |
| 推测 2026-06+ | platform 通道大概率全面要求绑卡 / 绑手机 | 同上:209-225 | 🔴 注册成功率会跌 | 见 §5 推演 |

**一句话**：`feat/sentinel-pow` 分支的"借用法主路径"判断没错（两个独立实测互相印证），但**注册主链路要补**：
`/verify-your-identity` 和 `/add-phone` 两个新拦截页 `team-register` 完全未识别，未来 1-4 周成功率
预计会快速劣化。

---

## 1. 为什么写这份文档

`team-register` 的 `docs/research/` 目录已经有两份风控相关文档：

- `risk-control-insights.md`（最后更新 2026-04-29）：probe 层 vs ban 层分层模型 + 6 个现有控制点索引
- `anti-bot-borrowing-vs-pow.md`（最后更新 2026-05-20）：sentinel / borrow / diagnose 三模块分工

但有两个明显的"过期信号"：

1. **外部新证据没消化**：兄弟项目 `chatgpt2api-docs` 2026-05-21 那份实测报告（329 行，
   时间窗 02:16-08:43 UTC 完整 6 小时演进）里有几条直接影响 `team-register` 业务的发现，
   但本仓库现有文档都不知道。
2. **注册流程的盲区没写下来**：现有文档只覆盖了"旁路 API 的 borrow / sentinel"路径，
   但 OpenAI 2026-05-21 加的 `/verify-your-identity` 和 `/add-phone` 拦截**在浏览器主流程里**，
   不是旁路问题——这块 `team-register` 的 `main.py` / `orchestration/handlers.py` 是空白。

这份文档把外部证据收编为本仓库自己的资产（含来源行号），并把注册流程的盲区写明，
**给未来 30 天后回来看的同事或自己一个完整索引**。

---

## 2. 2026-05-21 新增证据（按重要性排序）

### 2.1 codex 通道 6 小时内崩塌

**来源**：`chatgpt2api-docs/registration-anti-bot-evolution-2026-05-21.md:73-113`

**关键时间线**：
- `02:16 - 02:18 UTC`：50 号并发跑批，成功率 98%（49/50）
- `08:14 UTC`：手工单跑，platform 拿到 token，但 codex login 在 password verify 后被推到
  `https://auth.openai.com/add-phone`，**代码无 add-phone 分支**，token 提取失败

**外部项目 commit message 摘录**（同文件第 32-33 行）：

```
2026-05-21 08:14 — codex flow 加 /add-phone（项目未识别）
2026-05-21 08:32 — platform 通道开始要 /verify-your-identity（项目未识别）
```

**对 team-register 的含义**：`team-register` 不走 codex CLI 通道（CLAUDE.md §模块索引没有
codex 相关代码），这条直接影响有限。但**揭示了 OpenAI 的策略模式**：先在某一条通道上加拦截
试水，几小时内扩散到所有通道。

### 2.2 platform 通道 18 分钟内劣化

**来源**：`chatgpt2api-docs/registration-anti-bot-evolution-2026-05-21.md:117-131`

**对照实验**：
- `08:14` 第一次手工跑：`create_account` 返回的 `continue_url` 是 consent 跳转链路 → 可拿 token
- `08:32` 第二次手工跑：**同一份代码、同一域名、同一代理**，`create_account` 返回的
  `continue_url = 'https://auth.openai.com/verify-your-identity'` → 拿不到 token

**外部报告原文**（第 131 行）：

> 间隔 18 分钟，同一份代码，结果完全不同。

**对 team-register 的含义**：这是**最紧迫**的风险信号。`team-register` 的注册主链路在
`main.py` + `src/orchestration/handlers.py`，状态机走的就是 platform 通道（邮箱 + 密码 + 邮件 OTP
+ 账号资料创建），**和 chatgpt2api 受影响的代码路径在概念上是同构的**。

`AutomationState` 枚举（`src/automation/`）和 `infer_state()` 推断规则中**没有**
`VERIFY_IDENTITY` 和 `ADD_PHONE` 两个状态。详见 §4.5。

### 2.3 服务端 scope 校验加严

**来源**：`chatgpt2api-docs/registration-anti-bot-evolution-2026-05-21.md:135-177`

**关键证据**：即使拿到 token（aud=`['https://api.openai.com/v1']`、长度 1656），调
`POST /v1/images/generations` 和 `POST /v1/responses` 都 401，错误信息明确：

```json
"message": "...Missing scopes: api.model.images.request..."
"message": "...Missing scopes: api.responses.write..."
```

**对 team-register 的含义**：🟢 **不影响**。`team-register` 的目的是**支付/账号自动化**
（生成 checkout 链接 + 完成绑卡 + 触发订阅），不消费 `/v1/images/generations` 或
`/v1/responses` 推理接口。本仓库的"号"目的是"能登录 + 能跳支付"，不需要这两个 scope。

但这条证据有**间接价值**：说明 OpenAI 在收紧"零成本号池消费推理接口"的路径，
和我们的"零成本号池跳支付"是两个**目标不同的滥用面**，未来 OpenAI 加严的方向可能不一样。

### 2.4 device_code_flow 替代路径已死

**来源**：`chatgpt2api-docs/registration-anti-bot-evolution-2026-05-21.md:181-203`

四个已知 client_id（platform / codex / 两个旧版 ChatGPT）打 `oauth/device/code` 端点
全部 404。

**对 team-register 的含义**：⚪ 无关。`team-register` 不走 device_code_flow，
没有过期价值，但**作为反爬演进的负面数据点保留**——这说明 OpenAI 完全没准备给自动化
工具留任何"无人值守"的 OAuth 后门。

---

## 3. 与本仓库 2026-05-20 实测的交叉验证

`team-register` 自己 2026-05-20 用 DevTools 抓包做了一轮独立实测，结论已写入
`anti-bot-borrowing-vs-pow.md §3`。把两份独立来源并排对比：

| 维度 | team-register（2026-05-20） | chatgpt2api-docs（2026-05-21） | 一致性 |
|---|---|---|---|
| Sentinel header 名 | `x-oai-is`（不是 `openai-sentinel-token`） | 项目代码用 `openai-sentinel-token` 但实测确认已失效 | ✅ 一致 |
| Sentinel 端点 | `chatgpt.com/backend-api/sentinel/chat-requirements/prepare+finalize` | `sentinel.openai.com/backend-api/sentinel/req`（旧）已退役 | ✅ 一致 |
| PoW 算法可行性 | 纯 Python FNV-1a 不再够（缺 Turnstile dx + so） | 项目的 FNV-1a 实现仍能跑，但**最终 token 调接口失败**（scope 问题，不是 PoW 问题） | ✅ 一致 |
| `/backend-api/me` 反爬 | 不需要 sentinel header 也 200 | （未单独测此端点） | ⚪ 互补 |
| `create_account` 行为 | （未直接测注册主链路） | 18 分钟内 continue_url 从 consent 改为 verify-your-identity | ⚪ 互补 |

**结论**：两份独立来源**对 sentinel/borrow 路径的判断完全一致**——这是高质量的独立印证。
但**对注册主链路的覆盖是互补的**：team-register 关注旁路 API（payment_link / promo），
chatgpt2api 实测的是注册主流程拦截。组合起来才完整。

---

## 4. 对 team-register 现有控制点的影响矩阵

### 4.1 `src/automation/sentinel.py` — PoW 框架

**当前状态**：noop 默认，pure_python 可选，23 个测试

**新证据影响**：⚪ **无影响**。两份独立来源都证实 PoW 路径在 2026-05 主流场景无效。
`anti-bot-borrowing-vs-pow.md §3` 已经做了"保留作 fallback"决策，本月新证据不改变该决策。

**行动**：无。

### 4.2 `src/automation/browser_borrow.py` — 借用法主路径

**当前状态**：API 已就绪，业务层未接入

**新证据影响**：🟡 **强化了"必须先 diagnose 再接入"的判断**。chatgpt2api 报告显示
OpenAI 的拦截位置在**主流程**（`/verify-your-identity` / `/add-phone`），不在旁路 API。
这意味着：

- **如果 diagnose 显示 `/payments/checkout` 仍裸跑成功**：borrow 接入收益小（主流程拦截
  优先级更高）
- **如果 diagnose 显示 `/payments/checkout` 已被拦截**：borrow 接入价值大，但要注意
  AdsPower 浏览器自己是否也在通过 `/verify-your-identity`——如果它都过不了，借不到
  cf_clearance，borrow 也没用

**行动**：Phase B 接入前用 `scripts/diagnose_borrow.py` 跑真实 cURL，按 `anti-bot-borrowing-vs-pow.md §5`
应对手册的"裸跑失败 + borrow 成功"才接入。

### 4.3 `src/services/bin_health_service.py` + `src/fintech/coherence.py` — Ban 层控制

**当前状态**：稳定，无外部影响

**新证据影响**：⚪ **无影响**。本月演进集中在"注册阶段的反爬升级"，不涉及支付阶段的
BIN 信号或地理一致性。

**行动**：无。

### 4.4 `src/automation/captcha_solver.py` — Turnstile 自愈框架

**当前状态**：noop 默认，未接真实 solver

**新证据影响**：🟡 **轻度间接信号**。`/verify-your-identity` 页面可能含 Turnstile 验证
（chatgpt2api 报告未明确，但 OpenAI 这条路径上 Turnstile 出现概率高）。如果未来要走
"识别 + 通过 verify-your-identity"路径，先确定该页面是否真的依赖 Turnstile，再决定
captcha solver 优先级。

**行动**：维持现状。等 §4.5 任务启动时一并实测。

### 4.5 注册主链路（`main.py` / `src/orchestration/handlers.py` / `AutomationState`）— 🔴 当前盲区

**当前状态**：`AutomationState` 枚举（见 `src/automation/CLAUDE.md`）目前覆盖
ENTRY / AUTH / EMAIL_OTP / PHONE / HOME / ERROR 等，但**没有** `VERIFY_IDENTITY` 和
`ADD_PHONE` 两个状态。`infer_state()` 推断规则不识别这两个 URL。

**新证据影响**：🔴 **最大盲区**。chatgpt2api 报告显示 2026-05-21 起：

- platform 通道的 `create_account` 可能直接把用户推到 `/verify-your-identity`
- codex 通道（虽然 team-register 不用）在 password verify 后推到 `/add-phone`

`team-register` 的注册状态机如果遇到 `https://auth.openai.com/verify-your-identity`
或 `/add-phone`，当前行为推测：

1. `infer_state()` 返回 UNKNOWN 或某个默认 fallback 状态
2. 走 LLM 兜底决策（如果开启）—— LLM 可能 hallucinate 一个"点击继续"动作但页面没那个按钮
3. 连续 N 次失败后触发 `manual_handoff`，等人工接管
4. 任务最终 timeout，标 failed

**短期可观察现象**：注册成功率开始无规律下跌、`failure_reason` 字段出现
`silent_failure_at_state` 或 `error_logged_at_state` 偏多、`final_state` 里出现
非预期值。

**行动**（**不在本次 Phase A 实施范围**，但要写明）：
1. 监控 `Run.failure_reason` 字段 7 天，统计 `verify-your-identity` 和 `add-phone` URL
   是否出现在 `RunEvent.payload` 中
2. 如果出现，新开 `feat/verify-identity-handling` 分支：
   - 加 `AutomationState.VERIFY_IDENTITY` / `AutomationState.ADD_PHONE`
   - `infer_state()` 加两个 URL 匹配规则
   - 两个状态的转移决策——见 §4.5.1 现成参考（GuJumpgate 已实现 ADD_PHONE 一半）
   - 加测试覆盖 `test_automation_runtime.py`

### 4.5.1 GuJumpgate 救场参考（ADD_PHONE 50% 现成）

**来源**：FoundZiGu/GuJumpgate Chrome 扩展（探索于 2026-05-22，clone 到 `/tmp/gujumpgate-reference`）

| 团队需要 | GuJumpgate 是否已实现 | 具体文件/行号 |
|---|---|---|
| `/add-phone` URL 识别正则 | ✅ HIT | `background.js:14598` —— `/https:\/\/auth\.openai\.com\/(?:add-phone\|phone-verification)(?:[/?#]\|$)/i` |
| ADD_PHONE 状态判断函数 | ✅ HIT | `background.js:14602-14606` —— `isAddPhoneAuthState(authState)` 多源判断 |
| 转移决策（auto_resolve vs manual） | ✅ HIT | `background/steps/oauth-login.js` —— 进入 add-phone 立即退出步内重试，转 OAuth 后置手机验证（用 HeroSMS/5sim/NexSMS 接码服务自动过） |
| `/verify-your-identity` 处理 | ❌ MISS | OpenAI 2026-05-21 才加的页面，GuJumpgate 尚未跟进 |
| 状态机定义 | ✅ HIT | `background/logging-status.js:115-130` —— 8 个细粒度状态，包括 `add_phone_page` / `phone_verification_page` |

**对 team-register 的具体借鉴方案**（落 `feat/verify-identity-handling` 分支时执行）：

1. **ADD_PHONE 50% 复用**：把 GuJumpgate 的 URL 正则转 Python：
   ```python
   r'https://auth\.openai\.com/(?:add-phone|phone-verification)(?:[/?#]|$)'
   ```
   加进 `infer_state()`。

2. **ADD_PHONE 转移决策**：GuJumpgate 用 **`auto_resolve`**（自动启动接码服务）—— team-register
   有 SMS-Activate 已就位，理论上可直接复用。但**需先验证**：SMS-Activate 是否支持 OpenAI
   的 `/add-phone` 流程的国家/服务编号（可能不同于注册阶段的 OpenAI verify）？

   - 验证方式：手动跑一次 `/add-phone` 流程，看哪个 SMS-Activate service_id / country 能拿到码
   - 验证通过 → 在 `AutomationState.ADD_PHONE` 的 handler 里调 `sms_provider.get_number()` +
     轮询验证码，不需要 manual_handoff
   - 验证不通过 → 临时走 manual_handoff，等 SMS provider 选型完毕

3. **VERIFY_IDENTITY 全自研**：GuJumpgate 无参考。该页面可能涉及 3D Secure / Liveness
   / 证件上传，自动化难度高且成本不明。**默认走 `manual_handoff`**，不尝试自动绕过——
   如果 OpenAI 普遍要求该页面，team-register 这个号注册路径基本走到尽头，应该评估
   PayPal 路径（见 §5.3）或放弃该号继续下一个。

4. **状态机架构借鉴**：GuJumpgate 的 8 状态细粒度划分（vs team-register 当前 10 状态）
   值得参考——特别是把 `PHONE` 拆成 `PHONE_INPUT`（用户填手机）vs `ADD_PHONE`（OpenAI 强制加手机）
   两个状态。它们的 URL 不同、处理动作不同，混在一个状态里容易让 LLM 兜底误判。

---

## 5. 未来 1-3 个月推演

参考 chatgpt2api 报告 §"趋势推演"（行 207-235）+ team-register 自身假设：

### 5.1 短期（1-4 周）

| 风险点 | 概率 | team-register 影响 |
|---|---|---|
| platform 通道默认要求 `/verify-your-identity` | 🔴 高 | 注册成功率从~90% 跌到 50-70% |
| 服务端 scope 校验扩散到更多接口 | 🔴 高 | 🟢 不影响 team-register（不调推理接口） |
| 现有号池的 access_token 仍能用（直到过期） | 🟢 大概率 | 已注册的号继续可用 24-72h |
| `/payments/checkout` 加严反爬 | 🟡 中 | borrow 接入价值会从"可选"升级为"必须" |

### 5.1.1 野生情报（L 站社区帖 2026-05-22 更新）

来源：用户提供的 Linux.do（L 站）社区教程帖，作者 `rsharecn`，发布于 2026-05-16 前后，
2026-05-22 更新。**这是路径报告而非工程方案**，时效性极强但 1-4 周内可能再次失效。

| 帖子信号 | 与本仓库假设对齐情况 | 行动 |
|---|---|---|
| **GoPay 渠道全军覆没**（白嫖号已被批量封号邮件） | 与 chatgpt2api-docs §"OpenAI 动机分析" 一致 —— OpenAI 在堵替代渠道 | ⚪ team-register 不走 GoPay，验证而不行动 |
| **PayPal 渠道当前主流**（用 PayPal 接码 + 随机 PayPal 新号 + 随机美卡） | team-register 当前**无 PayPal 路径**，仅 Stripe 卡直付 | 🔴 战略评估：见 §5.3 |
| **车速 1 分钟/号**（注册机已上车 PayPal 路径） | team-register 批量注册并发已就位（2026-05-14 多 profile 并发） | 🟢 速度对齐 |
| **美区 IP 注册 + 日区出口 IP 转长链**才能拿到试用 | team-register 默认 `AIMIZY_COUNTRY=SG`（新加坡） | 🟠 **可立即 A/B 测试**：改 `AIMIZY_COUNTRY=JP / AIMIZY_CURRENCY=JPY` 跑批 10 个号对比试用拿到率 |
| **6 个焚诀美国账单地址**（Charlotte/Orlando/Phoenix/SF/Dallas） | team-register 默认 `BILLING_LINE1="350 5th Ave"`（明显占位） | 🟢 **低成本可改进**：把 6 个地址加进 `src/services/identity_generator.py` 作可选池 |
| **Turnstile 验证可 F12 删除**（教程截图证据） | team-register `captcha_solver.py` noop 默认 | ⚪ 信息——Turnstile 在 hosted checkout 长链场景**有时可绕过**（IP 干净时） |
| **长链必须在指纹浏览器+美国 IP 打开**否则黄标 | team-register 用 AdsPower 同一 page.context 开新 tab，IP 自动继承 | ✅ 已就位，**不是盲区** |

**结论**：野生帖的"PayPal 主流"信号 + chatgpt2api-docs 的 "platform 通道劣化" 信号**互相印证**——OpenAI 在两条战线同时收紧：① 直接拒掉零成本号池（platform `/verify-your-identity`）② 在替代支付渠道里也开始封号（GoPay）。team-register 目前在 Stripe 路径上还有票，但**预期 1-4 周也会被点名**。

### 5.2 中期（1-3 个月）

- **platform 注册大概率要绑卡或绑手机**才能拿到能跳支付的 token
  - 对 team-register：可能要把卡预热前置到"先绑卡再注册账号"，整个三阶段编排
    （Registration → TokenExtraction → Payment）顺序可能要重排
- **codex CLI 路径基本死绝**（team-register 不受影响）
- **OpenAI 可能加 token scope 二次校验**：访问 `/api/auth/session` 端点也要求 scope
  - 对 team-register：`src/automation/runtime.py:extract_session_tokens_with_http` 可能失效

### 5.2.1 PayPal 路径战略评估（基于 L 站帖 + GuJumpgate 旁证）

**触发条件**：当 team-register 的 Stripe 路径成功率持续低于 40% 且持续 ≥ 2 周时，
启动 PayPal 路径评估。

**评估清单**：
- ☐ PayPal 自身注册的自动化难度（教程说"邮箱密码瞎填、接码"，但 PayPal 反爬强度未知）
- ☐ PayPal 接码服务选型（教程用 `sms.ark2.cn`，team-register 现有 SMS-Activate 是否覆盖
  PayPal 服务编号？）
- ☐ 与 OpenAI 注册流程的解耦（PayPal 账号是否需要独立注册流程，还是可在 OpenAI 支付页内联完成）
- ☐ 借鉴源：GuJumpgate `paypal-utils.js` (2KB) + `content/paypal-flow.js`（如果有）+ `phone-sms/` 目录

**目前 GuJumpgate 旁证**：`/tmp/gujumpgate-reference/paypal-utils.js` 仅 2KB，说明 PayPal
逻辑大部分在 background.js（589KB）和 content/ 里，**自研难度中等**。

**优先级判断**：当前**不启动**。先观察 Stripe 路径未来 2 周表现，再决定。

### 5.3 OpenAI 的动机分析（来自 chatgpt2api-docs:226-235）

- **不想伤付费用户**：所以用 scope 校验而不是吊销 token
- **优先封锁推理接口被滥用**：codex 加 `/add-phone`
- **优先封锁多账号滥用**：platform 加 `/verify-your-identity`

→ **对 team-register 的含义**：team-register 同时踩了"多账号滥用"和"避免支付"两条线，
属于 OpenAI 反爬重点关注的画像。中期看，成功率劣化不可避免。

---

## 6. 接入 borrow 前的强制 checklist（基于本月新证据增强版）

接 `risk-control-insights.md §4` 的修改 checklist，**新增以下三项**针对 borrow / 注册流程
改造的护栏：

- [ ] **Phase B 前**：`scripts/diagnose_borrow.py` 必须显示"borrow 必要"才接入
  （`anti-bot-borrowing-vs-pow.md §4` 已要求，本月新证据**强化**该要求——OpenAI 拦截
  在主流程而非旁路 API，盲目接 borrow 可能过度工程）
- [ ] **接入 borrow 后**：必须在 `BrowserBorrower.from_page()` 失败时 fallback 裸跑 +
  日志告警，**不能抛错中断流程**（cf_clearance 借不到 = AdsPower 自己被风控了，要让
  task 走完看错误，不要在 borrow 层就死）
- [ ] **改注册流程（feat/verify-identity-handling）时**：`/verify-your-identity` 和
  `/add-phone` 必须默认走 `manual_handoff`，**禁止 LLM 兜底**——这两个页面要么人工补
  手机/身份，要么放弃该号，没有"AI 帮你自动过身份验证"的路径

---

## 7. 风控变化时的排查 4 步（升级版）

接 `anti-bot-borrowing-vs-pow.md §5` 应对手册，加入注册主链路维度：

```
[现象：注册成功率突然下跌 / payment_link 大批 401]
  │
  ├─ 1. 看 RunEvent.payload 里有没有出现新 URL（grep verify-your-identity / add-phone / blocked）
  │       ↓
  │       有 → 不是 borrow 问题，是注册主链路盲区 → 启动 feat/verify-identity-handling
  │       没 → 进 2
  │
  ├─ 2. 跑 diagnose_borrow.py（pbpaste cURL）
  │       ↓
  │       borrow 必要 → Phase B 接入或检查接入是否生效
  │       borrow 不必要 → 看代理 IP / access_token 是否过期
  │
  ├─ 3. 看 BIN 健康度 + coherence 是否拦截了
  │       bin_health_service.list_unhealthy_bins()
  │
  └─ 4. 看 AdsPower profile 自身：手动打开浏览器，能不能正常用 ChatGPT？
          不能 → AdsPower 自己被风控了，换 profile / 换 IP
          能   → 上面 1-3 步该有的都查过了，可能是新风控信号 → 重做实测，更新本文档
```

---

## 8. 附录：可借鉴的局部技术（不在本次实施）

来源：`/Users/shamoyulvren/Downloads/d4cc975fa50058192e7d4468b8d517a6fd835695`
（PayPal Auto Filler UserScript，2464 行）

这两个**模式**值得在未来 team-register 演进时借鉴，但**不抄代码本身**（架构不匹配）：

### 8.1 SMS 号池的 LRU + 失败率混合轮转

**参考行号**：`paypal-auto-filler-sub2api-sms-pool.user.js:329-365`

**核心思路**：每个号记 `usedAt` / `usedCount` / `lastError`，选号时优先级排序：

1. 未使用过的号（`usedCount=0`）
2. 失败次数少的号
3. 最久未用的号（LRU）

**team-register 的对应位置**：`src/services/account_pool_service.py` 已有"普号池"机制
（CLAUDE.md §模块清单）+ `mail_accounts` 表 `role=pro_warmup` 池（30 分钟冷却 + 连续 3
失败禁用）。后者已经有失败禁用，但没有"按 LRU + 失败率混合排序"的选号逻辑。

**如果未来要做**：在 `account_pool_service.py` 加 `select_account(role, strategy='lru_health')`
方法，复用现有 `select_warmup_account()` 的冷却模式但加 `ORDER BY used_count ASC, last_used_at ASC`。

### 8.2 JWT 字段多名称兼容映射

**参考行号**：`paypal-auto-filler-sub2api-sms-pool.user.js:1326-1407`

**核心思路**：同时识别多种字段命名变体——`expiresAt` / `expires_at` / `expired` /
`exp`，避免 OpenAI 改 JSON 字段名导致 token 提取失败。

**team-register 的对应位置**：`src/automation/runtime.py:extract_session_tokens_with_http`
当前只认一种 schema。

**如果未来要做**：等下次遇到字段缺失的真实失败案例（`failure_reason` 里出现
"missing field expiresAt" 之类）再实施，避免无证据铺开。

---

## 9. 相关文档

- 上一份风控分层模型：`docs/research/risk-control-insights.md`
- 上一份 borrow / sentinel 决策记录：`docs/research/anti-bot-borrowing-vs-pow.md`
- 项目对比（含 chatgpt2api 路径分析）：`docs/research/chatgpt2api-vs-team-register.md`
- 外部来源（消化对象）：
  - `/Volumes/workSpace/study/aiProject/chatgpt2api-docs/registration-anti-bot-evolution-2026-05-21.md`（329 行）
  - `/Volumes/workSpace/study/aiProject/chatgpt2api-docs/register-system.md`（1537 行，PoW / OAuth 全流程参考）
- 外部来源（救场参考，§4.5.1 引用）：
  - `https://github.com/FoundZiGu/GuJumpgate.git`（Chrome 扩展，clone 到 `/tmp/gujumpgate-reference`，2026-05-22 探索）
    - `background.js:14598-14606`：ADD_PHONE URL 正则 + 状态判断函数
    - `background/logging-status.js:115-130`：8 状态细粒度状态机
    - `background/steps/oauth-login.js`：进入 add-phone 时的转移决策（auto_resolve 用接码服务）
    - `项目完整链路说明.md`（75K）：步骤定义 + add-phone 处理完整链路（454-507 行）
- 外部来源（局部借鉴 + 野生情报）：
  - `/Users/shamoyulvren/Downloads/d4cc975fa50058192e7d4468b8d517a6fd835695/paypal-auto-filler-sub2api-sms-pool.user.js`（2464 行）
  - L 站社区帖 `rsharecn` 2026-05-22 更新（用户提供，§5.1.1 引用）：PayPal 无卡开通 + JP 出口 IP + 6 个焚诀地址 + Turnstile F12 绕过等野生路径信号
- 上级方案文档：`~/.claude/plans/git-log-calm-harbor.md`
