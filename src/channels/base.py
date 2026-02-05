"""Abstract base class for interaction channels (text, voice, etc.)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from clawless.agent import Agent


class BaseChannel(ABC):
    """An interaction channel that feeds user input to the agent and presents responses."""

    def __init__(self, agent: Agent, profile_id: str) -> None:
        self._agent = agent
        self._profile_id = profile_id

    @property
    @abstractmethod
    def name(self) -> str:
        """Channel identifier (e.g. 'text', 'voice')."""

    @abstractmethod
    def run(self) -> None:
        """Start the channel's main loop. Blocks until the channel is stopped."""

    @abstractmethod
    def stop(self) -> None:
        """Signal the channel to stop."""
