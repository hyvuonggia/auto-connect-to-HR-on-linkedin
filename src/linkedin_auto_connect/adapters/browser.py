"""Playwright implementation of the browser ports.

This is the only module that imports Playwright. The application core depends on
:mod:`linkedin_auto_connect.domain.ports`; this adapter turns those protocols into
a Firefox session and a live view of the "People you may know" page.

Behaviour mirrors the original single-file script: the same selectors, the same
login detection, and the same click-then-verify-invite flow.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeout,
)

from ..config import Settings
from ..domain.matcher import candidate_name
from ..domain.models import Candidate, InviteResult, InviteStatus, LoginStatus
from ..domain.ports import CookieStore, ProfileCard, ProfileSource, never_stop

logger = logging.getLogger(__name__)

LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"
FIREFOX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
)

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 900

INVITE_PREFIX = "Invite"
INVITE_SUFFIX = "to connect"

CARD_SELECTOR = '[role="listitem"]'
CONNECT_BUTTON_SELECTOR = 'button[aria-label^="Invite"][aria-label$="to connect"]'
PENDING_BUTTON_SELECTOR = 'button[aria-label*="Pending"], button:has-text("Pending")'

DEBUG_SCREENSHOT = "linkedin_debug.png"
BUTTON_SAMPLE_LIMIT = 5

SCROLL_TO_BOTTOM_JS = """
async (distance) => {
  let target = null;
  let bestDelta = 20;
  for (const element of document.querySelectorAll('*')) {
    if (!/(auto|scroll)/.test(getComputedStyle(element).overflowY)) continue;
    const delta = element.scrollHeight - element.clientHeight;
    if (delta > bestDelta) {
      target = element;
      bestDelta = delta;
    }
  }
  if (!target) {
    target = document.scrollingElement || document.documentElement;
  }
  const step = Math.max(1, distance);
  for (let attempt = 0; attempt < 40; attempt += 1) {
    const beforeTop = target.scrollTop;
    const beforeHeight = target.scrollHeight;
    target.scrollTop = Math.min(beforeHeight, beforeTop + step);
    await new Promise((resolve) => setTimeout(resolve, 150));
    const atBottom = target.scrollTop + target.clientHeight >= target.scrollHeight - 10;
    if (atBottom) {
      await new Promise((resolve) => setTimeout(resolve, 800));
      if (target.scrollHeight === beforeHeight) break;
    } else if (target.scrollTop === beforeTop) {
      break;
    }
  }
  target.scrollTop = target.scrollHeight;
  return {
    tag: target.tagName,
    id: target.id || '',
    scrollTop: target.scrollTop,
    scrollHeight: target.scrollHeight,
    clientHeight: target.clientHeight,
  };
}
"""

SHOW_MORE_PATTERN = re.compile(
    r"^(show|see)\s+more(\s+suggestions)?$|^(xem|tải|hiển thị)\s+thêm$",
    re.IGNORECASE,
)

LIMIT_WALL_PATTERNS: tuple[str, ...] = (
    r"you'?ve\s+reached\s+your\s+weekly\s+(invitation|invite|connection)\s+limit",
    r"weekly\s+(invitation|invite|connection)\s+limit",
)

LOGIN_SETTLE_MS = 2_000
NETWORK_SETTLE_MS = 3_000
MANUAL_LOGIN_POLL_MS = 3_000
CLICK_TIMEOUT_MS = 5_000
LIMIT_WALL_INSPECT_LIMIT = 5
MILLISECONDS_PER_SECOND = 1_000
LOGIN_BANNER_WIDTH = 60


def parse_invite_label(label: str) -> str:
    """Extract the profile name from a Connect button's aria-label.

    LinkedIn labels the button ``"Invite <name> to connect"``. Only the leading
    ``"Invite"`` and the trailing ``"to connect"`` are removed, so a name that
    itself contains "to connect" survives intact, and whitespace is collapsed.
    """
    text = " ".join((label or "").split())
    if text.startswith(INVITE_PREFIX):
        text = text[len(INVITE_PREFIX) :].lstrip()
    if text.endswith(INVITE_SUFFIX):
        text = text[: -len(INVITE_SUFFIX)].rstrip()
    return text


def _close_quietly(handle: Any | None) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        logger.debug("Ignoring error while closing %r", handle, exc_info=True)


def _safe_aria_label(button: Locator) -> str:
    try:
        return (button.get_attribute("aria-label") or "").strip()
    except Exception:
        logger.debug("Could not read the Connect button aria-label.", exc_info=True)
        return ""


class PlaywrightSessionFactory:
    """Creates Firefox-backed :class:`PlaywrightSession` instances."""

    def __init__(
        self,
        cookie_store: CookieStore,
        settings: Settings,
        playwright: Playwright,
    ) -> None:
        self._cookie_store = cookie_store
        self._settings = settings
        self._playwright = playwright

    def open(self, *, headless: bool) -> PlaywrightSession:
        """Launch Firefox and open a page with the configured viewport/UA."""
        browser: Browser | None = None
        context: BrowserContext | None = None
        try:
            mode = "headless" if headless else "visible"
            slow_mo = ""
            if self._settings.slow_mo_ms:
                slow_mo = f", slow-mo {self._settings.slow_mo_ms} ms"
            logger.info("Launching Firefox (%s%s)...", mode, slow_mo)
            browser = self._playwright.firefox.launch(
                headless=headless,
                slow_mo=self._settings.slow_mo_ms,
            )
            context = browser.new_context(
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                user_agent=FIREFOX_USER_AGENT,
            )
            page = context.new_page()
            return PlaywrightSession(
                page=page,
                context=context,
                browser=browser,
                cookie_store=self._cookie_store,
                settings=self._settings,
            )
        except Exception:
            _close_quietly(context)
            _close_quietly(browser)
            raise


class PlaywrightSession:
    """One Firefox browser, its context, and the LinkedIn page inside it."""

    def __init__(
        self,
        *,
        page: Page,
        context: BrowserContext,
        browser: Browser,
        cookie_store: CookieStore,
        settings: Settings,
    ) -> None:
        self._page = page
        self._context = context
        self._browser = browser
        self._cookie_store = cookie_store
        self._settings = settings
        self._closed = False
        self._source = LinkedInNetworkPage(page, settings)

    @property
    def source(self) -> ProfileSource:
        """The network page this session scans."""
        return self._source

    def authenticate(
        self,
        *,
        allow_manual_login: bool,
        should_stop: Callable[[], bool] = never_stop,
    ) -> LoginStatus:
        """Load saved cookies, verify them, and land on the network page.

        With stale cookies and ``allow_manual_login`` a visible browser waits for
        the user to log in by hand; ``should_stop`` aborts that wait.
        """
        try:
            self._load_cookies()
            status = self._verify_login()
            if status is LoginStatus.EXPIRED and allow_manual_login:
                status = self._manual_login(should_stop)
            return status
        finally:
            self._open_network_page()

    def cookies(self) -> list[dict[str, Any]]:
        """Return the current cookie jar."""
        return self._context.cookies()

    def is_blocked(self) -> bool:
        """True when LinkedIn shows a challenge or login wall."""
        try:
            url = self._page.url
        except Exception:
            logger.debug("Could not read the current URL.", exc_info=True)
            return False
        return "/challenge" in url or "/login" in url

    def close(self) -> None:
        """Close the page, context, and browser; safe to call repeatedly."""
        if self._closed:
            return
        self._closed = True
        _close_quietly(self._page)
        _close_quietly(self._context)
        _close_quietly(self._browser)

    def _load_cookies(self) -> None:
        try:
            cookies = self._cookie_store.load()
        except Exception as exc:
            logger.warning("Could not load saved cookies: %s", exc)
            return
        if not cookies:
            logger.info("No saved cookies found.")
            return
        try:
            self._context.add_cookies(cookies)
            logger.info("Loaded %d saved cookie(s).", len(cookies))
        except Exception as exc:
            logger.warning("Could not add saved cookies to the browser: %s", exc)

    def _verify_login(self) -> LoginStatus:
        try:
            self._page.goto(
                LINKEDIN_FEED_URL,
                timeout=self._settings.page_load_timeout_ms,
                wait_until="domcontentloaded",
            )
            self._page.wait_for_timeout(LOGIN_SETTLE_MS)
            url = self._page.url
        except Exception as exc:
            logger.warning("Could not verify the saved login: %s", exc)
            return LoginStatus.EXPIRED

        if "/challenge" in url:
            logger.warning("LinkedIn bot challenge detected (URL contains /challenge).")
            return LoginStatus.CHALLENGE
        if "/login" in url:
            logger.info("Saved cookies are expired; LinkedIn redirected to /login.")
            return LoginStatus.EXPIRED
        if "/feed" in url or "/mynetwork" in url:
            logger.info("Saved cookies are valid; already logged in.")
            return LoginStatus.AUTHENTICATED
        logger.info("Unexpected page after login check (%s); treating cookies as expired.", url)
        return LoginStatus.EXPIRED

    def _manual_login(self, should_stop: Callable[[], bool]) -> LoginStatus:
        logger.info("Opening LinkedIn login page: %s", self._settings.login_url)
        try:
            self._page.goto(
                self._settings.login_url,
                timeout=self._settings.page_load_timeout_ms,
                wait_until="domcontentloaded",
            )
        except PlaywrightTimeout:
            logger.warning("Login page load timed out; waiting for a manual login anyway.")
        except Exception as exc:
            logger.warning("Could not open the login page: %s", exc)
            return LoginStatus.EXPIRED

        banner = "=" * LOGIN_BANNER_WIDTH
        logger.info(
            "\n%s\n  Please log in to LinkedIn in the browser window.\n"
            "  The script will continue once you are logged in.\n%s",
            banner,
            banner,
        )

        while not should_stop():
            try:
                self._page.wait_for_timeout(MANUAL_LOGIN_POLL_MS)
                if self._login_detected():
                    logger.info("Login detected.")
                    return LoginStatus.AUTHENTICATED
            except Exception as exc:
                logger.warning("Manual-login check failed: %s", exc)
                return LoginStatus.EXPIRED

        logger.info("Manual login interrupted; stopping.")
        return LoginStatus.EXPIRED

    def _login_detected(self) -> bool:
        url = self._page.url
        if "/feed" in url or "/mynetwork" in url or "/in/" in url:
            return True
        if "/login" in url or "/challenge" in url:
            return False
        return self._page.locator("nav").count() > 0

    def _open_network_page(self) -> None:
        logger.info("Navigating to %s...", self._settings.network_url)
        try:
            self._page.goto(
                self._settings.network_url,
                timeout=self._settings.page_load_timeout_ms,
                wait_until="domcontentloaded",
            )
        except PlaywrightTimeout:
            logger.warning("Network page load timed out; continuing anyway.")
            return
        self._page.wait_for_timeout(NETWORK_SETTLE_MS)


class LinkedInNetworkPage:
    """Live adapter over the "People you may know" page."""

    def __init__(self, page: Page, settings: Settings) -> None:
        """Wrap the page this session scans and the settings the cards need."""
        self._page = page
        self._settings = settings
        self._snapshot_taken = False

    def card_count(self) -> int:
        """Number of rendered profile cards; ``0`` on error."""
        try:
            return self._page.locator(CARD_SELECTOR).count()
        except Exception:
            logger.debug("Could not count profile cards.", exc_info=True)
            return 0

    def open_cards(self) -> list[ProfileCard]:
        """Return a live card handle for each rendered card with a Connect button.

        Every card without a Connect button is logged when ``explain_cards`` is
        on, because "rendered but not clickable" is otherwise indistinguishable
        from "not a match" in the run log.
        """
        cards: list[ProfileCard] = []
        try:
            locators = self._page.locator(CARD_SELECTOR)
            count = locators.count()
        except Exception:
            logger.debug("Could not enumerate profile cards.", exc_info=True)
            return cards

        for index in range(count):
            try:
                card = locators.nth(index)
                if card.locator(CONNECT_BUTTON_SELECTOR).count() == 0:
                    if self._settings.explain_cards:
                        logger.info(
                            "  [-] SKIP (no Connect button): %s",
                            self._card_label(card, index),
                        )
                    continue
                cards.append(PlaywrightCard(self._page, card, self._settings))
            except Exception:
                logger.debug(
                    "Skipping card %d after an error while snapshotting it.",
                    index,
                    exc_info=True,
                )
                continue

        logger.info(
            "  [*] Card scan: %d rendered, %d with a Connect button.",
            count,
            len(cards),
        )
        if count and not cards:
            self._explain_no_connect_cards(locators, count)
        return cards

    @staticmethod
    def _card_label(card: Locator, index: int) -> str:
        """Best-effort name for a card whose Connect button is missing."""
        try:
            name = candidate_name(card.inner_text())
        except Exception:
            name = "?"
        return name if name != "?" else f"card #{index + 1}"

    def _explain_no_connect_cards(self, locators: Locator, count: int) -> None:
        """Say why zero cards were clickable and save one screenshot to inspect."""
        logger.warning(
            "  [!] None of the %d rendered card(s) has a matching Connect button. "
            "LinkedIn may have changed its markup, the UI language may not be English, "
            "or this page is not the suggestions grid.",
            count,
        )
        try:
            buttons = locators.nth(0).locator("button")
            sample_count = min(buttons.count(), BUTTON_SAMPLE_LIMIT)
            samples = []
            for index in range(sample_count):
                button = buttons.nth(index)
                label = _safe_aria_label(button)
                text = " ".join((button.inner_text() or "").split())
                samples.append(f"aria-label={label!r} text={text!r}")
            logger.warning("  [!] First card buttons: %s", "; ".join(samples) or "none")
        except Exception:
            logger.debug("Could not sample the first card's buttons.", exc_info=True)
        self._save_debug_screenshot()

    def _save_debug_screenshot(self) -> None:
        """Save one screenshot per session so an empty scan can be inspected."""
        if self._snapshot_taken:
            return
        try:
            self._page.screenshot(path=DEBUG_SCREENSHOT)
            self._snapshot_taken = True
            logger.info("  [*] Saved a page screenshot to %s.", DEBUG_SCREENSHOT)
        except Exception:
            logger.debug("Could not save the debug screenshot.", exc_info=True)

    def scroll_down(self, pixels: int) -> None:
        """Step-scroll to the page bottom, then request more suggestions if offered."""
        try:
            result = self._page.evaluate(SCROLL_TO_BOTTOM_JS, pixels)
        except Exception:
            logger.debug("Could not scroll the page.", exc_info=True)
            return
        if isinstance(result, dict):
            level = logging.INFO if self._settings.explain_cards else logging.DEBUG
            container = f"{result.get('tag', '?')}#{result.get('id', '')}"
            logger.log(
                level,
                "  [*] Scrolled %s to %s/%s.",
                container,
                result.get("scrollTop", "?"),
                result.get("scrollHeight", "?"),
            )
        self._click_show_more()

    def _click_show_more(self) -> None:
        """Click a visible "Show more suggestions" button when LinkedIn renders one."""
        try:
            buttons = self._page.get_by_role("button", name=SHOW_MORE_PATTERN)
            for index in range(min(buttons.count(), BUTTON_SAMPLE_LIMIT)):
                button = buttons.nth(index)
                if button.is_visible():
                    button.click(timeout=CLICK_TIMEOUT_MS)
                    logger.info("  [*] Clicked 'Show more' to request more suggestions.")
                    return
        except Exception:
            logger.debug("Could not click a Show more button.", exc_info=True)

    def limit_wall_visible(self) -> bool:
        """True when a visible weekly-invitation-limit message is on the page."""
        try:
            for pattern in LIMIT_WALL_PATTERNS:
                matches = self._page.get_by_text(re.compile(pattern, re.IGNORECASE))
                for index in range(min(matches.count(), LIMIT_WALL_INSPECT_LIMIT)):
                    if matches.nth(index).is_visible():
                        logger.warning("LinkedIn weekly-limit message detected on page.")
                        return True
        except Exception:
            logger.debug("Could not inspect the page for a limit wall.", exc_info=True)
        return False


class PlaywrightCard:
    """One live profile card, tied to its page and card locator."""

    def __init__(self, page: Page, card_locator: Locator, settings: Settings) -> None:
        self._page = page
        self._card = card_locator
        self._settings = settings

    @property
    def candidate(self) -> Candidate:
        """Name (from the Connect aria-label) and visible card text."""
        label = ""
        try:
            button = self._connect_button()
            if button is not None:
                label = _safe_aria_label(button)
        except Exception:
            logger.debug("Could not read the Connect button for a card.", exc_info=True)

        try:
            text = self._card.inner_text() or ""
        except Exception:
            logger.debug("Could not read the text of a card.", exc_info=True)
            text = ""

        return Candidate(name=parse_invite_label(label), text=text)

    def send_invite(self) -> InviteResult:
        """Click Connect and report whether the invite registered."""
        try:
            return self._send_invite()
        except Exception as exc:
            logger.warning("Unexpected error while sending an invite: %s", exc, exc_info=True)
            return InviteResult(InviteStatus.UNCONFIRMED, f"{type(exc).__name__}: {exc}")

    def _send_invite(self) -> InviteResult:
        button = self._connect_button()
        if button is None:
            return InviteResult(InviteStatus.UNAVAILABLE, "no Connect button")

        label = _safe_aria_label(button)
        try:
            visible = button.is_visible()
        except Exception:
            logger.debug("Could not check Connect button visibility.", exc_info=True)
            visible = False
        if not visible:
            return InviteResult(
                InviteStatus.UNAVAILABLE,
                label or "Connect button not visible",
            )

        try:
            button.click(timeout=CLICK_TIMEOUT_MS)
        except PlaywrightTimeout as exc:
            logger.warning("Connect click timed out for %s: %s", label or "<unknown>", exc)
            return InviteResult(InviteStatus.TIMED_OUT, label or "click timed out")
        except Exception as exc:
            logger.warning("Connect click failed for %s: %s", label or "<unknown>", exc)
            return InviteResult(InviteStatus.UNCONFIRMED, f"click failed: {exc}")

        self._page.wait_for_timeout(self._settings.click_pause_seconds * MILLISECONDS_PER_SECOND)

        if self._invite_registered(button):
            return InviteResult(InviteStatus.REGISTERED, label)
        return InviteResult(
            InviteStatus.UNREGISTERED,
            label or "invite did not register",
        )

    def _connect_button(self) -> Locator | None:
        """Re-locate the Connect button; cards change between passes."""
        try:
            buttons = self._card.locator(CONNECT_BUTTON_SELECTOR)
            if buttons.count() == 0:
                return None
            return buttons.first
        except Exception:
            logger.debug("Could not locate a Connect button on the card.", exc_info=True)
            return None

    def _invite_registered(self, button: Locator) -> bool:
        try:
            button.wait_for(
                state="detached",
                timeout=self._settings.registration_wait_ms,
            )
            return True
        except PlaywrightTimeout:
            logger.debug("Connect button did not detach; checking for a Pending button.")
        except Exception as exc:
            logger.debug("Could not wait for the Connect button to detach: %s", exc)

        try:
            pending = self._card.locator(PENDING_BUTTON_SELECTOR)
            return pending.count() > 0 and pending.first.is_visible()
        except Exception:
            logger.debug("Could not inspect the card for a Pending button.", exc_info=True)
            return False
