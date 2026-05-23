# team-register 风控洞察手册

> **文档定位**：把分散在代码 + CLAUDE.md + 单点文档里的反风控认知索引化为单一入口。
> **不是**：实战教程 / 攻击 playbook / 第三方数据复述。
> **修改前请读**：本文档列出的"现有模块设计意图"，避免重复造轮子或破坏既有护栏。
>
> **方法论参考**：gpt-pp-team `docs/anti-fraud-research.md` 的"probe layer vs ban layer + 多维分散"框架（仅借鉴结构与思路，不引用其经验数据）。

最后更新：2026-04-29

---

## 1. 风控分层模型

业内公开资料 + team-register 自身排障经验，可以把支付 / 注册路径上的风控分两层：

| 层级 | 触发时机 | 颗粒度 | team-register 应对位置 |
|---|---|---|---|
| **Probe 层（请求级）** | 单次请求送达即触发 | IP / UA / 设备指纹 / 行为信号 | `src/automation/captcha_solver.py`（Turnstile）、`src/orchestration/warmup.py`（preflight） |
| **Ban 层（队列/批次级）** | 批量样本聚类后离线判定 | BIN 段、邮箱域、时间窗、地理一致性 | `src/services/bin_health_service.py`、`src/fintech/coherence.py`、`src/services/card_activation_service.py` |

> **关键判断**：probe 层失败可立即重试（换 IP / 解 captcha 即可恢复）；ban 层失败往往延迟几小时到几天才显现，需要**预防性**信号（健康度 + 一致性 + 冷却）来规避。

---

## 2. 现有控制点索引（按维度）

### 2.1 IP / 代理一致性（Probe + Ban 双层）

**位置**：`src/fintech/coherence.py` `CoherenceReport` + `check_coherence()`

**做什么**：在注册或绑卡前，校验 4 个维度的国家一致性 — 虚拟卡 BIN 所在国 ↔ 代理 IP 国 ↔ SMS 手机号国 ↔ 账单地址国。

**为什么**：Stripe Radar 对"美国卡 + 俄罗斯 IP + 法国手机号"这类组合直接拒；OpenAI 注册同样会比对邮箱/IP/支付国的一致性。

**当前阈值**：默认强校验，任意维度不一致 → fast-fail，不浪费下游资源。

### 2.2 BIN 健康度（Ban 层 / 预防）

**位置**：`src/services/bin_health_service.py`（208 行，3 个核心 API）

**做什么**：
- `record_run_bin(run_id, card_bin)`：注册时把 BIN 前 6 位写到 `Run.card_bin`
- `query_bin_health(card_bin, window_hours)`：查某 BIN 过去 N 小时的成功/失败计数
- `list_unhealthy_bins(...)`：批量列出"该冷却"的 BIN

**关键设计决策**（CLAUDE.md §H2）：
- **不做自动禁用**：失败率 ≥ 阈值时返回信号，决策权交给运维/调度器，避免代理短暂抖动导致整段 BIN 被错杀
- **BIN 仅取前 6 位**：足以聚合 issuer，不暴露完整卡号
- **DB 异常静默降级**：BIN 服务挂了不影响主流程（查询失败返回 None，主流程当作"未知健康度"继续）

**待补**（不在本计划范围）：基于 `query_bin_health` 的自动调度器，决定何时把某 BIN 切换到冷却状态。

### 2.3 卡密一次性 verify 缓存（X988 专属，Ban 层防御）

**位置**：`src/providers/card.py:X988CardProvider` + `src/services/card_activation_service.py` + `card_activations` 表

**做什么**：X988 (cards.779.chat) 的 `POST /api/exchange/verify` 接口**单卡密只能调一次**（一次性消耗），第二次 verify 返回错误。team-register 用 L1 内存 + L2 DB 双层缓存：
- L1：进程内 `Dict[cdk, X988CardCache]`
- L2：`card_activations` 表持久化跨任务复用
- 同一 cdk 在 X988 上**最多 verify 1 次**，后续命中缓存直接复用

**关键设计决策**（CLAUDE.md §三家卡商）：
- 仅 X988 需要这套缓存；EfunCard / NodeCard 的 query-first API 设计已天然防重，不需要缓存
- max_age 防御：`WARMUP_CARD_CACHE_MAX_AGE_DAYS=7`，超期视为失效
- 运维在 `/cards` 管理（admin 角色，敏感字段全脱敏）；手动作废后 X988 不可恢复

### 2.4 Captcha 自愈（Probe 层 / 实时）

**位置**：`src/automation/captcha_solver.py` (231 行框架)

**当前状态**（2026-04-29）：
- 已有 `SolverProvider` ABC + `NoOpSolver` + `ManualFallbackSolver`
- **未接入真实第三方 solver**（预留扩展点）
- 调用点：`src/automation/runtime.py` BLOCKED 状态前的 `try_solve_captcha(runtime)`

**OpenAI 用的是 Cloudflare Turnstile（不是 hCaptcha）**：
- Turnstile = 后台无感评分（PoW + 浏览器指纹 + 行为信号）
- hCaptcha = 显式让人选图（PayPal / Discord 等用，与 team-register 路径无关）
- 借鉴 gpt-pp-team 时**不要照抄**它的 4200 行 hCaptcha solver；那是 PayPal 路径的，不是 OpenAI 路径

**第三方服务对比**（来自内存 `reference_captcha_solvers.md`，正式接入前必须实测）：

| 服务 | 强项 | 弱点 |
|---|---|---|
| nocaptcha.io | Turnstile 低延迟 + 专门 universal 端点 | 覆盖面窄于 capsolver |
| yescaptcha | 覆盖广（hCaptcha / reCAPTCHA / Turnstile） | Turnstile 延迟略高 |
| capsolver | 综合度高 | VLM 介入时按 token 计费，成本难封顶 |

**护栏要求**（接入时必须）：
- `.env` 加 `CAPTCHA_SOLVER_BUDGET_CAP_USD`（每日上限）
- 默认仍 `noop`，需显式开启
- 失败回退到 `manual`（人工接管），不让 task 因 solver 故障无限期挂起

### 2.5 邮箱域 + 一次性使用（Probe 层）

**位置**：`src/services/account_pool_service.py` + `mail_accounts` 表 + email-provider 远程服务

**做什么**：
- 注册用邮箱来自池（`mail_accounts` 表），同一邮箱**只用一次注册**
- 卡预热邮箱独立池（`role=pro_warmup`），有 30 分钟冷却 + 连续 3 次失败自动禁用（CLAUDE.md §2026-04-26 修复）

**关键边界**：
- 邮件协议必须走 credentialed sessions（127.0.0.1:8000 上 `email-provider` 项目）
- 不再回退旧 `/sessions` 协议
- 排障顺序：① 确认 email-provider 是最新版 → ② `/health` + `/providers` → ③ `python scripts/manual_mail_check.py` → ④ 才看 INBOX/Junk

### 2.6 时间维度（Ban 层）

**位置**：`src/orchestration/orchestrator.py` 的 PhaseOrchestrator + `src/services/account_pool_service.py` 冷却

**做什么**：
- 卡预热 30 分钟冷却
- 连续 3 次失败自动禁用（避开"高频失败 → 整批被聚类标黑"）
- Checkpoint 断点续跑：单次 task 失败可恢复，不必从头开始（降低风控暴露面）

---

## 3. 行动清单（fallback 决策树）

按"成本由低到高"排序，遇到风控信号时按顺序尝试：

```
[Probe 层信号：Turnstile 出现]
   ↓
1. captcha_solver(noop) → 跳过
2. captcha_solver(nocaptcha) → 自动解
3. captcha_solver(manual) → 人工接管 (manual_handoff)
4. 标 manual_required，task 暂停，等运维
   ↓
[Ban 层信号：decline / 5xx / 卡批量失败]
   ↓
1. _classify_decline() 分类（handlers.py:1773）
   - insufficient_funds / do_not_honor → 走 decline_retry_service（计划中）
   - generic_decline / fraud → 不重试，走 BIN 健康度记录
2. bin_health_service.record_run_bin() 写入失败信号
3. 同卡同 BIN 24h 内 ≥3 次 decline → 标 unhealthy（运维介入）
4. coherence 检查（如尚未做） → 4 维国家不一致 → fast-fail
```

---

## 4. 修改风控相关代码的强制 checklist

任何 PR 触碰以下任一文件时，必须勾选：

- [ ] 没有引入硬编码 token / 卡号 / 邮箱（CI secret-scan 会卡）
- [ ] 没有破坏 X988 一次性 verify 的缓存（L1+L2 必须同时更新或同时回滚）
- [ ] 没有让 BIN 服务"自动禁用"（决策权必须留给运维）
- [ ] 没有跳过 coherence 校验（fast-fail 是设计，不是 bug）
- [ ] captcha solver 改动有预算护栏 + 默认 noop 开关
- [ ] 邮箱池冷却 / 失败禁用 阈值改动有 commit message 解释 + 单测覆盖

涉及文件（任一变更触发 checklist）：
- `src/automation/captcha_solver.py`
- `src/automation/runtime.py`
- `src/fintech/coherence.py` / `src/fintech/bin_lookup.py`
- `src/services/bin_health_service.py`
- `src/services/card_activation_service.py`
- `src/services/account_pool_service.py`
- `src/providers/card.py`（X988 缓存）
- `src/orchestration/handlers.py`（_classify_decline 等）

---

## 5. 不在本文档范围

- gpt-pp-team 的 IP 实证数据（每 IP N 次注册的具体阈值） — 那是它在 PayPal 路径下的实测，team-register 是 Stripe 路径，数字不可直接套用
- 攻击者视角的"如何绕过 Stripe Radar" — 本文档只讨论我方风控规避策略
- 实时风控信号采集 SDK（如 fingerprint.js） — 当前未集成；如需集成，独立立项
- 反人工接管自动化（manual_handoff 自动恢复）— 需求出现再做

---

## 6. 关联文档

- 项目结构与设计决策：`/CLAUDE.md`
- 卡预热流程：`docs/research/2026-04-25-card-preheat-stripe-radar.md`
- 账号预热信任度：`docs/research/2026-04-25-account-warmup-openai-trust.md`
- 项目对比（含 chatgpt2api 路径分析）：`docs/research/chatgpt2api-vs-team-register.md`
- **2026-05 月度风控演进**：`docs/research/openai-risk-control-timeline-2026-05.md`（消化外部 chatgpt2api-docs 2026-05-21 实测报告 + 本仓库 2026-05-20 borrow 实测的交叉验证；注册主链路盲区 `/verify-your-identity` + `/add-phone` 待补；接入 borrow 前必读 §6 checklist）
- **借用 vs PoW 决策记录**：`docs/research/anti-bot-borrowing-vs-pow.md`（sentinel / borrow / diagnose 三模块分工，2026-05-20）
- Provider 配置流：`docs/architecture/provider-config-flow.md`
- 借鉴来源声明：`/NOTICE`
