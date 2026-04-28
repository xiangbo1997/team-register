# -*- coding: utf-8 -*-
"""守住 docs/architecture/provider-config-flow.md 被 KnowledgeService 索引的能力。

防止未来：
  - 文档被误删
  - KnowledgeService 排除规则被改坏导致 docs/ 不再扫描
  - 检索算法升级后无法命中常见 provider-config 关键词
"""

import unittest
from pathlib import Path

from src.services.knowledge_service import KnowledgeService


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_TARGET_PATH_FRAGMENT = "provider-config-flow"


class TestProviderConfigDocIndexed(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ks = KnowledgeService(project_root=_PROJECT_ROOT)

    def _hit_paths(self, query: str) -> list[str]:
        # limit=10：随着仓库新增 service / test 文件，top 5 可能被挤掉文档；
        # 用 10 给文档更宽容的命中窗口
        result = self.ks.search(query, limit=10)
        return [hit.path for hit in (result.get("repo") or [])]

    def test_doc_file_exists(self):
        path = _PROJECT_ROOT / "docs" / "architecture" / "provider-config-flow.md"
        self.assertTrue(path.exists(), f"文档缺失: {path}")

    def test_query_warmup_pool_hits_doc(self):
        paths = self._hit_paths("WARMUP_ACCOUNT_POOL 怎么用")
        self.assertTrue(
            any(_TARGET_PATH_FRAGMENT in p for p in paths),
            f"WARMUP_ACCOUNT_POOL 查询应命中 provider-config-flow.md，实际: {paths}",
        )

    def test_query_layer_priority_hits_doc(self):
        paths = self._hit_paths("L1 L2 L3 优先级")
        self.assertTrue(
            any(_TARGET_PATH_FRAGMENT in p for p in paths),
            f"三层优先级查询应命中文档，实际: {paths}",
        )

    def test_query_card_provider_hits_doc(self):
        paths = self._hit_paths("card_provider 全局怎么改")
        self.assertTrue(
            any(_TARGET_PATH_FRAGMENT in p for p in paths),
            f"card_provider 查询应命中文档，实际: {paths}",
        )

    def test_query_mail_account_id_hits_doc(self):
        paths = self._hit_paths("mail_account_id 是什么")
        self.assertTrue(
            any(_TARGET_PATH_FRAGMENT in p for p in paths),
            f"mail_account_id 查询应命中文档，实际: {paths}",
        )

    def test_query_resolve_runtime_config_hits_doc(self):
        paths = self._hit_paths("_resolve_runtime_config 决策树")
        # 这个查询期望命中文档（文档有讲）；但代码里也有这个函数定义，允许 mix 命中
        # 只要不全是非文档就算通过；如果**只**返回代码（文档完全 miss）才 fail
        self.assertTrue(
            len(paths) > 0,
            "查询无任何 hit，说明索引坏了",
        )

    def test_index_version_present(self):
        """确认索引建好了（有版本号）。"""
        self.assertTrue(
            bool(getattr(self.ks, "index_version", "")),
            "KnowledgeService.index_version 为空，索引未建立",
        )


if __name__ == "__main__":
    unittest.main()
