"""Skill system: abstract base classes, registry, and manifest-based loader.

Skills are loaded ONLY at startup from a manifest file (skills_manifest.yaml).
The manifest lists module paths that are allowed to be imported. After startup,
no further dynamic imports are performed.
"""

from __future__ import annotations

import importlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import yaml

from clawless.user.types import Event, KernelContext, SkillOrigin, SkillResult

logger = logging.getLogger(__name__)


class BaseTool(ABC):
    """A tool that a skill can expose for the agent to use."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique tool name (used in LLM function-calling)."""

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this tool does."""

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """JSON Schema for the tool's parameters. Override if the tool takes arguments."""
        return {}

    @abstractmethod
    def execute(self, **kwargs: Any) -> str:
        """Execute the tool with the given arguments and return a string result."""


class BaseSkill(ABC):
    """Abstract base class for all Clawless skills.

    Skills communicate exclusively via events dispatched through the kernel.
    They never hold direct references to each other.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique skill identifier (e.g. 'cli', 'memory', 'skill-proposer')."""

    @property
    @abstractmethod
    def description(self) -> str:
        """What this skill does (shown to the LLM in the system prompt)."""

    @property
    def version(self) -> str:
        """Skill version string."""
        return "0.1.0"

    @property
    def origin(self) -> SkillOrigin:
        """How this skill was added (metadata only, no runtime effect)."""
        return SkillOrigin.BUILTIN

    @property
    def capabilities(self) -> frozenset[str]:
        """Capability tokens this skill declares.

        The kernel enforces these at runtime: a skill can only dispatch events
        that require capabilities it has declared.

        Standard tokens: user:input, user:output, memory:read, memory:write,
        llm:call, file:write, audio:read, audio:write, gpio:read, gpio:write,
        network:read, network:write.
        """
        return frozenset()

    @property
    def handles_events(self) -> list[str]:
        """Event types this skill responds to (e.g. ['user_input', 'memory_query'])."""
        return []

    @property
    def dependencies(self) -> list[str]:
        """Names of skills this skill requires to function."""
        return []

    @property
    def trigger_phrases(self) -> list[str]:
        """Optional phrases that hint the agent should use this skill."""
        return []

    @property
    def tools(self) -> list[BaseTool]:
        """Tools this skill provides. Override to expose tools."""
        return []

    def on_load(self, ctx: KernelContext) -> None:
        """Called once after all skills are registered and the kernel is ready.

        Use this for initialization that needs kernel services (LLM, config, etc.).
        """

    @abstractmethod
    def handle(self, event: Event, ctx: KernelContext) -> SkillResult | None:
        """Handle an event. Return a SkillResult if handled, else None."""

    def run(self, ctx: KernelContext) -> None:
        """Main loop for driver skills (e.g. communication skills).

        Only called on the skill that owns the primary interaction loop.
        Most skills do NOT override this.
        """

    def on_unload(self) -> None:
        """Called on shutdown. Clean up resources."""


class SkillRegistry:
    """Registry of loaded skills.

    Skills are loaded once at startup via a manifest file. The manifest lists
    fully-qualified module paths and the class name within each module.
    """

    def __init__(self) -> None:
        self._skills: dict[str, BaseSkill] = {}
        self._frozen = False

    @property
    def skills(self) -> dict[str, BaseSkill]:
        return dict(self._skills)

    @property
    def skill_names(self) -> list[str]:
        return list(self._skills.keys())

    @property
    def all_tools(self) -> list[BaseTool]:
        """All tools from all registered skills."""
        tools = []
        for skill in self._skills.values():
            tools.extend(skill.tools)
        return tools

    def freeze(self) -> None:
        """Prevent further skill registration. Called after boot."""
        self._frozen = True
        logger.info("Skill registry frozen with %d skills", len(self._skills))

    def register(self, skill: BaseSkill) -> None:
        """Register a skill instance."""
        if self._frozen:
            raise RuntimeError("Skill registry is frozen — cannot register after boot")
        if skill.name in self._skills:
            logger.warning("Skill '%s' already registered, skipping duplicate", skill.name)
            return
        self._skills[skill.name] = skill
        logger.info("Registered skill: %s", skill.name)

    def get(self, name: str) -> BaseSkill | None:
        return self._skills.get(name)

    def find_by_event(self, event_type: str) -> list[BaseSkill]:
        """Find skills that handle a given event type."""
        return [s for s in self._skills.values() if event_type in s.handles_events]

    def find_by_trigger(self, text: str) -> list[BaseSkill]:
        """Find skills whose trigger phrases match the input text."""
        text_lower = text.lower()
        matches = []
        for skill in self._skills.values():
            for phrase in skill.trigger_phrases:
                if phrase.lower() in text_lower:
                    matches.append(skill)
                    break
        return matches

    def find_driver(self) -> BaseSkill | None:
        """Find the skill with user:input capability (the primary interaction driver)."""
        for skill in self._skills.values():
            if "user:input" in skill.capabilities:
                return skill
        return None

    def load_from_manifest(self, manifest_path: Path) -> None:
        """Load skills listed in a YAML manifest file.

        Manifest format:
            skills:
              - module: "clawless.user.skills.cli"
                class: "CLICommunicationSkill"
              - module: "clawless.user.skills.memory"
                class: "MemorySkill"
        """
        if not manifest_path.is_file():
            logger.info("No skill manifest found at %s, starting with no skills", manifest_path)
            return

        with open(manifest_path) as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict) or "skills" not in data:
            logger.warning("Manifest at %s has no 'skills' key", manifest_path)
            return

        for entry in data["skills"]:
            module_path = entry.get("module", "")
            class_name = entry.get("class", "")
            if not module_path or not class_name:
                logger.warning("Skipping manifest entry with missing module/class: %s", entry)
                continue
            try:
                mod = importlib.import_module(module_path)
                cls = getattr(mod, class_name)
                if not (isinstance(cls, type) and issubclass(cls, BaseSkill)):
                    logger.warning(
                        "%s.%s is not a BaseSkill subclass, skipping", module_path, class_name
                    )
                    continue
                instance = cls()
                self.register(instance)
            except Exception:
                logger.exception("Failed to load skill %s.%s", module_path, class_name)
