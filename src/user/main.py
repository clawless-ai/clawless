"""Clawless user agent entry point.

Parses CLI arguments, loads config and skills, and boots the kernel.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from clawless.user.config import load_settings, load_system_profile
from clawless.user.kernel import Kernel
from clawless.user.llm import LLMRouter
from clawless.user.sandbox import ensure_profile_dirs, ensure_proposals_dir
from clawless.user.skills.base import SkillRegistry


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cl-bot",
        description="Clawless — safety-first agent framework",
    )
    parser.add_argument(
        "--profile", "-p",
        default="",
        help="Profile ID to use (default: from config)",
    )
    parser.add_argument(
        "--config",
        default="",
        help="Path to YAML config file (default: auto-detect)",
    )
    parser.add_argument(
        "--data-dir",
        default="",
        help="Override data directory path",
    )
    parser.add_argument(
        "--log-level",
        default="",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level override",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (INFO-level) logging",
    )
    return parser.parse_args(argv)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    # Load config
    config_path = Path(args.config) if args.config else None
    settings = load_settings(config_path)

    # Apply CLI overrides
    if args.data_dir:
        settings.data_dir = args.data_dir
    if args.verbose and not args.log_level:
        settings.log_level = "INFO"
    if args.log_level:
        settings.log_level = args.log_level

    setup_logging(settings.log_level)
    logger = logging.getLogger(__name__)

    profile_id = args.profile or settings.default_profile
    logger.info("Clawless starting — profile=%s", profile_id)
    logger.info("Data directory: %s", settings.data_path)

    # Ensure data directories exist
    ensure_profile_dirs(settings.data_path, profile_id)
    ensure_proposals_dir(settings.data_path)

    # Initialize LLM router
    llm_router = LLMRouter()
    for endpoint in settings.llm_endpoints:
        llm_router.add_endpoint(endpoint)

    if llm_router.provider_count == 0:
        logger.warning(
            "No LLM endpoints configured. "
            "Configure endpoints in config/default.yaml or via environment variables."
        )

    # Load skills from manifest
    registry = SkillRegistry()
    manifest_path = Path(settings.skills_dir) / "skills_manifest.yaml" if settings.skills_dir else None
    if manifest_path is None:
        # Auto-detect manifest in config/ directory
        from clawless.user.config import _find_config_sibling

        manifest_path = _find_config_sibling("skills_manifest.yaml")
    if manifest_path:
        registry.load_from_manifest(manifest_path)

    # Load system profile (active_skills + descriptions populated from registry)
    skill_descriptions = tuple(
        (skill.name, skill.description) for skill in registry.skills.values()
    )
    system_profile = load_system_profile(
        active_skills=tuple(registry.skill_names),
        skill_descriptions=skill_descriptions,
    )

    # Boot the kernel
    kernel = Kernel(
        registry=registry,
        llm_router=llm_router,
        settings=settings,
        system_profile=system_profile,
        data_dir=settings.data_path,
    )

    try:
        kernel.boot()
    except RuntimeError as e:
        logger.error("Boot failed: %s", e)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
