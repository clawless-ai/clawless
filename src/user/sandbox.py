"""Path sandboxing and validation utilities.

Every write operation in Clawless MUST go through the functions in this module.
This is the primary enforcement layer for Security Invariant #1.
"""

from __future__ import annotations

import re
from pathlib import Path


class PathViolationError(Exception):
    """Raised when a write targets a path outside the allowed data directory."""


# Allowed subdirectories under the data root that the agent may write to
_ALLOWED_WRITE_SUBDIRS = frozenset({"profiles", "proposals"})

# Profile ID pattern: alphanumeric, hyphens, underscores, 1-64 chars
_PROFILE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def validate_profile_id(profile_id: str) -> str:
    """Validate and return a safe profile identifier.

    Raises ValueError if the profile ID contains unsafe characters or is empty.
    """
    if not _PROFILE_ID_RE.match(profile_id):
        raise ValueError(
            f"Invalid profile ID '{profile_id}'. "
            "Must be 1-64 chars, alphanumeric/hyphens/underscores, starting with alphanumeric."
        )
    return profile_id


def resolve_safe_write_path(data_dir: Path, relative_path: str | Path) -> Path:
    """Resolve a write target path and verify it falls within the data directory.

    Args:
        data_dir: The root data directory (must be absolute or will be resolved).
        relative_path: The path relative to data_dir where the write should go.

    Returns:
        The resolved absolute path.

    Raises:
        PathViolationError: If the resolved path escapes the data directory.
    """
    data_root = Path(data_dir).resolve()
    target = (data_root / relative_path).resolve()

    # The resolved target must be under the data root
    if not _is_path_under(target, data_root):
        raise PathViolationError(
            f"Write target '{target}' is outside data directory '{data_root}'"
        )

    # The first subdirectory must be in the allowed set
    try:
        rel = target.relative_to(data_root)
        top_dir = rel.parts[0] if rel.parts else ""
    except ValueError:
        raise PathViolationError(
            f"Write target '{target}' is outside data directory '{data_root}'"
        )

    if top_dir not in _ALLOWED_WRITE_SUBDIRS:
        raise PathViolationError(
            f"Write target is under '{top_dir}/' which is not an allowed writable subdirectory. "
            f"Allowed: {_ALLOWED_WRITE_SUBDIRS}"
        )

    return target


def ensure_profile_dirs(data_dir: Path, profile_id: str) -> Path:
    """Create profile directory structure if it doesn't exist.

    If a persona.md doesn't exist yet, copies the default template from
    config/persona.default.md (if available).

    Returns the profile root path.
    """
    validate_profile_id(profile_id)
    profile_root = resolve_safe_write_path(data_dir, f"profiles/{profile_id}")
    memory_dir = profile_root / "memory"
    logs_dir = profile_root / "logs"
    memory_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Copy default persona template if persona.md doesn't exist yet
    persona_path = profile_root / "persona.md"
    if not persona_path.exists():
        default_persona = _find_default_persona()
        if default_persona:
            persona_path.write_text(default_persona.read_text(encoding="utf-8"), encoding="utf-8")

    return profile_root


def _find_default_persona() -> Path | None:
    """Locate config/persona.default.md relative to the package."""
    current = Path(__file__).resolve().parent
    for _ in range(5):
        for parent in (current, current.parent):
            candidate = parent / "config" / "persona.default.md"
            if candidate.is_file():
                return candidate
        current = current.parent
    return None


def ensure_proposals_dir(data_dir: Path) -> Path:
    """Create the proposals directory if it doesn't exist."""
    proposals = resolve_safe_write_path(data_dir, "proposals")
    proposals.mkdir(parents=True, exist_ok=True)
    return proposals


def safe_write_file(data_dir: Path, relative_path: str | Path, content: str) -> Path:
    """Write content to a file within the data directory, with full path validation.

    Returns the path that was written to.
    """
    target = resolve_safe_write_path(data_dir, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def safe_append_file(data_dir: Path, relative_path: str | Path, content: str) -> Path:
    """Append content to a file within the data directory, with full path validation.

    Returns the path that was appended to.
    """
    target = resolve_safe_write_path(data_dir, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write(content)
    return target


def safe_read_file(path: Path) -> str:
    """Read a file. No path restrictions on reads — only writes are sandboxed."""
    return path.read_text(encoding="utf-8")


def _is_path_under(path: Path, parent: Path) -> bool:
    """Check if path is equal to or a descendant of parent (both must be resolved)."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
