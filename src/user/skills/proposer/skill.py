"""Skill proposer — generates YAML-only skill proposals from user requests.

The user agent NEVER generates code. It produces structured YAML proposals
that describe *what* and *why*, never *how*. The admin service handles
implementation. A human admin approves activation.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone

import yaml

from clawless.user.sandbox import ensure_proposals_dir, safe_write_file
from clawless.user.skills.base import BaseSkill
from clawless.user.types import Event, KernelContext, SkillResult

logger = logging.getLogger(__name__)

_PROPOSAL_PROMPT = """\
You are a skill proposal system for an agent framework. The user wants a new \
skill. Your job is to produce a structured JSON specification — NOT code.

## What you know about the system
Platform: {platform}
Available capabilities: {capabilities}
Active skills: {active_skills}

## Standard capability tokens
user:input, user:output — communication with user
memory:read, memory:write — profile memory store
llm:call — invoke LLM
file:write — write to data sandbox
audio:read, audio:write — microphone/speaker
gpio:read, gpio:write — hardware pins
network:read, network:write — outbound HTTP

## Standard event types
user_input (reserved for reasoning skill), memory_query, memory_store, skill_proposal

## Instructions
1. Based on the user's request, determine what skill is needed
2. Select only the capability tokens the skill would require
3. Check that required system capabilities are available on this platform
4. Identify any existing skills this new skill should depend on
5. Define the tools (functions) the skill should expose, with typed input parameters
6. Write a clear rationale explaining why this skill is needed

## Response format
Return ONLY a JSON object (no markdown, no explanation, no code fences):
{{
  "name": "skill-name-kebab-case",
  "description": "One sentence description",
  "capabilities": ["token1", "token2"],
  "dependencies": ["existing-skill-name"],
  "handles_events": [],
  "tools": [
    {{
      "name": "tool_function_name",
      "description": "What this tool does",
      "parameters": {{
        "param_name": {{"type": "string", "description": "What this parameter is"}}
      }},
      "required": ["param_name"]
    }}
  ],
  "requirements": {{
    "system_capabilities": ["audio_input"]
  }},
  "rationale": "Why this skill is needed and what it would do.",
  "feasible": true
}}

Set "feasible" to false if the required system capabilities are not available.
If not feasible, explain why in the rationale.

User request: {request}"""


class SkillProposerSkill(BaseSkill):
    """Generates YAML skill proposals from user descriptions."""

    @property
    def name(self) -> str:
        return "skill-proposer"

    @property
    def description(self) -> str:
        return (
            "Propose new skills for the agent. "
            "Produces structured proposals for admin review — never generates code."
        )

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"llm:call", "file:write"})

    @property
    def handles_events(self) -> list[str]:
        return ["skill_proposal"]

    @property
    def trigger_phrases(self) -> list[str]:
        return ["create a skill", "learn how to", "make a skill", "new skill", "add a skill"]

    def handle(self, event: Event, ctx: KernelContext) -> SkillResult | None:
        if event.type == "skill_proposal":
            return self._generate_proposal(event, ctx)
        return None

    def _generate_proposal(self, event: Event, ctx: KernelContext) -> SkillResult:
        """Generate a YAML skill proposal from a user request."""
        request = event.payload
        profile_id = event.profile_id
        profile = ctx.system_profile
        conversation_trail = self._format_conversation_trail(event)

        prompt = _PROPOSAL_PROMPT.format(
            platform=profile.platform,
            capabilities=", ".join(profile.available_capabilities),
            active_skills=", ".join(profile.active_skills),
            request=f"{request}\n\n## Conversation leading to this request\n{conversation_trail}",
        )

        try:
            response = ctx.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1024,
                temperature=0.2,
            )
            spec = _parse_spec(response.content)
        except Exception as e:
            logger.exception("Proposal generation failed")
            return SkillResult(
                success=False,
                output=f"I couldn't generate a skill proposal: {e}",
            )

        # Build the full proposal YAML
        timestamp = datetime.now(timezone.utc).isoformat()
        skill_id = str(uuid.uuid4())
        slug = _sanitize_name(spec.get("name", request))
        proposal = {
            "proposal": {
                "id": skill_id,
                "slug": slug,
                "name": spec.get("name", "unnamed"),
                "description": spec.get("description", ""),
                "capabilities": spec.get("capabilities", []),
                "dependencies": spec.get("dependencies", []),
                "handles_events": spec.get("handles_events", []),
                "tools": spec.get("tools", []),
                "requirements": spec.get("requirements", {}),
                "rationale": spec.get("rationale", ""),
                "user_context": conversation_trail,
                "generated_by": self.name,
                "generated_at": timestamp,
                "profile_id": profile_id,
            },
            "status": "new",
            "history": [
                {
                    "timestamp": timestamp,
                    "status": "new",
                    "actor": self.name,
                },
            ],
        }

        # Write to proposals directory
        ts_slug = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"proposed_{slug}_{ts_slug}.yaml"

        ensure_proposals_dir(ctx.data_dir)
        yaml_content = yaml.dump(proposal, default_flow_style=False, sort_keys=False)
        written_path = safe_write_file(ctx.data_dir, f"proposals/{filename}", yaml_content)

        feasible = spec.get("feasible", True)
        feasibility_note = "" if feasible else " (NOTE: may not be feasible on this system)"

        return SkillResult(
            success=True,
            output=(
                f"Skill proposal '{spec.get('name', 'unnamed')}' written to "
                f"{written_path}{feasibility_note}. "
                f"The admin service will review and implement it."
            ),
            data={"path": str(written_path), "feasible": feasible},
        )


    def _format_conversation_trail(self, event: Event) -> str:
        """Format the conversation history leading to this proposal."""
        history = event.metadata.get("conversation_history", [])
        if not history:
            return event.payload
        lines = []
        for msg in history:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            lines.append(f"{role}: {content}")
        return "\n".join(lines)


def _parse_spec(raw: str) -> dict:
    """Parse the LLM's JSON response into a dict."""
    text = raw.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()

    # Find JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        logger.warning("No JSON object in proposal response: %.200s", text)
        return {"name": "unnamed", "rationale": text}

    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse proposal JSON: %s", e)
        return {"name": "unnamed", "rationale": text}


def _sanitize_name(name: str) -> str:
    """Convert a name into a kebab-case slug for unified file naming."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9\s_-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    # Take first 4 words to keep it concise
    parts = slug.split("-")[:4]
    slug = "-".join(parts)
    return slug[:50] or "unnamed"
