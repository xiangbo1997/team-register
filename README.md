# team-register

当前仓库是一个基于 Python 的自动化注册/支付流程项目，现行入口为 `main.py`，核心能力拆分在 `src/` 中。

当前主流程已升级为：

- **规则优先状态机**：优先基于 URL、结构化控件属性和动作结果推进流程
- **受限 LLM 决策兜底**：仅在状态歧义、DOM 变体或连续失败时，从候选动作中做选择
- **证据包与断点信息**：每次运行会在 `RUN_ARTIFACTS_DIR` 下记录 step 级证据，便于回放与排障
- **可恢复执行**：新增 preflight、clean-start、错误页恢复和人工接管等待机制

同时，仓库现在包含一个 **FastAPI 控制台**，提供：

- `/login`：控制台登录页
- `/help`：帮助中心
- `/manual`：使用手册 HTML 页面
- `/`、`/tasks`、`/config`、`/providers`、`/mail-accounts`：控制台页面
- 右下角单助手浮窗：并列检索使用手册与安全全库知识，支持 preview → commit 的受控执行

## 当前结构

```text
.
├── main.py                  # 当前主入口
├── src/                     # 模块化业务代码
├── tests/                   # 当前主项目测试
├── scripts/                 # 手动排障/辅助脚本
├── docs/                    # 当前文档
├── legacy/                  # 已归档的单文件原型与旧说明
└── email-provider/          # 内嵌可复用邮件服务子项目（独立维护）
```

## 运行方式

1. 安装依赖：

```bash
pip install -r requirements.txt
playwright install chromium
```

2. 配置环境变量：

```bash
cp .env.example .env
```

3. 启动当前主流程：

```bash
python main.py
```

4. 启动控制台：

```bash
uvicorn src.api.app:app --reload --port 8080
```

## 测试

当前仓库使用根目录 `pytest.ini` 约束只收集 `tests/`，避免与 `email-provider/tests/` 冲突。

```bash
python -m pytest -q
python -m unittest discover -s tests -v
```

## 模块说明

- `src/config.py`：加载 `.env` 并校验关键配置。
- `src/services/config_service.py`：持久化运行时配置覆盖、Provider 配置与 Mail Account 管理。
- `src/browser.py`：封装代理提取与 AdsPower CDP 连接。
- `src/automation/`：状态机、证据采集、LLM 协议、产物记录与独立 session 提取。
- `src/mail.py`：通过本地 `email-provider` 获取邮箱验证码。
- `src/sms.py`：封装 SMS-Activate 获取号码/验证码。
- `src/efuncard.py`：封装 Efuncard 卡查询 / 首次激活 / 3DS 轮询，并优先复用 1 小时内已激活卡。
- `src/payment_link.py`：根据 Access Token 生成支付链接，支持 Team/Plus 计划与 app/hosted 两种 checkout 返回形态。
- `src/models.py` / `src/utils.py`：数据模型与通用工具。

## 文档

- 架构与调用关系：`docs/architecture.md`
- 控制台使用手册：`docs/usage-manual.md`
- 归档说明：`legacy/README.md`
- 手动邮件排障脚本：`scripts/manual_mail_check.py`

## 控制台单助手

控制台默认只暴露一个 AI 助手，不暴露内部多 agent 细节。当前能力边界：

- 文档问答：同时返回手册引用和代码引用
- 受控 preview：assistant 白名单仅允许 `upsert_provider` 与 `update_config` 两类动作
- 受控 commit：必须基于 preview_id，不接受自然语言直接提交
- 高风险流程（注册自动化、浏览器/CDP、token、支付/绑卡/3DS）当前只说明，不直接 commit

## 控制台配置与任务级 Provider

- `/config`：维护系统级默认值、运行阈值、邮件服务连接地址/API Key，以及默认 browser/card/mail provider。
- `/providers`：维护 browser/card/mail 三类 Provider profile，任务创建时可以显式选择。
- `/mail-accounts`：维护 credentialed 邮箱账号池（例如 AppleMail 的 `email/client_id/refresh_token`）。
- `/tasks/create`：现在支持按任务选择 `browser_provider`、`card_provider`、`mail_provider`、`mail_account_id`；未选择时自动回退到 `/config` 默认值和 legacy `.env` 凭据。若选择了 `mail_account_id`，任务邮箱必须与该账号记录中的邮箱一致，避免把 A 邮箱跑成 B 邮箱的 `client_id/refresh_token`。

## 关键环境变量

### LLM 决策器

- `LLM_ENABLED`：是否启用 OpenAI 兼容 LLM 兜底
- `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`：OpenAI 兼容接口配置
- `LLM_TIMEOUT_MS`：单次决策超时
- `LLM_CONFIDENCE_THRESHOLD`：低于该阈值不执行 LLM 结果
- `LLM_MAX_CONSECUTIVE_UNCERTAIN`：连续低置信/继续请求证据的上限

### 自动化运行时

- `ENABLE_PAYMENT_FLOW`：是否在注册成功后继续跑支付阶段
- `PAYMENT_PLAN`：支付计划，当前默认 `team`（Team 免费试用）
- `PAYMENT_LINK_ONLY`：若为 `true`，只生成支付链接，不打开支付页、不绑卡
- `PAYMENT_LINK_RETURN_MODE`：链接返回模式；`long` 会优先要求 hosted checkout（`pay.openai.com`），`app` 则使用站内 checkout 链接
- `AIMIZY_COUNTRY` / `AIMIZY_CURRENCY`：Team 免费试用长链接生成时透传给 aimizy 的国家与币种参数
- `BILLING_COUNTRY`：checkout 页账单国家，提交前会参与回读校验
- `BILLING_LINE1` / `BILLING_LINE2`：checkout 页账单地址 1 / 地址 2
- `BILLING_CITY` / `BILLING_STATE` / `BILLING_POSTAL_CODE`：checkout 页城市、省州、邮编；若回读不一致会中止提交
- `RUN_ARTIFACTS_DIR`：证据包输出目录
- `TRACE_ON_FAILURE`：失败时是否保留额外追踪产物
- `CLEAN_CONTEXT_MODE`：当前默认 `reuse_and_clean`
- `MAX_NAVIGATION_RETRIES` / `MAX_EMAIL_ATTEMPTS` / `MAX_PROFILE_RECONNECTS` / `MAX_MANUAL_HANDOFFS`：恢复与人工兜底阈值

## 兼容说明

- 根目录 `gpt_automation.py` 已退化为兼容入口，真实旧实现已迁移到 `legacy/gpt_automation.py`。
- `MAIL_DOMAIN` 目前仅作为兼容保留字段。
- 推荐通过 `EMAIL_PROVIDER_NAME` + `KNOWN_MAIL_ACCOUNTS_JSON` 明确声明邮箱 provider 与既有账号凭据；当前 `applemail` 会优先走 `credentialed` 会话模式并精确匹配任务邮箱。
- `MAIL_REFRESH_TOKEN` + `MAIL_CLIENT_ID` 仍保留为单邮箱兼容凭据来源：当未配置 `KNOWN_MAIL_ACCOUNTS_JSON`，或当前任务邮箱未命中 JSON 但仍保留 legacy 凭据时，主流程会用这两个值为当前任务邮箱动态注入单账号上下文；**但客户端只接受最新 `email-provider` 运行态，不再回退旧 `/sessions` 协议。**
- `team-register` 现已改成 **latest-only**：任务启动前会先检查 `GET /api/mailbox-service/health`、`GET /api/mailbox-service/providers` 与目标 provider 的 `supported_session_modes`；若当前 `127.0.0.1:8000` 缺少 `credentialed` 能力、缺少 `/credentialed-sessions`，或返回 5xx，会直接失败，不再进入邮箱验证码页里的 fallback `/sessions`、LLM uncertain、manual handoff。
- 邮箱链路最短排障顺序：① 确认 `127.0.0.1:8000` 是最新 `email-provider` 进程；② 检查 `/health` 与 `/providers`；③ 运行 `python scripts/manual_mail_check.py`；④ 只有在运行态/凭据都通过后，再去看 `INBOX` / `Junk` 是否收到了验证码。
