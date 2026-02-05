"""Core agent loop: input -> safety -> memory -> LLM -> skills -> memory -> output.

This is the central orchestrator. It never writes to disk except through
the sandboxed memory manager and skill proposer.
"""

from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path

from clawless.config import Settings
from clawless.llm.router import LLMRouter
from clawless.memory.extractor import extract_from_text, extract_with_llm
from clawless.memory.manager import MemoryManager
from clawless.types import MemoryEntry as MemEntry
from clawless.safety.guard import SafetyGuard
from clawless.skills.base import SkillRegistry
from clawless.skills.proposer import SkillProposer
from clawless.types import Message, Role, Session

logger = logging.getLogger(__name__)


class Agent:
    """The Clawless agent — processes messages through a safe, structured pipeline."""

    def __init__(
        self,
        settings: Settings,
        llm_router: LLMRouter,
        skill_registry: SkillRegistry,
        safety_guard: SafetyGuard,
    ) -> None:
        self._settings = settings
        self._llm = llm_router
        self._skills = skill_registry
        self._guard = safety_guard
        self._sessions: dict[str, Session] = {}
        self._memory_managers: dict[str, MemoryManager] = {}

    def get_or_create_session(
        self, profile_id: str, channel: str = "text"
    ) -> Session:
        """Get an existing session or create a new one for the profile."""
        key = f"{profile_id}:{channel}"
        if key not in self._sessions:
            session = Session(
                session_id=str(uuid.uuid4()),
                profile_id=profile_id,
                channel=channel,
            )
            self._sessions[key] = session
        return self._sessions[key]

    def get_memory_manager(self, profile_id: str) -> MemoryManager:
        """Get or create a memory manager for a profile."""
        if profile_id not in self._memory_managers:
            self._memory_managers[profile_id] = MemoryManager(
                data_dir=self._settings.data_path,
                profile_id=profile_id,
                top_k=self._settings.memory.retrieval_top_k,
            )
        return self._memory_managers[profile_id]

    def process_message(self, profile_id: str, user_input: str, channel: str = "text") -> str:
        """Process a user message through the full pipeline. Returns the agent's response."""

        # 1. Safety check on input
        input_check = self._guard.check_input(user_input)
        if not input_check.allowed:
            logger.warning("Input blocked: %s", input_check.reason)
            return "I'm sorry, I can't process that input."

        # 2. Get session and memory
        session = self.get_or_create_session(profile_id, channel)
        memory = self.get_memory_manager(profile_id)

        # 3. Add user message to session
        user_msg = session.add_message(Role.USER, user_input)

        # 4. Check for skill triggers
        triggered_skills = self._skills.find_by_trigger(user_input)
        for skill in triggered_skills:
            result = skill.handle(session, user_msg)
            if result and result.success:
                # Handle skill proposer specially
                if isinstance(skill, SkillProposer) and result.data.get("action") == "propose":
                    return self._handle_skill_proposal(
                        session, user_msg, skill, result.data.get("request", user_input)
                    )
                # Other skills return directly
                session.add_message(Role.ASSISTANT, result.output)
                return result.output

        # 5. Load persona (base file + learned memories)
        persona_text = self._load_persona_file(profile_id)
        persona_memories = memory.build_persona_context()

        # 6. Build context from memory
        memory_context = memory.build_context(user_input)

        # 7. Build skill descriptions for the system prompt
        skill_descriptions = self._build_skill_descriptions()

        # 8. Build system prompt
        system_prompt = self._guard.build_system_prompt(
            persona=persona_text,
            persona_memories=persona_memories,
            memory_context=memory_context,
            skill_descriptions=skill_descriptions,
        )

        # 8. Build messages for LLM
        llm_messages = [{"role": "system", "content": system_prompt}]
        llm_messages.extend(session.history)

        # 9. Call LLM
        try:
            response = self._llm.chat(
                messages=llm_messages,
                max_tokens=self._settings.llm_endpoints[0].max_tokens
                if self._settings.llm_endpoints
                else 1024,
            )
            assistant_text = response.content
        except RuntimeError as e:
            logger.error("LLM call failed: %s", e)
            assistant_text = "I'm having trouble connecting to my language model. Please try again."

        # 10. Safety check on output
        output_check = self._guard.check_output(assistant_text)
        if not output_check.allowed:
            logger.warning("Output blocked: %s", output_check.reason)
            assistant_text = "I generated a response but it was filtered by safety checks."

        # 11. Add assistant response to session
        session.add_message(Role.ASSISTANT, assistant_text)

        # 12. Extract and store memories in background (non-blocking)
        extraction_mode = self._settings.memory.extraction_mode
        thread = threading.Thread(
            target=self._run_background_extraction,
            args=(memory, user_input, assistant_text, extraction_mode),
            daemon=True,
        )
        thread.start()

        return assistant_text

    def _run_background_extraction(
        self,
        memory: MemoryManager,
        user_input: str,
        assistant_response: str,
        extraction_mode: str,
    ) -> None:
        """Run memory extraction in a background thread.

        Tries LLM-based extraction first (if mode allows), falls back to regex.
        All exceptions are caught — this must never crash.
        """
        try:
            extractions = []

            if extraction_mode in ("auto", "llm"):
                try:
                    existing = memory.get_recent_summaries()
                    extractions = extract_with_llm(
                        user_message=user_input,
                        assistant_response=assistant_response,
                        existing_memories=existing,
                        llm_router=self._llm,
                    )
                    logger.debug("LLM extraction produced %d entries", len(extractions))
                except Exception:
                    if extraction_mode == "llm":
                        logger.warning("LLM extraction failed (mode=llm, no fallback)")
                        return
                    logger.debug("LLM extraction failed, falling back to regex")
                    extractions = extract_from_text(user_input)
            elif extraction_mode == "regex":
                extractions = extract_from_text(user_input)

            for extraction in extractions:
                source = "llm_extracted" if extraction_mode != "regex" else "extracted"
                memory.store(MemEntry(
                    type=extraction.type,
                    content=extraction.content,
                    source=source,
                    confidence=extraction.confidence,
                    keywords=extraction.keywords,
                ))

        except Exception:
            logger.exception("Background memory extraction failed (non-fatal)")

    def _handle_skill_proposal(
        self, session: Session, user_msg: Message, proposer: SkillProposer, request: str
    ) -> str:
        """Handle a skill proposal request by generating code via LLM."""
        from clawless.skills.proposer import GENERATION_PROMPT

        prompt = GENERATION_PROMPT.format(request=request)
        try:
            response = self._llm.chat(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2048,
                temperature=0.3,
            )
            generated_code = response.content

            result = proposer.propose(
                skill_description=request,
                generated_code=generated_code,
                data_dir=self._settings.data_path,
            )
            session.add_message(Role.ASSISTANT, result.output)
            return result.output
        except Exception as e:
            logger.exception("Skill proposal generation failed")
            msg = f"I couldn't generate a skill proposal: {e}"
            session.add_message(Role.ASSISTANT, msg)
            return msg

    def _load_persona_file(self, profile_id: str) -> str:
        """Load the persona.md file for a profile, if it exists."""
        persona_path = self._settings.data_path / "profiles" / profile_id / "persona.md"
        if persona_path.is_file():
            try:
                return persona_path.read_text(encoding="utf-8")
            except Exception:
                logger.warning("Failed to read persona.md for profile %s", profile_id)
        return ""

    def _build_skill_descriptions(self) -> str:
        """Build a description of available skills for the system prompt."""
        skills = self._skills.skills
        if not skills:
            return ""
        lines = ["Available skills:"]
        for name, skill in skills.items():
            lines.append(f"- {name}: {skill.description}")
            if skill.trigger_phrases:
                lines.append(f"  Triggers: {', '.join(skill.trigger_phrases)}")
        return "\n".join(lines)
