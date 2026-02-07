"""Memory skill — event handler wrapper around MemoryManager and extractor.

Handles two event types:
  - memory_query: retrieve relevant memories for a query string
  - memory_store: extract and store memories from a conversation turn
"""

from __future__ import annotations

import logging
import threading

from clawless.user.skills.base import BaseSkill
from clawless.user.skills.memory.extractor import extract_from_text, extract_with_llm
from clawless.user.skills.memory.manager import MemoryManager
from clawless.user.types import Event, KernelContext, MemoryEntry, SkillResult

logger = logging.getLogger(__name__)


class MemorySkill(BaseSkill):
    """Per-profile memory storage and retrieval."""

    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return "Remember facts, preferences, and persona adjustments about the user."

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"memory:read", "memory:write", "llm:call"})

    @property
    def handles_events(self) -> list[str]:
        return ["memory_query", "memory_store"]

    def on_load(self, ctx: KernelContext) -> None:
        self._managers: dict[str, MemoryManager] = {}
        self._settings = ctx.settings

    def handle(self, event: Event, ctx: KernelContext) -> SkillResult | None:
        if event.type == "memory_query":
            return self._handle_query(event, ctx)
        if event.type == "memory_store":
            return self._handle_store(event, ctx)
        return None

    def _get_manager(self, profile_id: str, ctx: KernelContext) -> MemoryManager:
        """Get or create a MemoryManager for a profile."""
        if profile_id not in self._managers:
            self._managers[profile_id] = MemoryManager(
                data_dir=ctx.data_dir,
                profile_id=profile_id,
                top_k=self._settings.memory.retrieval_top_k,
            )
        return self._managers[profile_id]

    def _handle_query(self, event: Event, ctx: KernelContext) -> SkillResult:
        """Retrieve relevant memories and persona context for a query."""
        manager = self._get_manager(event.profile_id, ctx)

        memory_context = manager.build_context(event.payload)
        persona_context = manager.build_persona_context()

        return SkillResult(
            success=True,
            output=memory_context,
            data={
                "memory_context": memory_context,
                "persona_context": persona_context,
            },
        )

    def _handle_store(self, event: Event, ctx: KernelContext) -> SkillResult:
        """Extract and store memories from a conversation turn."""
        manager = self._get_manager(event.profile_id, ctx)
        user_input = event.payload
        assistant_response = event.metadata.get("assistant_response", "")
        extraction_mode = self._settings.memory.extraction_mode

        extractions = []

        if extraction_mode in ("auto", "llm"):
            try:
                existing = manager.get_recent_summaries()
                extractions = extract_with_llm(
                    user_message=user_input,
                    assistant_response=assistant_response,
                    existing_memories=existing,
                    llm_router=ctx.llm,
                )
                logger.debug("LLM extraction produced %d entries", len(extractions))
            except Exception:
                if extraction_mode == "llm":
                    logger.warning("LLM extraction failed (mode=llm, no fallback)")
                    return SkillResult(success=False, output="LLM extraction failed")
                logger.debug("LLM extraction failed, falling back to regex")
                extractions = extract_from_text(user_input)
        elif extraction_mode == "regex":
            extractions = extract_from_text(user_input)

        stored = 0
        for extraction in extractions:
            source = "llm_extracted" if extraction_mode != "regex" else "extracted"
            manager.store(MemoryEntry(
                type=extraction.type,
                content=extraction.content,
                source=source,
                confidence=extraction.confidence,
                keywords=extraction.keywords,
            ))
            stored += 1

        return SkillResult(
            success=True,
            output=f"Stored {stored} memory entries",
            data={"stored_count": stored},
        )
