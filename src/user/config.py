"""Configuration management using Pydantic settings + YAML defaults."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


def _find_config_file() -> Path | None:
    """Locate default.yaml relative to the package or project root."""
    # Check env override first
    env_path = os.getenv("CLAWLESS_CONFIG_FILE")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p

    # Walk up from this file to find config/default.yaml (or .example fallback)
    current = Path(__file__).resolve().parent
    for _ in range(5):
        for parent in (current, current.parent):
            for name in ("default.yaml", "default.yaml.example"):
                candidate = parent / "config" / name
                if candidate.is_file():
                    return candidate
        current = current.parent
    return None


class LLMEndpoint(BaseModel):
    """A single LLM endpoint configuration."""

    name: str
    base_url: str
    model: str
    api_key: str = ""
    provider: str = "openai"  # "openai" (default, works for any compatible API) or "anthropic"
    priority: int = 0
    timeout: float = 30.0
    max_tokens: int = 1024


class VoiceConfig(BaseModel):
    """Voice channel configuration."""

    enabled: bool = False
    wake_word_engine: str = "vosk"
    stt_engine: str = "vosk"
    tts_engine: str = "piper"
    vosk_model_path: str = ""
    piper_model_path: str = ""
    sample_rate: int = 16000


class SafetyConfig(BaseModel):
    """Safety guardrail configuration."""

    blocklist_file: str = ""
    max_input_length: int = 4096
    max_output_length: int = 4096
    system_prompt_template: str = "default"


class MemoryConfig(BaseModel):
    """Memory system configuration."""

    backend: str = "keyword"  # "keyword" or "faiss"
    max_facts_per_profile: int = 10000
    retrieval_top_k: int = 5
    embedding_model: str = "all-MiniLM-L6-v2"  # only used with faiss backend
    extraction_mode: str = "auto"  # "auto" (LLM if available, regex fallback), "llm", or "regex"


class SkillsConfig(BaseModel):
    """Skill system configuration."""

    auto_propose: bool = True  # auto-propose for explicit requests; ask first for implicit gaps


class Settings(BaseSettings):
    """Root application settings.

    Values are resolved in order:
      1. Environment variables (prefixed CLAWLESS_)
      2. YAML config file
      3. Field defaults
    """

    model_config = {"env_prefix": "CLAWLESS_"}

    # Paths
    data_dir: str = Field(default="./data", description="Root data directory for all writable state")
    skills_dir: str = Field(default="", description="Path to enabled skills directory (read-only)")

    # Profile
    default_profile: str = "default"

    # Subsystem configs
    llm_endpoints: list[LLMEndpoint] = Field(default_factory=list)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)

    # Logging
    log_level: str = "INFO"

    @property
    def data_path(self) -> Path:
        """Resolved absolute path to the data directory."""
        return Path(self.data_dir).resolve()

    @property
    def profiles_path(self) -> Path:
        return self.data_path / "profiles"

    @property
    def proposals_path(self) -> Path:
        return self.data_path / "proposals"


def load_yaml_config(path: Path | None = None) -> dict[str, Any]:
    """Load configuration from a YAML file."""
    if path is None:
        path = _find_config_file()
    if path is None or not path.is_file():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def load_settings(config_path: Path | None = None) -> Settings:
    """Create Settings by merging YAML defaults with environment overrides."""
    yaml_data = load_yaml_config(config_path)
    return Settings(**yaml_data)


def load_system_profile(
    profile_path: Path | None = None,
    active_skills: tuple[str, ...] = (),
    skill_descriptions: tuple[tuple[str, str], ...] = (),
) -> "SystemProfile":
    """Load the abstract system profile from YAML.

    The active_skills and skill_descriptions tuples are populated at boot time
    from the skill registry, not from the YAML file.
    """
    from clawless.user.types import SystemProfile

    if profile_path is None:
        profile_path = _find_config_sibling("system_profile.yaml")
    if profile_path is None or not profile_path.is_file():
        return SystemProfile(
            platform="unknown",
            active_skills=active_skills,
            skill_descriptions=skill_descriptions,
        )

    with open(profile_path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        return SystemProfile(
            platform="unknown",
            active_skills=active_skills,
            skill_descriptions=skill_descriptions,
        )

    return SystemProfile(
        platform=data.get("platform", "unknown"),
        available_capabilities=tuple(data.get("available_capabilities", [])),
        active_skills=active_skills,
        skill_descriptions=skill_descriptions,
    )


def _find_config_sibling(filename: str) -> Path | None:
    """Find a file in the config/ directory near this package."""
    current = Path(__file__).resolve().parent
    for _ in range(5):
        for parent in (current, current.parent):
            candidate = parent / "config" / filename
            if candidate.is_file():
                return candidate
        current = current.parent
    return None
