"""Reasoning skill — conversation pipeline, intent routing, capability awareness.

Handles user_input events from communication skills (cli, voice, etc.) and
orchestrates the full conversation pipeline:
  safety check → memory retrieval → prompt building → LLM call → tool execution → memory storage

The LLM is made aware of available skills and can:
  - Invoke skill tools via native LLM tool-calling (function calling)
  - Detect capability gaps and offer to learn new skills
  - Trigger skill proposals via [ACTION:skill_proposal] tags
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from clawless.user.guard import SafetyGuard
from clawless.user.skills.base import BaseSkill
from clawless.user.types import Event, KernelContext, SkillResult

logger = logging.getLogger(__name__)

_ACTION_RE = re.compile(r"^\[ACTION:(\w+)\]\s*")


class ReasoningSkill(BaseSkill):
    """Conversation pipeline and intent routing."""

    @property
    def name(self) -> str:
        return "reasoning"

    @property
    def description(self) -> str:
        return (
            "Processes user messages: builds context, calls the language model, "
            "detects skill invocations and capability gaps, and manages memory storage."
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"llm:call", "memory:read", "memory:write"})

    @property
    def handles_events(self) -> list[str]:
        return ["user_input"]

    def on_load(self, ctx: KernelContext) -> None:
        settings = ctx.settings
        self._guard = SafetyGuard(
            max_input_length=settings.safety.max_input_length,
            max_output_length=settings.safety.max_output_length,
            blocklist_path=settings.safety.blocklist_file,
        )
        self._auto_propose = settings.skills.auto_propose

    def handle(self, event: Event, ctx: KernelContext) -> SkillResult | None:
        if event.type != "user_input":
            return None
        return self._process(event, ctx)

    def _process(self, event: Event, ctx: KernelContext) -> SkillResult:
        """Process a single user message through the full pipeline."""
        user_input = event.payload
        profile_id = event.profile_id
        history = event.metadata.get("history", [])

        # 1. Safety check on input
        input_check = self._guard.check_input(user_input)
        if not input_check.allowed:
            logger.warning("Input blocked: %s", input_check.reason)
            return SkillResult(
                success=True,
                output="I'm sorry, I can't process that input.",
            )

        # 2. Retrieve relevant memories via dispatch
        memory_context = ""
        persona_memories = ""
        result = ctx.dispatch(Event(
            type="memory_query",
            payload=user_input,
            source=self.name,
            session_id=event.session_id,
            profile_id=profile_id,
        ))
        if result and result.success:
            memory_context = result.data.get("memory_context", "")
            persona_memories = result.data.get("persona_context", "")

        # 3. Load persona file
        persona_text = self._load_persona_file(ctx, profile_id)

        # 4. Build skill descriptions with routing instructions
        skill_descriptions = self._build_skill_descriptions(ctx)

        # 5. Build system prompt
        system_prompt = self._guard.build_system_prompt(
            persona=persona_text,
            persona_memories=persona_memories,
            memory_context=memory_context,
            skill_descriptions=skill_descriptions,
        )

        # 6. Call LLM (with tool-calling loop)
        llm_messages = [{"role": "system", "content": system_prompt}]
        llm_messages.extend(history)

        max_tokens = (
            ctx.settings.llm_endpoints[0].max_tokens
            if ctx.settings.llm_endpoints
            else 1024
        )
        tool_kwargs = {"tools": list(ctx.tool_schemas)} if ctx.tool_schemas else {}

        try:
            response = ctx.llm.chat(
                messages=llm_messages,
                max_tokens=max_tokens,
                **tool_kwargs,
            )

            # Tool-calling loop: execute tools and feed results back to the LLM
            for _ in range(3):
                if not response.tool_calls:
                    break

                # Append assistant message (with tool calls) to conversation
                assistant_msg: dict = {"role": "assistant", "tool_calls": response.tool_calls}
                if response.content:
                    assistant_msg["content"] = response.content
                llm_messages.append(assistant_msg)

                # Execute each tool call and append results
                for tc in response.tool_calls:
                    func = tc.get("function", {})
                    tool_name = func.get("name", "")
                    try:
                        args = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        args = {}
                    logger.info("Calling tool '%s' with %s", tool_name, args)
                    result_str = ctx.call_tool(tool_name, args)
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result_str,
                    })

                # Call LLM again with tool results
                response = ctx.llm.chat(
                    messages=llm_messages,
                    max_tokens=max_tokens,
                    **tool_kwargs,
                )

            assistant_text = response.content or ""
        except RuntimeError as e:
            logger.error("LLM call failed: %s", e)
            return SkillResult(
                success=True,
                output="I'm having trouble connecting to my language model. Please try again.",
            )

        # 7. Parse and handle action directives (backward compat for skill_proposal)
        action, clean_text = self._parse_action(assistant_text)
        if action:
            action_result = ctx.dispatch(Event(
                type=action,
                payload=clean_text or user_input,
                source=self.name,
                session_id=event.session_id,
                profile_id=profile_id,
                metadata={"conversation_history": history},
            ))
            if action_result and action_result.success:
                assistant_text = (
                    f"{clean_text}\n\n{action_result.output}" if clean_text else action_result.output
                )
            else:
                assistant_text = clean_text

        # 8. Safety check on output
        output_check = self._guard.check_output(assistant_text)
        if not output_check.allowed:
            logger.warning("Output blocked: %s", output_check.reason)
            assistant_text = "I generated a response but it was filtered by safety checks."

        # 9. Dispatch memory_store (background, non-blocking)
        thread = threading.Thread(
            target=self._store_memories,
            args=(ctx, event, assistant_text),
            daemon=True,
        )
        thread.start()

        return SkillResult(success=True, output=assistant_text)

    def _parse_action(self, text: str) -> tuple[str | None, str]:
        """Extract [ACTION:type] tag from LLM response.

        Returns (event_type, clean_text) where clean_text has the tag stripped.
        """
        match = _ACTION_RE.match(text)
        if match:
            return match.group(1), text[match.end():].strip()
        return None, text

    def _store_memories(self, ctx: KernelContext, event: Event, assistant_text: str) -> None:
        """Extract and store memories in a background thread."""
        try:
            ctx.dispatch(Event(
                type="memory_store",
                payload=event.payload,
                source=self.name,
                session_id=event.session_id,
                profile_id=event.profile_id,
                metadata={"assistant_response": assistant_text},
            ))
        except Exception:
            logger.exception("Background memory storage failed (non-fatal)")

    def _load_persona_file(self, ctx: KernelContext, profile_id: str) -> str:
        """Load persona.md for the current profile, if it exists."""
        persona_path = ctx.data_dir / "profiles" / profile_id / "persona.md"
        if persona_path.is_file():
            try:
                return persona_path.read_text(encoding="utf-8")
            except Exception:
                logger.warning("Failed to read persona.md for profile %s", profile_id)
        return ""

    def _build_skill_descriptions(self, ctx: KernelContext) -> str:
        """Build skill descriptions with routing instructions for the LLM."""
        # List available skills (exclude ourselves and the communication skill)
        skill_lines = []
        for name, desc in ctx.system_profile.skill_descriptions:
            if name in (self.name, "cli"):
                continue
            skill_lines.append(f"- **{name}**: {desc}")

        sections = ["## Your Current Skills"]
        if skill_lines:
            sections.extend(skill_lines)
        else:
            sections.append("(no additional skills loaded)")

        if ctx.tool_schemas:
            sections.append("")
            sections.append(
                "Some skills provide tools (functions) you can call directly. "
                "When the user's request matches a tool's purpose, call it with "
                "appropriate parameters. Use information from the conversation "
                "and user memories to fill in parameters (e.g. if you know the "
                "user's location from memory, use it for location-based tools)."
            )

        sections.append("")
        sections.append("## Handling Requests")

        next_num = 1
        if self._auto_propose:
            sections.append(
                f"{next_num}. If the user EXPLICITLY asks for a new capability "
                "(e.g. 'learn how to...', 'lerne wie man...', 'add a skill for...', "
                "'create a skill that...'), "
                "include `[ACTION:skill_proposal]` at the very start of your response "
                "and explain what you're creating."
            )
            next_num += 1
            sections.append(
                f"{next_num}. If the user's request implies a capability you don't have "
                "and no matching tool is available, "
                "tell them you can't do that yet and offer to learn it. "
                "Do NOT include any action tag — wait for their confirmation."
            )
            next_num += 1
            sections.append(
                f"{next_num}. When the user confirms they want you to learn a previously offered "
                "capability (e.g. 'yes', 'ja', 'sure', 'do it'), "
                "include `[ACTION:skill_proposal]` at the very start of your response."
            )
            next_num += 1
        else:
            sections.append(
                f"{next_num}. If the user's request requires a capability you don't have "
                "and no matching tool is available, "
                "tell them you can't do that yet and offer to learn it. "
                "Do NOT include any action tag — wait for their confirmation."
            )
            next_num += 1
            sections.append(
                f"{next_num}. When the user confirms they want you to learn a new capability "
                "(e.g. 'yes', 'ja', 'sure', 'do it'), "
                "include `[ACTION:skill_proposal]` at the very start of your response."
            )
            next_num += 1

        sections.append(
            f"{next_num}. For normal conversation, "
            "respond naturally without any tags."
        )

        return "\n".join(sections)
