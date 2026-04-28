# Team Register 控制台使用手册

## 1. 项目概览与入口

当前仓库同时包含两条主线：

- `main.py`：自动化注册/支付主流程
- `src/api/app.py`：FastAPI 控制台

控制台默认入口：

```bash
uvicorn src.api.app:app --reload --port 8080
```

页面入口：

- `/login`：登录页
- `/`：概览页
- `/tasks`：任务页
- `/config`：运行时配置页
- `/providers`：Provider 管理页
- `/mail-accounts`：邮件账号管理页
- `/help`：帮助中心
- `/manual`：本手册 HTML 版本

## 2. 登录与访问控制

控制台使用服务端 session cookie，不在前端持久化明文 token。

默认管理员账号仅用于本地开发，建议通过环境变量覆盖：

- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `SESSION_SECRET`

建议角色边界：

- `viewer`：只读页面、只读 API、文档问答
- `operator`：在 viewer 基础上允许任务创建 / 重试 / 取消
- `admin`：在 operator 基础上允许 provider/config 变更、助手 preview/commit、revision/rollback

## 3. 控制台页面导航

### 概览页

用于查看统计数据、快速入口和最近任务。

### 任务页

用于查看任务列表、任务详情、SSE 事件流，以及有限的任务控制动作。

### 配置页

当前仅允许在线修改**白名单运行时字段**。这些修改会持久化到数据库，重启控制台后仍然生效。

与邮件验证码链路直接相关的字段（如 `EMAIL_PROVIDER_BASE_URL`、`EMAIL_PROVIDER_API_KEY`）也在这里统一维护。当前 `team-register` 已改成 **latest-only**：如果 `127.0.0.1:8000` 上的 `email-provider` 不是最新运行态，任务会在启动前直接失败，不再回退旧 `/sessions`。

### Provider 页

用于查看、创建、更新、删除 browser / card / mail 三类 Provider 配置。页面/API 返回给前端的 Provider 配置会先做脱敏。

### Mail Accounts 页

用于维护 credentialed 邮箱账号池（如 AppleMail）。每条记录包含邮箱、`client_id`、`refresh_token` 与额外 JSON；页面显示脱敏值，任务创建时可直接选择。

当任务选择了 `applemail` 且命中某条 Mail Account 记录时，注册流程会优先走 `credentialed` 模式。注意：任务表单里的邮箱必须与所选 Mail Account 的邮箱完全一致；系统现在会拒绝“邮箱 A + 邮箱 B 的 client_id/refresh_token”这种错绑组合。任务开始前还会先检查 `/health`、`/providers` 和 provider 的 `supported_session_modes`；若 live `email-provider` 不支持 `credentialed`、缺少 `/credentialed-sessions`，或返回 5xx，任务会直接失败，不再进入验证码页反复人工接管。

## 4. Help 页怎么用

`/help` 是控制台内的帮助中心，作用是：

- 说明登录流程
- 说明单助手的问答方式
- 解释 preview/commit 的差异
- 给出推荐提问方式
- 内嵌本手册内容，便于快速查阅

如果你只想看完整手册，可以直接打开 `/manual`。

## 5. 单助手怎么问

控制台右下角只暴露**一个 AI 助手**。这个助手默认具备：

- 检索使用手册
- 检索安全全库代码知识
- 回答“当前代码真实如何实现”
- 在允许范围内发起 preview

推荐提问方式：

- “当前 provider 配置页真实是怎么实现的？”
- “登录后哪些页面必须认证？”
- “帮我 preview 一个 browser provider：名称 adspower-main，先不要启用。”
- “把 payment_plan 改成 team，先给我看 preview。”

回答会并列返回两类引用：

- `manual_citations[]`
- `repo_citations[]`

## 6. 助手如何发起 Preview

涉及写操作时，助手不会直接执行，而是先进入 preview。

当前白名单动作：

- `upsert_provider`
- `update_config`

Preview 阶段会做这些事情：

1. 检查动作类型是否在白名单
2. 收集缺失字段
3. 通过隐藏审核逻辑做规则审计
4. 返回摘要、diff、warnings
5. 生成 `preview_id`

如果缺少字段，会返回 `required_fields[]`，你需要补齐后再次发起 preview。

如果消息包含高危语义（例如 token / session / cookie 导出、支付 / 绑卡 / 3DS、删除 / 清空），助手会直接回退普通问答，不自动推断动作。

如果同一句同时命中多个动作语义（例如既改 provider 又改 config），助手也会回退普通问答，不自动推断。

同一套隐藏审核链也覆盖 admin 直接调用的 config/providers 写接口，例如：

- `PUT /api/config`
- `POST /api/config/reload`
- `PUT /api/providers/{type}/{name}`
- `DELETE /api/providers/{type}/{name}`
- `POST /api/providers/{type}/{name}/test`
- `POST /api/providers/{type}/{name}/revisions/{id}/rollback`

## 7. 助手如何 Commit

Commit 只能基于 `preview_id` 执行，不接受自然语言直接提交。

标准流程：

1. 提问或描述操作目标
2. 获取 preview
3. 检查 diff / warnings / 引用
4. 点 Commit
5. 查看结果与 action 记录

Commit 后会产生审计记录：

- `AssistantActionLog`
- `ProviderConfigRevision`（Provider 相关变更）

## 8. 权限边界与人工确认

以下动作当前只允许解释、展示或 preview，不允许直接 commit：

- 注册自动化主流程
- 浏览器 / CDP 操作
- token 提取
- 支付 / 绑卡 / 3DS
- 删除不可恢复业务数据

当前助手知识库明确排除：

- `.env*`
- `.db` / `.sqlite` / `.sqlite3`
- `artifacts/`
- 截图 / 图片
- 缓存目录
- `__pycache__`
- `.venv`

## 9. “新增卡片 Provider”完整示例

### 示例目标

新增一个 card provider，但先不启用。

### 推荐操作

1. 在助手中输入：

```text
帮我 preview 一个 card provider，名字 efuncard-main，先不要启用
```

2. 如果助手提示缺失字段，则补充：

- `provider_type`
- `provider_name`
- `config`
- `is_active`

3. 查看 preview：

- 是否命中白名单动作
- 是否只展示脱敏后的配置
- diff 是否符合预期

4. 确认后再 Commit。

## 10. 常见失败与排查

### 401 / 需要登录

- 检查是否已经通过 `/api/auth/login` 建立 session
- 检查 session 是否过期

### 助手后端未就绪

- 优先打开 `/help` 或 `/manual`
- 检查 `/api/assistant/bootstrap`

### Preview 被拒绝

- 检查动作是否在白名单
- 检查字段是否完整
- 检查是否使用了非白名单 config key

### Commit 失败

- 确认当前账号角色是否足够
- 确认 preview 没有过期或被重复消费
- 确认 provider/config diff 没有冲突

### 邮箱验证码阶段卡住 / `credentialed-sessions` 404

- 先确认 `127.0.0.1:8000` 对应的就是最新 `email-provider` 进程；若不确定，直接到 `email-provider` 项目目录重启本地 8000 服务，并确保加载的是最新源码和 `.env`
- 再检查 `GET /api/mailbox-service/health` 与 `GET /api/mailbox-service/providers`：`applemail` 必须声明 `supported_session_modes` 且包含 `credentialed`
- 再运行 `python scripts/manual_mail_check.py`，确认 `EMAIL_PROVIDER_BASE_URL`、`EMAIL_PROVIDER_API_KEY`、`MAIL_CLIENT_ID`、`MAIL_REFRESH_TOKEN` 与 `TEST_EMAIL` 都已配置；脚本会先做 runtime preflight，再尝试拉码
- 若 runtime preflight 和手动检查都通过，再回到任务页重试注册；若仍失败，才继续检查验证码是否落在 `INBOX` 或 `Junk`

### 为什么回答没有引用

- 可能当前关键词没有命中本地索引
- 可以换成更具体的页面、文件名、函数名或动作名
