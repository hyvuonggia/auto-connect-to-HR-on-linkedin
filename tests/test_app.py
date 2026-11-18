"""Behaviour tests for :mod:`linkedin_auto_connect.application.app`."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from fakes import (
    FakeCard,
    FakeCookieStore,
    FakeLimitPolicy,
    FakeSession,
    FakeSessionFactory,
    FakeSource,
)
from linkedin_auto_connect.application.app import Application
from linkedin_auto_connect.config import Settings
from linkedin_auto_connect.domain.matcher import KeywordMatcher
from linkedin_auto_connect.domain.models import LoginStatus

COOKIES: list[dict[str, Any]] = [{"name": "li_at", "value": "secret"}]

RESTART_BASE_SECONDS = 5.0
RESTART_MAX_SECONDS = 60.0


def make_settings(**overrides: object) -> Settings:
    """Return app-test settings with small loops and short restart delays."""
    base = Settings(
        headless=True,
        max_passes_per_session=1,
        max_no_match_passes=1,
        max_restarts=2,
        restart_base_delay_seconds=RESTART_BASE_SECONDS,
        restart_max_delay_seconds=RESTART_MAX_SECONDS,
    )
    return replace(base, **overrides)


def make_app(
    factory: FakeSessionFactory,
    store: FakeCookieStore,
    *,
    settings: Settings | None = None,
    policy: FakeLimitPolicy | None = None,
    sleeps: list[float] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Application:
    """Build an application wired to fakes and a recording sleeper."""
    sleeper = sleeps.append if sleeps is not None else (lambda _seconds: None)
    return Application(
        factory,
        store,
        settings or make_settings(),
        KeywordMatcher(["hr"]),
        should_stop=should_stop if should_stop is not None else (lambda: False),
        sleeper=sleeper,
        policy=policy or FakeLimitPolicy(),
    )


def test_headless_expired_logs_in_visibly_saves_and_reopens_headless() -> None:
    """The full fallback chain runs in order and closes every intermediate session."""
    events: list[str] = []
    cookies = list(COOKIES)
    sessions = [
        FakeSession(status=LoginStatus.EXPIRED, label="s1", events=events),
        FakeSession(
            status=LoginStatus.AUTHENTICATED,
            cookies=cookies,
            label="s2",
            events=events,
        ),
        FakeSession(status=LoginStatus.AUTHENTICATED, label="s3", events=events),
    ]
    factory = FakeSessionFactory(sessions, events=events)
    store = FakeCookieStore(events=events)

    result = make_app(factory, store, settings=make_settings(max_restarts=0)).run()

    assert result == 0
    assert factory.headless_flags == [True, False, True]
    assert events == [
        "open:True",
        "s1:authenticate",
        "s1:close",
        "open:False",
        "s2:authenticate",
        "s2:cookies",
        "store:save",
        "s2:close",
        "open:True",
        "s3:authenticate",
        "s3:cookies",
        "s3:close",
    ]
    assert store.saved == [cookies]
    assert sessions[0].authenticate_calls[0]["allow_manual_login"] is False
    assert sessions[1].authenticate_calls[0]["allow_manual_login"] is True
    assert sessions[2].authenticate_calls[0]["allow_manual_login"] is False


def test_challenge_stops_and_saves_nothing() -> None:
    """A bot challenge is terminal and never persists cookies."""
    session = FakeSession(status=LoginStatus.CHALLENGE)
    factory = FakeSessionFactory([session])
    store = FakeCookieStore()

    result = make_app(factory, store).run()

    assert result == 0
    assert factory.headless_flags == [True]
    assert session.close_calls == 1
    assert store.saved == []


def test_blocked_session_never_overwrites_cookies() -> None:
    """An authenticated session sitting on a login wall must not save its jar."""
    session = FakeSession(status=LoginStatus.AUTHENTICATED, blocked=True)
    factory = FakeSessionFactory([session])
    store = FakeCookieStore()

    result = make_app(factory, store).run()

    assert result == 0
    assert store.saved == []
    assert session.close_calls == 1
    assert session.blocked_checks == 1


def test_stalled_session_restarts_until_max_restarts_then_stops() -> None:
    """Each stall opens a fresh session until the restart budget is exhausted."""
    settings = make_settings(max_restarts=2, max_no_match_passes=1, max_passes_per_session=10)
    sessions = [
        FakeSession(
            FakeSource([FakeCard("Plain Person", "software engineer")]),
            cookies=COOKIES,
            label=f"s{index}",
        )
        for index in range(3)
    ]
    factory = FakeSessionFactory(sessions)
    store = FakeCookieStore()
    sleeps: list[float] = []

    result = make_app(factory, store, settings=settings, sleeps=sleeps).run()

    assert result == 0
    assert factory.headless_flags == [True, True, True]
    assert sleeps == [RESTART_BASE_SECONDS, RESTART_BASE_SECONDS * 2]
    assert all(session.close_calls == 1 for session in sessions)
    assert len(store.saved) == 3


def test_scroll_cap_restarts_a_session_that_reached_its_pass_cap() -> None:
    """Reaching the per-session pass cap opens a fresh session instead of ending the run."""
    settings = make_settings(
        max_restarts=1,
        max_passes_per_session=1,
        max_no_match_passes=10,
    )
    sessions = [
        FakeSession(
            FakeSource([FakeCard("Plain Person", "software engineer")]),
            cookies=COOKIES,
            label=f"s{index}",
        )
        for index in range(2)
    ]
    factory = FakeSessionFactory(sessions)
    store = FakeCookieStore()
    sleeps: list[float] = []

    result = make_app(factory, store, settings=settings, sleeps=sleeps).run()

    assert result == 0
    assert factory.headless_flags == [True, True]
    assert sleeps == [RESTART_BASE_SECONDS]
    assert all(session.close_calls == 1 for session in sessions)


def test_a_session_with_an_invite_resets_the_restart_budget() -> None:
    """The run keeps going toward the target as long as sessions bring new invites."""
    settings = make_settings(
        max_restarts=1,
        max_passes_per_session=1,
        max_no_match_passes=10,
    )
    sessions = [
        FakeSession(FakeSource([FakeCard("First HR", "hr manager")]), cookies=COOKIES),
        FakeSession(FakeSource([FakeCard("Second HR", "hr manager")]), cookies=COOKIES),
        FakeSession(FakeSource([FakeCard("Plain Person", "software engineer")]), cookies=COOKIES),
    ]
    factory = FakeSessionFactory(sessions)
    store = FakeCookieStore()
    sleeps: list[float] = []

    result = make_app(factory, store, settings=settings, sleeps=sleeps).run()

    assert result == 2
    assert factory.headless_flags == [True, True, True]
    assert sleeps == [RESTART_BASE_SECONDS, RESTART_BASE_SECONDS]


def test_configured_cap_is_reported_as_target_not_as_the_weekly_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reaching ``--max-invites`` is a success, not a LinkedIn rate-limit warning."""
    settings = make_settings(max_passes_per_session=1, max_no_match_passes=10)
    source = FakeSource(
        [
            FakeCard("First HR", "hr manager"),
            FakeCard("Second HR", "hr manager"),
        ]
    )
    session = FakeSession(source, cookies=COOKIES)
    factory = FakeSessionFactory([session])
    store = FakeCookieStore()

    with caplog.at_level(logging.INFO, logger="linkedin_auto_connect.application.app"):
        result = make_app(
            factory,
            store,
            settings=settings,
            policy=FakeLimitPolicy(cap=1),
        ).run()

    messages = [record.getMessage() for record in caplog.records]
    assert result == 1
    assert any("Target reached: 1 registered invite(s)" in message for message in messages)
    assert not any("weekly connection limit" in message for message in messages)


def test_reaching_the_target_stops_without_a_restart() -> None:
    """The 200th (or configured) invite ends the run immediately, not one session later."""
    settings = make_settings(
        max_restarts=5,
        max_passes_per_session=2,
        max_no_match_passes=10,
    )
    session = FakeSession(FakeSource([FakeCard("Last HR", "hr manager")]), cookies=COOKIES)
    factory = FakeSessionFactory([session])
    store = FakeCookieStore()

    result = make_app(
        factory,
        store,
        settings=settings,
        policy=FakeLimitPolicy(cap=1),
    ).run()

    assert result == 1
    assert factory.headless_flags == [True]


def test_limit_reached_stops_without_a_restart() -> None:
    """A weekly-limit stop is terminal; no replacement session is opened."""
    settings = make_settings(max_restarts=5)
    session = FakeSession(FakeSource([], limit_walls=True))
    factory = FakeSessionFactory([session])
    store = FakeCookieStore()
    sleeps: list[float] = []

    result = make_app(factory, store, settings=settings, sleeps=sleeps).run()

    assert result == 0
    assert factory.headless_flags == [True]
    assert sleeps == []
    assert session.close_calls == 1


def test_visible_challenge_stops_and_closes_every_session() -> None:
    """A failed visible login closes the headless and the visible browser."""
    first = FakeSession(status=LoginStatus.EXPIRED, label="s1")
    visible = FakeSession(status=LoginStatus.CHALLENGE, label="s2")
    factory = FakeSessionFactory([first, visible])
    store = FakeCookieStore()

    result = make_app(factory, store).run()

    assert result == 0
    assert factory.headless_flags == [True, False]
    assert first.close_calls == 1
    assert visible.close_calls == 1
    assert store.saved == []


def test_stopped_reason_ends_the_run_without_a_restart() -> None:
    """Ctrl+C stops the run after the current session; no replacement opens."""
    settings = make_settings(max_restarts=5)
    session = FakeSession(cookies=COOKIES)
    factory = FakeSessionFactory([session])
    store = FakeCookieStore()
    calls = 0

    def should_stop() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    result = make_app(
        factory,
        store,
        settings=settings,
        should_stop=should_stop,
    ).run()

    assert result == 0
    assert factory.headless_flags == [True]
    assert session.close_calls == 1
    assert len(store.saved) == 1


def test_returns_the_total_registered_invites() -> None:
    """Registered invites from the session loop are returned to the CLI."""
    session = FakeSession(FakeSource([FakeCard("HR Person", "hr manager")]), cookies=COOKIES)
    policy = FakeLimitPolicy()
    settings = make_settings(max_no_match_passes=2, max_restarts=0)
    factory = FakeSessionFactory([session])
    store = FakeCookieStore()

    result = make_app(factory, store, settings=settings, policy=policy).run()

    assert result == 1
    assert policy.sent == 1
    assert len(store.saved) == 1


class _ExplodingSource(FakeSource):
    def open_cards(self) -> list[FakeCard]:
        """Fail the runner after the session authenticated."""
        raise RuntimeError("boom")


class _RaisingCloseSession(FakeSession):
    def close(self) -> None:
        """Record the close and fail, ensuring the app swallows close errors."""
        super().close()
        raise RuntimeError("close failed")


def test_close_error_does_not_mask_the_runner_exception() -> None:
    """The original failure reaches the caller even when close also raises."""
    session = _RaisingCloseSession(_ExplodingSource())
    factory = FakeSessionFactory([session])
    store = FakeCookieStore()

    with pytest.raises(RuntimeError, match="boom"):
        make_app(factory, store).run()

    assert session.close_calls == 1
