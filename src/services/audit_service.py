# -*- coding: utf-8 -*-
"""隐藏审核 agent：对助手 preview 做规则化审计。"""

from __future__ import annotations

from typing import Any

from src.services.config_service import ConfigService


class AuditService:
    """当前版本使用本地规则审计，拒绝高风险或越权动作。"""

    SAFE_CONFIG_KEYS = set(ConfigService.SAFE_UPDATE_FIELDS)
    ALLOWED_PROVIDER_TYPES = {"browser", "card", "mail", "sms", "openai"}

    def review(self, plan: dict[str, Any]) -> dict[str, Any]:
        action_type = str(plan.get("action_type", "") or "")
        payload = plan.get("payload", {}) or {}

        if action_type == "upsert_provider":
            target_check = self._review_provider_target(payload)
            if target_check:
                return target_check
            if not isinstance(payload.get("config"), dict):
                return {"status": "DENY", "reason": "provider 配置必须是对象"}
            return {"status": "ALLOW", "reason": "provider 配置属于白名单动作"}

        if action_type == "update_config":
            updates = payload.get("updates", {}) or {}
            if not isinstance(updates, dict) or not updates:
                return {"status": "NEEDS_INFO", "reason": "未提供要修改的配置"}
            forbidden = sorted([key for key in updates.keys() if key not in self.SAFE_CONFIG_KEYS])
            if forbidden:
                return {"status": "DENY", "reason": f"存在非白名单配置字段: {', '.join(forbidden)}"}
            return {"status": "ALLOW", "reason": "运行时配置修改属于白名单动作"}

        if action_type == "reload_config":
            return {"status": "ALLOW", "reason": "基础配置重载属于管理员白名单动作"}

        if action_type == "delete_provider":
            target_check = self._review_provider_target(payload)
            if target_check:
                return target_check
            return {"status": "ALLOW", "reason": "provider 删除属于可审计可回滚动作"}

        if action_type == "rollback_provider":
            target_check = self._review_provider_target(payload)
            if target_check:
                return target_check
            if not payload.get("revision_id"):
                return {"status": "NEEDS_INFO", "reason": "缺少 revision_id"}
            return {"status": "ALLOW", "reason": "provider rollback 属于可审计可回滚动作"}

        if action_type == "test_provider":
            target_check = self._review_provider_target(payload)
            if target_check:
                return target_check
            return {"status": "ALLOW", "reason": "provider 测试属于白名单动作"}

        if action_type in {"create_mail_account", "update_mail_account", "delete_mail_account", "test_mail_account"}:
            if action_type != "delete_mail_account" and action_type != "test_mail_account":
                provider_name = str(payload.get("provider_name") or "").strip().lower()
                email = str(payload.get("email") or "").strip().lower()
                if not provider_name or not email:
                    return {"status": "NEEDS_INFO", "reason": "邮箱账号基本信息不完整"}
            if action_type in {"delete_mail_account", "test_mail_account"} and not str(payload.get("account_id") or "").strip():
                return {"status": "NEEDS_INFO", "reason": "缺少 account_id"}
            return {"status": "ALLOW", "reason": "邮箱账号管理属于白名单动作"}

        return {"status": "DENY", "reason": f"不允许的动作类型: {action_type}"}

    def _review_provider_target(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        provider_type = str(payload.get("provider_type") or "").strip().lower()
        provider_name = str(payload.get("provider_name") or "").strip()
        if not provider_type or not provider_name:
            return {"status": "NEEDS_INFO", "reason": "provider 基本信息不完整"}
        if provider_type not in self.ALLOWED_PROVIDER_TYPES:
            return {"status": "DENY", "reason": f"不允许的 provider_type: {provider_type}"}
        return None
