# auto-connect-to-HR-on-linkedin

Automatically send LinkedIn connection requests to HR and recruiter profiles with
Playwright and Firefox.

The script walks the "People you may know" cards on
`linkedin.com/mynetwork/grow/`, keeps the cards whose text matches HR-related
keywords, and clicks **Connect** on them. Every click is verified: if LinkedIn
starts silently dropping invites (the weekly cap), the run stops instead of
burning through your weekly quota.

## Requirements

- Python 3.10+
- Firefox for Playwright

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -e ".[dev]"
python -m playwright install firefox
```

## Usage

```bash
python -m linkedin_auto_connect
# or, after installation:
linkedin-auto-connect
```

> The old single-file `python linkedin-auto-connect.py` entry point no longer
> exists; it is still in git history if you need the original.

**First run:** a visible browser opens so you can log in by hand. Once you are
logged in, the cookies are saved to `linkedin_cookies.json` and reused.

**Later runs:** the saved cookies are loaded and the browser stays headless. The
window opens again only if the cookies expired and a manual login is needed, and
then closes as soon as the session is authenticated.

Press `Ctrl+C` to stop gracefully.

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--max-invites N` | `200` | Stop after N registered invites (`0` = unlimited) |
| `--no-headless` | headless | Watch the browser instead of hiding it |
| `--keywords "a,b,c"` | HR list | Replace the audience keywords |
| `--cookies PATH` | `linkedin_cookies.json` | Where the cookie jar is stored |
| `--max-restarts N` | `15` | Browser restarts when a session stops finding profiles |
| `--scroll-pause S` | `2.0` | Seconds to wait after each scroll |
| `--click-pause S` | `1.5` | Seconds to wait after each Connect click |
| `--verbose` | off | Debug logging |
| `--version` | | Print the version |

Defaults live in [src/linkedin_auto_connect/config.py](src/linkedin_auto_connect/config.py).

## How the weekly limit is detected

At the weekly cap LinkedIn still accepts clicks and shows "Pending" while
dropping the invites server-side. Three signals are combined, all of them
visible in headless mode:

1. a visible weekly-limit dialog or toast,
2. a streak of clicks whose button never turned into "Pending",
3. a click that a modal intercepted (timeout).

Any of them ends the run; restarting cannot help, so the script does not retry.

## Project layout

```
src/linkedin_auto_connect/
├── cli.py            # argument parsing, logging, composition root
├── app.py            # sessions, restarts, cookie persistence
├── runner.py         # scan → match → click loop
├── browser.py        # Playwright adapter (the only module importing it)
├── matcher.py        # keyword audience
├── limits.py         # weekly-limit and per-run cap arithmetic
├── cookies.py        # JSON cookie store
├── models.py         # value objects
├── ports.py          # Protocols the core depends on
└── config.py         # Settings dataclass and endpoint defaults

tests/                # unit tests, no browser required
docs/ARCHITECTURE.md  # layering, ports, SOLID rationale
```

Run the tests with `pytest`; they never launch a browser.

## Notes

- `linkedin_cookies.json` holds your live LinkedIn session. It is git-ignored —
  **do not commit or share it**.
- Automating LinkedIn can violate its User Agreement and may get the account
  rate-limited or restricted. Use at your own risk.
