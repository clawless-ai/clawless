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
    # Shared arguments on the parent parser
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
        "--log-level",
        default="CRITICAL",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: silent; use -v for INFO)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose (INFO-level) logging",
    )

    subparsers = parser.add_subparsers(dest="command")

    # cl-admin run [--once]
    run_parser = subparsers.add_parser("run", help="Start the proposal pipeline loop")
    run_parser.add_argument(
        "--once",
        action="store_true",
        help="Process pending proposals once and exit (no loop)",
    )

    # cl-admin list [--status STATUS]
    list_parser = subparsers.add_parser("list", help="List proposals")
    list_parser.add_argument(
        "--status",
        default=None,
        help="Filter by status (e.g. human-review, rejected, accepted)",
    )

    # cl-admin approve <id-or-slug> [--force]
    approve_parser = subparsers.add_parser("approve", help="Approve a proposal")
    approve_parser.add_argument(
        "id_or_slug",
        help="Proposal ID (UUID) or slug",
    )
    approve_parser.add_argument(
        "--force",
        action="store_true",
        help="Approve regardless of current status",
    )

    # cl-admin remove <id-or-slug>
    remove_parser = subparsers.add_parser("remove", help="Remove an installed skill")
    remove_parser.add_argument(
        "id_or_slug",
        help="Proposal ID (UUID) or slug of the skill to remove",
    )

    args = parser.parse_args(argv)
    # Default to 'run' when no subcommand given (backward-compatible)
    if args.command is None:
        args.command = "run"
        args.once = False
    return args


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


def _build_service(args: argparse.Namespace, need_llm: bool = True) -> tuple[AdminService, LLMRouter | None]:
    """Build an AdminService instance from CLI args.

    Args:
        args: Parsed CLI arguments.
        need_llm: Whether to initialize the LLM router (not needed for list/approve).

    Returns:
        Tuple of (AdminService, LLMRouter or None).
    """
    config_path = Path(args.config) if args.config else None
    settings = load_settings(config_path)

    admin_config_path = Path(args.admin_config) if args.admin_config else None
    admin_config = _load_admin_config(admin_config_path)

    llm_router = None
    llm_chat = None
    if need_llm:
        llm_router = LLMRouter()
        for endpoint in settings.llm_endpoints:
            llm_router.add_endpoint(endpoint)
        if llm_router.provider_count > 0:
            def llm_chat(messages, max_tokens=2048, temperature=0.2):
                response = llm_router.chat(
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                return response.content

    notifier = CLINotifier()
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
        system_context=_build_system_context() if need_llm else {},
        poll_interval=admin_config.get("poll_interval_seconds", 30),
    )
    return service, llm_router


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    log_level = args.log_level
    if args.verbose and log_level == "CRITICAL":
        log_level = "INFO"
    logging.basicConfig(
        level=getattr(logging, log_level, logging.CRITICAL),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger(__name__)

    if args.command == "list":
        service, _ = _build_service(args, need_llm=False)
        proposals = service.list_proposals(args.status)
        print(service.format_proposals_table(proposals))
        return

    if args.command == "approve":
        service, _ = _build_service(args, need_llm=False)
        try:
            result = service.approve_proposal(args.id_or_slug, force=args.force)
            print(f"  {result}")
        except (ValueError, RuntimeError) as e:
            print(f"  Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.command == "remove":
        service, _ = _build_service(args, need_llm=False)
        try:
            result = service.remove_skill(args.id_or_slug)
            print(f"  {result}")
        except (ValueError, RuntimeError) as e:
            print(f"  Error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    # Default: run
    service, llm_router = _build_service(args, need_llm=True)
    logger.info("Clawless admin service starting")

    if args.once:
        service.run_once()
    else:
        try:
            service.run_loop()
        except KeyboardInterrupt:
            logger.info("Shutting down")

    if llm_router:
        llm_router.close()


if __name__ == "__main__":
    main()
