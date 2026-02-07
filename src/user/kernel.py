"""Minimal agent kernel — event dispatcher, capability enforcement, boot.

The kernel is the irreducible core of the user agent. It loads skills from a
manifest, wires up shared services, and dispatches events between skills.
All user-facing behavior lives in skills, not here.
"""

from __future__ import annotations

import logging
from pathlib import Path

from clawless.user.llm import LLMRouter
from clawless.user.skills.base import BaseSkill, SkillRegistry
from clawless.user.types import Event, KernelContext, SkillResult, SystemProfile

logger = logging.getLogger(__name__)

# Maps event types to the capability token required to *dispatch* that event.
# If an event type is not listed, any skill may dispatch it.
_EVENT_CAPABILITIES: dict[str, str] = {
    "user_input": "user:input",
    "user_output": "user:output",
    "memory_query": "memory:read",
    "memory_store": "memory:write",
    # skill_proposal: not enforced on dispatcher — the handler (proposer) has file:write
}


class Kernel:
    """Event dispatcher and boot orchestrator.

    Boot sequence:
      1. Load skills from manifest → freeze registry
      2. Validate: at least one skill with ``user:input`` capability exists
      3. Build KernelContext (frozen, read-only)
      4. Call ``on_load(ctx)`` on all skills
      5. Call ``run(ctx)`` on the driver skill (the interaction loop)
    """

    def __init__(
        self,
        registry: SkillRegistry,
        llm_router: LLMRouter,
        settings: object,
        system_profile: SystemProfile,
        data_dir: Path,
    ) -> None:
        self._registry = registry
        self._llm = llm_router
        self._settings = settings
        self._system_profile = system_profile
        self._data_dir = data_dir
        self._ctx: KernelContext | None = None

    def boot(self) -> None:
        """Freeze registry, validate, initialize skills, and run the driver."""
        self._registry.freeze()

        # Validate: must have a driver skill
        driver = self._registry.find_driver()
        if driver is None:
            raise RuntimeError(
                "No skill with 'user:input' capability found. "
                "The agent needs at least one communication skill to start."
            )

        # Build frozen context
        self._ctx = KernelContext(
            llm=self._llm,
            settings=self._settings,
            system_profile=self._system_profile,
            dispatch=self.dispatch,
            data_dir=self._data_dir,
            tool_schemas=self._build_tool_schemas(),
            call_tool=self._call_tool,
        )

        # Lifecycle: on_load
        for skill in self._registry.skills.values():
            logger.debug("Calling on_load for skill '%s'", skill.name)
            skill.on_load(self._ctx)

        # Hand control to the driver skill's main loop
        logger.info("Booting driver skill '%s'", driver.name)
        try:
            driver.run(self._ctx)
        finally:
            self._shutdown()

    def dispatch(self, event: Event) -> SkillResult | None:
        """Route an event to all skills that handle its type.

        Enforces capability tokens: the *source* skill must have the
        capability required to dispatch this event type.

        Returns the first non-None SkillResult, or None if no skill handled it.
        """
        assert self._ctx is not None, "dispatch() called before boot()"

        # Capability enforcement on the dispatching skill
        required_cap = _EVENT_CAPABILITIES.get(event.type)
        if required_cap:
            source_skill = self._registry.get(event.source)
            if source_skill and required_cap not in source_skill.capabilities:
                logger.warning(
                    "Skill '%s' tried to dispatch '%s' without capability '%s' — blocked",
                    event.source,
                    event.type,
                    required_cap,
                )
                return SkillResult(success=False, output=f"Missing capability: {required_cap}")

        # Route to handlers
        handlers = self._registry.find_by_event(event.type)
        for skill in handlers:
            if skill.name == event.source:
                continue  # don't send events back to the source
            try:
                result = skill.handle(event, self._ctx)
                if result is not None:
                    return result
            except Exception:
                logger.exception("Skill '%s' failed handling '%s'", skill.name, event.type)

        return None

    def _build_tool_schemas(self) -> tuple[dict, ...]:
        """Convert all BaseTool instances from the registry to LLM tool-calling format."""
        schemas = []
        for tool in self._registry.all_tools:
            schemas.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_schema or {
                        "type": "object",
                        "properties": {},
                    },
                },
            })
        if schemas:
            logger.info("Built %d tool schema(s) from skill registry", len(schemas))
        return tuple(schemas)

    def _call_tool(self, name: str, arguments: dict) -> str:
        """Execute a registered tool by name."""
        for tool in self._registry.all_tools:
            if tool.name == name:
                try:
                    return tool.execute(**arguments)
                except Exception as e:
                    logger.exception("Tool '%s' failed", name)
                    return f"Tool error: {e}"
        return f"Unknown tool: {name}"

    def _shutdown(self) -> None:
        """Call on_unload on all skills."""
        for skill in self._registry.skills.values():
            try:
                skill.on_unload()
            except Exception:
                logger.exception("Error during on_unload for skill '%s'", skill.name)
        self._llm.close()
