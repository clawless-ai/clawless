"""Admin service — stateful proposal pipeline with configurable gates.

Runs as a continuous loop, polling for new proposals and driving them
through the status lifecycle:

  new → discovered → implementation → agent-review → human-review → accepted/rejected

Each status transition can be configured to require human approval or
proceed automatically.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from clawless.admin.analyzer import analyze_file
from clawless.admin.implementer import generate_implementation, write_implementation
from clawless.admin.notifier import Notifier

logger = logging.getLogger(__name__)

# Valid proposal statuses in lifecycle order
_STATUSES = ("new", "discovered", "implementation", "agent-review", "human-review", "accepted", "rejected")


class AdminService:
    """Pipeline loop for processing skill proposals."""

    def __init__(
        self,
        proposals_dir: Path,
        skills_dir: Path,
        manifest_path: Path,
        notifier: Notifier,
        gates: dict[str, str],
        llm_chat: callable | None = None,
        system_context: dict | None = None,
        poll_interval: int = 30,
    ) -> None:
        self._proposals_dir = proposals_dir
        self._skills_dir = skills_dir
        self._manifest_path = manifest_path
        self._notifier = notifier
        self._gates = gates  # status → "auto" or "human"
        self._llm_chat = llm_chat
        self._system_context = system_context or {}
        self._poll_interval = poll_interval

    def run_loop(self) -> None:
        """Run the continuous polling loop."""
        logger.info(
            "Admin service started — polling %s every %ds",
            self._proposals_dir,
            self._poll_interval,
        )
        while True:
            try:
                self._scan_and_process()
            except KeyboardInterrupt:
                logger.info("Admin service stopped by user")
                break
            except Exception:
                logger.exception("Error in admin service loop")
            time.sleep(self._poll_interval)

    def run_once(self) -> None:
        """Process all pending proposals once (for testing / one-shot mode)."""
        self._scan_and_process()

    def _scan_and_process(self) -> None:
        """Scan proposals directory and advance each proposal through the pipeline."""
        if not self._proposals_dir.is_dir():
            return

        for path in sorted(self._proposals_dir.glob("proposed_*.yaml")):
            try:
                proposal = self._load_proposal(path)
                if proposal is None:
                    continue
                self._process_proposal(proposal, path)
            except Exception:
                logger.exception("Failed to process proposal %s", path.name)

    def _process_proposal(self, proposal: dict, path: Path) -> None:
        """Drive a single proposal through the pipeline."""
        status = proposal.get("status", "new")

        if status == "new":
            self._transition(proposal, path, "discovered", self._validate_schema)

        status = proposal.get("status")
        if status == "discovered":
            self._transition(proposal, path, "implementation", self._generate_code)

        status = proposal.get("status")
        if status == "implementation":
            self._transition(proposal, path, "agent-review", self._run_analysis)

        status = proposal.get("status")
        if status == "agent-review":
            self._transition(proposal, path, "human-review", None)

        status = proposal.get("status")
        if status == "human-review":
            self._transition(proposal, path, "accepted", self._install_skill)

    def _transition(
        self,
        proposal: dict,
        path: Path,
        target_status: str,
        action: callable | None,
    ) -> None:
        """Attempt to transition a proposal to the next status.

        Checks the gate config, optionally runs an action, and requests
        human approval if the gate requires it.
        """
        gate = self._gates.get(target_status, "auto")

        # Run the action (if any) before the gate check
        if action:
            try:
                action(proposal, path)
            except Exception as e:
                self._reject(proposal, path, f"Action failed: {e}")
                return

        # Check gate
        if gate == "human":
            context = proposal.get("_context", {})
            approved = self._notifier.request_approval(proposal, target_status, context)
            if not approved:
                self._reject(proposal, path, f"Rejected at {target_status} gate")
                return

        # Transition
        proposal["status"] = target_status
        self._append_history(proposal, target_status, "admin-service")
        self._save_proposal(proposal, path)
        self._notifier.notify(proposal, target_status, f"Transitioned to {target_status}")
        logger.info("Proposal %s → %s", path.name, target_status)

    def _validate_schema(self, proposal: dict, path: Path) -> None:
        """Validate the proposal YAML has required fields."""
        spec = proposal.get("proposal", {})
        required = ("name", "description", "capabilities")
        missing = [f for f in required if not spec.get(f)]
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(missing)}")

    def _generate_code(self, proposal: dict, path: Path) -> None:
        """Generate implementation code from the proposal spec."""
        if self._llm_chat is None:
            raise RuntimeError("No LLM configured for code generation")

        code = generate_implementation(proposal, self._llm_chat, self._system_context)
        skill_name = proposal.get("proposal", {}).get("name", "unnamed")
        code_path = write_implementation(code, skill_name, self._proposals_dir / "_implementations")

        # Store the code path in the proposal for later stages
        proposal.setdefault("_context", {})["code_path"] = str(code_path)

    def _run_analysis(self, proposal: dict, path: Path) -> None:
        """Run static analysis and composition checks on generated code."""
        code_path_str = proposal.get("_context", {}).get("code_path")
        if not code_path_str:
            raise RuntimeError("No implementation code path found")

        code_path = Path(code_path_str)
        active_skills = self._get_active_skill_capabilities()
        result = analyze_file(code_path, proposal, active_skills)

        proposal.setdefault("_context", {})["analysis"] = {
            "clean": result.clean,
            "issues": result.issues,
            "composition_warnings": result.composition_warnings,
        }

        if result.has_critical_issues:
            raise RuntimeError(
                f"Analysis found critical issues: {'; '.join(result.issues)}"
            )

    def _install_skill(self, proposal: dict, path: Path) -> None:
        """Install the accepted skill to the manifest."""
        code_path_str = proposal.get("_context", {}).get("code_path")
        if not code_path_str:
            raise RuntimeError("No implementation code path found")

        skill_name = proposal.get("proposal", {}).get("name", "unnamed")
        self._notifier.notify(
            proposal, "accepted",
            f"Skill '{skill_name}' accepted. Restart the agent to activate."
        )

    def _reject(self, proposal: dict, path: Path, reason: str) -> None:
        """Mark a proposal as rejected."""
        proposal["status"] = "rejected"
        proposal["rejection_reason"] = reason
        self._append_history(proposal, "rejected", "admin-service")
        self._save_proposal(proposal, path)
        self._notifier.notify(proposal, "rejected", reason)
        logger.info("Proposal %s rejected: %s", path.name, reason)

    def _get_active_skill_capabilities(self) -> dict[str, list[str]]:
        """Read active skill capabilities from the manifest."""
        # For now, return empty — the full implementation would parse the
        # manifest and introspect loaded skills
        return {}

    @staticmethod
    def _append_history(proposal: dict, status: str, actor: str) -> None:
        history = proposal.setdefault("history", [])
        history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "actor": actor,
        })

    @staticmethod
    def _load_proposal(path: Path) -> dict | None:
        """Load a proposal YAML file."""
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                return None
            # Skip terminal statuses
            if data.get("status") in ("accepted", "rejected"):
                return None
            return data
        except Exception:
            logger.exception("Failed to load proposal %s", path.name)
            return None

    @staticmethod
    def _save_proposal(proposal: dict, path: Path) -> None:
        """Save proposal back to its YAML file."""
        # Remove internal context before saving
        clean = {k: v for k, v in proposal.items() if not k.startswith("_")}
        with open(path, "w") as f:
            yaml.dump(clean, f, default_flow_style=False, sort_keys=False)
