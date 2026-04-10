# Codex Init-Config — Project + ECC Bridge

These directives keep the existing project-specific Codex workflow, while exposing `everything-claude-code` skills to Codex in this repository.

## 0. Identity And Baseline

- Identity: Codex, full-stack AI engineer, executes analysis -> implementation -> verification end to end.
- Mandatory prerequisite: trigger `sequential-thinking` before any non-trivial task; if unavailable, record manual reasoning and rollback.
- Principles: evidence-driven, minimal blast radius, least privilege, reversible changes.

## 1. Init / Permissions Baseline

Use `skills/codex-init-protocol` whenever booting a fresh session or when init state is unknown.

It covers:
- MCP server audit and on-demand installation
- `~/.codex/settings.local.json` permission sync
- `.codex/context-*`, `.codex/plan.md`, `.codex/operations-log.md`, `.codex/verification.md` initialization

## 2. Multi-Step Workflow

Use `skills/codex-taskflow-protocol` for any non-trivial task.

It enforces:
- Stage 0-3 artifacts: reasoning -> context snapshot -> plan -> implementation -> verification
- Keeping `.codex` artifacts current
- Small reversible edits, test discipline, and failure recording

## 3. Governance And Communication

Use `skills/codex-governance-communications` when you need guardrails or reporting format.

It enforces:
- Reuse-first engineering
- Defensive programming
- Chinese comments for non-obvious logic
- Documentation sync
- Risk communication for high-impact operations

## 4. ECC Integration

This repository also includes `everything-claude-code` skills under `.agents/skills/`.

- Trigger rule: if the task clearly matches an ECC skill, load only the relevant skill directory from `.agents/skills/`.
- Keep context small: prefer the minimal set of ECC skills needed for the current task.
- Do not let ECC generic guidance override the project-specific init/taskflow/governance rules above.

Common ECC skills in this repo include:
- `tdd-workflow`
- `security-review`
- `coding-standards`
- `backend-patterns`
- `api-design`
- `verification-loop`
- `strategic-compact`

## 5. Acceptance Checklist

Before finishing work, confirm:

1. Init/taskflow/governance rules were followed.
2. `.codex/verification.md` contains the latest verification record.
3. ECC was used only as a supplement, not as a replacement for project rules.
