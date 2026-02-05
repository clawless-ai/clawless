"""Text channel — simple CLI stdin/stdout interaction loop."""

from __future__ import annotations

import logging
import sys

from clawless.agent import Agent
from clawless.channels.base import BaseChannel

logger = logging.getLogger(__name__)


class TextChannel(BaseChannel):
    """Interactive text channel using stdin/stdout."""

    def __init__(self, agent: Agent, profile_id: str, prompt: str = "You: ") -> None:
        super().__init__(agent, profile_id)
        self._prompt = prompt
        self._running = False

    @property
    def name(self) -> str:
        return "text"

    def run(self) -> None:
        """Start the interactive CLI loop."""
        self._running = True
        print(f"Clawless text channel — profile: {self._profile_id}")
        print("Type 'quit' or 'exit' to end the session.\n")

        while self._running:
            try:
                user_input = input(self._prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                break

            stripped = user_input.strip()
            if not stripped:
                continue
            if stripped.lower() in ("quit", "exit"):
                print("Goodbye!")
                break

            response = self._agent.process_message(
                profile_id=self._profile_id,
                user_input=stripped,
                channel=self.name,
            )
            print(f"Assistant: {response}\n")

    def stop(self) -> None:
        self._running = False
