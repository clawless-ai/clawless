"""Notification and approval channels for the admin service.

MVP: CLINotifier (interactive terminal prompts).
Future: EmailNotifier, WebhookNotifier, etc.
"""

from __future__ import annotations

import logging
import textwrap
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class Notifier(ABC):
    """Base class for human notification/approval channels."""

    @abstractmethod
    def notify(self, proposal: dict, status: str, message: str) -> None:
        """Send an informational notification about a proposal."""

    @abstractmethod
    def request_approval(self, proposal: dict, status: str, context: dict) -> bool:
        """Request human approval. Returns True if approved, False if rejected."""


class CLINotifier(Notifier):
    """Interactive CLI notifier — prints summaries and prompts for input."""

    def notify(self, proposal: dict, status: str, message: str) -> None:
        name = proposal.get("proposal", {}).get("name", "unknown")
        print(f"\n  [{status.upper()}] {name}: {message}")

    def request_approval(self, proposal: dict, status: str, context: dict) -> bool:
        spec = proposal.get("proposal", {})
        name = spec.get("name", "unknown")
        description = spec.get("description", "")
        capabilities = spec.get("capabilities", [])
        rationale = spec.get("rationale", "")

        separator = "=" * 60
        print(f"\n{separator}")
        print(f"  SKILL PROPOSAL: {name}")
        print(separator)
        print(f"\n  Description: {description}")
        print(f"  Capabilities: {', '.join(capabilities)}")
        if rationale:
            print(f"  Rationale: {rationale}")

        # Show extra context (implementation code, analysis, etc.)
        code_path = context.get("code_path")
        if code_path:
            print(f"\n  Implementation: {code_path}")

        analysis = context.get("analysis")
        if analysis:
            issues = analysis.get("issues", [])
            if issues:
                print(f"\n  Analysis: {len(issues)} issue(s) found")
                for issue in issues[:5]:
                    print(f"    - {issue}")
            else:
                print("\n  Analysis: CLEAN (no issues found)")

        print(f"\n  Status: {status}")
        print(f"\n  [A]pprove  [R]eject")
        print(separator)

        while True:
            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  Rejected (interrupted)")
                return False

            if choice in ("a", "approve", "y", "yes"):
                return True
            if choice in ("r", "reject", "n", "no"):
                return False
            print("  Please enter 'a' to approve or 'r' to reject.")
