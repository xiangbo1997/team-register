# AGENTS.md

## Purpose

这个仓库提供一个固定的邮箱接入层，主要解决三件事：

1. 分配邮箱或接管一个已知邮箱账号。
2. 轮询验证码，并把 provider 差异收口成统一结果。
3. 在成功/失败后回收会话，避免调用方直接操作 provider 细节。

## Read First

开始改动或接入前，按这个顺序看：

1. `README.md`
2. `docs/INTEGRATION.md`
3. `api/mailbox_service.py`
4. `services/mailbox_service.py`
5. `core/base_mailbox.py`

## Stable Integration Surface

当前推荐把这个仓库当成 HTTP 服务来接，不要让业务项目直接依赖各个 provider 类。

固定 API 路径：

- `GET /api/mailbox-service/health`
- `GET /api/mailbox-service/providers`
- `POST /api/mailbox-service/providers/{provider}/validate-config`
- `POST /api/mailbox-service/sessions`
- `GET /api/mailbox-service/sessions/{session_id}`
- `POST /api/mailbox-service/sessions/{session_id}/poll-code`
- `POST /api/mailbox-service/sessions/{session_id}/complete`

如果你必须在 Python 进程内复用旧调用面，再看 `core/base_mailbox.py` 里的：

- `MailboxServiceBackedMailbox`
- `create_mailbox()`

## Required Flow

标准 OTP 接入流程必须是：

1. `POST /api/mailbox-service/sessions` 创建租约。
2. 保存 `session_id` 和 `lease_token`。
3. 用返回的 `email` 触发上游平台发送验证码。
4. `POST /api/mailbox-service/sessions/{id}/poll-code` 轮询验证码。
5. 任务结束后无论成功失败都调用 `POST /api/mailbox-service/sessions/{id}/complete`。

不要跳过 `complete`。成功路径里的 provider 清理逻辑依赖它。

## Two Session Modes

`POST /api/mailbox-service/sessions` 有两种模式：

1. 新分配邮箱。
   不传 `email`，服务会调用 provider 的 `get_email()`。
2. 接管已知邮箱。
   传 `email`，必要时传 `account_id` 和 `account_extra`。这用于“第二阶段二次取码”或“业务端已知邮箱上下文”的场景。

第二种模式是给 Kiro/LuckMail 这类已知邮箱场景准备的，避免主项目重复实现 provider 特判。

## Important Semantics

- `session_id` 是服务层会话 id。
- `lease_token` 是会话操作凭证。
- `before_ids` 是创建会话时抓取的邮件快照，用来过滤旧邮件。
- `provider_meta` 是向上透出的 provider 补充信息，常见字段包括：
  - `mailbox_token`
  - `mailbox_order_no`
  - `allocated_email`
  - `source_email`

在兼容模式下，`MailboxServiceBackedMailbox.get_email()` 返回的 `account_id` 是 `session_id`，不是 provider 原始账号 id。不要把它当成 provider token 使用。

## Security Constraints

- 当前 HTTP API 没有内建鉴权，不要直接暴露到公网。
- provider 配置会写入会话表，数据库里可能包含上游 token、账号、代理等敏感信息。
- 推荐部署在内网，或放到带鉴权的 API 网关后面。

## Deployment Recommendation

当前更适合作为单实例内部服务使用。若要多实例部署，请先补充鉴权、共享数据库策略和并发租约约束。

## Where To Change What

- 改 HTTP 契约：`api/mailbox_service.py`
- 改会话状态机、持久化、错误码：`services/mailbox_service.py`
- 改 provider 实现和兼容层：`core/base_mailbox.py`
- 改 AppleMail 诊断能力：`core/applemail_diagnostics.py`、`scripts/applemail_diagnose.py`
- 改回归测试：`tests/`

## Minimum Validation After Changes

至少跑这些：

```bash
python -m unittest \
  tests.test_mailbox_service \
  tests.test_applemail_mailbox \
  tests.test_applemail_diagnostics
```
