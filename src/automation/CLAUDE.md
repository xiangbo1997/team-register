# src/automation 模块文档

[根目录](../../CLAUDE.md) > [src](../) > **automation**

## 模块职责

自动化注册流程的状态机核心，包括状态推断、规则决策、LLM 兜底决策、证据采集与经验存储。

## 入口与启动

- 通过 `src/automation/__init__.py` 统一导出
- 主流程在 `main.py` 中实例化 `RegistrationStateMachine` 和 `AutomationRuntime`

## 对外接口

### 核心类

| 类名 | 文件 | 职责 |
|------|------|------|
| `RegistrationStateMachine` | `runtime.py` | 规则引擎，基于 URL+DOM 信号驱动状态转移 |
| `AutomationRuntime` | `runtime.py` | 运行时上下文 dataclass（page/config/handlers 等） |
| `EvidenceCollector` | `runtime.py` | 从页面采集结构化证据（URL/信号/可操作元素） |
| `RuleDecisionProvider` | `runtime.py` | 基于规则的决策提供者 |
| `LLMDecisionProvider` | `llm.py` | LLM 兜底决策，仅在规则无法决策时介入 |
| `OpenAICompatibleLLMClient` | `llm.py` | OpenAI 兼容接口的 HTTP 客户端 |
| `ArtifactRecorder` | `artifacts.py` | 每步操作的证据包落盘（JSON + 截图） |
| `ExperienceStore` | `experience.py` | 持久化"页面特征 -> 成功动作"映射 |
| `SentinelProvider` / `NoOpSentinelProvider` / `PurePythonSentinelProvider` | `sentinel.py` | 旁路 API 反爬 PoW 框架（FNV-1a brute-force，默认 noop）。详见 `docs/research/anti-bot-borrowing-vs-pow.md` |
| `BrowserBorrower` / `BorrowSnapshot` | `browser_borrow.py` | 从 AdsPower 浏览器借 header + cookie 给旁路 curl_cffi 调用（实测主路径） |
| `SolverProvider` / `NoOpSolver` / `ManualFallbackSolver` / `NoCaptchaSolver` | `captcha_solver.py` | Turnstile 自愈框架（默认 noop，可接 nocaptcha.io / 人工接管） |
| `Triage*` | `triage.py` | 决策前置的页面分类（HOME / VERIFY / BLOCKED 等） |

### 核心函数

| 函数 | 文件 | 职责 |
|------|------|------|
| `infer_state(url, signals)` | `runtime.py` | 根据 URL 和 DOM 信号推断 `AutomationState` |
| `extract_session_tokens_with_http()` | `runtime.py` | 通过 HTTP 提取 ChatGPT session token |
| `build_llm_evidence_payload()` | `artifacts.py` | 构建脱敏后的 LLM 请求 payload |
| `sanitize_url()` | `artifacts.py` | URL 脱敏（移除敏感 query 参数） |

### 数据模型 (`models.py`)

| 模型 | 类型 | 说明 |
|------|------|------|
| `AutomationState` | Enum | 10 个状态：ENTRY/AUTH/VERIFY_EMAIL/ABOUT_YOU/HOME/PHONE/PAYMENT/ERROR/BLOCKED/UNKNOWN |
| `ActionKind` | Enum | 动作类型：click/fill/press/select/wait/refresh/back/new_tab/close_tab/clear_target_storage/reconnect_profile |
| `DecisionKind` | Enum | 决策类型：choose_action/request_evidence/abort |
| `Action` | dataclass | 执行器消费的动作对象 |
| `Evidence` | dataclass | 状态识别/决策/审计共用的证据包 |
| `Decision` | dataclass | 规则/LLM 统一决策协议 |
| `MachineResult` | dataclass | 状态机运行结果 |

## 关键依赖与配置

- `curl_cffi`：用于 session token 提取（绕过 TLS 指纹检测）
- `requests`：LLM API 调用
- 配置通过 `AppConfig` 注入（LLM 相关：`llm_enabled`/`llm_base_url`/`llm_api_key`/`llm_model`）

## 数据模型

- 证据包（`Evidence`）：URL + 页面标题 + 状态候选 + 可操作元素 + 信号字典
- 脱敏规则（`artifacts.py`）：邮箱/卡号/手机号/验证码/Bearer token/代理凭证/WebSocket URL 均自动掩码
- 经验存储（`experience.py`）：按 URL 路径 + 信号签名匹配历史成功动作

## 测试与质量

- 当前无独立测试文件（状态机逻辑通过集成测试间接覆盖）
- 建议补充：`infer_state()` 的单元测试、`ArtifactRecorder` 脱敏测试

## 相关文件清单

```
src/automation/
  __init__.py        # 统一导出
  models.py          # 数据模型与枚举
  runtime.py         # 状态机、规则引擎、证据采集
  llm.py             # LLM 决策客户端
  artifacts.py       # 证据录制与脱敏
  experience.py      # 经验存储
  triage.py          # 决策前置的页面分类
  sentinel.py        # PoW 反爬框架（446 行，默认 noop；feat/sentinel-pow 2026-05-20 引入）
  browser_borrow.py  # 借浏览器 header/cookie（261 行；feat/sentinel-pow 2026-05-20 引入）
  captcha_solver.py  # Turnstile 自愈框架（NoOp/ManualFallback/NoCaptcha）
```

## 反爬子系统索引

新增于 2026-05 月度的反爬框架（旁路 API 加固），三者关系详见 `docs/research/anti-bot-borrowing-vs-pow.md`：

| 模块 | 角色 | 当前状态 |
|---|---|---|
| `sentinel.py` | PoW 法（推测路线，fallback） | API 备好，noop 默认；实测算法在 2026-05 主流场景已失效 |
| `browser_borrow.py` | 借用法（实测主路径） | API 备好，业务层**未接入**（等 Phase B 验证数据） |
| `captcha_solver.py` | Turnstile 自愈 | 框架备好，未接真实第三方 solver |

最新风控变化跟踪：`docs/research/openai-risk-control-timeline-2026-05.md`

## 变更记录 (Changelog)

| 日期 | 变更内容 | 执行者 |
|------|---------|--------|
| 2026-04-11 | 初始创建模块文档 | Claude Code |
| 2026-05-22 | 补全反爬子系统索引：sentinel/browser_borrow/captcha_solver/triage 加入"核心类"和"文件清单"；新增"反爬子系统索引"小节联动 anti-bot-borrowing-vs-pow.md + openai-risk-control-timeline-2026-05.md | Claude Code |
