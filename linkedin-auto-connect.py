"""
LinkedIn Auto-Connect Script
Uses Playwright with Firefox to automatically send connection requests.

Prerequisites:
    pip install playwright
    playwright install firefox

Usage:
    python linkedin_auto_connect.py

First run: opens browser for manual login, then saves cookies.
Subsequent runs: loads saved cookies to skip login.

By default the browser runs hidden (headless); a visible window opens only when a
manual login is required (saved cookies expired), then closes again. Set HEADLESS = False
to always watch the browser.

Press Ctrl+C to stop the script gracefully.
"""

import json
import os
import signal
import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# --- Configuration ---
LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_NETWORK_URL = "https://www.linkedin.com/mynetwork/grow/"
INVITE_BUTTON_PREFIX = "Invite"
INVITE_BUTTON_SUFFIX = "to connect"

COOKIES_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "linkedin_cookies.json"
)

SCROLL_PAUSE = 2
CLICK_PAUSE = 1.5
PAGE_LOAD_TIMEOUT = 30_000
MAX_SCROLLS_PER_SESSION = 200  # Processing passes per browser session (safety cap)
MAX_CONSECUTIVE_NO_MATCH_PASSES = 4  # Restart browser after this many passes with 0 new connections
MAX_RESTARTS = 15  # Safety cap on automatic browser restarts (allows finding enough profiles across sessions to reach target)
RESTART_BASE_DELAY = 5  # Seconds; restart delay grows with each attempt
RESTART_MAX_DELAY = 60  # Seconds; max restart delay
MAX_UNREGISTERED_CLICKS = 5  # Stop after this many consecutive clicks that fail to register (weekly-limit signal; works headless too)
REGISTRATION_WAIT_MS = 3000  # How long to wait for a Connect button to become "Pending" after clicking
MAX_INVITES_PER_RUN = 200  # Stop after this many REGISTERED invites per run (0 = disabled).
HEADLESS = True  # Run without a visible browser window; opens one only for manual login (set False to watch)

# Visible weekly-limit dialog/toast phrases (case-insensitive regexes)
LIMIT_WALL_PATTERNS = [
    r"you'?ve\s+reached\s+your\s+weekly\s+(invitation|invite|connection)\s+limit",
    r"weekly\s+(invitation|invite|connection)\s+limit",
]

# Keywords to match against profile titles/descriptions (case-insensitive)
HR_KEYWORDS = [
    "hr",
    "recruiter",
    "talent",
    "acquisition",
    "human",
    "resource",
    "recruitment",
    "headhunter",
    "hiring",
    "employment",
    "people",
    "onboarding",
    "learning",
    "looking for",
    "hrbp",
]

# --- Graceful shutdown ---
running = True


def signal_handler(sig, frame):
    global running
    print("\n\n[!] Stopping script gracefully...")
    running = False


signal.signal(signal.SIGINT, signal_handler)


def save_cookies(context):
    cookies = context.cookies()
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookies, f, indent=2)
    print(f"[+] Cookies saved to {COOKIES_FILE}")


def load_cookies(context):
    if not os.path.exists(COOKIES_FILE):
        return False
    with open(COOKIES_FILE, "r") as f:
        cookies = json.load(f)
    if not cookies:
        return False
    context.add_cookies(cookies)
    print(f"[+] Loaded cookies from {COOKIES_FILE}")
    return True


def limit_wall_detected(page):
    """True if a VISIBLE LinkedIn weekly-limit dialog/toast is present.

    At the weekly limit LinkedIn may still accept clicks and show "Pending" while
    silently dropping invites server-side — the dialog/toast text is the only direct
    client-side evidence.
    """
    import re

    try:
        for pattern in LIMIT_WALL_PATTERNS:
            matches = page.get_by_text(re.compile(pattern, re.IGNORECASE))
            n = matches.count()
            for i in range(min(n, 5)):
                if matches.nth(i).is_visible():
                    print("[!] LinkedIn weekly-limit message detected on page.")
                    return True
    except Exception:
        pass
    return False


def is_logged_in(page):
    """Check authentication state.

    Returns True (logged in), False (login page / expired cookies), or "challenge"
    (LinkedIn bot challenge — handled distinctly, not as expired cookies).
    """
    try:
        page.goto(
            "https://www.linkedin.com/feed/",
            timeout=PAGE_LOAD_TIMEOUT,
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(2000)
        if "/challenge" in page.url:
            return "challenge"
        if "/login" in page.url:
            return False
        return "/feed" in page.url or "/mynetwork" in page.url
    except Exception:
        return False


def is_blocked_state(page):
    """Return True if LinkedIn is showing a challenge or login wall."""
    try:
        url = page.url
        return "/challenge" in url or "/login" in url
    except Exception:
        return False


def manual_login(page):
    print("[*] Opening LinkedIn login page...")
    page.goto(
        LINKEDIN_LOGIN_URL, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded"
    )
    print()
    print("=" * 60)
    print("  Please log in to LinkedIn in the browser window.")
    print("  The script will continue once you're logged in.")
    print("=" * 60)
    print()

    while running:
        page.wait_for_timeout(3000)
        current_url = page.url
        if (
            "/feed" in current_url
            or "/mynetwork" in current_url
            or "/in/" in current_url
        ):
            print("[+] Login detected!")
            return True
        if "/login" not in current_url and "/challenge" not in current_url:
            if page.locator("nav").count() > 0:
                print("[+] Login detected!")
                return True

    return False


def matches_keywords(text):
    """Check if text contains any HR-related keyword (case-insensitive).

    Use word boundaries to avoid matching name parts like 'An' in 'Le An'.
    """
    import re

    if not text:
        return False
    text_lower = text.lower()
    for keyword in HR_KEYWORDS:
        # Use word boundary to avoid partial matches in names
        # e.g., "an" in "Le An" shouldn't match
        pattern = r"\b" + re.escape(keyword.lower()) + r"\b"
        if re.search(pattern, text_lower):
            return True
    return False


def process_connections_on_page(page, processed_names, limit_state):
    """Find all connection cards, check for HR keywords, and click Connect.

    Returns (matched_count, limit_hit). `limit_hit` is True when several clicks in a
    row fail to register (the Connect button never becomes "Pending") — the LinkedIn
    weekly connection limit has likely been reached. This works in headless mode too,
    where LinkedIn silently ignores invites instead of blocking the click.
    """
    matched_count = 0

    # A weekly-limit dialog/toast already on the page means we're done.
    if limit_wall_detected(page):
        print("[!] LinkedIn weekly-limit message detected — stopping.")
        return 0, True

    try:
        # Find all list items (profile cards) in the "People you may know" section
        # Using the role="listitem" selector within the section
        cards = page.locator('[role="listitem"]')
        card_count = cards.count()

        if card_count == 0:
            return 0, False

        print(f"[*] Found {card_count} profile card(s) on page.")

        for i in range(card_count):
            if not running:
                break

            try:
                # Re-locate card each iteration as DOM may change
                card = cards.nth(i)

                # Get the card's full text to check for HR keywords
                card_text = card.inner_text().lower()

                # Get the name from aria-label of connect button or profile link
                connect_btn = card.locator(
                    f'button[aria-label^="{INVITE_BUTTON_PREFIX}"][aria-label$="{INVITE_BUTTON_SUFFIX}"]'
                )

                if connect_btn.count() == 0:
                    continue

                # Extract name from aria-label
                aria_label = connect_btn.first.get_attribute("aria-label") or ""
                name = (
                    aria_label.replace(INVITE_BUTTON_PREFIX, "")
                    .replace(INVITE_BUTTON_SUFFIX, "")
                    .strip()
                )

                # Skip malformed cards without a usable name
                if not name:
                    continue

                # Skip if already processed
                if name in processed_names:
                    continue

                # Check if this profile matches HR keywords in card text
                if not matches_keywords(card_text):
                    print(f"  [-] Skipped (no HR keyword): {name[:40]}")
                    processed_names.add(name)
                    continue

                print(f"  [*] HR keyword match: {name[:40]}")

                # Configured invite cap per run
                if (
                    MAX_INVITES_PER_RUN > 0
                    and limit_state["run_count"] >= MAX_INVITES_PER_RUN
                ):
                    print(
                        f"[!] Reached configured invite cap ({MAX_INVITES_PER_RUN}) for this run. Stopping."
                    )
                    return matched_count, True

                # Click the Connect button
                if connect_btn.first.is_visible():
                    # Mark attempted BEFORE clicking: a timed-out click must never
                    # re-try the same person (observed duplicate-click loop).
                    processed_names.add(name)
                    try:
                        connect_btn.first.click(timeout=5000)
                    except PlaywrightTimeout:
                        # Click itself timed out (e.g. a weekly-limit modal is
                        # intercepting the button) — the invite did not register.
                        limit_state["unregistered_streak"] += 1
                        streak = limit_state["unregistered_streak"]
                        print(
                            f"  [!] Click timed out for {name[:40]} "
                            f"({streak}x unregistered in a row) — weekly limit likely reached."
                        )
                        if limit_wall_detected(page):
                            return matched_count, True
                        if streak >= MAX_UNREGISTERED_CLICKS:
                            return matched_count, True
                        continue

                    page.wait_for_timeout(CLICK_PAUSE)

                    # Verify the invite registered: the Connect button should become
                    # "Pending" (the old button detaches). A button that is still
                    # there means LinkedIn silently ignored the request (weekly
                    # limit) — the only signal that works in headless mode.
                    registered = False
                    try:
                        connect_btn.first.wait_for(
                            state="detached", timeout=REGISTRATION_WAIT_MS
                        )
                        registered = True
                    except PlaywrightTimeout:
                        # Fallback: a Pending button may have appeared on the card
                        # without the old button detaching (e.g. aria-label changed).
                        try:
                            pending = card.locator(
                                'button[aria-label*="Pending"], button:has-text("Pending")'
                            )
                            registered = pending.count() > 0 and pending.first.is_visible()
                        except Exception:
                            pass

                    if registered:
                        matched_count += 1
                        limit_state["unregistered_streak"] = 0
                        if MAX_INVITES_PER_RUN > 0:
                            limit_state["run_count"] += 1
                            print(
                                f"  [+] [{limit_state['run_count']}/{MAX_INVITES_PER_RUN}] Sent: {aria_label[:50]} (Pass: {matched_count})"
                            )
                        else:
                            print(f"  [+] Sent: {aria_label[:50]} (Pass: {matched_count})")
                    else:
                        limit_state["unregistered_streak"] += 1
                        streak = limit_state["unregistered_streak"]
                        print(
                            f"  [!] Invite did not register for {name[:40]} "
                            f"({streak}x unregistered in a row) — weekly limit likely reached."
                        )
                        if streak >= MAX_UNREGISTERED_CLICKS:
                            return matched_count, True

                    # The weekly-limit dialog/toast may appear even when the button
                    # became "Pending" (invite silently dropped server-side).
                    if limit_wall_detected(page):
                        return matched_count, True
                else:
                    print(f"  [!] Connect button not visible for: {name[:40]}")

            except PlaywrightTimeout:
                print("  [!] Timed out while processing card. Skipping...")
            except Exception as e:
                print(f"  [!] Error processing card: {e}")
                continue

    except Exception as e:
        print(f"[!] Error in process_connections_on_page: {e}")

    return matched_count, False


def close_session(browser, context, page):
    """Best-effort close of a browser session (page, context, browser)."""
    for handle in (page, context):
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
    try:
        browser.close()
    except Exception:
        pass


def start_session(p, headless):
    """Launch browser, authenticate (cookies or manual login), navigate to network page.

    `headless` hides the browser window. In headless mode manual login is skipped
    (nobody can interact with an invisible window) — callers fall back to a visible
    session for that.

    Returns (browser, context, page, authenticated, challenged):
      - authenticated: True only when login was verified or completed successfully;
      - challenged: True when LinkedIn showed a bot challenge (do not retry login).
    Closes everything and re-raises on failure.
    """
    print("[*] Launching Firefox..." + (" (headless)" if headless else ""))
    browser = p.firefox.launch(headless=headless)
    context = None
    page = None
    try:
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
        )
        page = context.new_page()

        # --- Authentication flow ---
        authenticated = False
        challenged = False
        if load_cookies(context):
            print("[*] Verifying saved cookies...")
            login_status = is_logged_in(page)
            if login_status is True:
                print("[+] Cookies valid — already logged in!")
                authenticated = True
            elif login_status == "challenge":
                challenged = True
                print("[!] LinkedIn bot challenge detected (URL contains /challenge).")
            else:
                print("[!] Saved cookies expired. Need to log in again.")
        if not authenticated and not challenged:
            if headless:
                # Nobody can complete a login in an invisible window; the caller
                # (open_authenticated_session) will retry with a visible browser.
                print("[*] No valid session in headless mode.")
            elif manual_login(page):
                if not running:
                    # Interrupted during login — don't navigate or persist a partial jar
                    return browser, context, page, False, False
                save_cookies(context)
                authenticated = True

        # --- Navigate to network page ---
        print(f"[*] Navigating to {LINKEDIN_NETWORK_URL}...")
        try:
            page.goto(
                LINKEDIN_NETWORK_URL,
                timeout=PAGE_LOAD_TIMEOUT,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(3000)
        except PlaywrightTimeout:
            print("[!] Page load timed out. Continuing anyway...")

        return browser, context, page, authenticated, challenged
    except Exception:
        close_session(browser, context, page)
        raise


def open_authenticated_session(p):
    """Open an authenticated session ready for the connection loop.

    HEADLESS=True: an invisible browser is used; if the saved cookies are invalid, a
    visible browser briefly opens for manual login, then we return to headless.
    HEADLESS=False: always uses a visible browser (manual login possible directly).

    Intermediate sessions are closed here; only the returned session is owned by the
    caller.
    """
    browser, context, page, authenticated, challenged = start_session(p, HEADLESS)
    if not HEADLESS or authenticated or not running or challenged:
        return browser, context, page, authenticated, challenged

    print("\n[*] No valid cookies — opening a VISIBLE browser for manual login...")
    close_session(browser, context, page)
    browser, context, page, authenticated, challenged = start_session(p, False)
    if not authenticated or not running or challenged:
        return browser, context, page, authenticated, challenged

    print("[*] Login successful. Returning to headless mode...")
    close_session(browser, context, page)
    return start_session(p, HEADLESS)


def cards_rendered(page):
    """True if LinkedIn has rendered any profile cards (page-ready signal).

    A zero-match pass with no rendered cards usually means the page is still loading,
    not that HR profiles have dried up — so it should not count toward the stall streak.
    """
    try:
        return page.locator('[role="listitem"]').count() > 0
    except Exception:
        return False


def run_connection_loop(page, processed_names, limit_state):
    """Process + scroll until a stall, the scroll cap, a stop signal, or the limit.

    Returns (session_total, reason) with reason in
    {"stalled", "scroll_cap", "stopped", "limit_reached"}.
    """
    session_total = 0
    no_match_streak = 0

    for pass_num in range(1, MAX_SCROLLS_PER_SESSION + 1):
        if not running:
            return session_total, "stopped"

        matched, limit_hit = process_connections_on_page(
            page, processed_names, limit_state
        )
        session_total += matched
        if limit_hit:
            return session_total, "limit_reached"

        if matched > 0:
            no_match_streak = 0
            print(
                f"[*] Pass {pass_num}: {matched} new connections. Session total: {session_total}"
            )
        else:
            if cards_rendered(page):
                no_match_streak += 1
                print(
                    f"[*] Pass {pass_num}: No new matches (streak {no_match_streak}). Session total: {session_total}"
                )
                if no_match_streak >= MAX_CONSECUTIVE_NO_MATCH_PASSES:
                    return session_total, "stalled"
            else:
                print(
                    f"[*] Pass {pass_num}: No profile cards rendered yet — giving LinkedIn more time (streak unchanged)."
                )

        # Scroll down to load more
        print(f"[*] Scrolling down to load more profiles...")
        page.evaluate("window.scrollBy(0, 600)")
        page.wait_for_timeout(SCROLL_PAUSE * 1000)

    return session_total, "scroll_cap"


def main():
    global running

    total_invited = 0
    processed_names = set()

    limit_state = {
        "unregistered_streak": 0,
        "run_count": 0,
    }
    if MAX_INVITES_PER_RUN > 0:
        print(f"[*] Target invites for this run: {MAX_INVITES_PER_RUN}")

    with sync_playwright() as p:
        restart_count = 0

        while running and restart_count <= MAX_RESTARTS:
            print(
                f"\n[*] Starting browser session (restart {restart_count}/{MAX_RESTARTS})..."
            )

            browser, context, page, authenticated, _ = open_authenticated_session(p)
            blocked = False
            reason = "stopped"

            try:
                if not authenticated:
                    print(
                        "[!] No authenticated session (login interrupted or bot "
                        "challenge). Stopping."
                    )
                else:
                    print("\n[*] Starting connection process...")
                    print("[*] Script will scroll, find HR profiles, and click Connect.")
                    print("[*] Press Ctrl+C to stop.\n")

                    session_total, reason = run_connection_loop(
                        page, processed_names, limit_state
                    )
                    total_invited += session_total

                    if reason == "stalled":
                        blocked = is_blocked_state(page)
            finally:
                # Refresh persisted cookies only when this session authenticated cleanly
                # and the page is not a challenge/login wall — otherwise we could
                # overwrite a good cookie file with an empty or incomplete jar.
                try:
                    if authenticated and not is_blocked_state(page):
                        save_cookies(context)
                except Exception:
                    pass
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

            if not authenticated:
                break

            if not running or reason in ("stopped", "scroll_cap", "limit_reached"):
                if reason == "limit_reached":
                    print(
                        "[!] LinkedIn weekly connection limit likely reached — stopping. "
                        "Restarting won't help; try again next week."
                    )
                break

            if blocked:
                print(
                    "[!] LinkedIn challenge/login wall detected. Stopping to avoid rate-limiting."
                )
                break

            restart_count += 1
            if restart_count > MAX_RESTARTS:
                print(f"[!] Reached max restarts ({MAX_RESTARTS}). Stopping.")
                break

            delay = min(RESTART_BASE_DELAY * restart_count, RESTART_MAX_DELAY)
            print(
                f"[*] No new connections for {MAX_CONSECUTIVE_NO_MATCH_PASSES} passes — "
                f"restarting browser in {delay}s (attempt {restart_count}/{MAX_RESTARTS}, "
                f"total invites so far: {total_invited})..."
            )
            time.sleep(delay)

    print(f"\n[+] Script finished. Total invitations sent: {total_invited}")


if __name__ == "__main__":
    main()
