"""Clawless admin service entry point.

Starts the proposal pipeline loop. Run with: cl-admin
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import yaml

from clawless.admin.notifier import CLINotifier
from clawless.admin.service import AdminService
from clawless.user.config import load_settings
from clawless.user.llm import LLMRouter


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cl-admin",
        description="Clawless admin service — proposal pipeline",
    )
    parser.add_argument(
        "--config",
        default="",
        help="Path to main config file (default: auto-detect)",
    )
    parser.add_argument(
        "--admin-config",
        default="",
        help="Path to admin config file (default: auto-detect)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process pending proposals once and exit (no loop)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    return parser.parse_args(argv)


def _find_admin_config() -> Path | None:
    """Locate admin.yaml in the config/ directory."""
    from clawless.user.config import _find_config_sibling

    return _find_config_sibling("admin.yaml")


def _load_admin_config(path: Path | None = None) -> dict:
    """Load admin service configuration."""
    if path is None:
        path = _find_admin_config()
    if path is None or not path.is_file():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def _build_system_context() -> dict:
    """Gather system context for the implementation generator."""
    import platform as plat

    context = {
        "python_version": plat.python_version(),
        "platform": plat.machine(),
        "installed_packages": _get_installed_packages(),
    }
    return context


def _get_installed_packages() -> list[str]:
    """List installed Python packages."""
    try:
        from importlib.metadata import distributions

        return sorted({d.metadata["Name"] for d in distributions() if d.metadata["Name"]})
    except Exception:
        return []


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger(__name__)

    # Load configs
    config_path = Path(args.config) if args.config else None
    settings = load_settings(config_path)

    admin_config_path = Path(args.admin_config) if args.admin_config else None
    admin_config = _load_admin_config(admin_config_path)

    # Set up LLM router (admin service uses its own LLM calls)
    llm_router = LLMRouter()
    for endpoint in settings.llm_endpoints:
        llm_router.add_endpoint(endpoint)

    llm_chat = None
    if llm_router.provider_count > 0:
        def llm_chat(messages, max_tokens=2048, temperature=0.2):
            response = llm_router.chat(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return response.content

    # Set up notifier
    notifier = CLINotifier()

    # Build admin service
    gates = admin_config.get("gates", {
        "discovered": "auto",
        "implementation": "auto",
        "agent-review": "auto",
        "human-review": "human",
    })

    service = AdminService(
        proposals_dir=settings.data_path / "proposals",
        skills_dir=Path(settings.skills_dir) if settings.skills_dir else settings.data_path / "skills",
        manifest_path=Path("config/skills_manifest.yaml"),
        notifier=notifier,
        gates=gates,
        llm_chat=llm_chat,
        system_context=_build_system_context(),
        poll_interval=admin_config.get("poll_interval_seconds", 30),
    )

    logger.info("Clawless admin service starting")

    if args.once:
        service.run_once()
    else:
        try:
            service.run_loop()
        except KeyboardInterrupt:
            logger.info("Shutting down")

    llm_router.close()


if __name__ == "__main__":
    main()
