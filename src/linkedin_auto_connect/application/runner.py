"""Scan-and-click pass loop for one authenticated profile source.

The runner is pure application logic: it talks only to the protocols in
:mod:`linkedin_auto_connect.domain.ports` and never imports Playwright.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..config import Settings
from ..domain.models import UNLIMITED_INVITES, InviteStatus, PassResult, SessionResult, StopReason
from ..domain.ports import LimitPolicy, Matcher, ProfileSource, never_stop

logger = logging.getLogger(__name__)


class ConnectionRunner:
    """Process rendered cards pass by pass until a stop condition fires."""

    def __init__(
        self,
        source: ProfileSource,
        matcher: Matcher,
        policy: LimitPolicy,
        settings: Settings,
        *,
        should_stop: Callable[[], bool] = never_stop,
        sleeper: Callable[[float], None] = time.sleep,
        processed: set[str] | None = None,
    ) -> None:
        """Store injected collaborators; ``processed`` is shared across sessions."""
        self._source = source
        self._matcher = matcher
        self._policy = policy
        self._settings = settings
        self._should_stop = should_stop
        self._sleeper = sleeper
        self._processed: set[str] = processed if processed is not None else set()

    def run(self) -> SessionResult:
        """Run passes until the session stops, reporting invites and the reason."""
        total = 0
        no_match_streak = 0
        max_passes = self._settings.max_passes_per_session

        for pass_num in range(1, max_passes + 1):
            if self._should_stop():
                logger.info("[*] Stop requested — ending session.")
                return SessionResult(total, StopReason.STOPPED)
            if self._source.limit_wall_visible():
                logger.warning("[!] LinkedIn weekly-limit message detected — stopping.")
                return SessionResult(total, StopReason.LIMIT_REACHED)

            outcome = self._run_pass(pass_num)
            total += outcome.matched
            if self._should_stop():
                logger.info("[*] Stop requested — ending session.")
                return SessionResult(total, StopReason.STOPPED)
            if outcome.limit_hit:
                return SessionResult(total, StopReason.LIMIT_REACHED)

            if outcome.matched > 0:
                no_match_streak = 0
                logger.info(
                    "[*] Pass %d: %d new invite(s). Session total: %d",
                    pass_num,
                    outcome.matched,
                    total,
                )
            else:
                rendered = self._source.card_count()
                if rendered > 0:
                    no_match_streak += 1
                    logger.info(
                        "[*] Pass %d: no new matches (streak %d/%d).",
                        pass_num,
                        no_match_streak,
                        self._settings.max_no_match_passes,
                    )
                    if no_match_streak >= self._settings.max_no_match_passes:
                        logger.warning(
                            "[!] No new matches for %d passes — session stalled.",
                            no_match_streak,
                        )
                        return SessionResult(total, StopReason.STALLED)
                else:
                    logger.info(
                        "[*] Pass %d: no cards rendered yet — giving LinkedIn more time.",
                        pass_num,
                    )

            if pass_num >= max_passes:
                break
            logger.debug("[*] Scrolling down to load more profiles...")
            self._source.scroll_down(self._settings.scroll_pixels)
            self._sleeper(self._settings.scroll_pause_seconds)

        return SessionResult(total, StopReason.SCROLL_CAP)

    def _run_pass(self, pass_num: int) -> PassResult:
        """Scan the cards rendered now and click every matching Connect button.

        A name is recorded as attempted before the click, so a timed-out click
        can never become a duplicate invite in a later pass. A card whose
        Connect button is not visible is forgotten again instead: nothing was
        clicked, and the button is usually just slow to render.
        """
        cards = self._source.open_cards()
        if not cards:
            logger.debug("[*] Pass %d: no cards to process yet.", pass_num)
            return PassResult(0, False)

        matched = 0
        for card in cards:
            if self._should_stop():
                break
            candidate = card.candidate
            name = candidate.name.strip()
            if not name:
                continue
            if name in self._processed:
                continue
            if not self._matcher.matches(candidate.text):
                logger.info("  [-] Skipped (audience filter): %.40s", name)
                self._processed.add(name)
                continue
            logger.info("  [*] Audience match: %.40s", name)
            if self._policy.at_invite_cap:
                logger.warning(
                    "[!] Reached configured invite cap (%d) for this run. Stopping.",
                    self._settings.max_invites_per_run,
                )
                return PassResult(matched, True)

            self._processed.add(name)
            invite = card.send_invite()

            if invite.status is InviteStatus.UNCONFIRMED:
                logger.warning(
                    "  [!] Invite state unknown for %.40s (%s) — not retrying.",
                    name,
                    invite.detail or "unexpected error",
                )
                continue

            if invite.status in (InviteStatus.TIMED_OUT, InviteStatus.UNREGISTERED):
                detail = f" ({invite.detail})" if invite.detail else ""
                limit_assumed = self._policy.record_unregistered()
                logger.warning(
                    "  [!] Invite did not register for %.40s%s (%dx unregistered in a row) "
                    "— weekly limit likely reached.",
                    name,
                    detail,
                    self._policy.unregistered_streak,
                )
                if limit_assumed or self._source.limit_wall_visible():
                    logger.warning("[!] Weekly invite limit detected — stopping session.")
                    return PassResult(matched, True)
                continue

            if invite.status is InviteStatus.REGISTERED:
                matched += 1
                self._policy.record_registered()
                sent = self._policy.sent
                if self._settings.max_invites_per_run > UNLIMITED_INVITES:
                    logger.info(
                        "  [+] [%d/%d] Sent: %.40s",
                        sent,
                        self._settings.max_invites_per_run,
                        name,
                    )
                else:
                    logger.info("  [+] Sent: %.40s (pass total: %d)", name, matched)
                if self._source.limit_wall_visible():
                    logger.warning("[!] Weekly invite limit detected — stopping session.")
                    return PassResult(matched, True)
                continue

            logger.warning("  [!] Connect button not visible for: %.40s", name)
            self._processed.discard(name)

        return PassResult(matched, False)
