# -*- coding: utf-8 -*-
"""Provider 类级注册表（单例 + 目录扫描发现）。

设计目标：
- 「插拔式」—— 新增 provider 文件即被发现，无需修改注册表代码
- 「DB 驱动激活」—— ``build_active(provider_type)`` 查 ProviderConfig 表里
  ``is_active=True`` 的那一行，按其 ``provider_name`` 找类、按 ``config_json``
  构造实例。切换 active 行就等于切换供应商，**无需重启**
- 「Schema 委派」—— 每个 provider 类自己在装饰器里声明必填字段，
  Registry.validate() 统一校验，路由层不再有硬编码白名单

关键 API：
- ``ProviderRegistry.instance()``：单例
- ``register_class(cls)``：装饰器内部调用，把类挂到 ``_classes[(type, kind)]``
- ``discover()``：扫描 ``src/providers/{browsers,cards,mails,sms,captcha,llm}/``
  完成自注册（import 时 @register_provider 装饰器自动执行）
- ``list_kinds(provider_type)``：列出某类型下所有 kind（前端 select options）
- ``get_meta(provider_type, kind)``：返回 ProviderMeta（前端动态表单 schema）
- ``validate(provider_type, kind, config)``：返回缺失字段列表
- ``build(provider_type, kind, config)``：实例化
- ``build_active(provider_type, config_service)``：查 DB active 行后实例化
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from typing import TYPE_CHECKING, Any, ClassVar, Optional

if TYPE_CHECKING:
    from src.providers.base import ProviderMeta
    from src.services.config_service import ConfigService

logger = logging.getLogger(__name__)

# Provider 子目录约定 —— 新增类型时在此追加即可（不算硬编码 provider，
# 只是声明"哪些子包要扫"；放进去的文件用装饰器自注册）
_PROVIDER_SUBPACKAGES = ("browsers", "cards", "mails", "sms", "captcha", "llm")


class ProviderNotRegisteredError(KeyError):
    """请求的 (provider_type, kind) 未注册。"""


class ProviderConfigInvalidError(ValueError):
    """provider 配置缺少必填字段。"""

    def __init__(self, provider_type: str, kind: str, missing: list[str]) -> None:
        super().__init__(
            f"provider {provider_type}/{kind} 缺必填字段: {missing}"
        )
        self.provider_type = provider_type
        self.kind = kind
        self.missing_fields = missing


class ProviderRegistry:
    """Provider 类级注册表（进程级单例）。"""

    _singleton: ClassVar[Optional["ProviderRegistry"]] = None

    def __init__(self) -> None:
        # key = (provider_type, kind), value = provider class
        self._classes: dict[tuple[str, str], type] = {}
        self._discovered: bool = False
        # 旧 API 兼容：实例级注册表（按名称查找已构造的 provider 实例）
        # 给老代码用 reg.register_browser(name, instance) / reg.get_browser(name) 套路
        self._browser_instances: dict[str, Any] = {}
        self._card_instances: dict[str, Any] = {}
        self._mail_instances: dict[str, Any] = {}

    # ── 单例 ──────────────────────────────────────────

    @classmethod
    def instance(cls) -> "ProviderRegistry":
        if cls._singleton is None:
            cls._singleton = cls()
        return cls._singleton

    @classmethod
    def reset_for_tests(cls) -> None:
        """仅供测试 fixture 清理状态。"""
        cls._singleton = None

    # ── 注册 ──────────────────────────────────────────

    def register_class(self, cls: type) -> None:
        """装饰器内部调用。要求 cls.PROVIDER_META 已设置。"""
        meta = getattr(cls, "PROVIDER_META", None)
        if meta is None:
            raise TypeError(
                f"{cls.__name__} 未声明 PROVIDER_META；"
                f"请用 @register_provider(...) 装饰器"
            )
        key = (meta.provider_type, meta.kind)
        existing = self._classes.get(key)
        if existing is not None and existing is not cls:
            logger.warning(
                "provider %s/%s 已被 %s 注册，将被 %s 覆盖",
                meta.provider_type, meta.kind,
                existing.__name__, cls.__name__,
            )
        self._classes[key] = cls
        logger.debug("registered provider: %s/%s → %s", meta.provider_type, meta.kind, cls.__name__)

    # ── 发现 ──────────────────────────────────────────

    def discover(self, *, force: bool = False) -> int:
        """扫描所有 provider 子目录，import 每个模块触发 @register_provider。

        返回新注册的 provider 类数量（已注册的不重复算）。
        ``force=True`` 时重新扫描（即便已扫过）。
        """
        if self._discovered and not force:
            return 0

        before = len(self._classes)
        for subpkg in _PROVIDER_SUBPACKAGES:
            try:
                pkg = importlib.import_module(f"src.providers.{subpkg}")
            except ModuleNotFoundError:
                # 子包不存在 → 跳过（允许逐步迁移，没建的子包不报错）
                continue
            if not hasattr(pkg, "__path__"):
                continue
            for _, mod_name, _is_pkg in pkgutil.iter_modules(pkg.__path__, pkg.__name__ + "."):
                try:
                    importlib.import_module(mod_name)
                except Exception as exc:
                    logger.error("加载 provider 模块 %s 失败: %s", mod_name, exc)
                    continue
        after = len(self._classes)
        self._discovered = True
        logger.info("provider discover 完成: %d → %d 个类已注册", before, after)
        return after - before

    # ── 查询 ──────────────────────────────────────────

    def list_kinds(self, provider_type: str) -> list[str]:
        return sorted(k for (t, k) in self._classes if t == provider_type)

    def list_all_metas(self) -> list["ProviderMeta"]:
        return [cls.PROVIDER_META for cls in self._classes.values()]

    def get_meta(self, provider_type: str, kind: str) -> Optional["ProviderMeta"]:
        cls = self._classes.get((provider_type, kind))
        return cls.PROVIDER_META if cls is not None else None

    def get_class(self, provider_type: str, kind: str) -> Optional[type]:
        return self._classes.get((provider_type, kind))

    # ── 校验 + 构造 ───────────────────────────────────

    def validate(self, provider_type: str, kind: str, config: dict[str, Any]) -> list[str]:
        """返回缺失必填字段列表；空列表表示通过。

        会先按 schema 的 aliases 把 config 里的别名字段重命名为正式 name 再判空。
        """
        meta = self.get_meta(provider_type, kind)
        if meta is None:
            return []  # 未注册的不在此校验，build() 会抛 ProviderNotRegisteredError
        payload = self._apply_aliases(meta.schema, config or {})
        return [
            f.name for f in meta.schema
            if f.required and not str(payload.get(f.name) or "").strip()
        ]

    def build(self, provider_type: str, kind: str, config: dict[str, Any]):
        """按 kind + config 构造 provider 实例。

        - 缺必填字段 → ``ProviderConfigInvalidError``
        - kind 未注册 → ``ProviderNotRegisteredError``
        - alias 字段自动重命名为正式 name
        - 自动按 ``__init__`` 形参过滤多余字段（兼容前端多塞了 _csrf 之类）
        """
        cls = self._classes.get((provider_type, kind))
        if cls is None:
            raise ProviderNotRegisteredError(
                f"未注册的 provider: {provider_type}/{kind} "
                f"（已注册: {self.list_kinds(provider_type)}）"
            )
        meta = cls.PROVIDER_META
        normalized = self._apply_aliases(meta.schema, config or {})
        missing = [
            f.name for f in meta.schema
            if f.required and not str(normalized.get(f.name) or "").strip()
        ]
        if missing:
            raise ProviderConfigInvalidError(provider_type, kind, missing)
        kwargs = self._filter_init_kwargs(cls, normalized)
        return cls(**kwargs)

    @staticmethod
    def _apply_aliases(schema: tuple, config: dict[str, Any]) -> dict[str, Any]:
        """按 schema 的 aliases 把 config 里的别名 key 重命名为正式 name。

        正式 name 已存在的字段优先，alias 不覆盖。
        """
        if not schema:
            return dict(config)
        result = dict(config)
        for spec in schema:
            if spec.name in result:
                continue
            for alias in spec.aliases:
                if alias in result:
                    result[spec.name] = result.pop(alias)
                    break
        return result

    def build_active(
        self,
        provider_type: str,
        config_service: "ConfigService",
        *,
        extras: Optional[dict[str, Any]] = None,
    ):
        """查 DB 拿 active 行，按 kind + config 构造实例。

        ``extras`` 是「类型级共用参数」（如 mail 类的 base_url/api_key 是
        email-provider 服务端地址，所有 mail provider 共用），会与 DB 里的
        config 合并；DB 中已存在的字段优先（per-instance 覆盖 per-type 共用）。

        返回 None 表示该类型当前无 active provider。
        """
        pc = config_service.get_active_provider(provider_type)
        if pc is None:
            return None
        kind, config = self._extract_kind_and_config(pc)
        if extras:
            # DB 字段优先；extras 只填 DB 没有/为空的字段
            merged = {k: v for k, v in extras.items() if k not in config or not config[k]}
            merged.update(config)
            config = merged
        return self.build(provider_type, kind, config)

    def build_all_active(
        self,
        provider_type: str,
        config_service: "ConfigService",
        *,
        extras: Optional[dict[str, Any]] = None,
    ) -> list:
        """构造该类型下所有 active provider 实例（mail 多 provider 路由用）。"""
        instances = []
        for pc in config_service.list_active_providers(provider_type):
            try:
                kind, config = self._extract_kind_and_config(pc)
                if extras:
                    merged = {k: v for k, v in extras.items() if k not in config or not config[k]}
                    merged.update(config)
                    config = merged
                instances.append(self.build(provider_type, kind, config))
            except (ProviderNotRegisteredError, ProviderConfigInvalidError) as exc:
                logger.warning(
                    "跳过 active provider %s/%s: %s",
                    provider_type, pc.provider_name, exc,
                )
        return instances

    @staticmethod
    def _extract_kind_and_config(pc) -> tuple[str, dict[str, Any]]:
        """从 ProviderConfig 行解析 (kind, config dict)。

        历史设计：DB 里 ``provider_name`` 是用户起的「实例名」（如 ``efuncard-default``、
        ``mail-cfworker-default``），真正的「kind」（注册装饰器里声明的标识）放在
        ``config_json`` 里，按以下优先级提取：

        1. ``config['driver']`` —— card 类用这个 key（如 driver='efuncard'）
        2. ``config['provider_name']`` —— mail 类用这个 key（如 provider_name='cfworker'）
        3. ``pc.provider_name`` —— 兜底；新建 ProviderConfig 时如果实例名直接 == kind 也能 work

        返回的 config dict 会**剥掉** driver / provider_name 这两个仅用于路由的 key，
        其余字段直接当 kwargs 传给 provider 类构造器。
        """
        raw = dict(pc.config or {})
        kind = (
            str(raw.pop("driver", "") or "").strip()
            or str(raw.pop("provider_name", "") or "").strip()
            or str(pc.provider_name or "").strip()
        )
        return kind, raw

    # ── 工具 ──────────────────────────────────────────

    @staticmethod
    def _filter_init_kwargs(cls: type, config: dict[str, Any]) -> dict[str, Any]:
        """按 cls.__init__ 形参过滤 config，避免多余字段引发 TypeError。"""
        try:
            sig = inspect.signature(cls.__init__)
        except (ValueError, TypeError):
            return dict(config)
        param_names = {
            name for name, p in sig.parameters.items()
            if name != "self"
            and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        # 若 __init__ 有 **kwargs 则全部透传
        has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )
        if has_var_keyword:
            return dict(config)
        return {k: v for k, v in config.items() if k in param_names}


    # ── 旧 API 兼容（实例级注册，逐步废弃） ──────────
    #
    # 历史上 ``ProviderRegistry`` 是「按名字注册已构造实例」的简单字典，
    # 没有"按 kind + 元数据声明 + DB 驱动"的能力。为了不破坏现存测试和
    # 极少数残留用法，保留这套旧 API，但内部和新的 class-level 注册表互不干扰。

    def register_browser(self, name: str, provider) -> None:
        self._browser_instances[name] = provider

    def register_card(self, name: str, provider) -> None:
        self._card_instances[name] = provider

    def register_mail(self, name: str, provider) -> None:
        self._mail_instances[name] = provider

    def get_browser(self, name: str):
        return self._browser_instances.get(name)

    def get_card(self, name: str):
        return self._card_instances.get(name)

    def get_mail(self, name: str):
        return self._mail_instances.get(name)

    def list_browsers(self) -> list[str]:
        return list(self._browser_instances.keys())

    def list_cards(self) -> list[str]:
        return list(self._card_instances.keys())

    def list_mails(self) -> list[str]:
        return list(self._mail_instances.keys())


def get_registry() -> ProviderRegistry:
    """便捷函数。"""
    return ProviderRegistry.instance()
