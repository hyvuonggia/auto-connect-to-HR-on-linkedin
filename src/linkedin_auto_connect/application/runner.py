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
            if outcome.matcher_unavailable:
                return SessionResult(total, StopReason.JEV_UNAVAILABLE)
            if self._should_stop():
                logger.info("[*] Stop requested — ending session.")
                return SessionResult(total, StopReason.STOPPED)
            if outcome.limit_hit:
                return SessionResult(total, StopReason.LIMIT_REACHED)

            if outcome.matched > 0:
                no_match_streak = 0
                logger.info(
                    "[*] Pass %d: %d new invite(s) from %d card(s) checked. Session total: %d",
                    pass_num,
                    outcome.matched,
                    outcome.checked,
                    total,
                )
            else:
                rendered = self._source.card_count()
                if rendered > 0:
                    no_match_streak += 1
                    logger.info(
                        "[*] Pass %d: no new matches (streak %d/%d, %d checked).",
                        pass_num,
                        no_match_streak,
                        self._settings.max_no_match_passes,
                        outcome.checked,
                    )
                    if outcome.checked == 0:
                        logger.info(
                            "[*] Pass %d: every clickable card was already checked earlier in "
                            "this run; waiting for LinkedIn to render a new batch.",
                            pass_num,
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
            logger.log(
                logging.INFO if self._settings.explain_cards else logging.DEBUG,
                "  [*] Scrolling to the bottom to load more profiles...",
            )
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
            logger.debug("[*] Pass %d: no clickable cards to process.", pass_num)
            return PassResult(0, False)

        matched = 0
        checked = 0
        total = len(cards)
        for position, card in enumerate(cards, start=1):
            if self._should_stop():
                break
            candidate = card.candidate
            name = candidate.name.strip()
            if not name:
                if self._settings.explain_cards:
                    logger.info("  [%d/%d] SKIP (no name)", position, total)
                continue
            if name in self._processed:
                level = logging.INFO if self._settings.explain_cards else logging.DEBUG
                logger.log(level, "  [%d/%d] SKIP (already checked): %.40s", position, total, name)
                continue
            if self._settings.explain_cards:
                logger.info("  [%d/%d] CHECK: %.40s", position, total, name)
            checked += 1
            if not self._matcher.matches(candidate.text):
                logger.info("  [-] FAIL (audience): %.40s", name)
                self._processed.add(name)
                if self._matcher.unavailable:
                    logger.error("[!] Audience filter is unavailable — stopping this session.")
                    return PassResult(matched, False, checked, matcher_unavailable=True)
                continue
            logger.info("  [+] PASS (audience): %.40s", name)
            if self._policy.at_invite_cap:
                logger.warning(
                    "[!] Reached configured invite cap (%d) for this run. Stopping.",
                    self._settings.max_invites_per_run,
                )
                return PassResult(matched, True, checked)

            self._processed.add(name)
            invite = card.send_invite()

            if invite.status is InviteStatus.UNCONFIRMED:
                logger.warning(
                    "  [!] UNKNOWN invite state for %.40s (%s) — not retrying.",
                    name,
                    invite.detail or "unexpected error",
                )
                continue

            if invite.status in (InviteStatus.TIMED_OUT, InviteStatus.UNREGISTERED):
                detail = f" ({invite.detail})" if invite.detail else ""
                limit_assumed = self._policy.record_unregistered()
                logger.warning(
                    "  [-] FAIL (invite did not register): %.40s%s "
                    "(%dx unregistered in a row) — weekly limit likely reached.",
                    name,
                    detail,
                    self._policy.unregistered_streak,
                )
                if limit_assumed or self._source.limit_wall_visible():
                    logger.warning("[!] Weekly invite limit detected — stopping session.")
                    return PassResult(matched, True, checked)
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
                if self._policy.at_invite_cap:
                    logger.info("  [+] Target reached: %d invite(s).", sent)
                    return PassResult(matched, True, checked)
                if self._source.limit_wall_visible():
                    logger.warning("[!] Weekly invite limit detected — stopping session.")
                    return PassResult(matched, True, checked)
                continue

            logger.warning("  [-] FAIL (Connect button not visible): %.40s", name)
            self._processed.discard(name)

        return PassResult(matched, False, checked)
