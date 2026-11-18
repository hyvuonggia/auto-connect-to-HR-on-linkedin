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

**Watching the run:** `--watch` opens the browser and slows every Playwright
action by 500 ms so you can follow what the script does. Every rendered card is
logged as `CHECK`, `PASS (audience)`, or `FAIL (...)`; with Jev enabled the line
also carries the HR/IT/evidence scores. If cards render but none has a Connect
button, the run logs the first card's button labels and saves
`linkedin_debug.png`. `--watch` always implies a visible browser, even if
`--headless` is passed. Combine it with `--max-invites 3` for a short demo run.
`--verbose` enables the same per-card log without slowing the browser.

Press `Ctrl+C` to stop gracefully.

### Options

| Flag | Default | Meaning |
| --- | --- | --- |
| `--max-invites N` | `200` | Stop after N registered invites (`0` = unlimited) |
| `--no-headless` | headless | Watch the browser instead of hiding it (`--headless` is the default form) |
| `--watch` | off | Show the browser, slow every action down, and log every profile card |
| `--keywords "a,b,c"` | HR list | Replace the audience keywords |
| `--cookies PATH` | `linkedin_cookies.json` | Where the cookie jar is stored |
| `--max-restarts N` | `15` | Browser restarts when a session stops finding profiles |
| `--scroll-pause S` | `2.0` | Seconds to wait after each scroll |
| `--click-pause S` | `1.5` | Seconds to wait after each Connect click |
| `--no-jev` | Jev on if configured | Use keyword-only HR matching |
| `--jev-timeout S` | `15` | Timeout for one Jev request |
| `--it-yes-threshold P` | `0.7` | Connect when the HR and IT probabilities are at least P |
| `--it-no-threshold P` | `0.3` | Log a confident non-IT decision at or below P |
| `--it-evidence-threshold P` | `0.4` | Skip cards below P evidence |
| `--it-cache PATH` | `linkedin_it_decisions.json` | Jev decision cache |
| `--verbose` | off | Debug logging |
| `--version` | | Print the version |

Defaults live in [src/linkedin_auto_connect/config.py](src/linkedin_auto_connect/config.py).

## IT-relevance filter (Jev)

When a `.env` file provides both `JEV_API_KEY` and `JEV_BASE_URL`, every HR
keyword match is also checked by
[Jev](https://opencode.ai/docs/console/models/#jev), a System One decision
model served by the OpenCode Console. Jev answers three yes/no questions in one
request:

1. is this person actually in HR or recruiting?
2. is their HR work tied to the IT industry (an IT employer, or IT recruiting)?
3. does the card contain enough evidence?

A card is connected only when the persona passes the HR gate, the evidence gate,
and the IT gate (defaults: HR ≥ 0.7, evidence ≥ 0.4, IT ≥ 0.7). The keyword
prefilter still runs first, so a software engineer or a hiring manager who
happens to match a broad keyword such as `looking for` or `hiring` never becomes
an invite. The filter is on by default when Jev is configured; `--no-jev`
restores keyword-only matching.

```env
# .env (git-ignored)
JEV_API_KEY=your-opencode-console-key
JEV_BASE_URL=https://opencode.ai/inference/systemone
```

The filter uses the free `jev-1.13-free` model; there is no model-selection
flag. If the model is unavailable, the setup fallback keeps the old HR-only
behaviour.

If the variables are missing, the script logs a warning and keeps the old
HR-only behaviour. If Jev is configured but rejects the setup (bad key, model
unavailable, malformed request), the same keyword-only fallback applies.
Transient failures (timeout, 429/5xx) skip the card; five consecutive failures
disable Jev for the rest of the run rather than sending non-IT invites.

Decisions are cached in `linkedin_it_decisions.json` by a hash of the card
text, so restarts and repeated passes do not call Jev again. The cache stores
no profile text. The visible card text (name, headline, and social-proof lines)
is sent to the OpenCode Console; review its privacy terms before using the
filter on sensitive data.

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
├── cli.py                    # argument parsing, logging, composition root
├── config.py                 # Settings dataclass and endpoint defaults
├── domain/                   # pure rules, no I/O
│   ├── models.py             # value objects and Jev errors
│   ├── ports.py              # Protocols the core depends on
│   ├── matcher.py            # keyword prefilter + Jev composite
│   └── limits.py             # weekly-limit and per-run cap arithmetic
├── application/              # orchestration over the ports
│   ├── app.py                # sessions, restarts, cookie persistence
│   └── runner.py             # scan → match → click loop
└── adapters/                 # I/O at the edge
    ├── browser.py            # Playwright adapter (the only module importing it)
    ├── cookies.py            # JSON cookie store
    ├── jev.py                # OpenCode/TypeSafe System One HTTP adapter
    └── jev_cache.py          # persistent decision cache + cached classifier

tests/                # unit tests, no browser required
docs/ARCHITECTURE.md  # layering, ports, SOLID rationale
```

Dependencies point inward: `adapters` → `application` → `domain`. `cli.py` is
the only module that names concrete adapters.

Run the tests with `pytest`; they never launch a browser.

## Notes

- `linkedin_cookies.json` holds your live LinkedIn session and is resolved from
  the current working directory (`--cookies` overrides it). It is git-ignored —
  **do not commit or share it**.
- `.env` holds the Jev credentials and `linkedin_it_decisions.json` holds the
  decision cache; both are git-ignored. The Jev adapter uses only the Python
  standard library, so enabling the filter adds no dependency.
- The `--scroll-pause` and `--click-pause` defaults match the original script,
  with one fix: the original waited ~1.5 *milliseconds* after a click because of
  a missing unit conversion, so clicks are now paced as intended (1.5 s).
- Knobs that are not CLI flags (`scroll_pixels`, `registration_wait_ms`,
  `max_unregistered_clicks`, `max_passes_per_session`, `max_no_match_passes`)
  live in `Settings` in
  [config.py](src/linkedin_auto_connect/config.py).
- Automating LinkedIn can violate its User Agreement and may get the account
  rate-limited or restricted. Use at your own risk.
