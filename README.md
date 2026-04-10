# team-register

当前仓库是一个基于 Python 的自动化注册/支付流程项目，现行入口为 `main.py`，核心能力拆分在 `src/` 中。

当前主流程已升级为：

- **规则优先状态机**：优先基于 URL、结构化控件属性和动作结果推进流程
- **受限 LLM 决策兜底**：仅在状态歧义、DOM 变体或连续失败时，从候选动作中做选择
- **证据包与断点信息**：每次运行会在 `RUN_ARTIFACTS_DIR` 下记录 step 级证据，便于回放与排障
- **可恢复执行**：新增 preflight、clean-start、错误页恢复和人工接管等待机制

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

## 测试

当前仓库使用根目录 `pytest.ini` 约束只收集 `tests/`，避免与 `email-provider/tests/` 冲突。

```bash
python -m pytest -q
python -m unittest discover -s tests -v
```

## 模块说明

- `src/config.py`：加载 `.env` 并校验关键配置。
- `src/browser.py`：封装代理提取与 AdsPower CDP 连接。
- `src/automation/`：状态机、证据采集、LLM 协议、产物记录与独立 session 提取。
- `src/mail.py`：通过本地 `email-provider` 获取邮箱验证码。
- `src/sms.py`：封装 SMS-Activate 获取号码/验证码。
- `src/efuncard.py`：封装 Efuncard 卡查询 / 首次激活 / 3DS 轮询，并优先复用 1 小时内已激活卡。
- `src/payment_link.py`：根据 Access Token 生成支付链接，支持 Team/Plus 计划与 app/hosted 两种 checkout 返回形态。
- `src/models.py` / `src/utils.py`：数据模型与通用工具。

## 文档

- 架构与调用关系：`docs/architecture.md`
- 归档说明：`legacy/README.md`
- 手动邮件排障脚本：`scripts/manual_mail_check.py`

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
- `MAIL_DOMAIN` 目前仅作为兼容保留字段；主流程的邮件验证码获取依赖 `MAIL_REFRESH_TOKEN` 与 `MAIL_CLIENT_ID`，并直接调用本地 `email-provider`。
