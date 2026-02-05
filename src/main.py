"""Clawless entry point — CLI argument parsing, initialization, and channel startup."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from clawless.agent import Agent
from clawless.channels.text import TextChannel
from clawless.config import Settings, load_settings
from clawless.llm.router import LLMRouter
from clawless.safety.guard import SafetyGuard
from clawless.skills.base import SkillRegistry
from clawless.skills.proposer import SkillProposer
from clawless.utils.helpers import ensure_profile_dirs, ensure_proposals_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="clawless",
        description="Clawless — a minimal, restricted, memory-only agent framework",
    )
    parser.add_argument(
        "--profile", "-p",
        default="",
        help="Profile ID to use (default: from config)",
    )
    parser.add_argument(
        "--channel", "-c",
        choices=["text", "voice"],
        default="text",
        help="Interaction channel (default: text)",
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
    return parser.parse_args(argv)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def init_app(args: argparse.Namespace) -> tuple[Agent, str, str]:
    """Initialize all components and return (agent, profile_id, channel_name)."""

    # Load config
    config_path = Path(args.config) if args.config else None
    settings = load_settings(config_path)

    # Apply CLI overrides
    if args.data_dir:
        settings.data_dir = args.data_dir
    if args.log_level:
        settings.log_level = args.log_level

    setup_logging(settings.log_level)
    logger = logging.getLogger(__name__)

    profile_id = args.profile or settings.default_profile
    channel_name = args.channel

    logger.info("Clawless starting — profile=%s, channel=%s", profile_id, channel_name)
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
            "No LLM endpoints configured. The agent will not be able to generate responses. "
            "Configure endpoints in config/default.yaml or via environment variables."
        )

    # Initialize safety guard
    guard = SafetyGuard(
        max_input_length=settings.safety.max_input_length,
        max_output_length=settings.safety.max_output_length,
        blocklist_path=settings.safety.blocklist_file,
    )

    # Initialize skill registry
    registry = SkillRegistry()

    # Register built-in skills
    proposer = SkillProposer(data_dir=settings.data_path)
    registry.register(proposer)

    # Load skills from manifest (if it exists)
    manifest_path = Path(settings.skills_dir) / "skills_manifest.yaml" if settings.skills_dir else None
    if manifest_path and manifest_path.is_file():
        registry.load_from_manifest(manifest_path)

    # Create the agent
    agent = Agent(
        settings=settings,
        llm_router=llm_router,
        skill_registry=registry,
        safety_guard=guard,
    )

    return agent, profile_id, channel_name


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    agent, profile_id, channel_name = init_app(args)

    if channel_name == "text":
        channel = TextChannel(agent=agent, profile_id=profile_id)
        channel.run()
    elif channel_name == "voice":
        print("Voice channel is not yet implemented. Use --channel text.", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Unknown channel: {channel_name}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
