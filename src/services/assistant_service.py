# -*- coding: utf-8 -*-
"""单助手编排服务：文档问答、preview 与 commit。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from src.automation.artifacts import redact_structure
from src.db.engine import get_session
from src.db.models import AssistantActionLog, User
from src.services.audit_service import AuditService
from src.services.config_service import ConfigService
from src.services.knowledge_service import KnowledgeHit, KnowledgeService


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _role_allows(user_role: str, required_role: str) -> bool:
    role_levels = {"viewer": 1, "operator": 2, "admin": 3}
    return role_levels.get((user_role or "").strip().lower(), 0) >= role_levels.get(required_role, 999)


class AssistantService:
    """把知识检索、preview、审核与 commit 串成一个单助手接口。"""

    _ALLOWED_PROVIDER_TYPES = set(AuditService.ALLOWED_PROVIDER_TYPES)
    _ACTION_INTENT_KEYWORDS = (
        "新增",
        "新建",
        "添加",
        "创建",
        "更新",
        "修改",
        "变更",
        "改成",
        "设为",
        "设置",
        "启用",
        "开启",
        "关闭",
        "禁用",
        "upsert",
        "update",
        "set",
        "enable",
        "disable",
    )
    _HIGH_RISK_PATTERNS = (
        r"\b(delete|drop|truncate|remove|erase|destroy)\b",
        r"删除|清空|销毁|抹除|移除",
        r"\b3ds?\b|3-d\s*secure|三维验证|三方验证",
        r"绑卡|银行卡|信用卡|卡号|cvv|cvc|expiry|expiration",
        r"\b(access|refresh)\s*token\b",
        r"\btoken\b",
        r"(?:导出|泄露|窃取|倒出|dump|export|exfiltrate|steal).{0,10}(?:session|cookie|token|会话)",
        r"(?:session|cookie|会话).{0,10}(?:导出|泄露|窃取|dump|export|exfiltrate|steal)",
        r"支付\s*(?:凭据|密钥|令牌|卡|信息|3ds)",
        r"payment\s*(?:credential|secret|card|3ds)",
    )
    _RESERVED_CONFIG_KEYS = {
        "provider_type",
        "provider_name",
        "action_type",
        "intent_mode",
        "name",
        "named",
    }

    def __init__(
        self,
        *,
        config_service: ConfigService,
        knowledge_service: KnowledgeService,
        audit_service: AuditService,
    ) -> None:
        self._config_service = config_service
        self._knowledge_service = knowledge_service
        self._audit_service = audit_service

    def get_bootstrap(self, user: User) -> dict[str, Any]:
        return {
            "assistant_enabled": True,
            "assistant_backend_ready": True,
            "user": {
                "id": user.id,
                "username": user.username,
                "role": user.role,
            },
            "capabilities": {
                "doc_qa": True,
                "repo_knowledge": True,
                "preview_actions": ["upsert_provider", "update_config"],
                "commit_requires_confirmation": True,
            },
            "index_version": self._knowledge_service.index_version,
            "manual_html": self._knowledge_service.manual_html,
        }

    def chat(
        self,
        *,
        user: User,
        message: str,
        intent_mode: str = "auto",
        page_context: Optional[dict[str, Any]] = None,
        draft_action: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        text = (message or "").strip()
        if draft_action:
            return self.preview_action(
                user=user,
                message=text or str(draft_action.get("title") or "助手动作预览"),
                draft_action=draft_action,
            )
        if intent_mode in {"action", "auto"} and text:
            inferred = self._infer_action_from_message(text, intent_mode=intent_mode)
            if inferred:
                return self.preview_action(
                    user=user,
                    message=text,
                    draft_action=inferred,
                )
        if not text:
            return {
                "mode": "needs_info",
                "answer": "请先描述你想查询的问题，或者提交一个待 preview 的动作。",
                "required_fields": ["message"],
                "manual_citations": [],
                "repo_citations": [],
                "can_commit": False,
            }

        hits = self._knowledge_service.search(text, limit=3)
        manual_citations = [self._serialize_hit(item) for item in hits["manual"]]
        repo_citations = [self._serialize_hit(item) for item in hits["repo"]]
        answer = self._compose_answer(
            message=text,
            manual_hits=hits["manual"],
            repo_hits=hits["repo"],
            page_context=page_context or {},
            intent_mode=intent_mode,
        )
        log = AssistantActionLog(
            user_id=user.id,
            mode="doc_qa",
            intent=(text[:80] or "doc_qa"),
            status="completed",
            title=text[:160] or "文档问答",
            payload={"page_context": page_context or {}},
            result={
                "answer": answer,
                "manual_citations": manual_citations,
                "repo_citations": repo_citations,
            },
        )
        self._save_action_log(log)
        return {
            "mode": "answer",
            "answer": answer,
            "manual_citations": manual_citations,
            "repo_citations": repo_citations,
            "required_fields": [],
            "can_commit": False,
            "action_id": log.id,
        }

    def _infer_action_from_message(self, message: str, *, intent_mode: str = "auto") -> Optional[dict[str, Any]]:
        text = (message or "").strip()
        if not text:
            return None
        if self._contains_high_risk_semantics(text):
            return None
        if intent_mode != "action" and self._looks_like_qa(text):
            return None
        provider_action = self._infer_upsert_provider_from_message(text)
        config_action = self._infer_update_config_from_message(text)
        # 保持“单动作”原则：同时命中两类动作语义时不自动推断，避免误触发。
        if provider_action and config_action:
            return None
        return provider_action or config_action

    def _infer_upsert_provider_from_message(self, message: str) -> Optional[dict[str, Any]]:
        lowered = message.lower()
        has_provider_context = ("provider" in lowered) or ("供应商" in message)
        has_action_verb = any(keyword in lowered or keyword in message for keyword in self._ACTION_INTENT_KEYWORDS)
        if not (has_provider_context and has_action_verb):
            return None

        provider_type = self._extract_provider_type(message)
        provider_name = self._extract_provider_name(message)
        config = self._extract_inline_config(message)
        is_active = self._extract_is_active(message)

        payload: dict[str, Any] = {
            "provider_type": provider_type,
            "provider_name": provider_name,
            "config": config,
            "is_active": is_active,
        }
        return {"action_type": "upsert_provider", "payload": payload}

    def _infer_update_config_from_message(self, message: str) -> Optional[dict[str, Any]]:
        lowered = message.lower()
        has_action_verb = any(keyword in lowered or keyword in message for keyword in self._ACTION_INTENT_KEYWORDS)
        safe_keys = sorted(self._config_service.SAFE_UPDATE_FIELDS)
        has_safe_field = any(key in lowered for key in safe_keys)
        has_config_context = ("config" in lowered) or ("配置" in message)
        if not has_action_verb or not (has_safe_field or has_config_context):
            return None

        updates: dict[str, Any] = {}
        for key in safe_keys:
            value = self._extract_config_value(message, key)
            if value is not None:
                updates[key] = value

        return {
            "action_type": "update_config",
            "payload": {"updates": updates},
        }

    def _extract_provider_type(self, message: str) -> str:
        lowered = message.lower()
        alias_map: list[tuple[str, str]] = [
            ("browser", "browser"),
            ("浏览器", "browser"),
            ("card", "card"),
            ("mail", "mail"),
            ("邮件", "mail"),
            ("邮箱", "mail"),
            ("sms", "sms"),
            ("短信", "sms"),
            ("openai", "openai"),
            ("llm", "openai"),
        ]
        for alias, provider_type in alias_map:
            if alias in lowered or alias in message:
                return provider_type if provider_type in self._ALLOWED_PROVIDER_TYPES else ""
        return ""

    def _extract_provider_name(self, message: str) -> str:
        quoted = re.search(
            r"(?:名字|名称|命名|name|named|called)\s*(?:叫做?|是|为|is|=|:|：)?\s*[\"'“‘「『]([^\"'”’」』]+)[\"'”’」』]",
            message,
            flags=re.IGNORECASE,
        )
        if quoted:
            return quoted.group(1).strip()

        patterns = [
            r"(?:名字|名称|命名)\s*(?:叫做?|是|为|:|：)\s*([A-Za-z0-9._-]+)",
            r"(?:named|name\s+is|called)\s*([A-Za-z0-9._-]+)",
            r"(?:provider)\s*(?:名字|名称|name)?\s*(?:叫做?|是|为|named|called|:|：)\s*([A-Za-z0-9._-]+)",
            r"(?:provider)\s*[\"'“‘「『]([^\"'”’」』]+)[\"'”’」』]",
        ]
        for pattern in patterns:
            matched = re.search(pattern, message, flags=re.IGNORECASE)
            if matched:
                raw = matched.group(1).strip()
                cleaned = re.split(r"\s*(?:,|，|;|；|。|并且|并|且|and)\s*", raw, maxsplit=1)[0].strip()
                if cleaned:
                    return cleaned
        return ""

    def _extract_inline_config(self, message: str) -> dict[str, Any]:
        pairs = re.findall(
            r"\b([a-zA-Z_][a-zA-Z0-9_.-]*)\s*(?:=|为|是|:|：)\s*(?:[\"'“‘「『]([^\"'”’」』]+)[\"'”’」』]|([^\s,，;；。!！?？]+))",
            message,
            flags=re.IGNORECASE,
        )
        config: dict[str, Any] = {}
        for key, quoted_value, bare_value in pairs:
            normalized_key = key.strip().lower()
            if normalized_key in self._RESERVED_CONFIG_KEYS:
                continue
            value = quoted_value if quoted_value else bare_value
            token = (value or "").strip()
            if not token:
                continue
            config[key.strip()] = self._coerce_scalar_token(token.strip().strip("'\"“”‘’"))
        return config

    def _extract_is_active(self, message: str) -> bool:
        lowered = message.lower()
        if any(word in lowered or word in message for word in ("禁用", "停用", "关闭", "inactive", "disable", "false")):
            return False
        return True

    def _extract_config_value(self, message: str, key: str) -> Optional[Any]:
        lowered = message.lower()
        key_lower = key.lower()
        if key_lower not in lowered:
            return None

        value_pattern = r"(?:[\"'“‘「『]([^\"'”’」』]+)[\"'”’」』]|([^\s,，;；。!！?？]+))"
        matched = re.search(
            rf"(?:把|将)?\s*{re.escape(key)}\s*(?:=|改成|设为|设置为|为|是|to|:|：)\s*{value_pattern}",
            message,
            flags=re.IGNORECASE,
        )
        if matched:
            token = (matched.group(1) or matched.group(2) or "").strip().strip("'\"“”‘’")
            if token:
                return self._coerce_scalar_token(token)

        enable_hint = re.search(
            rf"(?:启用|开启|打开|enable|enabled)\s*{re.escape(key)}|{re.escape(key)}\s*(?:启用|开启|打开|enable|enabled)",
            message,
            flags=re.IGNORECASE,
        )
        disable_hint = re.search(
            rf"(?:禁用|停用|关闭|disable|disabled)\s*{re.escape(key)}|{re.escape(key)}\s*(?:禁用|停用|关闭|disable|disabled)",
            message,
            flags=re.IGNORECASE,
        )
        if enable_hint:
            return True
        if disable_hint:
            return False
        return None

    def _contains_high_risk_semantics(self, message: str) -> bool:
        safe_payment_fields = {"payment_plan", "payment_link_only", "payment_link_return_mode", "enable_payment_flow"}
        lowered = message.lower()
        for field in safe_payment_fields:
            lowered = lowered.replace(field, " ")
        for pattern in self._HIGH_RISK_PATTERNS:
            if re.search(pattern, lowered, flags=re.IGNORECASE):
                return True
            if re.search(pattern, message, flags=re.IGNORECASE):
                return True
        return False

    def _coerce_scalar_token(self, token: str) -> Any:
        lowered = token.lower()
        if lowered in {"true", "false"}:
            return lowered == "true"
        if lowered in {"on", "off"}:
            return lowered == "on"
        if re.fullmatch(r"-?\d+", token):
            try:
                return int(token)
            except ValueError:
                return token
        return token

    def _looks_like_qa(self, message: str) -> bool:
        lowered = message.lower()
        if "?" in message or "？" in message:
            return True
        qa_markers = ("如何", "怎么", "请问", "what", "why", "how")
        return any(marker in lowered or marker in message for marker in qa_markers)

    def preview_action(self, *, user: User, message: str, draft_action: dict[str, Any]) -> dict[str, Any]:
        if not _role_allows(user.role, "admin"):
            return {
                "mode": "answer",
                "answer": "当前账号只有查看权限，不能发起配置类 preview。",
                "manual_citations": [],
                "repo_citations": [],
                "required_fields": [],
                "can_commit": False,
            }

        action_type = str(draft_action.get("action_type") or "").strip()
        payload = draft_action.get("payload") or {}
        title = str(draft_action.get("title") or message or action_type or "助手动作预览")[:160]
        missing = self._get_required_fields(action_type, payload)
        if missing:
            log = AssistantActionLog(
                user_id=user.id,
                mode="action",
                intent=(message[:80] or action_type),
                action_type=action_type,
                status="needs_info",
                title=title,
                payload=draft_action,
                result={"required_fields": missing},
            )
            self._save_action_log(log)
            return {
                "mode": "needs_info",
                "answer": f"还缺少这些字段后才能 preview：{', '.join(missing)}",
                "manual_citations": [],
                "repo_citations": [],
                "required_fields": missing,
                "can_commit": False,
                "action_id": log.id,
            }

        audit_plan = {"action_type": action_type, "payload": self._redact_for_audit(action_type, payload)}
        audit = self._audit_service.review(audit_plan)
        if audit.get("status") != "ALLOW":
            status = "needs_info" if audit.get("status") == "NEEDS_INFO" else "denied"
            log = AssistantActionLog(
                user_id=user.id,
                mode="action",
                intent=(message[:80] or action_type),
                action_type=action_type,
                status=status,
                title=title,
                payload=draft_action,
                audit=audit,
                result={"preview_denied": True},
            )
            self._save_action_log(log)
            return {
                "mode": "needs_info" if status == "needs_info" else "answer",
                "answer": str(audit.get("reason") or "该动作未通过审核"),
                "manual_citations": [],
                "repo_citations": [],
                "required_fields": missing if status == "needs_info" else [],
                "can_commit": False,
                "action_id": log.id,
            }

        preview = self._build_preview(action_type, payload)
        log = AssistantActionLog(
            user_id=user.id,
            mode="action",
            intent=(message[:80] or action_type),
            action_type=action_type,
            status="preview_ready",
            title=title,
            payload=draft_action,
            audit={"preview": audit},
            result={"preview": preview},
        )
        self._save_action_log(log)
        return {
            "mode": "preview",
            "answer": preview["summary"],
            "manual_citations": [],
            "repo_citations": [],
            "required_fields": [],
            "preview": preview,
            "preview_id": log.id,
            "action_id": log.id,
            "can_commit": True,
        }

    def commit_action(self, *, user: User, action_id: str) -> dict[str, Any]:
        if not _role_allows(user.role, "admin"):
            raise ValueError("只有 admin 可以提交助手动作")

        log = self._get_action_log(action_id)
        if not log:
            raise ValueError("未找到对应的 preview")
        if log.status != "preview_ready":
            raise ValueError(f"该动作当前状态不是 preview_ready，而是 {log.status}")
        if log.user_id and log.user_id != user.id and not _role_allows(user.role, "admin"):
            raise ValueError("不能提交其他用户的 preview")

        payload = log.payload or {}
        action_type = str(log.action_type or payload.get("action_type") or "").strip()
        action_payload = payload.get("payload") or {}
        audit_plan = {"action_type": action_type, "payload": self._redact_for_audit(action_type, action_payload)}
        audit = self._audit_service.review(audit_plan)
        if audit.get("status") != "ALLOW":
            log.status = "denied"
            log.audit = {**(log.audit or {}), "commit": audit}
            log.updated_at = _utc_now()
            self._save_action_log(log)
            raise ValueError(str(audit.get("reason") or "commit 审核未通过"))

        result = self._execute_action(action_type, action_payload, user=user, action_id=log.id)
        log.status = "completed"
        log.audit = {**(log.audit or {}), "commit": audit}
        log.result = {**(log.result or {}), "commit": result}
        log.updated_at = _utc_now()
        self._save_action_log(log)
        return {
            "ok": True,
            "action_id": log.id,
            "status": log.status,
            "result": result,
        }

    def get_action(self, *, user: User, action_id: str) -> dict[str, Any]:
        log = self._get_action_log(action_id)
        if not log:
            raise ValueError("未找到助手动作")
        if log.user_id and log.user_id != user.id and not _role_allows(user.role, "admin"):
            raise ValueError("无权查看该动作")
        return self._serialize_action_log(log)

    def _execute_action(self, action_type: str, payload: dict[str, Any], *, user: User, action_id: str) -> dict[str, Any]:
        if action_type == "upsert_provider":
            saved = self._config_service.save_provider_config(
                provider_type=str(payload["provider_type"]),
                provider_name=str(payload["provider_name"]),
                config=dict(payload["config"]),
                is_active=bool(payload.get("is_active", True)),
                action_log_id=action_id,
                actor=user.id,
            )
            return {
                "action_type": action_type,
                "provider": {
                    "provider_type": saved.provider_type,
                    "provider_name": saved.provider_name,
                    "is_active": saved.is_active,
                    "config": self._config_service.redact_provider_config(saved.config),
                },
            }
        if action_type == "update_config":
            self._config_service.update_config(
                dict(payload["updates"]),
                allowed_fields=self._config_service.SAFE_UPDATE_FIELDS,
            )
            return {
                "action_type": action_type,
                "config": self._config_service.get_editable_config_snapshot(),
            }
        raise ValueError(f"不支持的动作类型: {action_type}")

    def _build_preview(self, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action_type == "upsert_provider":
            provider_type = str(payload["provider_type"])
            provider_name = str(payload["provider_name"])
            existing = self._config_service.get_provider_config(provider_type, provider_name)
            redacted_new = self._config_service.redact_provider_config(dict(payload["config"]))
            diff: list[dict[str, Any]] = []
            if existing:
                previous = self._config_service.redact_provider_config(existing.config)
                changed_keys = sorted(set(previous.keys()) | set(redacted_new.keys()))
                diff = [
                    {"field": key, "before": previous.get(key), "after": redacted_new.get(key)}
                    for key in changed_keys
                    if previous.get(key) != redacted_new.get(key)
                ]
                summary = f"将更新 {provider_type}/{provider_name}，变更 {len(diff)} 个配置字段。"
            else:
                diff = [{"field": key, "before": None, "after": value} for key, value in redacted_new.items()]
                summary = f"将创建新的 {provider_type}/{provider_name} Provider。"
            return {
                "action_type": action_type,
                "summary": summary,
                "diff": diff,
                "warnings": ["敏感字段已在 preview 中脱敏；commit 后会写入 provider revision。"],
            }

        if action_type == "update_config":
            current = self._config_service.get_editable_config_snapshot()
            updates = dict(payload["updates"])
            diff = [
                {"field": key, "before": current.get(key), "after": value}
                for key, value in updates.items()
                if current.get(key) != value
            ]
            return {
                "action_type": action_type,
                "summary": f"将更新 {len(diff)} 个运行时配置字段。",
                "diff": diff,
                "warnings": ["仅 SAFE_UPDATE_FIELDS 白名单字段会真正写入。"],
            }

        raise ValueError(f"不支持的动作类型: {action_type}")

    def _compose_answer(
        self,
        *,
        message: str,
        manual_hits: list[KnowledgeHit],
        repo_hits: list[KnowledgeHit],
        page_context: dict[str, Any],
        intent_mode: str,
    ) -> str:
        sections: list[str] = []
        if page_context.get("path"):
            sections.append(f"当前页面上下文：{page_context.get('path')}")
        if manual_hits:
            manual_lines = [f"- 手册《{item.title}》提到：{self._compact_snippet(item.snippet)}" for item in manual_hits[:2]]
            sections.append("使用手册线索：\n" + "\n".join(manual_lines))
        if repo_hits:
            repo_lines = [f"- 代码 `{item.path}` 显示：{self._compact_snippet(item.snippet)}" for item in repo_hits[:2]]
            sections.append("仓库实现线索：\n" + "\n".join(repo_lines))
        if not sections:
            sections.append("当前本地知识库里没有检索到直接匹配内容，请换个关键词试试。")
        if intent_mode and intent_mode != "auto":
            sections.append(f"回答模式：{intent_mode}")
        return "\n\n".join(sections)

    def _serialize_hit(self, item: KnowledgeHit) -> dict[str, Any]:
        reference = f"{item.path}#{item.anchor}" if item.anchor else item.path
        return {
            "source_type": item.source_type,
            "path": item.path,
            "title": item.title,
            "anchor": item.anchor,
            "reference": reference,
            "snippet": self._compact_snippet(item.snippet),
            "score": round(item.score, 2),
        }

    def _serialize_action_log(self, log: AssistantActionLog) -> dict[str, Any]:
        payload = dict(log.payload or {})
        action_type = str(log.action_type or payload.get("action_type") or "")
        if action_type == "upsert_provider":
            inner = dict(payload.get("payload") or {})
            if inner.get("config"):
                inner["config"] = self._config_service.redact_provider_config(dict(inner["config"]))
            payload["payload"] = inner
        result = dict(log.result or {})
        preview = result.get("preview")
        if isinstance(preview, dict) and preview.get("diff"):
            result["preview"] = preview
        return {
            "id": log.id,
            "mode": log.mode,
            "intent": log.intent,
            "action_type": log.action_type,
            "status": log.status,
            "title": log.title,
            "payload": payload,
            "audit": log.audit or {},
            "result": result,
            "created_at": log.created_at.isoformat() if log.created_at else "",
            "updated_at": log.updated_at.isoformat() if log.updated_at else "",
        }

    def _get_required_fields(self, action_type: str, payload: dict[str, Any]) -> list[str]:
        if action_type == "upsert_provider":
            required = ["provider_type", "provider_name", "config"]
        elif action_type == "update_config":
            required = ["updates"]
        else:
            return ["action_type"]
        return [field for field in required if not payload.get(field)]

    def _redact_for_audit(self, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action_type == "upsert_provider":
            redacted = dict(payload)
            redacted["config"] = redact_structure(dict(payload.get("config") or {}))
            return redacted
        return redact_structure(payload)

    def _compact_snippet(self, snippet: str) -> str:
        normalized = " ".join((snippet or "").split())
        if len(normalized) <= 180:
            return normalized
        return normalized[:177] + "..."

    def _save_action_log(self, log: AssistantActionLog) -> AssistantActionLog:
        if not log.created_at:
            log.created_at = _utc_now()
        log.updated_at = _utc_now()
        with get_session() as session:
            session.add(log)
            session.commit()
            session.refresh(log)
        return log

    def _get_action_log(self, action_id: str) -> Optional[AssistantActionLog]:
        with get_session() as session:
            return session.get(AssistantActionLog, action_id)
