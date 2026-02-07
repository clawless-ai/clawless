"""Admin service — stateful proposal pipeline with configurable gates.

Runs as a continuous loop, polling for new proposals and driving them
through the status lifecycle:

  new → discovered → implementation → agent-review → human-review → accepted/rejected

Each status transition can be configured to require human approval or
proceed automatically.
"""

from __future__ import annotations

import logging
import re
import select
import shutil
import sys
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
        """Run the continuous polling loop with interactive command support."""
        logger.info(
            "Admin service started — polling %s every %ds",
            self._proposals_dir,
            self._poll_interval,
        )
        print("  Type 'help' for available commands.\n")
        while True:
            try:
                self._scan_and_process()
            except KeyboardInterrupt:
                logger.info("Admin service stopped by user")
                break
            except Exception:
                logger.exception("Error in admin service loop")
            # Wait for poll_interval, but wake on stdin input
            try:
                ready, _, _ = select.select([sys.stdin], [], [], self._poll_interval)
                if ready:
                    line = sys.stdin.readline().strip()
                    if line:
                        self._handle_command(line)
            except KeyboardInterrupt:
                logger.info("Admin service stopped by user")
                break

    def run_once(self) -> None:
        """Process all pending proposals once (for testing / one-shot mode)."""
        self._scan_and_process()

    def list_proposals(self, status_filter: str | None = None) -> list[dict]:
        """List all proposals, optionally filtered by status.

        Returns a list of summary dicts with: id, slug, name, created_at, status.
        """
        if not self._proposals_dir.is_dir():
            return []

        results = []
        for path in sorted(self._proposals_dir.glob("proposed_*.yaml")):
            try:
                with open(path) as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    continue
                status = data.get("status", "unknown")
                if status_filter and status != status_filter:
                    continue
                spec = data.get("proposal", {})
                results.append({
                    "id": spec.get("id", ""),
                    "slug": spec.get("slug", ""),
                    "created_at": spec.get("generated_at", ""),
                    "status": status,
                    "path": path,
                })
            except Exception:
                logger.debug("Skipping unreadable proposal %s", path.name)
        return results

    def approve_proposal(self, id_or_slug: str, force: bool = False) -> str:
        """Approve a proposal by ID or slug.

        Args:
            id_or_slug: Full UUID or kebab-case slug to match.
            force: If True, approve regardless of current status.

        Returns:
            Success message string.

        Raises:
            ValueError: If proposal not found or not in approvable status.
        """
        if not self._proposals_dir.is_dir():
            raise ValueError("Proposals directory not found")

        for path in self._proposals_dir.glob("proposed_*.yaml"):
            try:
                with open(path) as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    continue
                spec = data.get("proposal", {})
                if spec.get("id") != id_or_slug and spec.get("slug") != id_or_slug:
                    continue

                # Found the proposal
                status = data.get("status", "unknown")
                slug = spec.get("slug", "unknown")
                skill_id = spec.get("id", "unknown")

                if not force and status != "human-review":
                    raise ValueError(
                        f"Proposal '{slug}' is in status '{status}', "
                        f"not 'human-review'. Use --force to override."
                    )

                # Transition to accepted
                self._install_skill(data, path)
                data["status"] = "accepted"
                self._append_history(data, "accepted", "admin-cli")
                self._save_proposal(data, path)
                logger.info("Proposal %s approved via CLI", path.name)
                return f"Approved: {slug} ({skill_id})"
            except (ValueError, RuntimeError):
                raise
            except Exception:
                logger.exception("Error processing proposal %s", path.name)

        raise ValueError(f"No proposal found matching '{id_or_slug}'")

    _CORE_MODULES = frozenset({"cli", "reasoning", "memory", "proposer"})

    def remove_skill(self, id_or_slug: str) -> str:
        """Remove an installed skill by proposal ID or slug.

        Deletes the skill package from the skills directory and removes
        its entry from the manifest. Core skills cannot be removed.

        Returns:
            Success message string.

        Raises:
            ValueError: If proposal not found or skill is a core skill.
            RuntimeError: If removal fails.
        """
        import clawless.user.skills as skills_pkg

        if not self._proposals_dir.is_dir():
            raise ValueError("Proposals directory not found")

        for path in self._proposals_dir.glob("proposed_*.yaml"):
            try:
                with open(path) as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    continue
                spec = data.get("proposal", {})
                if spec.get("id") != id_or_slug and spec.get("slug") != id_or_slug:
                    continue

                slug = spec.get("slug", "unknown")
                skill_id = spec.get("id", "unknown")
                module_name = self._slug_to_module_name(slug)

                # Guard: refuse to remove core skills
                if module_name in self._CORE_MODULES:
                    raise ValueError(
                        f"Cannot remove core skill '{slug}'. "
                        f"Only user-installed skills can be removed."
                    )

                # Determine target directory
                skills_base = Path(skills_pkg.__file__).parent
                target_dir = skills_base / module_name
                module_path = f"clawless.user.skills.{module_name}"

                # Remove from manifest first
                self._remove_from_manifest(module_path)

                # Remove skill directory
                if target_dir.is_dir():
                    shutil.rmtree(target_dir)
                    logger.info("Removed skill directory %s", target_dir)

                # Update proposal status
                data["status"] = "removed"
                self._append_history(data, "removed", "admin-cli")
                self._save_proposal(data, path)

                logger.info("Skill '%s' removed via CLI", slug)
                return f"Removed: {slug} ({skill_id})"
            except (ValueError, RuntimeError):
                raise
            except Exception:
                logger.exception("Error processing proposal %s", path.name)

        raise ValueError(f"No proposal found matching '{id_or_slug}'")

    @staticmethod
    def format_proposals_table(proposals: list[dict]) -> str:
        """Format a list of proposal summaries as a table string."""
        if not proposals:
            return "  No proposals found."

        headers = ("ID", "SLUG", "CREATED_AT", "STATUS")
        rows = []
        for p in proposals:
            created = p.get("created_at", "")
            if "." in created:
                created = created.split(".")[0]
            elif "+" in created:
                created = created.split("+")[0]
            rows.append((
                p.get("id", ""),
                p.get("slug", ""),
                created,
                p.get("status", ""),
            ))

        # Calculate column widths
        widths = [len(h) for h in headers]
        for row in rows:
            for i, val in enumerate(row):
                widths[i] = max(widths[i], len(val))

        def fmt_row(values: tuple) -> str:
            return "  " + " | ".join(v.ljust(widths[i]) for i, v in enumerate(values))

        lines = [fmt_row(headers)]
        lines.append("  " + "-+-".join("-" * w for w in widths))
        for row in rows:
            lines.append(fmt_row(row))
        return "\n".join(lines)

    def _handle_command(self, line: str) -> None:
        """Parse and execute an interactive command during the run loop."""
        parts = line.split()
        cmd = parts[0].lower()

        if cmd == "list":
            status_filter = parts[1] if len(parts) > 1 else None
            proposals = self.list_proposals(status_filter)
            print(self.format_proposals_table(proposals))
        elif cmd == "approve":
            if len(parts) < 2:
                print("  Usage: approve <ID|SLUG> [--force]")
                return
            id_or_slug = parts[1]
            force = "--force" in parts[2:]
            try:
                result = self.approve_proposal(id_or_slug, force=force)
                print(f"  {result}")
            except (ValueError, RuntimeError) as e:
                print(f"  Error: {e}")
        elif cmd == "help":
            print(
                "  Available commands:\n"
                "    list [STATUS]                List proposals (filter by status)\n"
                "    approve <ID|SLUG> [--force]  Approve a proposal\n"
                "    help                         Show this help"
            )
        else:
            print(f"  Unknown command: '{cmd}'. Type 'help' for available commands.")

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
                self._reject(
                    proposal, path, f"Action failed: {e}",
                    reason_type=type(e).__name__,
                )
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
        required = ("id", "slug", "name", "description", "capabilities")
        missing = [f for f in required if not spec.get(f)]
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(missing)}")

    def _generate_code(self, proposal: dict, path: Path) -> None:
        """Generate implementation code from the proposal spec."""
        if self._llm_chat is None:
            raise RuntimeError("No LLM configured for code generation")

        code = generate_implementation(proposal, self._llm_chat, self._system_context)
        spec = proposal.get("proposal", {})
        slug = spec.get("slug", spec.get("name", "unnamed"))
        code_path = write_implementation(code, slug, self._proposals_dir / "_implementations")

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
        """Install the accepted skill: copy to skills dir and update manifest."""
        import clawless.user.skills as skills_pkg

        # 1. Resolve implementation file path
        code_path_str = proposal.get("_context", {}).get("code_path")
        if not code_path_str:
            spec = proposal.get("proposal", {})
            slug = spec.get("slug", spec.get("name", "unnamed"))
            candidate = self._proposals_dir / "_implementations" / f"{slug}.py"
            if candidate.is_file():
                code_path_str = str(candidate)
            else:
                raise RuntimeError(
                    f"No implementation file found for '{slug}' "
                    f"(expected {candidate})"
                )

        code_path = Path(code_path_str)
        spec = proposal.get("proposal", {})
        slug = spec.get("slug", spec.get("name", "unnamed"))
        skill_name = spec.get("name", "unnamed")

        # 2. Extract class name from generated code
        class_name = self._extract_class_name(code_path)

        # 3. Derive Python module path
        module_name = self._slug_to_module_name(slug)
        module_path = f"clawless.user.skills.{module_name}"

        # 4. Determine target directory
        skills_base = Path(skills_pkg.__file__).parent
        target_dir = skills_base / module_name

        if target_dir.exists():
            raise RuntimeError(
                f"Target directory already exists: {target_dir}. "
                f"Skill '{module_name}' may already be installed."
            )

        # 5. Update manifest first (easier to roll back than file ops)
        self._update_manifest(module_path, class_name)

        # 6. Create skill package (with rollback on failure)
        try:
            target_dir.mkdir(parents=True)
            shutil.copy2(code_path, target_dir / "skill.py")
            init_content = (
                f'"""{skill_name} skill."""\n'
                f"\n"
                f"from {module_path}.skill import {class_name}\n"
                f"\n"
                f'__all__ = ["{class_name}"]\n'
            )
            (target_dir / "__init__.py").write_text(init_content, encoding="utf-8")
        except Exception:
            self._remove_from_manifest(module_path)
            if target_dir.exists():
                shutil.rmtree(target_dir)
            raise

        # 7. Notify
        self._notifier.notify(
            proposal, "accepted",
            f"Skill '{skill_name}' installed to {target_dir} and added to manifest. "
            f"Restart the agent to activate.",
        )
        logger.info(
            "Installed skill '%s' (%s.%s) to %s",
            skill_name, module_path, class_name, target_dir,
        )

    @staticmethod
    def _extract_class_name(code_path: Path) -> str:
        """Extract the BaseSkill subclass name from a generated implementation file."""
        content = code_path.read_text(encoding="utf-8")
        match = re.search(r"class\s+(\w+)\s*\(.*\bBaseSkill\b.*\)", content)
        if not match:
            raise RuntimeError(
                f"No BaseSkill subclass found in {code_path}. "
                f"Cannot determine class name for manifest entry."
            )
        return match.group(1)

    @staticmethod
    def _slug_to_module_name(slug: str) -> str:
        """Convert a kebab-case slug to a valid Python module name."""
        return slug.replace("-", "_")

    def _update_manifest(self, module_path: str, class_name: str) -> None:
        """Add a new skill entry to the skills manifest YAML file."""
        if not self._manifest_path.is_file():
            raise RuntimeError(
                f"Manifest file not found at {self._manifest_path}"
            )

        with open(self._manifest_path) as f:
            data = yaml.safe_load(f) or {}

        skills = data.setdefault("skills", [])
        for entry in skills:
            if entry.get("module") == module_path:
                raise RuntimeError(
                    f"Module '{module_path}' already exists in the manifest. "
                    f"Skill may already be installed."
                )

        skills.append({"module": module_path, "class": class_name})
        with open(self._manifest_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        logger.info("Updated manifest: added %s.%s", module_path, class_name)

    def _remove_from_manifest(self, module_path: str) -> None:
        """Remove a skill entry from the manifest (rollback helper)."""
        if not self._manifest_path.is_file():
            return
        with open(self._manifest_path) as f:
            data = yaml.safe_load(f) or {}
        skills = data.get("skills", [])
        data["skills"] = [e for e in skills if e.get("module") != module_path]
        with open(self._manifest_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        logger.info("Rolled back manifest: removed %s", module_path)

    def _reject(
        self, proposal: dict, path: Path, reason: str,
        reason_type: str | None = None,
    ) -> None:
        """Mark a proposal as rejected."""
        proposal["status"] = "rejected"
        proposal["rejection_reason"] = reason
        if reason_type:
            proposal["rejection_reason_type"] = reason_type
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
