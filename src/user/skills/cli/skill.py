"""CLI communication skill — pure stdin/stdout I/O.

This is the default driver skill. It owns the interactive loop and dispatches
user_input events to the reasoning skill for processing. All conversation
logic (prompt building, LLM calls, intent routing) lives in the reasoning skill.
"""

from __future__ import annotations

import logging
import uuid

from clawless.user.skills.base import BaseSkill
from clawless.user.types import Event, KernelContext, Role, Session, SkillResult

logger = logging.getLogger(__name__)


class CLICommunicationSkill(BaseSkill):
    """Interactive CLI communication via stdin/stdout."""

    @property
    def name(self) -> str:
        return "cli"

    @property
    def description(self) -> str:
        return "Communicate with the user via text in the terminal."

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"user:input", "user:output"})

    @property
    def handles_events(self) -> list[str]:
        return []  # driver skill — runs the loop, doesn't handle dispatched events

    def on_load(self, ctx: KernelContext) -> None:
        self._profile_id = ctx.settings.default_profile
        self._session = Session(
            session_id=str(uuid.uuid4()),
            profile_id=self._profile_id,
            channel="text",
        )

    def handle(self, event: Event, ctx: KernelContext) -> SkillResult | None:
        return None  # driver skill does not handle events

    def run(self, ctx: KernelContext) -> None:
        """Main stdin/stdout interaction loop."""
        print(f"Clawless — profile: {self._profile_id}")
        print("Type 'quit' or 'exit' to end the session.\n")

        while True:
            try:
                user_input = input("You: ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            stripped = user_input.strip()
            if not stripped:
                continue
            if stripped.lower() in ("quit", "exit"):
                print("Goodbye!")
                break

            # Add user message to session history
            self._session.add_message(Role.USER, stripped)

            # Dispatch to reasoning skill for processing
            result = ctx.dispatch(Event(
                type="user_input",
                payload=stripped,
                source=self.name,
                session_id=self._session.session_id,
                profile_id=self._profile_id,
                metadata={"history": self._session.history},
            ))

            if result and result.success:
                response = result.output
            else:
                response = "I'm having trouble processing your request. Please try again."

            # Add assistant response to session history
            self._session.add_message(Role.ASSISTANT, response)
            print(f"Assistant: {response}\n")
