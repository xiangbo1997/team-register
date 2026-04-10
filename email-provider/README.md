# email-provider

可复用的邮箱接收服务，抽离自 `any-auto-register`，目标是把“邮箱分配 + 验证码轮询 + 成功/失败回收”收口成一个固定接口，供多个项目复用。

当前仓库包含：

- 统一邮箱 provider 适配层
- 会话/租约式邮箱服务核心
- FastAPI 路由
- AppleMail 诊断工具
- 基础回归测试

推荐阅读顺序：

- [AGENTS.md](AGENTS.md)：给 agent 的快速上手说明
- [docs/INTEGRATION.md](docs/INTEGRATION.md)：详细对接文档和接口契约

快速启动：

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

默认接口：

- `GET /`
- `GET /api/mailbox-service/health`
- `GET /api/mailbox-service/providers`
- `POST /api/mailbox-service/providers/{provider}/validate-config`
- `POST /api/mailbox-service/sessions`
- `GET /api/mailbox-service/sessions/{id}`
- `POST /api/mailbox-service/sessions/{id}/poll-code`
- `POST /api/mailbox-service/sessions/{id}/complete`

默认数据库：

- `MAILBOX_SERVICE_DATABASE_URL` 未设置时，使用当前目录下的 `sqlite:///mailbox_service.db`

回归测试：

```bash
python -m unittest \
  tests.test_mailbox_service \
  tests.test_applemail_mailbox \
  tests.test_applemail_diagnostics
```
