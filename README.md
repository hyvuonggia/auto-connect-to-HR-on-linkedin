# auto-connect-to-HR-on-linkedin

Automatically send LinkedIn connection requests to HR / recruiter profiles using
Playwright + Firefox.

The script walks the "People you may know" cards on
`linkedin.com/mynetwork/grow/`, keeps the cards whose text matches HR-related
keywords, and clicks **Connect** on them.

## Requirements

- Python 3.9+
- Firefox for Playwright

```bash
pip install -r requirements.txt
python -m playwright install firefox
```

## Usage

```bash
python linkedin-auto-connect.py
```

**First run:** a visible browser opens so you can log in by hand. Once you are
logged in, the session cookies are saved to `linkedin_cookies.json` and reused.

**Later runs:** the saved cookies are loaded and the browser stays headless
(the window opens again only if the cookies expired and a manual login is
needed).

Press `Ctrl+C` to stop gracefully.

## Configuration

Edit the constants at the top of `linkedin-auto-connect.py`:

| Constant | Default | Meaning |
| --- | --- | --- |
| `MAX_INVITES_PER_RUN` | `200` | Stop after this many registered invites (`0` = unlimited) |
| `HEADLESS` | `True` | Run without a visible window; set `False` to watch |
| `HR_KEYWORDS` | HR list | Keywords matched against each card's text (case-insensitive) |
| `MAX_UNREGISTERED_CLICKS` | `5` | Consecutive invites that fail to register before stopping (weekly-limit signal) |
| `MAX_RESTARTS` | `15` | Browser restarts when a session stops finding new profiles |
| `MAX_CONSECUTIVE_NO_MATCH_PASSES` | `4` | Passes with no new matches before a restart |

## How it avoids hitting the weekly limit

LinkedIn often still shows "Pending" after a click while silently dropping the
invite server-side at the weekly cap. The script therefore verifies that every
click actually registered (the Connect button turns into "Pending", or a
weekly-limit dialog/toast becomes visible) and stops as soon as a streak of
clicks fails to register — this works in headless mode too.

## Notes

- `linkedin_cookies.json` holds your live LinkedIn session. It is git-ignored —
  **do not commit or share it**.
- Automating LinkedIn can violate its User Agreement and may get the account
  rate-limited or restricted. Use at your own risk.
