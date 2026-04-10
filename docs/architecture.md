# 架构与模块调用关系

## 主流程调用图

```mermaid
flowchart TD
    A[main.py] --> B[load_config / AppConfig.validate]
    B --> C[EfunCard]
    B --> D[SMSManager]
    B --> E[MailManager]
    B --> F[run_task]
    F --> G[get_browser_ws / AdsPower]
    F --> H[Playwright page state machine]
    H --> E
    E --> I[email-provider/core.base_mailbox]
    H --> J[/api/auth/session 提取 token]
    J --> K[export_success -> accounts.csv]
    J --> L[PaymentLinkGenerator]
    L --> M[chatgpt.com/backend-api/payments/checkout]
    F --> C
    C --> N[Efuncard query / redeem / 3DS verify]
    F --> D
    D --> O[SMS-Activate]
```

## 模块职责

| 模块 | 职责 | 关键输出 |
| --- | --- | --- |
| `main.py` | 装配配置与依赖，触发主流程 | 运行任务 |
| `src/config.py` | 读取 `.env`、校验必填项 | `AppConfig` |
| `src/browser.py` | 获取代理、启动 AdsPower | CDP WebSocket |
| `src/mail.py` | 复用本地 `email-provider` 获取验证码 | 邮件验证码 |
| `src/sms.py` | 获取手机号与短信验证码 | `SMSOrder` / code |
| `src/efuncard.py` | 查询已激活卡、必要时激活 CDK、轮询 3DS | `CardInfo` / OTP |
| `src/payment_link.py` | 基于 Access Token 获取 Team/Plus 支付链接，并支持 app/hosted checkout 形态 | checkout URL |
| `src/models.py` | 数据结构建模 | dataclass |
| `src/utils.py` | 日志与随机等待 | logger / delay |

## 目录层次建议

- `src/`：只放当前主流程使用的模块化代码。
- `tests/`：只放当前主项目单元测试。
- `scripts/`：手动排障脚本，不参与自动测试收集。
- `legacy/`：单文件原型、旧说明、兼容入口映射。
- `email-provider/`：作为内嵌子项目独立维护，不纳入根项目 pytest 默认收集范围。
