# Mail Provider 跨端 Contract

> 客户端：`team-register` (本仓库)
> 服务端：`email-provider`（远程，仓库 `/software/email-provider`）
> 适用版本：2026-04-28 起

## 1. 为什么需要这份 contract

`email-provider` 是一个独立部署的 HTTP 服务，提供 `POST /api/mailbox-service/{managed,credentialed}-sessions` 等接口给客户端创建邮箱会话、轮询验证码。历史上：

- 服务端把**业务约束错误**（如 cfworker 缺 `config_name`）抛裸 `RuntimeError`，被 FastAPI 转裸 5xx，客户端只能 retry 5 次（110s）然后空转失败。
- 客户端把所有非 401/404 的 4xx 都当成"运行态不兼容"，分类污染上层（重试决策 / 账号状态 / 错误信息）。

为防止再陷入这类**架构错位**，本文档定义双方必须遵循的 contract。

## 2. 状态码语义（强约束）

| HTTP 状态 | 错误类 (服务端 / 客户端异常) | 语义 | 客户端应当做什么 |
|---|---|---|---|
| **200** | — | 成功 | 继续业务 |
| **401** | `MailRuntimeIncompatibleError` | API key 不匹配 | 修 `EMAIL_PROVIDER_API_KEY` 后重试 |
| **404** | `MailRuntimeIncompatibleError` | 端点缺失（服务端版本太旧） | 升级服务端 |
| **422** | `ProviderConfigIncompleteError` / `MissingProviderConfigError` | provider 必填配置缺失 | **修 admin UI / .env 后重试，不重试同请求** |
| **424** | `ProviderUpstreamError` | provider 上游 API 返回 4xx | 换 provider 或修参数，**不重试同请求** |
| **5xx** | `MailboxServiceError` (兜底) / `MailRuntimeIncompatibleError` | 服务端运行态异常 | 客户端按 `_RETRY_BACKOFF_SECONDS` 重试（默认 5/15/30/60s 共 5 次） |

**关键**：客户端 `_RETRYABLE_STATUS_CODES` 只含 `{500, 502, 503, 504}`。422/424 永远不重试 — 这是稳态故障，重试浪费时间。

## 3. 4xx 响应体 contract

所有 4xx（包括 422/424）响应体必须按以下 JSON 形态：

```json
{
  "detail": {
    "code": "<MACHINE_READABLE>",
    "message": "<人类可读>",
    "missing_fields": ["..."],     // 仅 PROVIDER_NOT_CONFIGURED 提供
    "upstream_status": <int>       // 仅 PROVIDER_UPSTREAM_ERROR 提供
  }
}
```

### 已定义的 `code` 值

| code | 状态码 | 触发场景 | 必填扩展字段 |
|---|---|---|---|
| `PROVIDER_NOT_CONFIGURED` | 422 | 必填 provider 配置（`config_name` / `cfworker_api_url` / 等）缺失 | `missing_fields: list[str]` |
| `PROVIDER_UPSTREAM_ERROR` | 424 | provider 调上游 API 收到 4xx（domain 错 / auth 错 / 余额不足） | `upstream_status: int` |
| `MAILBOX_ERROR` | 400 | 兜底业务错误 | — |

### 客户端解析

`HttpMailProvider._extract_error_payload` (`src/providers/mail.py`) 容忍旧服务端的非 contract 响应（如裸字符串 / 非 dict 响应体），降级用空值，避免抛 `JSONDecodeError`。

## 4. Provider 必填字段约束（managed-session）

| Provider | session_mode | 必填字段 | 说明 |
|---|---|---|---|
| `cfworker` | `managed` | `config_name` | 服务端从加密 DB 注入 `cfworker_api_url` + `admin_token` |
| `skymail` | `managed` | `config_name` | 同上，从 DB 注入 token |
| `applemail` | `credentialed` | （走 `KNOWN_MAIL_ACCOUNTS_JSON` 凭据池）| 不通过 `config_name` |
| `freemail` / `tempmail_lol` / `moemail` / `qqemail` / `duckmail` / `maliapi` / `laoudo` | `managed` | （auto-allocate）| 服务端不依赖客户端配置 |

**新加 provider 时必须更新本表 + 三处白名单常量保持同步**：

- `src/api/routes/config.py:_MAIL_MANAGED_REQUIRED_FIELDS`（L1 admin UI 校验）
- `src/mail.py:_PROVIDERS_REQUIRING_CONFIG_NAME`（L3 运行时 fail-fast）
- `src/config.py:_MAIL_PROVIDERS_REQUIRING_CONFIG_NAME`（L4 .env load warning）

`tests/test_mail_config_failfast.py:TestWhitelistConsistency` 钉死三者必须相等。

## 5. 客户端 4 层 fail-fast 防御

```
L1 admin UI (routes/config.py:upsert_provider)
   └─ 创建/更新 mail-* ProviderConfig 时，managed 模式 + 白名单 provider
      要求 config_name 等必填字段，缺则直接 422 PROVIDER_NOT_CONFIGURED

L2 任务创建 preflight (routes/tasks.py:create_task)
   └─ 任务入队前先 resolve mail provider config，managed 模式必填字段缺失
      直接 422，不让任务进队列

L3 运行时 fail-fast (mail.py:MailManager.ensure_runtime_ready)
   └─ 不发任何 HTTP 请求就检查 self._config_name 非空，缺则 raise
      MissingProviderConfigError（适用 main.py 直跑、跳过 UI 的场景）

L4 启动 warning (config.py:load_config 后置 _warn_if_mail_config_incomplete)
   └─ MAIL_CONFIG_NAME 为空 + EMAIL_PROVIDER_NAME 在白名单 → logger.warning，
      不 raise（向后兼容：admin 通过 UI 配 ProviderConfig 而不依赖 .env）
```

## 6. 失败分类与号池保护

`record_warmup_outcome(failure_class=...)` 接受两类失败：

| failure_class | 何时用 | 影响 |
|---|---|---|
| `account_failure` | 账号自身原因（cookies 失效、风控、配置缺失） | 累计 `consecutive_failures`；≥3 次自动 disable + 清 cooldown |
| `external_failure` | 外部依赖故障（5xx / AdsPower 抖动 / 网络超时） | 不累计，避免远程抖动连带 disable 整个 pro_warmup 号池 |

`warmup._classify_failure` 按 reason 字符串关键词自动分类：

- 含 `PROVIDER_NOT_CONFIGURED` / `missing_fields` / `必填.*配置` → `account_failure`（**override 优先**）
- 含 `5\d{2}` / `mailbox-service` / `adspower` / `timeout` / `PROVIDER_UPSTREAM_ERROR` → `external_failure`
- 默认 → `account_failure`

## 7. 服务端架构原则（远程 email-provider）

**禁止**（用户明确要求）：
- ❌ 在 cfworker / skymail 实现里硬塞默认 `cfworker_api_url`
- ❌ 在 endpoint 层用 `try: ... except RuntimeError: HTTPException(4xx)` 兜底（这是症状治疗，不是架构修复）
- ❌ 在 endpoint 重复 try/except；用 FastAPI `app.add_exception_handler` 统一处理

**必须**：
- ✅ 业务异常分层：`MailboxServiceError` 不继承 `RuntimeError`；具体子类 `ProviderConfigIncompleteError` / `ProviderUpstreamError`
- ✅ provider 实现的 `_ensure_*_config` / `_ensure_api_configured` 抛业务异常（带 `missing_fields`），不抛裸 `RuntimeError`
- ✅ `acquire_session` 进 provider 实现前消费 `PROVIDER_SESSION_METADATA[*]["required_fields_by_mode"]` 做前置校验
- ✅ FastAPI exception handlers 把 `ProviderConfigIncompleteError` → 422、`ProviderUpstreamError` → 424、`MailboxServiceError` → 400

## 8. 新 mail provider 接入 checklist

实现一个新 mail provider 时：

- [ ] 服务端：实现 `_ensure_*_config()` 抛 `ProviderConfigIncompleteError(missing_fields=[...])`
- [ ] 服务端：调上游收到 4xx 抛 `ProviderUpstreamError(upstream_status=...)`
- [ ] 服务端：在 `PROVIDER_SESSION_METADATA` 声明 `required_fields_by_mode`
- [ ] 客户端：如果 managed 模式有必填字段，加到 `_MAIL_MANAGED_REQUIRED_FIELDS` / `_PROVIDERS_REQUIRING_CONFIG_NAME` / `_MAIL_PROVIDERS_REQUIRING_CONFIG_NAME` 三处
- [ ] 文档：更新本文件的 §4 表格
- [ ] 测试：`tests/test_mail_config_failfast.py` + `tests/test_mail_provider_errors.py` 加覆盖

## 9. 验证

```bash
# 客户端：单元测试钉死分类
python -m pytest tests/test_mail_provider_errors.py tests/test_mail_config_failfast.py -v

# 客户端：实际探活（需要远程服务运行）
python scripts/diagnose_warmup.py --probe-providers

# 服务端：curl contract 测试（在 SSH agent 改完后跑）
curl -sX POST -H 'Authorization: Bearer $API_KEY' -H 'Content-Type: application/json' \
  -d '{"provider":"cfworker","purpose":"diag","lease_seconds":60,"session_mode":"managed"}' \
  https://email.feixingqi.shop/api/mailbox-service/managed-sessions
# 期望：HTTP 422 + {"detail":{"code":"PROVIDER_NOT_CONFIGURED","missing_fields":["config_name"]}}
```

## 10. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-04-28 | 初版：定义 PROVIDER_NOT_CONFIGURED / PROVIDER_UPSTREAM_ERROR contract，4 层 fail-fast，6 个失败分类规则 |
