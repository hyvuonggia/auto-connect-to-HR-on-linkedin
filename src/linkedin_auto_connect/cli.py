"""Command-line entry point: argument parsing, logging and adapter wiring."""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import FrameType

from . import __version__
from .app import Application
from .config import Settings

logger = logging.getLogger(__name__)


def _non_negative_int(value: str) -> int:
    """argparse type for counts: reject negatives instead of treating them as unlimited."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def build_parser() -> argparse.ArgumentParser:
    """Build the parser shared by the console script and ``python -m``."""
    parser = argparse.ArgumentParser(
        prog="linkedin-auto-connect",
        description="Send LinkedIn connection requests to HR and recruiter profiles.",
    )
    parser.add_argument(
        "--max-invites",
        type=_non_negative_int,
        default=None,
        metavar="N",
        help="stop after N registered invites (0 = unlimited)",
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="run without a visible browser (default: on)",
    )
    parser.add_argument(
        "--keywords",
        default=None,
        metavar='"a,b,c"',
        help="comma-separated keywords that replace the built-in list",
    )
    parser.add_argument(
        "--cookies",
        type=Path,
        default=None,
        metavar="PATH",
        help="path to the persisted cookie jar",
    )
    parser.add_argument(
        "--max-restarts",
        type=_non_negative_int,
        default=None,
        metavar="N",
        help="maximum browser restarts after a stalled session",
    )
    parser.add_argument(
        "--scroll-pause",
        type=float,
        default=None,
        metavar="SECONDS",
        help="pause after each scroll",
    )
    parser.add_argument(
        "--click-pause",
        type=float,
        default=None,
        metavar="SECONDS",
        help="pause after each Connect click",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _parse_keywords(value: str | None) -> tuple[str, ...] | None:
    """Split a comma-separated list, trimming whitespace and dropping blanks."""
    if value is None:
        return None
    return tuple(item.strip() for item in value.split(",") if item.strip())


def build_settings(args: argparse.Namespace) -> Settings:
    """Return ``Settings`` with every explicitly supplied CLI flag applied."""
    overrides: dict[str, object] = {}

    max_invites = getattr(args, "max_invites", None)
    if max_invites is not None:
        overrides["max_invites_per_run"] = max_invites
    headless = getattr(args, "headless", None)
    if headless is not None:
        overrides["headless"] = headless
    keywords = getattr(args, "keywords", None)
    if keywords is not None:
        overrides["keywords"] = _parse_keywords(keywords)
    cookies = getattr(args, "cookies", None)
    if cookies is not None:
        overrides["cookies_path"] = Path(cookies)
    max_restarts = getattr(args, "max_restarts", None)
    if max_restarts is not None:
        overrides["max_restarts"] = max_restarts
    scroll_pause = getattr(args, "scroll_pause", None)
    if scroll_pause is not None:
        overrides["scroll_pause_seconds"] = scroll_pause
    click_pause = getattr(args, "click_pause", None)
    if click_pause is not None:
        overrides["click_pause_seconds"] = click_pause

    return replace(Settings(), **overrides)


def configure_logging(verbose: bool) -> None:
    """Configure message-only logging at INFO or DEBUG."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        force=True,
    )


class Interruption:
    """Cooperative stop flag fed by SIGINT and, when available, SIGTERM."""

    def __init__(self) -> None:
        """Create the event that the signal handlers set."""
        self._event = threading.Event()

    def should_stop(self) -> bool:
        """Return True once a stop signal has arrived."""
        return self._event.is_set()

    def install(self) -> None:
        """Bind the supported signals to a graceful-stop handler."""
        for name in ("SIGINT", "SIGTERM"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                signal.signal(signum, self._handle)
            except (OSError, ValueError):
                logger.debug("Could not install the %s handler.", name)

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        logger.info("Stopping gracefully...")
        self._event.set()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return its process exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2

    configure_logging(args.verbose)
    settings = build_settings(args)
    interruption = Interruption()
    interruption.install()

    logger.info(
        "[*] Target invites for this run: %s",
        settings.max_invites_per_run or "unlimited",
    )
    try:
        _run_application(settings, interruption)
        return 0
    except KeyboardInterrupt:
        logger.info("Stopping gracefully...")
        return 0
    except Exception:
        logger.exception("[!] Unexpected error")
        return 1


def _run_application(settings: Settings, interruption: Interruption) -> int:
    """Import the real adapters, wire the application, and run it.

    Returns the number of registered invites; the exit code is decided by
    :func:`main`, because a process exit status is not a metric.
    """
    from playwright.sync_api import sync_playwright

    from .browser import PlaywrightSessionFactory
    from .cookies import JsonCookieStore
    from .limits import InviteLimitPolicy
    from .matcher import KeywordMatcher

    store = JsonCookieStore(settings.cookies_path)
    with sync_playwright() as playwright:
        factory = PlaywrightSessionFactory(
            cookie_store=store,
            settings=settings,
            playwright=playwright,
        )
        application = Application(
            factory,
            store,
            settings,
            KeywordMatcher(settings.keywords),
            should_stop=interruption.should_stop,
            policy=InviteLimitPolicy(
                max_invites=settings.max_invites_per_run,
                max_unregistered=settings.max_unregistered_clicks,
            ),
        )
        return application.run()
