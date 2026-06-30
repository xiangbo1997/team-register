# -*- coding: utf-8 -*-
"""
共享依赖

为 FastAPI 路由提供单例服务实例。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from src.services.assistant_service import AssistantService
from src.services.audit_service import AuditService
from src.services.auth_service import AuthService
from src.services.config_service import ConfigService
from src.services.event_service import EventBroadcaster
from src.services.knowledge_service import KnowledgeService
from src.services.registration_profile_service import RegistrationProfileService

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def get_config_service() -> ConfigService:
    return ConfigService()


@lru_cache(maxsize=1)
def get_event_broadcaster() -> EventBroadcaster:
    return EventBroadcaster()


@lru_cache(maxsize=1)
def get_auth_service() -> AuthService:
    return AuthService()


@lru_cache(maxsize=1)
def get_knowledge_service() -> KnowledgeService:
    return KnowledgeService(project_root=_PROJECT_ROOT)


@lru_cache(maxsize=1)
def get_audit_service() -> AuditService:
    return AuditService()


@lru_cache(maxsize=1)
def get_assistant_service() -> AssistantService:
    return AssistantService(
        config_service=get_config_service(),
        knowledge_service=get_knowledge_service(),
        audit_service=get_audit_service(),
    )


@lru_cache(maxsize=1)
def get_registration_profile_service() -> RegistrationProfileService:
    # 复用 ConfigService 内部已加载的 base_config，避免重复读 .env
    return RegistrationProfileService(
        base_config=get_config_service().get_config(),
    )
