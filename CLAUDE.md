# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Python 自动化项目，通过 Playwright + AdsPower 反检测浏览器完成 OpenAI 账号注册/支付流程。核心架构是**规则优先状态机 + LLM 兜底决策**。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt
playwright install chromium

# 运行主流程
python main.py

# 测试（pytest.ini 限定只收集 tests/，不会碰 email-provider/tests/）
python -m pytest -q
python -m pytest tests/test_sms.py -v          # 单个测试文件
python -m pytest tests/test_sms.py::TestClass::test_method  # 单个用例
```

## 架构

### 主流程 (`main.py`)

入口文件（~1400行），组装所有模块并驱动注册/支付流程。包含 CSS 选择器常量、`export_success()` 导出 CSV、以及完整的自动化编排逻辑。

### 核心模块 (`src/`)

- **`config.py`** — `AppConfig` dataclass，从 `.env` 加载配置并校验必填项
- **`browser.py`** — AdsPower CDP 连接 + 代理提取 + preflight 检查
- **`automation/`** — 状态机核心：
  - `models.py` — 状态枚举 `AutomationState`、`Action`/`Decision`/`Evidence` 等数据模型
  - `runtime.py` — `RegistrationStateMachine`（规则引擎）、`AutomationRuntime`（执行器）、`EvidenceCollector`、`RuleDecisionProvider`、`infer_state()`、`extract_session_tokens_with_http()`
  - `llm.py` — `OpenAICompatibleLLMClient` + `LLMDecisionProvider`，仅在规则无法决策时介入
  - `artifacts.py` — `ArtifactRecorder` 记录 step 级证据包
  - `experience.py` — `ExperienceStore` 经验存储
- **`sms.py`** — SMS-Activate 接码（获取手机号/验证码）
- **`mail.py`** — 调用本地 `email-provider` 服务获取邮箱验证码
- **`efuncard.py`** — Efuncard 虚拟信用卡管理（查询/激活/3DS轮询），支持1小时内已激活卡复用
- **`payment_link.py`** — 基于 Access Token 生成支付短链
- **`models.py` / `utils.py`** — 数据模型与工具函数

### 子项目 (`email-provider/`)

独立维护的邮件服务模块，有自己的 `main.py`、`tests/`、`services/`。核心是 LuckMail 客户端（`core/luckmail/`）。测试独立运行：
```bash
cd email-provider && python -m pytest
```

### 其他目录

- `legacy/` — 已归档的单文件原型（`gpt_automation.py` 旧实现）
- `scripts/` — 手动排障脚本
- `artifacts/` — 运行时证据包输出（`RUN_ARTIFACTS_DIR`）
- 根目录 `gpt_automation.py` — 兼容入口，已退化

## 关键设计决策

- **状态机优先于 LLM**：`RuleDecisionProvider` 基于 URL + DOM 结构推进流程，`LLMDecisionProvider` 仅在状态歧义/连续失败时从候选动作中选择
- **证据包机制**：每步操作记录到 `artifacts/runs/` 下，便于回放排障
- **可恢复执行**：支持 preflight、clean-start、错误页恢复和人工接管等待
- **外部服务依赖**：AdsPower（浏览器指纹）、SMS-Activate（手机号）、Efuncard（虚拟卡）、LuckMail（邮箱）

## 环境变量

参见 `.env.example`。关键分组：
- AdsPower 连接：`ADS_API`, `ADS_API_KEY`
- 接码/邮件：`SMS_API_KEY`, `MAIL_REFRESH_TOKEN`, `MAIL_CLIENT_ID`
- LLM 兜底：`LLM_ENABLED`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
- 支付控制：`ENABLE_PAYMENT_FLOW`, `PAYMENT_PLAN`, `PAYMENT_LINK_ONLY`
- 运行时阈值：`MAX_NAVIGATION_RETRIES`, `MAX_EMAIL_ATTEMPTS` 等

## 代码风格

- Python 3.11+，使用 dataclass 和 type hints
- 中文日志和注释
- 模块间通过 `src/__init__.py` 和 `src/automation/__init__.py` 显式导出
