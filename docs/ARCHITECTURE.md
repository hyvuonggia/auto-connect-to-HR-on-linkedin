# Architecture

`linkedin_auto_connect` is a small program with one genuinely awkward dependency:
everything useful about it requires a live browser. The layout is built around
keeping that dependency at the edge, so the decisions (what counts as an HR
profile, when the weekly limit is hit, when to stop) are ordinary Python that
can be tested in milliseconds.

## Layers

```
                 ┌───────────────────────────────┐
   entry point   │ cli.py  /  __main__.py        │  argparse, logging, wiring
                 └───────────────┬───────────────┘
                                 │
                 ┌───────────────▼───────────────┐
   orchestration │ app.py        session lifecycle, restarts, cookie persistence
                 │ runner.py     scan → match → click loop
                 └───────────────┬───────────────┘
                                 │  depends on Protocols only
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
   ┌────▼─────┐          ┌───────▼───────┐        ┌───────▼────────┐
   │ matcher  │          │   limits      │        │    models      │
   │ keywords │          │ streak + cap  │        │ value objects  │
   └──────────┘          └───────────────┘        └────────────────┘
                                 │
                 ┌───────────────▼───────────────┐
   adapters      │ browser.py    Playwright       │  the only module that
                 │ cookies.py    JSON file        │  imports playwright
                 └───────────────────────────────┘
```

The dependency rule: arrows point inward. `models`, `ports`, `config`,
`matcher`, `limits`, `cookies`, `runner` and `app` never import Playwright.
`tests/test_layering.py` enforces that by scanning the source, so the rule cannot
rot silently.

## Ports and why each exists

`ports.py` holds `typing.Protocol` interfaces, not base classes: adapters do not
inherit from anything, and tests implement the same shape with a dozen lines.

| Port | Implemented by | Exists so that |
| --- | --- | --- |
| `ProfileSource` | `browser.LinkedInNetworkPage` | The runner can be tested against a scripted page |
| `ProfileCard` | `browser.PlaywrightCard` | Click-and-verify lives with the DOM, not in the loop |
| `BrowserSession` | `browser.PlaywrightSession` | Auth, restarts and cookie persistence are orchestration |
| `SessionFactory` | `browser.PlaywrightSessionFactory` | Visible/headless retry is a policy of `app` |
| `CookieStore` | `cookies.JsonCookieStore` | Persistence is replaceable and testable on `tmp_path` |
| `Matcher` | `matcher.KeywordMatcher` | The audience definition is data, not DOM code |
| `LimitPolicy` | `limits.InviteLimitPolicy` | Limit arithmetic is a pure state machine |

`Candidate` is a value object, not a handle: the matcher sees only `name` and the
card's visible text. The card keeps the DOM handle, which is why `send_invite`
takes no arguments.

## Where the weekly limit is detected

LinkedIn accepts the click, shows "Pending", and drops the invite server-side
once the weekly cap is hit. Three independent signals are therefore combined, all
of them observable without a visible browser:

1. `ProfileSource.limit_wall_visible()` — an actual dialog/toast is on screen.
2. `InviteLimitPolicy.record_unregistered()` — several clicks in a row never
   turned the Connect button into "Pending".
3. `InviteStatus.TIMED_OUT` — a modal intercepted the click.

Any of them ends the run as `StopReason.LIMIT_REACHED`; restarting does not help,
so `app` does not retry.

## Restart policy

A session that runs out of new profiles (`STALLED`) is restarted with a growing
delay, because LinkedIn serves a different "People you may know" batch per
session. `STOPPED`, `SCROLL_CAP`, `LIMIT_REACHED`, an unauthenticated session and
a blocked session never restart.

## SOLID, concretely

- **SRP** — `browser.py` talks to the DOM; `runner.py` decides; `limits.py`
  counts; `cookies.py` persists; `cli.py` parses arguments. The old
  `process_connections_on_page` did all five.
- **OCP** — a new audience (`SeniorityMatcher`, `TitleMatcher`) or a new limit
  signal is a new class behind an existing port; no loop edits.
- **LSP** — `FakeSource` and `PlaywrightPage` are interchangeable from the
  runner's point of view, including the "no cards rendered yet" state.
- **ISP** — `ProfileCard` has two members; the runner never sees cookies,
  navigation or settings beyond the timings it uses.
- **DIP** — `runner` and `app` depend on `ports`, and `cli.main` is the single
  place that names concrete adapters.

## Deliberate simplifications

- No dependency-injection framework and no service container: `cli.main` is the
  composition root.
- No reporting abstraction: modules log, `cli.configure_logging` owns formatting.
- No async: Playwright's sync API is what the original script used and the
  workload is inherently serial (one browser, one page).
