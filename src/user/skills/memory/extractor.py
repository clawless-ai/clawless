"""Fact, preference, and persona extraction from conversation turns.

Two extraction paths:
  1. LLM-based (primary) — sends the conversation turn to the LLM for
     semantic extraction. Works in any language, catches nuance and inference.
     Produces multilingual keywords for language-neutral retrieval.
  2. Regex-based (fallback) — fast, offline, English-only pattern matching.

The extraction_mode config controls which path runs:
  "auto"  — try LLM first, fall back to regex on failure (default)
  "llm"   — LLM only (skip if no LLM available)
  "regex" — regex only (offline, English-only)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clawless.user.llm import LLMRouter

logger = logging.getLogger(__name__)

# Max keywords per extraction (prevents LLM from being too verbose)
_MAX_KEYWORDS = 20

# Stop words excluded from auto-generated keywords (regex path)
_STOP_WORDS = frozenset({
    "the", "is", "in", "at", "of", "a", "an", "and", "or", "for",
    "to", "has", "user", "users", "their", "that", "this", "more",
    "less", "like", "keep", "adopt",
})


@dataclass
class Extraction:
    """A single extracted fact, preference, or persona adjustment."""

    type: str  # "fact", "preference", or "persona"
    content: str
    confidence: float
    action: str = "new"  # "new" or "update"
    keywords: list[str] = field(default_factory=list)  # multilingual search terms


# ---------------------------------------------------------------------------
# Regex-based extraction (offline fallback, English-only)
# ---------------------------------------------------------------------------

_FACT_PATTERNS = [
    (r"\b(?:my name is|i'm called|call me)\s+(\w+)", "User's name is {0}"),
    (r"\bi (?:live|am) in\s+(.+?)(?:\.|,|$)", "User lives in {0}"),
    (r"\bi work (?:at|for)\s+(.+?)(?:\.|,|$)", "User works at {0}"),
    (r"\bi am (\d+)\s*(?:years old)?", "User is {0} years old"),
    (r"\bi have (?:a |an )?(\w+(?:\s+\w+)?)\s+(?:named|called)\s+(\w+)", "User has a {0} named {1}"),
    (r"\bi speak\s+(\w+)", "User speaks {0}"),
]

_PREFERENCE_PATTERNS = [
    (r"\bi (?:like|love|enjoy|prefer)\s+(.+?)(?:\.|,|$)", "User likes {0}"),
    (r"\bi (?:don't like|dislike|hate)\s+(.+?)(?:\.|,|$)", "User dislikes {0}"),
    (r"\bmy fav(?:ou?rite)?\s+\w+\s+is\s+(.+?)(?:\.|,|$)", "User's favourite is {0}"),
    (r"\bi (?:always|usually|often)\s+(.+?)(?:\.|,|$)", "User often {0}"),
]

_PERSONA_PATTERNS = [
    (r"\b(?:be more|sound more|act more)\s+(\w+)", "Adopt a more {0} tone"),
    (r"\b(?:be less|sound less)\s+(\w+)", "Adopt a less {0} tone"),
    (r"\b(?:speak|talk|respond)\s+(?:more\s+)?(\w+ly)\b", "Respond {0}"),
    (r"\b(?:speak|talk|respond) (?:like|as) (?:a |an )?(.+?)(?:\.|,|$)", "Speak like {0}"),
    (r"\b(?:use|try)\s+(?:a\s+)?(?:more\s+)?(\w+)\s+(?:language|tone|style)", "Use {0} language"),
    (r"\b(?:can you be|could you be|i'd (?:like|prefer) (?:if )?you (?:were|are))\s+(?:more\s+)?(\w+)", "Be more {0}"),
    (r"\bkeep (?:it|things|your answers?|responses?)\s+(\w+)", "Keep responses {0}"),
]


def _auto_keywords(content: str) -> list[str]:
    """Generate basic keywords from extracted content (for the regex path)."""
    return [
        w
        for w in re.split(r"[^\w]+", content.lower(), flags=re.UNICODE)
        if len(w) > 2 and w not in _STOP_WORDS
    ]


def extract_from_text(text: str) -> list[Extraction]:
    """Extract facts and preferences from user text using regex patterns.

    Offline fallback — English-only, pattern-based. Returns a list of
    Extraction objects with heuristic confidence scores and auto-generated keywords.
    """
    results: list[Extraction] = []

    for pattern, template in _FACT_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            groups = [g.strip() for g in match.groups() if g]
            if groups:
                content = template.format(*groups)
                results.append(Extraction(
                    type="fact", content=content, confidence=0.7,
                    keywords=_auto_keywords(content),
                ))

    for pattern, template in _PREFERENCE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            groups = [g.strip() for g in match.groups() if g]
            if groups:
                content = template.format(*groups)
                results.append(Extraction(
                    type="preference", content=content, confidence=0.6,
                    keywords=_auto_keywords(content),
                ))

    for pattern, template in _PERSONA_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            groups = [g.strip() for g in match.groups() if g]
            if groups:
                content = template.format(*groups)
                results.append(Extraction(
                    type="persona", content=content, confidence=0.7,
                    keywords=_auto_keywords(content),
                ))

    return results


# ---------------------------------------------------------------------------
# LLM-based extraction (primary path, multilingual)
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM_PROMPT = """\
You are a memory extraction system. Your job is to analyze a conversation turn \
and extract personal information about the user that is worth remembering.

## What to extract
- **Facts**: concrete information about the user (name, location, age, job, \
family members, pets, languages spoken, etc.)
- **Preferences**: likes, dislikes, interests, habits, favourite things
- **Persona**: requests directed at the assistant to change its behavior, tone, \
style, verbosity, or communication approach

## Rules
1. Extract from the USER's messages only (the assistant's response is context)
2. Always write the "content" field in English, regardless of conversation language
3. Be concise — write short statements like "User's name is Alice" or \
"User lives in Amsterdam", not full sentences
4. For persona: extract BEHAVIOR/TONE changes only (e.g. "Keep responses short", \
"Adopt a more casual tone"), NOT identity statements about the assistant
5. Compare against existing memories listed below:
   - Use action "update" if the new info corrects or supersedes an existing memory
   - Use action "new" if this is novel information
   - Do NOT extract something that already exists unchanged
6. Only extract what is clearly stated or strongly implied — do not speculate
7. Set confidence between 0.5 (implied/uncertain) and 1.0 (explicitly stated)
8. For each item, include a "keywords" array (5-15 terms) containing:
   - Key search terms in English
   - If the conversation is NOT in English, also include equivalent terms \
in the original conversation language (transliterated if needed)
   - Include synonyms and closely related terms
   - All keywords must be lowercase

## Response format
Return ONLY a JSON array with no markdown formatting, no explanation, no code fences:
[{"type": "fact", "content": "...", "confidence": 0.9, "action": "new", \
"keywords": ["term1", "term2"]}]
Return [] if nothing is extractable."""

_EXTRACTION_USER_TEMPLATE = """\
## Existing memories
{existing_memories}

## Conversation turn
User: {user_message}
Assistant: {assistant_response}"""


def build_extraction_prompt(
    user_message: str,
    assistant_response: str,
    existing_memories: str = "(none)",
) -> list[dict[str, str]]:
    """Build messages for LLM-based extraction.

    Returns a list of message dicts ready for the LLM router.
    """
    return [
        {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _EXTRACTION_USER_TEMPLATE.format(
                existing_memories=existing_memories,
                user_message=user_message,
                assistant_response=assistant_response,
            ),
        },
    ]


def _parse_llm_response(raw: str) -> list[Extraction]:
    """Parse the LLM's JSON response into Extraction objects.

    Handles common LLM quirks: markdown code fences, trailing text, etc.
    """
    text = raw.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()

    # Find the JSON array in the response
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        if text in ("[]", ""):
            return []
        logger.warning("LLM extraction response has no JSON array: %.200s", text)
        return []

    json_str = text[start : end + 1]

    try:
        items = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse LLM extraction JSON: %s — %.200s", e, json_str)
        return []

    if not isinstance(items, list):
        logger.warning("LLM extraction returned non-list: %s", type(items).__name__)
        return []

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry_type = item.get("type", "")
        content = item.get("content", "")
        if entry_type not in ("fact", "preference", "persona") or not content:
            continue

        # Parse keywords: validate, lowercase, cap count
        raw_keywords = item.get("keywords", [])
        if not isinstance(raw_keywords, list):
            raw_keywords = []
        keywords = [
            str(k).lower().strip()
            for k in raw_keywords
            if k and isinstance(k, str)
        ][:_MAX_KEYWORDS]

        results.append(
            Extraction(
                type=entry_type,
                content=content,
                confidence=min(1.0, max(0.0, float(item.get("confidence", 0.7)))),
                action=item.get("action", "new"),
                keywords=keywords,
            )
        )

    return results


def extract_with_llm(
    user_message: str,
    assistant_response: str,
    existing_memories: str,
    llm_router: LLMRouter,
) -> list[Extraction]:
    """Run LLM-based extraction on a conversation turn.

    Sends the turn + existing memories to the LLM and parses the structured
    JSON response. Works in any language — the LLM normalizes content to English
    and produces multilingual keywords for retrieval.

    Raises RuntimeError if the LLM call fails (caller should handle fallback).
    """
    messages = build_extraction_prompt(user_message, assistant_response, existing_memories)
    response = llm_router.chat(
        messages=messages,
        max_tokens=512,
        temperature=0.1,
    )
    extractions = _parse_llm_response(response.content)
    logger.debug(
        "LLM extraction found %d entries (provider: %s)",
        len(extractions),
        response.provider_name,
    )
    return extractions
