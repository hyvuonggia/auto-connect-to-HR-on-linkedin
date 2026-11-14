# Architecture

`linkedin_auto_connect` is a small program with one genuinely awkward dependency:
everything useful about it requires a live browser. The package is split into
three layers so that dependency stays at the edge and the decisions (what counts
as an HR profile, when the weekly limit is hit, when to stop) remain ordinary
Python that can be tested in milliseconds.

## Package layout

```
src/linkedin_auto_connect/
├── cli.py                    # argparse, logging, composition root
├── config.py                 # frozen Settings, endpoint defaults
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
    └── jev_cache.py          # JSON decision cache + cached classifier
```

Dependencies point inward: `adapters` → `application` → `domain`. `cli.py` is
the only module that names concrete adapters; `config.py` is a shared,
I/O-free settings object passed to the layers that need it.

## Layers

```
                 ┌───────────────────────────────┐
   entry point   │ cli.py  /  __main__.py        │  argparse, logging, wiring
                 └───────────────┬───────────────┘
                                 │
                 ┌───────────────▼───────────────┐
   application   │ application/app.py            │  session lifecycle, restarts,
                 │ application/runner.py         │  scan → match → click loop
                 └───────────────┬───────────────┘
                                 │  depends on Protocols only
         ┌────────────────────────┼────────────────────────┐
         │                        │                        │
    ┌────▼─────┐          ┌───────▼───────┐        ┌───────▼────────┐
    │ domain/  │          │ domain/       │        │ domain/        │
    │ matcher  │          │ limits        │        │ models + ports │
    └──────────┘          └───────────────┘        └────────────────┘
                                 │
                 ┌───────────────▼───────────────┐
   adapters      │ adapters/browser.py           │  browser.py is the only
                 │ adapters/jev.py               │  module importing
                 │ adapters/cookies.py           │  playwright
                 │ adapters/jev_cache.py         │
                 └───────────────────────────────┘
```

The dependency rule is enforced by `tests/test_layering.py`: `domain` and
`application` never import an adapter, Playwright, or an HTTP client, and
`domain` does not import `config`. The test scans the source, so the rule cannot
rot silently.

## Ports and why each exists

`domain/ports.py` holds `typing.Protocol` interfaces, not base classes: adapters
do not inherit from anything, and tests implement the same shape with a dozen
lines.

| Port | Implemented by | Exists so that |
| --- | --- | --- |
| `ProfileSource` | `adapters.browser.LinkedInNetworkPage` | The runner can be tested against a scripted page |
| `ProfileCard` | `adapters.browser.PlaywrightCard` | Click-and-verify lives with the DOM, not in the loop |
| `BrowserSession` | `adapters.browser.PlaywrightSession` | Auth, restarts and cookie persistence are orchestration |
| `SessionFactory` | `adapters.browser.PlaywrightSessionFactory` | Visible/headless retry is a policy of `application.app` |
| `CookieStore` | `adapters.cookies.JsonCookieStore` | Persistence is replaceable and testable on `tmp_path` |
| `Matcher` | `domain.matcher.KeywordMatcher`, `domain.matcher.ItRelevanceMatcher` | The audience definition is data, not DOM code |
| `Classifier` | `adapters.jev.JevClassifier`, `adapters.jev_cache.CachedClassifier` | The HR/IT judgment is a typed decision, not generated text |
| `LimitPolicy` | `domain.limits.InviteLimitPolicy` | Limit arithmetic is a pure state machine |

`Candidate` is a value object, not a handle: the matcher sees only `name` and the
card's visible text. The card keeps the DOM handle, which is why `send_invite`
takes no arguments.

## The IT-relevance decision

`domain.matcher.ItRelevanceMatcher` is a composite behind the same `Matcher`
port. The keyword prefilter runs first, so a card that is not even HR-shaped
never reaches the model. Jev then answers three atomic `noul` questions in one
request:

1. `is_hr_role` — is this person actually in HR or recruiting? This is what
   keeps a software engineer who matched `looking for`, a hiring manager who
   matched `hiring`, or a machine-learning engineer who matched `learning` out
   of the audience.
2. `it_role_or_company` — is their HR work tied to IT (an IT employer, or IT
   recruiting at any employer)?
3. `has_enough_evidence` — does the card name an employer or a role whose
   industry can be identified?

A card is connected only when HR ≥ 0.7, evidence ≥ 0.4, and IT ≥ 0.7. The
classifier is an edge adapter (`adapters/jev.py`); the cache is another adapter
(`adapters/jev_cache.py`) wrapping it behind the same `Classifier` port, so the
composite matcher is unit-tested with a fake classifier and no network.

A setup rejection falls back to the keyword decision, which preserves the old
HR-only behaviour when Jev is not configured. Transient failures skip the card,
and five consecutive failures disable Jev for the rest of the run rather than
silently sending non-IT invites.

## Where the weekly limit is detected

LinkedIn accepts the click, shows "Pending", and drops the invite server-side
once the weekly cap is hit. Three independent signals are therefore combined, all
of them observable without a visible browser:

1. `ProfileSource.limit_wall_visible()` — an actual dialog/toast is on screen.
2. `InviteLimitPolicy.record_unregistered()` — several clicks in a row never
   turned the Connect button into "Pending".
3. `InviteStatus.TIMED_OUT` — a modal intercepted the click.

Any of them ends the run as `StopReason.LIMIT_REACHED`; restarting does not help,
so `application.app` does not retry.

## Restart policy

A session that runs out of new profiles (`STALLED`) is restarted with a growing
delay, because LinkedIn serves a different "People you may know" batch per
session. `STOPPED`, `SCROLL_CAP`, `LIMIT_REACHED`, an unauthenticated session and
a blocked session never restart.

## SOLID, concretely

- **SRP** — `adapters/browser.py` talks to the DOM; `application/runner.py`
  decides; `domain/limits.py` counts; `adapters/cookies.py` persists; `cli.py`
  parses arguments. The old `process_connections_on_page` did all five.
- **OCP** — a new audience (`SeniorityMatcher`, `TitleMatcher`) or a new limit
  signal is a new class behind an existing port; no loop edits.
- **LSP** — `FakeSource` and `LinkedInNetworkPage` are interchangeable from the
  runner's point of view, including the "no cards rendered yet" state.
- **ISP** — `ProfileCard` has two members; the runner never sees cookies,
  navigation or settings beyond the timings it uses.
- **DIP** — `application` depends on `domain.ports`, and `cli.main` is the single
  place that names concrete adapters.

## Deliberate simplifications

- No dependency-injection framework and no service container: `cli.main` is the
  composition root.
- No reporting abstraction: modules log, `cli.configure_logging` owns formatting.
- The Jev adapter uses the standard library (`urllib`) instead of the vendor SDK:
  one small HTTP call, no added dependency, and `JEV_BASE_URL` keeps the endpoint
  replaceable.
- No async: Playwright's sync API is what the original script used and the
  workload is inherently serial (one browser, one page).
- The "never overwrite a good cookie jar with an empty one" guard lives in
  `adapters.cookies.JsonCookieStore.save`, not in the application: persisting is
  the store's job, and `Application` simply asks it to save after every clean
  session.
- The old single-file script was deleted rather than kept as a shim; git history
  has it, and one entry point is easier to keep honest.
- `parse_invite_label` strips only the leading "Invite" and trailing "to connect"
  (the old script removed every occurrence anywhere in the label). A name that
  happens to contain "to connect" is now preserved instead of mangled.
