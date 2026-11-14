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

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
"""argparse's convention for a malformed command line."""


def _non_negative_int(value: str) -> int:
    """argparse type for counts: reject negatives instead of treating them as unlimited."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def _probability(value: str) -> float:
    """argparse type for Jev thresholds: a number between 0 and 1."""
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid probability: {value!r}") from exc
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
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
        "--no-jev",
        dest="jev_enabled",
        action="store_false",
        default=None,
        help="use keyword-only HR matching even when Jev is configured",
    )
    parser.add_argument(
        "--jev-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="timeout for one Jev request",
    )
    parser.add_argument(
        "--it-yes-threshold",
        type=_probability,
        default=None,
        metavar="P",
        help="connect when Jev's IT probability is at least P (default: 0.7)",
    )
    parser.add_argument(
        "--it-no-threshold",
        type=_probability,
        default=None,
        metavar="P",
        help="log a confident non-IT decision below P (default: 0.3)",
    )
    parser.add_argument(
        "--it-evidence-threshold",
        type=_probability,
        default=None,
        metavar="P",
        help="skip cards below this evidence probability (default: 0.4)",
    )
    parser.add_argument(
        "--it-cache",
        type=Path,
        default=None,
        metavar="PATH",
        help="path to the Jev decision cache",
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
    jev_enabled = getattr(args, "jev_enabled", None)
    if jev_enabled is not None:
        overrides["jev_enabled"] = jev_enabled
    jev_timeout = getattr(args, "jev_timeout", None)
    if jev_timeout is not None:
        overrides["jev_timeout_seconds"] = jev_timeout
    it_yes = getattr(args, "it_yes_threshold", None)
    if it_yes is not None:
        overrides["it_yes_threshold"] = it_yes
    it_no = getattr(args, "it_no_threshold", None)
    if it_no is not None:
        overrides["it_no_threshold"] = it_no
    it_evidence = getattr(args, "it_evidence_threshold", None)
    if it_evidence is not None:
        overrides["it_evidence_threshold"] = it_evidence
    it_cache = getattr(args, "it_cache", None)
    if it_cache is not None:
        overrides["it_cache_path"] = Path(it_cache)

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
            return EXIT_OK
        return code if isinstance(code, int) else EXIT_USAGE

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
        return EXIT_OK
    except KeyboardInterrupt:
        logger.info("Stopping gracefully...")
        return EXIT_OK
    except Exception:
        logger.exception("[!] Unexpected error")
        return EXIT_FAILURE


def _run_application(settings: Settings, interruption: Interruption) -> int:
    """Import the real adapters, wire the application, and run it.

    Returns the number of registered invites; the exit code is decided by
    :func:`main`, because a process exit status is not a metric.
    """
    from playwright.sync_api import sync_playwright

    from .browser import PlaywrightSessionFactory
    from .cookies import JsonCookieStore
    from .jev import JevClassifier, load_jev_config
    from .jev_cache import CachedClassifier, JsonDecisionCache
    from .limits import InviteLimitPolicy
    from .matcher import ItRelevanceMatcher, KeywordMatcher
    from .ports import Matcher

    store = JsonCookieStore(settings.cookies_path)
    keyword_matcher = KeywordMatcher(settings.keywords)
    matcher: Matcher = keyword_matcher
    if settings.jev_enabled:
        jev_config = load_jev_config(
            timeout_seconds=settings.jev_timeout_seconds,
            max_attempts=settings.jev_max_attempts,
        )
        if jev_config is None:
            logger.warning(
                "[!] Jev is not configured (set JEV_API_KEY and JEV_BASE_URL in "
                ".env); falling back to keyword-only HR matching."
            )
        else:
            cache = JsonDecisionCache(settings.it_cache_path, jev_config.model)
            matcher = ItRelevanceMatcher(
                keyword_matcher,
                CachedClassifier(JevClassifier(jev_config), cache),
                yes_threshold=settings.it_yes_threshold,
                no_threshold=settings.it_no_threshold,
                evidence_threshold=settings.it_evidence_threshold,
            )
            logger.info(
                "[*] Jev IT-relevance filter enabled (model=%s).", jev_config.model
            )
    else:
        logger.info("[*] Jev disabled (--no-jev); using keyword-only HR matching.")

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
            matcher,
            should_stop=interruption.should_stop,
            policy=InviteLimitPolicy(
                max_invites=settings.max_invites_per_run,
                max_unregistered=settings.max_unregistered_clicks,
            ),
        )
        return application.run()
