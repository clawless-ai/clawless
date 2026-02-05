"""Per-profile memory manager.

All memory is stored as append-only JSONL files (one entry per line).
Retrieval uses lightweight keyword matching by default, with an optional
FAISS backend for semantic search on capable hardware.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from clawless.types import MemoryEntry
from clawless.utils.helpers import (
    ensure_profile_dirs,
    safe_append_file,
    validate_profile_id,
)

logger = logging.getLogger(__name__)


class MemoryManager:
    """Manages per-profile memory storage and retrieval."""

    def __init__(self, data_dir: Path, profile_id: str, top_k: int = 5) -> None:
        validate_profile_id(profile_id)
        self._data_dir = data_dir
        self._profile_id = profile_id
        self._top_k = top_k

        # Ensure directories exist
        ensure_profile_dirs(data_dir, profile_id)

        # Cache of loaded entries (lazy-loaded)
        self._entries: list[MemoryEntry] | None = None

    @property
    def _memory_file(self) -> str:
        return f"profiles/{self._profile_id}/memory/entries.jsonl"

    @property
    def _memory_path(self) -> Path:
        return self._data_dir / self._memory_file

    def store(self, entry: MemoryEntry) -> None:
        """Append a memory entry to the profile's JSONL store."""
        record = {
            "type": entry.type,
            "content": entry.content,
            "source": entry.source,
            "timestamp": entry.timestamp.isoformat(),
            "confidence": entry.confidence,
            "metadata": entry.metadata,
            "keywords": entry.keywords,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        safe_append_file(self._data_dir, self._memory_file, line)

        # Invalidate cache
        self._entries = None
        logger.debug("Stored %s memory entry for profile %s", entry.type, self._profile_id)

    def store_fact(self, content: str, source: str = "extracted", confidence: float = 1.0) -> None:
        """Convenience method to store a fact."""
        self.store(MemoryEntry(type="fact", content=content, source=source, confidence=confidence))

    def store_preference(
        self, content: str, source: str = "extracted", confidence: float = 1.0
    ) -> None:
        """Convenience method to store a preference."""
        self.store(
            MemoryEntry(type="preference", content=content, source=source, confidence=confidence)
        )

    def store_persona(
        self, content: str, source: str = "extracted", confidence: float = 0.8
    ) -> None:
        """Store a learned persona/behavior adjustment."""
        self.store(
            MemoryEntry(type="persona", content=content, source=source, confidence=confidence)
        )

    def retrieve(self, query: str, top_k: int | None = None) -> list[MemoryEntry]:
        """Retrieve the most relevant memory entries for a query.

        Uses keyword matching against both entry content and stored multilingual
        keywords. This enables cross-language retrieval: a German query can
        match an English-stored memory via its German keywords.
        """
        k = top_k or self._top_k
        entries = self._load_entries()
        if not entries:
            return []

        query_terms = _tokenize(query)
        if not query_terms:
            return entries[-k:]

        scored = []
        for entry in entries:
            entry_terms = _tokenize(entry.content)
            score = _keyword_score(query_terms, entry_terms, entry.keywords)
            if score > 0:
                scored.append((score, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = [entry for _, entry in scored[:k]]

        # Safety net: if keyword matching found nothing at all, return
        # recent entries so the LLM has some context. This handles the
        # transition period for old entries stored without keywords.
        if not results:
            return entries[-k:]

        return results

    def get_all(self, entry_type: str | None = None) -> list[MemoryEntry]:
        """Return all memory entries, optionally filtered by type."""
        entries = self._load_entries()
        if entry_type:
            return [e for e in entries if e.type == entry_type]
        return entries

    def get_recent(self, n: int = 10) -> list[MemoryEntry]:
        """Return the N most recent entries."""
        entries = self._load_entries()
        return entries[-n:]

    def build_context(self, query: str, max_entries: int = 10) -> str:
        """Build a context string from relevant memories for injection into the LLM prompt."""
        relevant = self.retrieve(query, top_k=max_entries)
        # Exclude persona entries — those go through build_persona_context()
        relevant = [e for e in relevant if e.type != "persona"]
        if not relevant:
            return ""

        lines = ["Relevant memories about this user:"]
        for entry in relevant:
            prefix = "Fact" if entry.type == "fact" else "Preference"
            lines.append(f"- [{prefix}] {entry.content}")
        return "\n".join(lines)

    def build_persona_context(self) -> str:
        """Build a context string from learned persona entries."""
        persona_entries = self.get_all(entry_type="persona")
        if not persona_entries:
            return ""
        lines = ["Learned behavior adjustments from this user:"]
        for entry in persona_entries:
            lines.append(f"- {entry.content}")
        return "\n".join(lines)

    def get_recent_summaries(self, max_entries: int = 50) -> str:
        """Return a compact summary of existing memories for dedup in extraction prompts."""
        entries = self._load_entries()
        if not entries:
            return "(none)"
        recent = entries[-max_entries:]
        lines = []
        for entry in recent:
            lines.append(f"[{entry.type}] {entry.content}")
        return "\n".join(lines)

    def _load_entries(self) -> list[MemoryEntry]:
        """Load all entries from the JSONL file (with caching)."""
        if self._entries is not None:
            return self._entries

        path = self._memory_path
        if not path.exists():
            self._entries = []
            return self._entries

        entries = []
        for line_num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                entry = MemoryEntry(
                    type=record.get("type", "fact"),
                    content=record.get("content", ""),
                    source=record.get("source", ""),
                    timestamp=datetime.fromisoformat(record["timestamp"])
                    if "timestamp" in record
                    else datetime.now(timezone.utc),
                    confidence=record.get("confidence", 1.0),
                    metadata=record.get("metadata", {}),
                    keywords=record.get("keywords", []),
                )
                entries.append(entry)
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Skipping malformed entry at line %d: %s", line_num, e)

        self._entries = entries
        return self._entries


def _tokenize(text: str) -> list[str]:
    """Unicode-aware word tokenizer: lowercase, split on non-word characters."""
    return [w for w in re.split(r"[^\w]+", text.lower(), flags=re.UNICODE) if len(w) > 1]


def _keyword_score(
    query_terms: list[str],
    entry_terms: list[str],
    entry_keywords: list[str] | None = None,
) -> float:
    """Score an entry against query terms using term overlap.

    Matches against both content tokens and stored multilingual keywords.
    Keyword matches get a boost since they are LLM-curated search terms.
    """
    if not entry_terms and not entry_keywords:
        return 0.0

    entry_set = set(entry_terms)
    entry_freq = Counter(entry_terms)

    # Build keyword token set (keywords may be multi-word, so tokenize them)
    keyword_tokens: set[str] = set()
    if entry_keywords:
        for kw in entry_keywords:
            keyword_tokens.update(_tokenize(kw))

    matches = 0.0
    for term in query_terms:
        if term in entry_set:
            # Content match — weight by inverse frequency
            matches += 1.0 / (1.0 + entry_freq[term])
        elif term in keyword_tokens:
            # Keyword match — boosted weight (curated by LLM)
            matches += 1.5

    return matches
