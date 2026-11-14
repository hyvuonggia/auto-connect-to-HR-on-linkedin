"""Application orchestration: authentication, restarts and cookie persistence.

The application depends only on the protocols in
:mod:`linkedin_auto_connect.domain.ports`; the Playwright adapter is injected by
the CLI.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..config import Settings
from ..domain.models import LoginStatus, StopReason
from ..domain.ports import (
    BrowserSession,
    CookieStore,
    LimitPolicy,
    Matcher,
    SessionFactory,
    never_stop,
)
from .runner import ConnectionRunner

logger = logging.getLogger(__name__)


class Application:
    """Run the auto-connect workflow end to end for one CLI invocation."""

    def __init__(
        self,
        factory: SessionFactory,
        store: CookieStore,
        settings: Settings,
        matcher: Matcher,
        policy: LimitPolicy,
        *,
        should_stop: Callable[[], bool] = never_stop,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Store the injected collaborators; the policy lives for the whole run."""
        self._factory = factory
        self._store = store
        self._settings = settings
        self._matcher = matcher
        self._policy = policy
        self._should_stop = should_stop
        self._sleeper = sleeper

    def run(self) -> int:
        """Run sessions until a terminal condition, returning registered invites."""
        total = 0
        processed: set[str] = set()
        policy = self._policy

        restarts = 0
        while True:
            if self._should_stop():
                logger.info("[*] Stop requested — ending run.")
                break

            logger.info(
                "[*] Starting browser session (restart %d/%d)...",
                restarts,
                self._settings.max_restarts,
            )
            session = self._open_authenticated_session()
            if session is None:
                break

            blocked: bool | None = None
            try:
                runner = ConnectionRunner(
                    session.source,
                    self._matcher,
                    policy,
                    self._settings,
                    should_stop=self._should_stop,
                    sleeper=self._sleeper,
                    processed=processed,
                )
                result = runner.run()
                total += result.invites
                reason = result.reason
                blocked = self._is_blocked(session)
            finally:
                self._persist_cookies(session, blocked=blocked)
                self._close(session)

            if reason is StopReason.STALLED:
                if blocked:
                    logger.warning(
                        "[!] LinkedIn challenge/login wall detected. Stopping to avoid "
                        "rate-limiting."
                    )
                    break
                restarts += 1
                if restarts > self._settings.max_restarts:
                    logger.warning(
                        "[!] Reached max restarts (%d). Stopping.",
                        self._settings.max_restarts,
                    )
                    break
                delay = self._settings.restart_delay(restarts)
                logger.info(
                    "[*] No new matches — restarting in %.1fs (attempt %d/%d, "
                    "total invites so far: %d)...",
                    delay,
                    restarts,
                    self._settings.max_restarts,
                    total,
                )
                self._sleeper(delay)
                continue

            if reason is StopReason.LIMIT_REACHED:
                logger.warning(
                    "[!] LinkedIn weekly connection limit likely reached — stopping. "
                    "Restarting won't help; try again next week."
                )
            elif reason is StopReason.STOPPED:
                logger.info("[*] Stop requested — ending run.")
            else:
                logger.info("[*] Scroll cap reached for this session — stopping.")
            break

        logger.info("[+] Run finished. Total registered invites: %d", total)
        return total

    def _open_authenticated_session(self) -> BrowserSession | None:
        """Open one browser and return it only when it authenticated cleanly."""
        headless = self._settings.headless
        logger.info("[*] Launching browser (%s)...", "headless" if headless else "visible")
        session = self._factory.open(headless=headless)
        try:
            status = session.authenticate(
                allow_manual_login=not headless,
                should_stop=self._should_stop,
            )
        except Exception:
            self._close(session)
            raise

        if status is LoginStatus.AUTHENTICATED:
            logger.info("[+] Authenticated browser session ready.")
            return session
        if status is LoginStatus.CHALLENGE:
            logger.warning("[!] LinkedIn bot challenge detected. Stopping.")
            self._close(session)
            return None

        logger.warning("[!] Saved cookies expired or no valid session.")
        if headless and not self._should_stop():
            self._close(session)
            return self._visible_login_then_reopen()
        self._close(session)
        return None

    def _visible_login_then_reopen(self) -> BrowserSession | None:
        """Log in by hand in a visible browser, persist cookies, reopen headless."""
        logger.info("[*] Opening a VISIBLE browser for manual login...")
        visible = self._factory.open(headless=False)
        try:
            status = visible.authenticate(
                allow_manual_login=True,
                should_stop=self._should_stop,
            )
            if status is LoginStatus.CHALLENGE:
                logger.warning("[!] LinkedIn bot challenge detected. Stopping.")
                return None
            if status is not LoginStatus.AUTHENTICATED:
                logger.warning("[!] Manual login did not complete. Stopping.")
                return None
            if self._should_stop():
                logger.info("[*] Login interrupted — not persisting cookies.")
                return None
            self._persist_cookies(visible)
        finally:
            self._close(visible)

        logger.info("[*] Login successful — returning to headless mode...")
        reopened = self._factory.open(headless=self._settings.headless)
        try:
            status = reopened.authenticate(
                allow_manual_login=not self._settings.headless,
                should_stop=self._should_stop,
            )
        except Exception:
            self._close(reopened)
            raise
        if status is not LoginStatus.AUTHENTICATED:
            logger.warning("[!] Could not re-authenticate in headless mode. Stopping.")
            self._close(reopened)
            return None
        return reopened

    def _persist_cookies(
        self,
        session: BrowserSession,
        *,
        blocked: bool | None = None,
    ) -> None:
        """Save the cookie jar unless the session is sitting on a login/challenge wall."""
        try:
            if blocked is None:
                blocked = session.is_blocked()
            if blocked:
                logger.debug("[*] Session is blocked — cookies not saved.")
                return
            self._store.save(session.cookies())
            logger.info("[+] Cookies saved.")
        except Exception:
            logger.warning("[!] Could not save cookies.", exc_info=True)

    def _is_blocked(self, session: BrowserSession) -> bool:
        """Best-effort blocked check; an unreadable page counts as not blocked."""
        try:
            return session.is_blocked()
        except Exception:
            logger.debug("[!] Could not read the blocked state.", exc_info=True)
            return False

    def _close(self, session: BrowserSession) -> None:
        """Close a session without masking the exception being handled."""
        try:
            session.close()
        except Exception:
            logger.debug("[!] Ignoring error while closing a session.", exc_info=True)
