"""Implementation generator — creates skill code from YAML proposals.

The admin service has full system access. It uses the LLM with detailed
system context (Python version, installed packages, device paths, skill
interface) to generate working BaseSkill implementations from abstract
proposal specs.
"""

from __future__ import annotations

import json
import logging
import re
import textwrap
from pathlib import Path

from clawless.admin.analyzer import FORBIDDEN_BUILTINS, FORBIDDEN_IMPORTS

logger = logging.getLogger(__name__)

_INFEASIBLE_PREFIX = "INFEASIBLE:"


class InfeasibleProposalError(Exception):
    """Raised when the LLM determines a proposal cannot be implemented within constraints."""

_IMPLEMENTATION_PROMPT = """\
Generate a complete Python skill module for the Clawless agent framework.

## Skill interface
The skill must subclass BaseSkill from clawless.user.skills.base.

Required overrides:
- name (property) -> str: unique skill identifier
- description (property) -> str: one-line description
- capabilities (property) -> frozenset[str]: declared capability tokens
- handles_events (property) -> list[str]: should be [] for tool-based skills
- handle(self, event: Event, ctx: KernelContext) -> SkillResult | None: return None for tool-based skills
- tools (property) -> list[BaseTool]: list of tool instances this skill provides

Optional overrides:
- version (property) -> str
- trigger_phrases (property) -> list[str]
- dependencies (property) -> list[str]
- on_load(self, ctx: KernelContext) -> None
- on_unload(self) -> None

## Tool interface
Skills expose functionality via BaseTool subclasses (from clawless.user.skills.base).
Each tool is a function the LLM can call with typed parameters.

Required overrides for each BaseTool:
- name (property) -> str: tool name for LLM function-calling (e.g. "get_weather")
- description (property) -> str: what the tool does (shown to the LLM)
- parameters_schema (property) -> dict: JSON Schema for input parameters, e.g.:
    {{"type": "object", "properties": {{"location": {{"type": "string", "description": "City name"}}}}, "required": ["location"]}}
- execute(self, **kwargs) -> str: execute the tool and return a result string (or JSON string)

The LLM calls tools with parameters populated from conversation context and user memories.
For example, if the user asks about weather and the LLM knows their location from memory,
it will call get_weather(location="Berlin") automatically.

## Types
- Event: type (str), payload (str), source (str), session_id, profile_id, metadata (dict)
- KernelContext: llm (LLMRouter), settings, system_profile, dispatch (callable), data_dir (Path)
- SkillResult: success (bool), output (str), data (dict)
- BaseTool: name, description, parameters_schema, execute(**kwargs) -> str

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
Tools: {tools}
Rationale: {rationale}

## Rules
1. Only import from: clawless.user.skills.base, clawless.user.types, and standard library
2. Additional third-party imports are allowed ONLY from: {installed_packages}
3. Do NOT import any of these modules (they are blocked by security policy): {forbidden_imports}
4. Do NOT call any of these builtins: {forbidden_builtins}
5. Write clean, well-structured Python 3.11+ code
6. Include appropriate error handling
7. The module should be self-contained
8. The handle() method MUST be synchronous (not async) — the kernel calls it without await
9. Do NOT handle "user_input" events — that is reserved for the reasoning skill
10. Expose all functionality through BaseTool subclasses with typed parameters_schema
11. Set handles_events to [] and have handle() return None — the LLM invokes tools directly

If the proposal is impossible to implement within these constraints, respond with \
ONLY a single line: INFEASIBLE: <reason>

Otherwise, respond with ONLY the Python code (no markdown fences, no explanation)."""


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

    # Filter out forbidden modules from the available packages list
    allowed_packages = [
        pkg for pkg in system_context.get("installed_packages", [])
        if pkg.lower().replace("-", "_") not in FORBIDDEN_IMPORTS
    ]

    # Format tools spec for the prompt
    tools_spec = spec.get("tools", [])
    if tools_spec:
        tools_str = json.dumps(tools_spec, indent=2)
    else:
        tools_str = "(none specified — infer appropriate tools from the description)"

    prompt = _IMPLEMENTATION_PROMPT.format(
        python_version=system_context.get("python_version", "3.11"),
        installed_packages=", ".join(allowed_packages),
        platform=system_context.get("platform", "unknown"),
        name=spec.get("name", "unnamed"),
        description=spec.get("description", ""),
        capabilities=", ".join(spec.get("capabilities", [])),
        dependencies=", ".join(spec.get("dependencies", [])),
        handles_events=", ".join(spec.get("handles_events", [])),
        tools=tools_str,
        rationale=spec.get("rationale", ""),
        forbidden_imports=", ".join(sorted(FORBIDDEN_IMPORTS)),
        forbidden_builtins=", ".join(sorted(FORBIDDEN_BUILTINS)),
    )

    raw = llm_chat(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2048,
        temperature=0.2,
    )

    stripped = raw.strip()
    if stripped.upper().startswith(_INFEASIBLE_PREFIX):
        reason = stripped[len(_INFEASIBLE_PREFIX):].strip()
        raise InfeasibleProposalError(reason)

    code = _clean_code(raw)
    return code


def write_implementation(code: str, slug: str, output_dir: Path) -> Path:
    """Write generated code to a file in the output directory.

    Args:
        code: The generated Python code.
        slug: Kebab-case skill slug from the proposal (e.g. "weather-service").
        output_dir: Directory to write the file into.

    Returns the path to the written file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{slug}.py"
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
