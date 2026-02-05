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

from clawless.types import Message, Session, SkillResult

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

    A skill encapsulates a capability: it declares trigger phrases,
    exposes tools, and handles execution.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique skill identifier."""

    @property
    @abstractmethod
    def description(self) -> str:
        """What this skill does (shown to the LLM in the system prompt)."""

    @property
    def trigger_phrases(self) -> list[str]:
        """Optional phrases that hint the agent should use this skill."""
        return []

    @property
    def tools(self) -> list[BaseTool]:
        """Tools this skill provides. Override to expose tools."""
        return []

    @abstractmethod
    def handle(self, session: Session, message: Message) -> SkillResult | None:
        """Process a message. Return a SkillResult if this skill handled it, else None."""


class SkillRegistry:
    """Registry of loaded skills.

    Skills are loaded once at startup via a manifest file. The manifest lists
    fully-qualified module paths and the class name within each module.
    """

    def __init__(self) -> None:
        self._skills: dict[str, BaseSkill] = {}

    @property
    def skills(self) -> dict[str, BaseSkill]:
        return dict(self._skills)

    @property
    def all_tools(self) -> list[BaseTool]:
        """All tools from all registered skills."""
        tools = []
        for skill in self._skills.values():
            tools.extend(skill.tools)
        return tools

    def register(self, skill: BaseSkill) -> None:
        """Register a skill instance."""
        if skill.name in self._skills:
            logger.warning("Skill '%s' already registered, skipping duplicate", skill.name)
            return
        self._skills[skill.name] = skill
        logger.info("Registered skill: %s", skill.name)

    def get(self, name: str) -> BaseSkill | None:
        return self._skills.get(name)

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

    def load_from_manifest(self, manifest_path: Path) -> None:
        """Load skills listed in a YAML manifest file.

        Manifest format:
            skills:
              - module: "clawless.skills.proposer"
                class: "SkillProposer"
              - module: "my_custom_skill"
                class: "MySkill"
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
