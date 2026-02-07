"""Implementation generator — creates skill code from YAML proposals.

The admin service has full system access. It uses the LLM with detailed
system context (Python version, installed packages, device paths, skill
interface) to generate working BaseSkill implementations from abstract
proposal specs.
"""

from __future__ import annotations

import logging
import re
import textwrap
from pathlib import Path

logger = logging.getLogger(__name__)

_IMPLEMENTATION_PROMPT = """\
Generate a complete Python skill module for the Clawless agent framework.

## Skill interface
The skill must subclass BaseSkill from clawless.user.skills.base.

Required overrides:
- name (property) -> str: unique skill identifier
- description (property) -> str: one-line description
- capabilities (property) -> frozenset[str]: declared capability tokens
- handles_events (property) -> list[str]: event types this skill responds to
- handle(self, event: Event, ctx: KernelContext) -> SkillResult | None

Optional overrides:
- version (property) -> str
- trigger_phrases (property) -> list[str]
- dependencies (property) -> list[str]
- on_load(self, ctx: KernelContext) -> None
- run(self, ctx: KernelContext) -> None  (only for driver skills)
- on_unload(self) -> None

## Types
- Event: type (str), payload (str), source (str), session_id, profile_id, metadata (dict)
- KernelContext: llm (LLMRouter), settings, system_profile, dispatch (callable), data_dir (Path)
- SkillResult: success (bool), output (str), data (dict)
- Skills dispatch events via ctx.dispatch(Event(...)) and return SkillResult

## System context
Python version: {python_version}
Installed packages: {installed_packages}
Platform: {platform}

## Proposal spec
Name: {name}
Description: {description}
Capabilities: {capabilities}
Dependencies: {dependencies}
Handles events: {handles_events}
Rationale: {rationale}

## Rules
1. Only import from: clawless.user.skills.base, clawless.user.types, and standard library
2. Additional third-party imports are allowed ONLY from: {installed_packages}
3. Do NOT use: os.system, subprocess, eval, exec, __import__
4. Write clean, well-structured Python 3.11+ code
5. Include appropriate error handling
6. The module should be self-contained

Respond with ONLY the Python code (no markdown fences, no explanation)."""


def generate_implementation(
    proposal: dict,
    llm_chat: callable,
    system_context: dict,
) -> str:
    """Generate a BaseSkill implementation from a proposal spec.

    Args:
        proposal: The full proposal dict (with 'proposal' key).
        llm_chat: Callable that sends messages to an LLM and returns content string.
        system_context: Dict with python_version, installed_packages, platform.

    Returns:
        Generated Python code as a string.
    """
    spec = proposal.get("proposal", {})

    prompt = _IMPLEMENTATION_PROMPT.format(
        python_version=system_context.get("python_version", "3.11"),
        installed_packages=", ".join(system_context.get("installed_packages", [])),
        platform=system_context.get("platform", "unknown"),
        name=spec.get("name", "unnamed"),
        description=spec.get("description", ""),
        capabilities=", ".join(spec.get("capabilities", [])),
        dependencies=", ".join(spec.get("dependencies", [])),
        handles_events=", ".join(spec.get("handles_events", [])),
        rationale=spec.get("rationale", ""),
    )

    raw = llm_chat(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2048,
        temperature=0.2,
    )

    code = _clean_code(raw)
    return code


def write_implementation(code: str, skill_name: str, output_dir: Path) -> Path:
    """Write generated code to a file in the output directory.

    Returns the path to the written file.
    """
    safe_name = re.sub(r"[^a-z0-9_]", "_", skill_name.lower())
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{safe_name}.py"
    path.write_text(code, encoding="utf-8")
    logger.info("Wrote implementation to %s", path)
    return path


def _clean_code(raw: str) -> str:
    """Strip markdown fences and leading/trailing whitespace from LLM output."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    return text
