# Email Provider Integration Guide

## 1. 仓库定位

`email-provider` 的目标是把各种邮箱 provider 的差异收口成一个稳定的“邮箱会话服务”。

对调用方来说，固定语义只有三步：

1. 创建邮箱会话。
2. 轮询验证码。
3. 标记成功或失败并释放会话。

这样做的目的有两个：

- 业务项目不用感知 AppleMail、LuckMail、QQEmail 等 provider 的细节差异。
- 业务项目切换邮箱 provider 时，只需要改 `provider + extra`，不需要重写主流程。

当前仓库是第一阶段拆分结果：

- 已经可以独立启动成一个 FastAPI 服务。
- 已经支持主项目中的 ChatGPT / Kiro 类 OTP 场景。
- 仍然保留 Python 进程内兼容层，方便旧代码平滑迁移。

## 2. 目录结构

- `main.py`
  FastAPI 入口，启动时初始化数据库。
- `api/mailbox_service.py`
  HTTP 接口定义，当前固定 API 契约在这里。
- `services/mailbox_service.py`
  会话状态机、租约、持久化、错误码映射。
- `core/base_mailbox.py`
  provider 抽象、各 provider 实现、兼容层 `MailboxServiceBackedMailbox`。
- `core/applemail_diagnostics.py`
  AppleMail 收信诊断客户端。
- `scripts/applemail_diagnose.py`
  AppleMail 诊断 CLI。
- `tests/`
  基础回归测试。

## 3. 核心概念

### 3.1 Provider

provider 是具体邮箱来源，例如：

- `laoudo`
- `tempmail_lol`
- `skymail`
- `duckmail`
- `freemail`
- `moemail`
- `maliapi`
- `cfworker`
- `luckmail`
- `qqemail`
- `applemail`

### 3.2 Mailbox Session

每次验证码任务都应该创建一个 mailbox session。session 持久化到数据库，用来记录：

- 分配到的邮箱地址
- provider 名称
- provider 配置
- `before_ids` 快照
- 当前状态
- 异常码和异常信息

### 3.3 Lease Token

`lease_token` 是操作某个 session 的租约令牌。`poll-code` 和 `complete` 都依赖它。

建议：

- 调用方把 `lease_token` 当成敏感信息处理。
- 不要打印到业务日志。
- 不要自己伪造或缓存复用其他任务的 `lease_token`。

### 3.4 before_ids

`before_ids` 是创建 session 时抓取到的“已有邮件 id 列表”。轮询验证码时要带回去，用来过滤旧邮件，避免误读上一轮任务的验证码。

### 3.5 provider_meta

`provider_meta` 是 provider 透出的补充上下文，常见字段：

- `mailbox_token`
- `mailbox_order_no`
- `allocated_email`
- `source_email`

有些 provider 还会透出自己的额外字段。调用方如果需要保留 provider 上下文，优先从这里取。

## 4. 启动方式

安装依赖：

```bash
pip install -r requirements.txt
```

启动服务：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

默认数据库：

```bash
MAILBOX_SERVICE_DATABASE_URL=sqlite:///mailbox_service.db
```

也可以改成其他 SQLAlchemy/SQLModel 支持的数据库 URL。

## 5. 推荐接入方式

### 5.1 首选：HTTP API

对其他项目、其他 agent 来说，最稳定的接入方式是直接调用 HTTP API，不要直接 import provider 类。

原因：

- HTTP API 是当前仓库的固定公共边界。
- provider 内部字段和类实现后续仍可能演进。
- 业务项目不需要和 provider 内部强绑定。

### 5.2 次选：Python 兼容层

如果你在一个 Python 项目里，需要兼容旧的 `BaseMailbox` 调用面，可以使用：

- `core.base_mailbox.MailboxServiceBackedMailbox`
- `core.base_mailbox.create_mailbox()`

这层适合渐进迁移，不适合跨语言或跨仓库长期集成。

## 6. HTTP API 契约

服务根前缀由 `main.py` 和路由共同决定，当前固定路径为：

- `/api/mailbox-service/health`
- `/api/mailbox-service/providers`
- `/api/mailbox-service/providers/{provider}/validate-config`
- `/api/mailbox-service/sessions`
- `/api/mailbox-service/sessions/{session_id}`
- `/api/mailbox-service/sessions/{session_id}/poll-code`
- `/api/mailbox-service/sessions/{session_id}/complete`

### 6.1 健康检查

`GET /api/mailbox-service/health`

用途：

- 检查服务是否启动。
- 确认当前可用 provider 列表。
- 查看数据库连接配置。

示例响应：

```json
{
  "ok": true,
  "providers": ["laoudo", "tempmail_lol", "skymail", "duckmail"],
  "database_url": "sqlite:///mailbox_service.db"
}
```

### 6.2 列出 provider

`GET /api/mailbox-service/providers`

示例响应：

```json
{
  "providers": [
    {"name": "laoudo", "mode": "legacy_adapter"},
    {"name": "applemail", "mode": "legacy_adapter"}
  ]
}
```

### 6.3 校验 provider 配置

`POST /api/mailbox-service/providers/{provider}/validate-config`

请求体：

```json
{
  "extra": {
    "applemail_accounts": "user@example.com----password----client_id----refresh_token"
  },
  "proxy": "socks5h://user:pass@127.0.0.1:1080"
}
```

语义：

- 服务会尝试实例化对应 provider。
- 这个接口主要用来提前发现配置缺失、参数错误。
- 当前实现不会主动调用 `get_email()` 或取码逻辑，所以它更接近“浅校验”而不是完整联通性校验。

成功响应：

```json
{
  "ok": true,
  "provider": "applemail"
}
```

### 6.4 创建会话

`POST /api/mailbox-service/sessions`

请求体字段：

- `provider`: provider 名称，必填。
- `purpose`: 业务目的，选填，默认 `generic`。
- `proxy`: 代理地址，选填。
- `extra`: provider 配置，选填。
- `email`: 已知邮箱地址，选填。
- `account_id`: 已知 provider 账号 id，选填。
- `account_extra`: 已知账号附加信息，选填。
- `lease_seconds`: 租约有效期，选填，默认 `900`。

这个接口有两种模式。

#### 模式 A：新分配邮箱

不传 `email`。

请求示例：

```json
{
  "provider": "applemail",
  "purpose": "chatgpt-signup",
  "proxy": "socks5h://user:pass@127.0.0.1:1080",
  "extra": {
    "applemail_accounts": "user@example.com----password----client_id----refresh_token"
  },
  "lease_seconds": 900
}
```

#### 模式 B：接管已知邮箱

传 `email`，必要时传 `account_id` 和 `account_extra`。

这用于：

- 业务端已经知道邮箱地址
- 第二阶段二次取码
- 保留已有 provider 上下文继续轮询

请求示例：

```json
{
  "provider": "luckmail",
  "purpose": "known-email-retry",
  "email": "demo@example.com",
  "account_id": "provider-mailbox-token",
  "account_extra": {
    "mailbox_token": "provider-mailbox-token",
    "source": "legacy-context"
  },
  "extra": {
    "luckmail_api_key": "your-api-key",
    "luckmail_project_code": "project-code"
  },
  "lease_seconds": 300
}
```

成功响应：

```json
{
  "session_id": "6f3d7d1b5d994f42b2b5158fd95a1b31",
  "lease_token": "opaque-token",
  "provider": "applemail",
  "email": "demo@example.com",
  "account_id": "provider-account-id",
  "state": "leased",
  "expires_at": "2026-04-08T12:00:00+00:00",
  "before_ids": ["m1", "m2"],
  "provider_meta": {
    "source_email": "demo@example.com",
    "mailbox_token": "provider-account-id"
  }
}
```

注意：

- `session_id` 是服务层 id。
- `account_id` 是 provider 层账号 id。
- 在兼容层里，旧代码看到的 `account_id` 可能会被替换成 `session_id`，不要把两个概念混用。

### 6.5 查询会话

`GET /api/mailbox-service/sessions/{session_id}`

当前实现要求服务内部校验 session 生命周期，若 session 已过期会返回错误。

成功响应包含：

- `session_id`
- `provider`
- `email`
- `account_id`
- `state`
- `expires_at`
- `before_ids`
- `provider_meta`

### 6.6 轮询验证码

`POST /api/mailbox-service/sessions/{session_id}/poll-code`

请求体：

```json
{
  "lease_token": "opaque-token",
  "timeout_seconds": 120,
  "keyword": "ChatGPT",
  "code_pattern": null,
  "otp_sent_at": 1760000000.0,
  "exclude_codes": ["000000"],
  "before_ids": ["m1", "m2"]
}
```

字段说明：

- `lease_token`: 必填。
- `timeout_seconds`: 等待时长。
- `keyword`: provider 实现需要时可用于筛选邮件。
- `code_pattern`: 自定义验证码正则；不传时走默认提取逻辑。
- `otp_sent_at`: OTP 触发时间戳；某些 provider 可用它过滤旧邮件。
- `exclude_codes`: 排除历史验证码。
- `before_ids`: 邮件快照。建议直接传创建会话时返回的 `before_ids`。

成功响应：

```json
{
  "status": "ready",
  "code": "584863",
  "message": "",
  "matched_mailbox": "",
  "error_code": ""
}
```

失败响应仍然是 200，但 `status=failed`：

```json
{
  "status": "failed",
  "code": "",
  "message": "invalid_grant",
  "matched_mailbox": "",
  "error_code": "INVALID_CREDENTIAL"
}
```

当前错误码映射：

- `INVALID_CREDENTIAL`
- `LEASE_EXPIRED`
- `CODE_TIMEOUT`
- `RATE_LIMITED`
- `UPSTREAM_5XX`
- `PROVIDER_ERROR`

### 6.7 错误响应外壳

当接口在参数校验或服务层直接抛错时，HTTP 会返回 `400`，响应结构为：

```json
{
  "detail": {
    "code": "UNSUPPORTED_PROVIDER",
    "message": "不支持的邮箱 provider: demo"
  }
}
```

### 6.8 完成会话

`POST /api/mailbox-service/sessions/{session_id}/complete`

请求体：

```json
{
  "lease_token": "opaque-token",
  "result": "success",
  "reason": ""
}
```

推荐值：

- 成功时：`result = "success"`
- 失败时：`result = "failed"`，并填写 `reason`

成功响应：

```json
{
  "session_id": "6f3d7d1b5d994f42b2b5158fd95a1b31",
  "provider": "applemail",
  "email": "demo@example.com",
  "state": "completed",
  "expires_at": "2026-04-08T12:00:00+00:00"
}
```

重要：

- 不要跳过这个接口。
- `result=success` 时，服务会尝试调用 provider 的清理逻辑，例如 `remove_used_account()`。
- 如果业务任务失败，也应该显式调用 `complete`，便于审计和后续回收。

## 7. 推荐业务流程

### 7.1 新邮箱分配场景

典型流程：

1. 调 `POST /sessions`，不带 `email`。
2. 保存返回的 `session_id`、`lease_token`、`email`、`before_ids`、`provider_meta`。
3. 用 `email` 去触发业务平台发送验证码。
4. 调 `POST /poll-code` 取验证码。
5. 成功后调 `POST /complete`，`result=success`。
6. 失败后也调 `POST /complete`，`result=failed`。

### 7.2 已知邮箱二次取码场景

这个流程是为 Kiro/LuckMail 这类“业务端已经知道邮箱上下文”的情况设计的。

典型流程：

1. 业务端持有既有邮箱地址，以及可能的 provider token。
2. 调 `POST /sessions` 时带上 `email`，必要时带 `account_id` 和 `account_extra`。
3. 服务内部会尝试恢复 provider 运行态，并重新抓一份 `before_ids`。
4. 再调用 `POST /poll-code`。
5. 最后调用 `POST /complete`。

这样做的价值是：

- 业务项目不需要再写“已知邮箱上下文”特判。
- provider 恢复逻辑集中在服务内部。
- 可以统一所有项目的二次取码语义。

## 8. Python 进程内兼容模式

如果你暂时不能切到 HTTP API，可以先在 Python 里使用兼容层。

### 8.1 通过工厂切到服务模式

`core.base_mailbox.create_mailbox()` 会在以下任一条件成立时返回 `MailboxServiceBackedMailbox`：

- `extra["mailbox_service_enabled"]` 为真值
- 环境变量 `MAILBOX_SERVICE_ENABLED` 为真值

### 8.2 兼容层行为

兼容层会把旧的调用面映射到 service 语义：

- `get_email()` -> `acquire_session()`
- `get_current_ids()` -> 读 session 的 `before_ids`
- `wait_for_code()` -> `poll_code()`
- `complete_success()` -> `complete_session(result="success")`
- `complete_failed()` -> `complete_session(result="failed")`

### 8.3 兼容层里的一个重要区别

`MailboxServiceBackedMailbox.get_email()` 返回的 `MailboxAccount.account_id` 是 `session_id`，不是 provider 原生 id。

如果你需要 provider 原生 token，请优先从：

- `account.extra["mailbox_token"]`
- `account.extra["provider"]`
- `account.extra["lease_token"]`
- `mailbox.get_provider_meta()`

里取，不要假设 `account_id` 仍然等于老系统里的 provider token。

## 9. Provider 配置键

所有 provider 配置都通过 `extra` 透传到 `core/base_mailbox.py:create_local_mailbox()`。

当前已知键如下。

### 9.1 `laoudo`

- `laoudo_auth`
- `laoudo_email`
- `laoudo_account_id`

### 9.2 `tempmail_lol`

- 无额外配置

### 9.3 `skymail`

- `skymail_api_base`
- `skymail_token`
- `skymail_domain`

### 9.4 `duckmail`

- `duckmail_api_url`
- `duckmail_provider_url`
- `duckmail_bearer`
- `duckmail_domain`
- `duckmail_api_key`

### 9.5 `freemail`

- `freemail_api_url`
- `freemail_admin_token`
- `freemail_username`
- `freemail_password`

### 9.6 `moemail`

- `moemail_api_url`

### 9.7 `maliapi`

- `maliapi_base_url`
- `maliapi_api_key`
- `maliapi_domain`
- `maliapi_auto_domain_strategy`

### 9.8 `cfworker`

- `cfworker_api_url`
- `cfworker_admin_token`
- `cfworker_domain`
- `cfworker_domain_override`
- `cfworker_domains`
- `cfworker_enabled_domains`
- `cfworker_fingerprint`
- `cfworker_custom_auth`

### 9.9 `luckmail`

- `luckmail_base_url`
- `luckmail_api_key`
- `luckmail_project_code`
- `luckmail_email_type`
- `luckmail_domain`

### 9.10 `qqemail`

- `qqemail_api_url`
- `qqemail_username`
- `qqemail_password`
- `qqemail_domain`

### 9.11 `applemail`

- `applemail_accounts`

`applemail_accounts` 是文本串，按账号行组织，当前实现按 provider 内部格式解析。

## 10. 状态机与持久化

当前会话状态可能包括：

- `leased`
- `polling`
- `code_ready`
- `failed`
- `completed`
- `expired`

数据库表：

- `mailbox_service_sessions`
- `mailbox_service_session_events`

注意：

- `extra`、`proxy`、`account_extra` 会被持久化到数据库。
- 如果这些字段里放了 provider token、代理账号、邮箱凭据，数据库就属于敏感资产。

## 11. 安全与部署建议

### 11.1 当前实现没有内建鉴权

因此：

- 不要把服务直接裸露到公网。
- 推荐只部署在内网。
- 或者挂在已有 API 网关后面做鉴权、限流、审计。

### 11.2 数据库是敏感资产

因为 session 表会保留：

- provider 配置
- 代理地址
- provider token
- 已知邮箱上下文

所以至少要做到：

- 数据库文件权限收紧
- 数据库备份加密
- 避免把表内容直接打到日志

### 11.3 当前更适合单实例部署

当前实现没有显式的分布式锁或多实例租约协调语义。

推荐：

- 先按单实例内部服务部署
- 如果要多实例，再补充鉴权、共享数据库、并发控制和幂等策略

这里的“更适合单实例”是基于当前代码实现得出的工程判断，不是协议层硬限制。

## 12. 扩展新 provider 的步骤

如果你要新增邮箱 provider，按这个顺序改：

1. 在 `core/base_mailbox.py` 里新增 provider 类，实现：
   - `get_email()`
   - `get_current_ids()`
   - `wait_for_code()`
2. 在 `create_local_mailbox()` 里接入新 provider。
3. 在 `services/mailbox_service.py` 的 `SUPPORTED_PROVIDERS` 里注册名字。
4. 如果 provider 需要额外上下文，确保能通过 `provider_meta` 向上暴露。
5. 补回归测试。

新 provider 至少要满足这几个约束：

- `get_email()` 返回 `MailboxAccount`
- `get_current_ids()` 能提供稳定的旧邮件快照
- `wait_for_code()` 超时时抛出明确异常
- provider 异常信息尽量可读，便于映射成统一错误码

## 13. AppleMail 诊断工具

仓库自带 AppleMail 诊断 CLI，用来排查“收不到验证码”“INBOX/Junk 行为异常”“返回 payload 结构变化”等问题。

示例：

```bash
python scripts/applemail_diagnose.py \
  --client-id YOUR_CLIENT_ID \
  --refresh-token YOUR_REFRESH_TOKEN \
  --email demo@example.com \
  --sender-filter openai.com \
  --json
```

支持：

- 同时检查 `INBOX` 和 `Junk`
- `latest` / `all` 两种模式
- 按时间窗口过滤
- 按发件人、主题、内容过滤

## 14. 回归测试

建议至少运行：

```bash
python -m unittest \
  tests.test_mailbox_service \
  tests.test_applemail_mailbox \
  tests.test_applemail_diagnostics
```

当前测试覆盖重点：

- mailbox session 生命周期
- 兼容层映射行为
- `invalid_grant` -> `INVALID_CREDENTIAL` 映射
- AppleMail payload 兼容性
- AppleMail 诊断工具过滤逻辑

## 15. 给接入方的最终建议

如果你是其他项目或其他 agent，要接这个仓库，按下面的原则做：

1. 优先走 HTTP API。
2. 固定保存 `session_id + lease_token + before_ids + provider_meta`。
3. 无论任务成功失败都调用 `complete`。
4. 业务项目不要自己重写 provider 特判，尤其不要重新实现“已知邮箱上下文二次取码”。
5. 不要把这个服务直接暴露到公网。

按这套方式接，后续即使切换 provider，主流程也不需要重写。
