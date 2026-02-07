"""Core data types for the Clawless user agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from clawless.user.llm import LLMRouter


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


@dataclass
class Message:
    """A single conversational message."""

    role: Role
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    """An active conversation session."""

    session_id: str
    profile_id: str
    messages: list[Message] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    channel: str = "text"

    def add_message(self, role: Role, content: str, **metadata: Any) -> Message:
        msg = Message(role=role, content=content, metadata=metadata)
        self.messages.append(msg)
        return msg

    @property
    def history(self) -> list[dict[str, str]]:
        """Return message history in the format expected by LLM APIs."""
        return [{"role": m.role.value, "content": m.content} for m in self.messages]


@dataclass
class Profile:
    """A user profile with associated memory and preferences."""

    profile_id: str
    display_name: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryEntry:
    """A single memory fact or preference stored in JSONL."""

    type: str  # "fact", "preference", or "persona"
    content: str
    source: str = ""  # e.g. "extracted", "llm_extracted", or "user_stated"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    keywords: list[str] = field(default_factory=list)  # multilingual search terms


@dataclass
class SkillResult:
    """The result of executing a skill."""

    success: bool
    output: str
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# New types for the event-based "everything is a skill" architecture
# ---------------------------------------------------------------------------


class SkillOrigin(str, Enum):
    """How a skill was added to the system (metadata only, no runtime effect)."""

    BUILTIN = "builtin"
    PROPOSED = "proposed"


@dataclass
class Event:
    """An event dispatched through the kernel to skills.

    Skills communicate exclusively via events — they never hold direct
    references to each other.
    """

    type: str  # "user_input", "memory_query", "memory_store", "skill_proposal", etc.
    payload: str
    source: str  # name of the skill that produced this event
    session_id: str = ""
    profile_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SystemProfile:
    """Abstract system capabilities — NO implementation details.

    The user agent only needs to know *what's possible* (e.g. "audio_input
    available"), never *how* (no device paths, package versions, etc.).
    """

    platform: str  # e.g. "raspberry_pi_4"
    available_capabilities: tuple[str, ...] = ()  # ("audio_input", "audio_output", ...)
    active_skills: tuple[str, ...] = ()  # populated at boot from manifest
    skill_descriptions: tuple[tuple[str, str], ...] = ()  # (name, description) pairs


@dataclass(frozen=True)
class KernelContext:
    """Read-only bag of kernel services passed to every skill.

    Frozen so skills cannot mutate shared state.
    """

    llm: LLMRouter
    settings: Any  # Settings (avoid circular import)
    system_profile: SystemProfile
    dispatch: Callable[[Event], SkillResult | None]
    data_dir: Path
