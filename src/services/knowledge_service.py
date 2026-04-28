# -*- coding: utf-8 -*-
"""使用手册 + 安全全库知识库构建与检索。"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_MANUAL_PATH = Path("docs/usage-manual.md")
_REPO_EXCLUDE_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".playwright-mcp",
    "artifacts",
    ".codex",
}
_REPO_EXCLUDE_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pyc",
    ".zip",
    ".pdf",
}
_ALLOWED_REPO_SUFFIXES = {
    ".py",
    ".md",
    ".html",
    ".json",
    ".toml",
    ".ini",
    ".txt",
    ".yml",
    ".yaml",
}


def _slugify(title: str) -> str:
    lowered = title.strip().lower()
    lowered = re.sub(r"[^\w\u4e00-\u9fff -]+", "", lowered)
    lowered = re.sub(r"[\s_]+", "-", lowered).strip("-")
    return lowered or "section"


def _extract_query_terms(query: str) -> list[str]:
    if not query:
        return []
    text = query.lower()
    terms = re.findall(r"[a-z0-9_./:-]{2,}", text)
    terms.extend(re.findall(r"[\u4e00-\u9fff]{2,}", query))
    unique: list[str] = []
    for term in terms:
        if term not in unique:
            unique.append(term)
    return unique[:20]


@dataclass
class KnowledgeChunk:
    source_type: str
    path: str
    title: str
    anchor: str
    text: str
    snippet: str


@dataclass
class KnowledgeHit:
    source_type: str
    path: str
    title: str
    anchor: str
    snippet: str
    score: float


class KnowledgeService:
    """构建帮助文档与仓库源码的本地知识索引。"""

    def __init__(self, project_root: Path | str) -> None:
        self._project_root = Path(project_root)
        self._manual_chunks: list[KnowledgeChunk] = []
        self._repo_chunks: list[KnowledgeChunk] = []
        self._manual_html: str = ""
        self._index_version: str = ""
        self.rebuild()

    @property
    def manual_html(self) -> str:
        return self._manual_html

    @property
    def index_version(self) -> str:
        return self._index_version

    def rebuild(self) -> None:
        manual_path = self._project_root / _MANUAL_PATH
        self._manual_chunks = self._build_manual_chunks(manual_path)
        self._manual_html = self._render_markdown(manual_path.read_text("utf-8")) if manual_path.exists() else ""
        self._repo_chunks = self._build_repo_chunks()
        version_src = f"{manual_path.stat().st_mtime if manual_path.exists() else 0}|{len(self._repo_chunks)}|{len(self._manual_chunks)}"
        self._index_version = hashlib.sha1(version_src.encode("utf-8")).hexdigest()[:12]
        logger.info("知识库索引完成：manual=%d, repo=%d", len(self._manual_chunks), len(self._repo_chunks))

    def search(self, query: str, limit: int = 3) -> dict[str, list[KnowledgeHit]]:
        manual_hits = self._score_chunks(query, self._manual_chunks, limit=limit)
        repo_hits = self._score_chunks(query, self._repo_chunks, limit=limit)
        return {"manual": manual_hits, "repo": repo_hits}

    def _score_chunks(self, query: str, chunks: Iterable[KnowledgeChunk], limit: int) -> list[KnowledgeHit]:
        terms = _extract_query_terms(query)
        if not terms:
            return []

        hits: list[KnowledgeHit] = []
        for chunk in chunks:
            haystack = f"{chunk.title}\n{chunk.path}\n{chunk.text}".lower()
            score = 0.0
            for term in terms:
                if term in chunk.path.lower():
                    score += 4.0
                if term in chunk.title.lower():
                    score += 3.0
                score += haystack.count(term) * 1.2
            if score <= 0:
                continue
            hits.append(
                KnowledgeHit(
                    source_type=chunk.source_type,
                    path=chunk.path,
                    title=chunk.title,
                    anchor=chunk.anchor,
                    snippet=chunk.snippet,
                    score=score,
                )
            )

        hits.sort(key=lambda item: item.score, reverse=True)
        return hits[:limit]

    def _build_manual_chunks(self, manual_path: Path) -> list[KnowledgeChunk]:
        if not manual_path.exists():
            logger.warning("主手册不存在，manual 索引为空：%s", manual_path)
            return []

        lines = manual_path.read_text("utf-8").splitlines()
        chunks: list[KnowledgeChunk] = []
        current_title = "使用手册"
        current_anchor = "usage-manual"
        buffer: list[str] = []

        def flush() -> None:
            if not buffer:
                return
            text = "\n".join(buffer).strip()
            if not text:
                buffer.clear()
                return
            chunks.append(
                KnowledgeChunk(
                    source_type="manual",
                    path=str(_MANUAL_PATH),
                    title=current_title,
                    anchor=current_anchor,
                    text=text,
                    snippet=text[:220],
                )
            )
            buffer.clear()

        for line in lines:
            if line.startswith("#"):
                flush()
                current_title = line.lstrip("#").strip() or "使用手册"
                current_anchor = _slugify(current_title)
                continue
            buffer.append(line)
        flush()
        return chunks

    def _build_repo_chunks(self) -> list[KnowledgeChunk]:
        chunks: list[KnowledgeChunk] = []
        for path in sorted(self._project_root.rglob("*")):
            if not path.is_file():
                continue
            rel_path = path.relative_to(self._project_root)
            if any(part in _REPO_EXCLUDE_PARTS for part in rel_path.parts):
                continue
            if path.name.startswith(".env"):
                continue
            if path.suffix.lower() in _REPO_EXCLUDE_SUFFIXES:
                continue
            if path.suffix.lower() not in _ALLOWED_REPO_SUFFIXES:
                continue

            try:
                content = path.read_text("utf-8")
            except Exception:
                continue

            lines = content.splitlines()
            if not lines:
                continue

            current_title = rel_path.as_posix()
            current_buffer: list[str] = []
            current_anchor = "top"

            def flush_buffer() -> None:
                if not current_buffer:
                    return
                text = "\n".join(current_buffer).strip()
                if not text:
                    current_buffer.clear()
                    return
                chunks.append(
                    KnowledgeChunk(
                        source_type="repo",
                        path=rel_path.as_posix(),
                        title=current_title,
                        anchor=current_anchor,
                        text=text,
                        snippet=text[:220],
                    )
                )
                current_buffer.clear()

            for idx, line in enumerate(lines, start=1):
                section_match = re.match(r"^(class|def)\s+([A-Za-z0-9_]+)", line.strip())
                heading_match = re.match(r"^#{1,3}\s+(.+)$", line.strip())
                if section_match:
                    flush_buffer()
                    current_title = f"{rel_path.as_posix()}::{section_match.group(2)}"
                    current_anchor = f"line-{idx}"
                elif heading_match:
                    flush_buffer()
                    current_title = f"{rel_path.as_posix()}::{heading_match.group(1).strip()}"
                    current_anchor = f"line-{idx}"

                current_buffer.append(line)
                if len(current_buffer) >= 40:
                    flush_buffer()
                    current_anchor = f"line-{idx + 1}"
            flush_buffer()
        return chunks

    def _render_markdown(self, markdown_text: str) -> str:
        lines = markdown_text.splitlines()
        html_parts: list[str] = []
        in_code = False
        in_list = False
        paragraph: list[str] = []

        def flush_paragraph() -> None:
            if paragraph:
                html_parts.append(f"<p>{html.escape(' '.join(paragraph).strip())}</p>")
                paragraph.clear()

        def close_list() -> None:
            nonlocal in_list
            if in_list:
                html_parts.append("</ul>")
                in_list = False

        for line in lines:
            stripped = line.rstrip()
            if stripped.startswith("```"):
                flush_paragraph()
                close_list()
                if in_code:
                    html_parts.append("</code></pre>")
                    in_code = False
                else:
                    html_parts.append("<pre><code>")
                    in_code = True
                continue

            if in_code:
                html_parts.append(html.escape(stripped))
                html_parts.append("\n")
                continue

            heading_match = re.match(r"^(#{1,3})\s+(.+)$", stripped)
            if heading_match:
                flush_paragraph()
                close_list()
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                anchor = _slugify(title)
                html_parts.append(f'<h{level} id="{anchor}">{html.escape(title)}</h{level}>')
                continue

            if stripped.startswith("- "):
                flush_paragraph()
                if not in_list:
                    html_parts.append("<ul>")
                    in_list = True
                html_parts.append(f"<li>{html.escape(stripped[2:].strip())}</li>")
                continue

            if not stripped:
                flush_paragraph()
                close_list()
                continue

            paragraph.append(stripped)

        flush_paragraph()
        close_list()
        if in_code:
            html_parts.append("</code></pre>")
        return "\n".join(html_parts)
