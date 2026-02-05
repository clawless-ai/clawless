"""Core data types for Clawless."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


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
