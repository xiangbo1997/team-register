# Provider 配置流：3 层架构与决策图

> 本文档基于 3 个 agent 并行调研的代码 trace 综合而成（数据流 / UI 字段对照 / 概念决策）。  
> 目的：**让你 1 分钟搞清"为什么同一个东西在 4 个页面都能改"，2 分钟知道某个改动该编辑哪里**。

---

## 30 秒速读

team-register 把 provider（browser / card / mail）配置分成 **3 层 + 2 个旁路池**：

```
┌─────────────────────────────────────────────────────────┐
│  L1 全局 AppConfig                                       │
│  • 来源: .env 文件 + AppSetting 表（DB 持久覆盖）       │
│  • 范围: 所有任务的基础默认值                           │
│  • UI:   /config 各 tab                                 │
├─────────────────────────────────────────────────────────┤
│  L2 Provider Profile（命名档案）                         │
│  • 来源: provider_configs 表（browser/card/mail 三类）  │
│  • 范围: 命名档案；同一类型可多个                       │
│  • UI:   /providers + /config 部分字段                   │
├─────────────────────────────────────────────────────────┤
│  L3 Run（任务级覆盖）                                    │
│  • 来源: runs 表的 {browser,card,mail}_provider 列      │
│  • 范围: 单个任务，选哪个 L2 档案                       │
│  • UI:   /tasks/create 三个下拉                          │
└─────────────────────────────────────────────────────────┘

┌─ 旁路池（统一在 mail_accounts 表）──────────────────────┐
│  mail_accounts(role=regular)  → credentialed 邮箱凭据池  │
│   • 任务级绑定: Run.mail_account_id                      │
│  mail_accounts(role=pro_warmup) → 卡预热垫脚石账号池    │
│   • 字段: email + extra.password + extra.adspower_*      │
│   • 调度: ConfigService.select_warmup_account()          │
│       cooldown_until / consecutive_failures              │
│  全部在 /mail-accounts UI 管理，role 下拉切换            │
└─────────────────────────────────────────────────────────┘
```

**核心运行时函数**：`src/api/worker.py:_resolve_runtime_config(run)` (line 404-508) —— 把 L1 → L2 → L3 折叠成单次任务用的 AppConfig 快照。

---

## 第 1 部分：3 层职责对照表

| 层 | 存储位置 | 改动生效时机 | 范围 | 改完是否要重启 | 有版本回滚？ |
|----|---------|-------------|------|--------------|-------------|
| **L1a `.env`** | 文件 | 进程启动时 load | 全局默认 | ✅ 必须重启 | ❌ |
| **L1b `AppSetting` 表** | DB | 立即（下次任务取） | 全局动态覆盖 | ❌ | ❌ |
| **L2 `ProviderConfig`** | DB | 立即 | 命名档案，类型隔离 | ❌ | ✅ `provider_config_revisions` |
| **L3 `Run.*_provider`** | DB | 仅本任务 | 单任务覆盖 | ❌ | ❌（任务即审计单元） |
| **池 `mail_accounts(regular)`** | DB | 立即 | credentialed 邮箱凭据 | ❌ | ❌ |
| **池 `mail_accounts(pro_warmup)`** | DB | 立即 | 卡预热垫脚石（号池调度，自带冷却 + 失败剔除） | ❌ | ❌ |

---

## 第 2 部分：字段权威表（最重要）

**核心规则**：字段值由 worker resolver 按下面顺序覆盖，**靠下的赢**。  
`AppConfig 默认 → AppSetting → ProviderConfig.config → Run → MailAccount`

### Card 相关字段

| 字段 | L1 .env | L1b AppSetting | L2 provider_configs.config | L3 Run | 谁赢 |
|------|---------|---------------|---------------------------|--------|------|
| `card_provider` (driver) | `CARD_PROVIDER` | `card_provider` | `driver` | `Run.card_provider` 选档案 | L2（worker line 434）|
| `efuncard_token` | `EFUNCARD_TOKEN` | `efuncard_token` | `efuncard_token` | — | L2 if key 存在，否则 L1（worker line 435-436）|
| `nodecard_*` | `NODECARD_*` | `nodecard_*` | `nodecard_*` | — | 同上 |
| `x988card_api_base` | `X988CARD_API_BASE` | `x988card_api_base` | `x988card_api_base` | — | 同上 |
| `card_key`（任务一次性卡密）| — | — | — | `Run.card_key`（必填）| L3（创建任务时填）|

### Mail 相关字段

| 字段 | L1 .env | L1b AppSetting | L2 provider_configs.config | L3 Run / MailAccount | 谁赢 |
|------|---------|---------------|---------------------------|---------------------|------|
| `email_provider_name` | `EMAIL_PROVIDER_NAME` | `email_provider_name` | `provider_name` | — | L2 if key 存在 |
| `email_provider_base_url` | `EMAIL_PROVIDER_BASE_URL` | 同名 | — | — | L1b > L1a |
| `email_provider_api_key` | `EMAIL_PROVIDER_API_KEY` | 同名 | — | — | L1b > L1a |
| `mail_session_mode_override` | — | — | `session_mode` | — | L2 |
| `mail_config_name`（远程引用）| — | — | `config_name` | — | L2 |
| `mail_client_id` / `mail_refresh_token` | `MAIL_CLIENT_ID` / `MAIL_REFRESH_TOKEN` | 同名 | — | `MailAccount.{client_id, refresh_token}` | L3 if `Run.mail_account_id` 非空 |
| `known_mail_accounts_json` | `KNOWN_MAIL_ACCOUNTS_JSON` | 同名 | — | 由 MailAccount 自动构造 | L3（自动 override）|

### Browser 相关字段

| 字段 | L1 .env | L1b AppSetting | L2 provider_configs.config | L3 Run | 谁赢 |
|------|---------|---------------|---------------------------|--------|------|
| `ads_api` | `ADS_API` | `ads_api` | `ads_api` / `api_url` | — | L2 if key 存在 |
| `ads_api_key` | `ADS_API_KEY` | `ads_api_key` | `ads_api_key` | — | 同上 |
| `proxy` | `PROXY` | `proxy` | `proxy`（browser/mail 二者中任一）| — | L2 if key 存在 |
| `profile_id`（任务一次性 ）| — | — | — | `Run.profile_id`（必填）| L3 |

---

## 第 3 部分：「我想改 X 该编辑哪里」决策表

| 你想做的事 | 推荐编辑位置 | DB 表/字段 | 立即生效？ | 版本回滚？ |
|-----------|-------------|-----------|----------|-----------|
| 全局换卡商（EfunCard → X988Card）| `/config` → 卡片 tab → `card_provider` | `app_settings.card_provider` | ✅ | ❌ |
| 给同一类卡商保留多个独立 token（卡池）| `/providers` 新建一条 card profile | `provider_configs(card, my-pool, {...})` | ✅ | ✅ |
| 让某个任务用别的卡商档案 | `/tasks/create` → card_provider 下拉选档案 | `runs.card_provider` | 仅该任务 | — |
| 改全局 OpenAI 邮箱 provider 默认 | `/config` → 邮箱 tab → `email_provider_name` | `app_settings.email_provider_name` | ✅ | ❌ |
| 添加凭据邮箱（credentialed mode）| `/mail-accounts` 新增 | `mail_accounts` 行 | ✅ | ❌ |
| 让某任务用某个特定凭据邮箱 | `/tasks/create` → mail_account_id 下拉 | `runs.mail_account_id` | 仅该任务 | — |
| 加 Pro 垫脚石账号（卡预热用）| `/config` → 支付 tab → `WARMUP_ACCOUNT_POOL` | `app_settings.warmup_account_pool` (JSON) | ✅ | ❌ |
| 改 AdsPower 默认 API 地址 | `/config` → 基础 tab → `ads_api` | `app_settings.ads_api` | ✅ | ❌ |
| 改全局默认 provider 档案名 | `/config` → 各 tab → `default_*_provider` | `app_settings.default_*_provider` | ✅（下次任务回退用）| ❌ |

---

## 第 4 部分：已识别的 6 个 ambiguity

> **更新（2026-04-26）**：Ambiguity #1 / #2 / #4 / #5 / #6 已实施清理（"5 项清理"plan 完成，+12 测试守护）。仅 #3（`mail_config_name` 远程引用）保持原貌（设计层语义，不是 bug）。

### 🔴 Ambiguity 1：L1 vs L2 优先级 UI 不可见 ✅ **已修复（2026-04-26）**

**问题**：`.env` 设 `EFUNCARD_TOKEN=abc`，又在 `/providers` card profile 里设 `efuncard_token=xyz` —— UI 上两边都显示，**实际任务用 xyz**（L2 赢，worker line 435-436）。

**已实施修复**：
- 后端新增 `GET /api/config/effective` 返回每个字段的 `value` + `source`（"appconfig" / "appsetting" / "provider_profile:type:name,..."）
- `/config` 页面渲染时，被 L2 覆盖的字段下方显示**琥珀色警告**："⚠ 当前值被 provider profile 覆盖：xxx"
- 被 L1b AppSetting 覆盖的字段显示**蓝色提示**："ℹ 此字段被运行时 AppSetting 覆盖"

---

### 🔴 Ambiguity 2：WARMUP_ACCOUNT_POOL vs mail_accounts 重叠 ✅ **已彻底解决（2026-04-26 v2）**

**v1 修复（2026-04-26）**：加 `MailAccount.role` 字段隔离凭据邮箱 vs 垫脚石账号，但池分界继续用 `WARMUP_ACCOUNT_POOL` 字符串。

**v2 修复（同日，本次）**：把垫脚石账号也迁到 `mail_accounts(role=pro_warmup)`，**`WARMUP_ACCOUNT_POOL` 配置项整体废弃**。原因：
- 旧 `WARMUP_ACCOUNT_POOL` 存预填的 `access_token`，token 寿命几小时就过期 → 用户要手动刷
- 新方案存 `email + extra.password + extra.adspower_profile_id`，预热时用 `pro_account_login()` 现场登录刷 fresh token
- 号池现在是 DB 表，支持 last_used_at 排序、cooldown_until 冷却（默认 30 分钟）、consecutive_failures 自动剔除（默认 ≥3 禁用）

**新代码路径**：
- 调度入口：`ConfigService.select_warmup_account() / record_warmup_outcome()` (`src/services/config_service.py`)
- 预热执行：`src/orchestration/warmup.py:execute_card_warmup` —— 已改为接 `svc` 参数
- UI：`/mail-accounts` 表单在 role=pro_warmup 时显示 `登录密码 + AdsPower Profile ID` 字段；列表显示"上次使用 / 冷却至 / 失败原因"
- API 校验：`POST/PUT /api/mail-accounts` 在 role=pro_warmup 时强制 `email + extra.password + extra.adspower_profile_id` 必填（返回 422）

**迁移指南**（旧 .env `WARMUP_ACCOUNT_POOL` 用户）：
1. 进 `/mail-accounts`，点"+ 新增账号"
2. 角色选 `pro_warmup`
3. 邮箱填 ChatGPT Pro 账号邮箱、密码填账号登录密码、AdsPower Profile ID 填原 JSON 里的 profile_id
4. 老 `WARMUP_ACCOUNT_POOL` 环境变量可以从 .env 删除（已不再被代码读取）

---

### 🟡 Ambiguity 3：`mail_config_name` 是远程引用，本地无意义

**问题**：本地 `provider_configs(mail).config.config_name = "mydomain-cfworker"` 仅是**指针**，真正的配置在远程 email-provider 服务的 DB。改本地不影响远程，反之亦然。

**当前规避**：在远程 email-provider admin UI 维护真正的 mailbox 配置，本地只填名字做引用。

**修复方向**：本地 UI 提示"此字段是远程 email-provider 的引用名称，请在远程管理"。

---

### 🟡 Ambiguity 4：Run.email 修改不会同步 Run.mail_account_id ✅ **已修复（2026-04-26）**

**问题**：用户在 `/tasks/create` 选了 `mail_account_id=mail-acc-xyz`（绑定 `userA@x.com`），后改 email 字段为 `userB@x.com` → 创建时不报错 → worker 启动时 line 483 校验失败：`任务邮箱与所选邮箱账号不一致`。

**已实施修复**：`src/api/routes/tasks.py:create_task` 加同步校验：
- 显式选 `mail_account_id` 时 line 81 已校验 email==account.email（旧）
- **新增**：用户没选 mail_account_id 但 `default_mail_account_id` 兜底场景下，credentialed 模式下校验 email 与默认账号邮箱一致；不一致直接 400 + 提示"请显式选 mail_account_id 或改 email 与默认账号一致或在 /config 改默认账号"
- 提示增加 role 隔离：选 `pro_warmup` 账号会被 400 拒绝

---

### 🟡 Ambiguity 5：`default_mail_account_id` 引用悬空 ✅ **已修复（2026-04-26）**

**问题**：`AppConfig.default_mail_account_id` 设了某 ID，后该 MailAccount 被删 → 任务跑起来才报错 `任务指定的邮箱账号不存在或已停用`。

**已实施修复**：
- `ConfigService.delete_mail_account()` 删除前先校验 default 引用，命中则抛 `MailAccountInDefaultUseError`
- `DELETE /api/mail-accounts/{id}` 捕获后返回 `409 Conflict` + 中文提示"请先在 /config 改 default_mail_account_id 或留空再删除"

---

### 🟡 Ambiguity 6：版本控制不对称 ✅ **已修复（2026-04-26）**

**问题**：
- `provider_configs` 改动写 `provider_config_revisions` 表，可回滚 ✅
- `app_settings`（即 L1b 全局动态覆盖）**没有** revision 表，改错没法回滚 ❌

**已实施修复**：
- 新增 `AppSettingRevision` 表（与 `ProviderConfigRevision` 镜像设计）
- `ConfigService._persist_override()` 写 `AppSetting` 前先把旧值快照到 `app_setting_revisions`
- 旧值与新值相等时跳过（避免审计噪音）
- 新增 `ConfigService.list_app_setting_revisions(key, limit)` 查询接口
- 新增 `GET /api/config/revisions` 端点，admin 可查看完整改动历史

---

## 第 5 部分：常见误区 Q&A

**Q1：我改 .env 没生效，为什么？**  
A：3 种原因：(a) 没重启 uvicorn；(b) `app_settings` 表里有同名 override，**赢过** .env；(c) `provider_configs` 里某 profile 有同名 key，赢过 AppConfig。**排查顺序**：先看 L2 → L1b → L1a。

**Q2：我在 `/config` 改了 efuncard_token，为什么任务用的还是旧值？**  
A：你的 `card-default` provider profile 里**也存了** `efuncard_token`（L2 优先）。要么改 profile，要么从 profile 删掉这个 key 让它回退到 AppConfig。

**Q3：mail_account_id 是必填的吗？**  
A：不是。空时根据 `email_provider_name` 决定：`applemail` 用 legacy `MAIL_REFRESH_TOKEN/MAIL_CLIENT_ID`；`cfworker` 等 managed 模式根本不需要凭据。

**Q4：垫脚石账号到底放哪？**  
A：**只放** `WARMUP_ACCOUNT_POOL`（`.env` 或 `/config` 支付 tab）。**不要**放 `/mail-accounts`，那是 credentialed 邮箱凭据池，跟 OpenAI 账号不是一回事。

**Q5：Run 的 config_snapshot 字段是什么？**  
A：任务创建时把当时的 AppConfig 完整快照存进 Run（`src/api/routes/tasks.py:create_task` line 100）—— 用于事后审计"这次任务实际用了什么配置"。注意它只存 L1+L1b 快照，不含 L2/L3 解析后的最终值。

---

## 第 6 部分：建议的清理方向（不动手，待批准）

| 改动 | 收益 | 工作量 |
|------|------|--------|
| `app_settings` 加 revision 表 | 统一审计模型，改错可回滚 | 中（参照 provider_config_revisions）|
| UI 字段加"被 L2 覆盖"提示 | 消除 Ambiguity 1 | 中（前端改 `/config` 渲染）|
| `mail_accounts` 加 `role` 字段 | 消除 Ambiguity 2 | 小（schema migration + UI 下拉）|
| 任务创建时同步校验 email vs mail_account_id | 消除 Ambiguity 4 | 小 |
| MailAccount 删除前校验 default 引用 | 消除 Ambiguity 5 | 小 |

---

## 附录：关键代码位置索引

| 文件 | 行 | 作用 |
|------|----|------|
| `src/config.py` | 305-396 | `load_config()` 从 .env 读 AppConfig |
| `src/db/engine.py` | 100-204 | `_seed_runtime_defaults()` 启动时把 AppConfig 字段拷到 provider_configs |
| `src/services/config_service.py` | 41 | `ConfigService.get_config()` AppConfig + AppSetting 折叠 |
| `src/services/config_service.py` | 227 | `resolve_provider_config()` L2 档案查询 |
| `src/api/routes/tasks.py` | 57-116 | `create_task` 校验 L3 + 落库 Run |
| `src/api/routes/config.py` | 226-238 | card provider driver 白名单 |
| `src/api/worker.py` | 404-508 | **`_resolve_runtime_config(run)` ← 核心 resolver** |
| `main.py` | 420-439 | `_build_runtime_clients(config)` 实例化 client |
| `src/orchestration/warmup.py` | 43-67 | `parse_warmup_accounts(config)` 读 WARMUP_ACCOUNT_POOL |
| `src/db/models.py` | 95 | `ProviderConfig` 表定义 |
| `src/db/models.py` | 108 | `MailAccount` 表定义 |

---

**最后修订**：本文档由 3 个 explore agent 并行调研后整合，所有声明均带 file:line 引用。如发现与代码不一致，以代码为准并提 issue 修订本文档。
