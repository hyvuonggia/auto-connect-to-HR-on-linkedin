"""Behaviour tests for :mod:`linkedin_auto_connect.application.runner`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from fakes import FakeCard, FakeLimitPolicy, FakeSource
from linkedin_auto_connect.application.runner import ConnectionRunner
from linkedin_auto_connect.config import Settings
from linkedin_auto_connect.domain.matcher import KeywordMatcher
from linkedin_auto_connect.domain.models import InviteStatus, SessionResult, StopReason


def make_settings(**overrides: object) -> Settings:
    """Return default settings with the given fields replaced."""
    return replace(Settings(), **overrides)


def make_runner(
    source: FakeSource,
    policy: FakeLimitPolicy,
    *,
    settings: Settings | None = None,
    processed: set[str] | None = None,
    should_stop: Callable[[], bool] | None = None,
    sleeps: list[float] | None = None,
) -> ConnectionRunner:
    """Build a runner wired to fakes and a recording sleeper."""
    stop = should_stop if should_stop is not None else (lambda: False)
    sleeper = sleeps.append if sleeps is not None else (lambda _seconds: None)
    return ConnectionRunner(
        source,
        KeywordMatcher(["hr"]),
        policy,
        settings or make_settings(max_passes_per_session=1, max_no_match_passes=10),
        should_stop=stop,
        sleeper=sleeper,
        processed=processed,
    )


def test_skips_non_matching_cards_and_marks_them_processed() -> None:
    """A card without a keyword is never clicked but is remembered."""
    matched_card = FakeCard("Mona Lisa", "HR manager")
    skipped_card = FakeCard("Bob Stone", "software engineer")
    source = FakeSource([matched_card, skipped_card])
    processed: set[str] = set()

    result = make_runner(source, FakeLimitPolicy(), processed=processed).run()

    assert result == SessionResult(invites=1, reason=StopReason.SCROLL_CAP)
    assert matched_card.send_calls == 1
    assert skipped_card.send_calls == 0
    assert processed == {"Mona Lisa", "Bob Stone"}


def test_invisible_connect_button_is_retried_on_the_next_pass() -> None:
    """A transiently invisible button is not burned: the name stays retryable."""
    card = FakeCard(
        "Mona Lisa",
        "HR manager",
        [InviteStatus.UNAVAILABLE, InviteStatus.REGISTERED],
    )
    source = FakeSource([card])
    processed: set[str] = set()

    result = make_runner(
        source,
        FakeLimitPolicy(),
        settings=make_settings(max_passes_per_session=2, max_no_match_passes=10),
        processed=processed,
    ).run()

    assert result == SessionResult(invites=1, reason=StopReason.SCROLL_CAP)
    assert card.send_calls == 2
    assert "Mona Lisa" in processed


def test_unconfirmed_invite_is_not_retried_and_does_not_trip_the_limit() -> None:
    """An unknown outcome suppresses retries but never counts as a limit signal."""
    card = FakeCard(
        "Flaky Person",
        "hr manager",
        [InviteStatus.UNCONFIRMED, InviteStatus.REGISTERED],
    )
    source = FakeSource([card])
    policy = FakeLimitPolicy(unregistered_limit=1)

    result = make_runner(
        source,
        policy,
        settings=make_settings(max_passes_per_session=2, max_no_match_passes=10),
    ).run()

    assert result == SessionResult(invites=0, reason=StopReason.SCROLL_CAP)
    assert card.send_calls == 1
    assert policy.unregistered_calls == 0


def test_blank_candidate_name_is_skipped() -> None:
    """A malformed card without a usable name is never clicked or remembered."""
    card = FakeCard("   ", "hr manager")
    processed: set[str] = set()

    result = make_runner(FakeSource([card]), FakeLimitPolicy(), processed=processed).run()

    assert result.invites == 0
    assert card.send_calls == 0
    assert processed == set()


def test_cap_reached_before_send_stops_as_limit_reached() -> None:
    """The per-run invite cap is checked before the click is attempted."""
    card = FakeCard("Cap Person", "hr manager")
    policy = FakeLimitPolicy(cap=5, sent=5)
    processed: set[str] = set()

    result = make_runner(FakeSource([card]), policy, processed=processed).run()

    assert result == SessionResult(invites=0, reason=StopReason.LIMIT_REACHED)
    assert card.send_calls == 0
    assert processed == set()


def test_unregistered_streak_stops_as_limit_reached() -> None:
    """A policy-reported failure streak stops the session at the second failure."""
    cards = [
        FakeCard(f"Person {index}", "hr manager", [InviteStatus.UNREGISTERED]) for index in range(3)
    ]
    policy = FakeLimitPolicy(unregistered_limit=2)

    result = make_runner(FakeSource(cards), policy).run()

    assert result == SessionResult(invites=0, reason=StopReason.LIMIT_REACHED)
    assert [card.send_calls for card in cards] == [1, 1, 0]
    assert policy.unregistered_calls == 2


def test_weekly_limit_wall_at_pass_start_stops_before_scanning() -> None:
    """A visible weekly-limit message stops the pass before any card is read."""
    source = FakeSource([FakeCard("Wall Person", "hr manager")], limit_walls=True)

    result = make_runner(source, FakeLimitPolicy()).run()

    assert result == SessionResult(invites=0, reason=StopReason.LIMIT_REACHED)
    assert source.open_calls == 0


def test_weekly_limit_wall_after_registered_invite_keeps_that_invite() -> None:
    """An invite counted before the wall appears still counts toward the session."""
    source = FakeSource(
        [FakeCard("Wall Person", "hr manager")],
        limit_walls=[False, True],
    )

    result = make_runner(source, FakeLimitPolicy()).run()

    assert result == SessionResult(invites=1, reason=StopReason.LIMIT_REACHED)


def test_weekly_limit_wall_after_failed_click_stops() -> None:
    """A wall that appears after a failed click is terminal even below the streak cap."""
    source = FakeSource(
        [FakeCard("Wall Person", "hr manager", [InviteStatus.UNREGISTERED])],
        limit_walls=[False, True],
    )
    policy = FakeLimitPolicy(unregistered_limit=5)

    result = make_runner(source, policy).run()

    assert result == SessionResult(invites=0, reason=StopReason.LIMIT_REACHED)
    assert policy.unregistered_calls == 1


def test_stall_after_configured_number_of_no_match_passes() -> None:
    """Rendered cards with no new matches stall the session after N passes."""
    settings = make_settings(
        max_passes_per_session=10,
        max_no_match_passes=2,
        scroll_pause_seconds=0.25,
    )
    source = FakeSource([FakeCard("Plain Person", "software engineer")])
    sleeps: list[float] = []

    result = make_runner(
        source,
        FakeLimitPolicy(),
        settings=settings,
        sleeps=sleeps,
    ).run()

    assert result == SessionResult(invites=0, reason=StopReason.STALLED)
    assert source.scroll_calls == [settings.scroll_pixels]
    assert sleeps == [settings.scroll_pause_seconds]


def test_zero_rendered_cards_does_not_count_as_a_stall() -> None:
    """A still-loading page leaves the no-match streak unchanged until the pass cap."""
    settings = make_settings(
        max_passes_per_session=3,
        max_no_match_passes=1,
        scroll_pause_seconds=0.5,
    )
    source = FakeSource([], card_counts=[0, 0, 0])
    sleeps: list[float] = []

    result = make_runner(
        source,
        FakeLimitPolicy(),
        settings=settings,
        sleeps=sleeps,
    ).run()

    assert result == SessionResult(invites=0, reason=StopReason.SCROLL_CAP)
    assert source.scroll_calls == [settings.scroll_pixels, settings.scroll_pixels]
    assert sleeps == [settings.scroll_pause_seconds, settings.scroll_pause_seconds]


def test_stop_signal_returns_stopped_without_scanning() -> None:
    """Ctrl+C between passes ends the session as STOPPED."""
    source = FakeSource([FakeCard("Stop Person", "hr manager")])

    result = make_runner(source, FakeLimitPolicy(), should_stop=lambda: True).run()

    assert result == SessionResult(invites=0, reason=StopReason.STOPPED)
    assert source.open_calls == 0


def test_already_processed_names_are_not_clicked_again() -> None:
    """A name in the shared processed set is skipped on later sessions and passes."""
    card = FakeCard("Known Person", "hr manager")
    processed = {"Known Person"}

    result = make_runner(
        FakeSource([card]),
        FakeLimitPolicy(),
        processed=processed,
    ).run()

    assert result.invites == 0
    assert card.send_calls == 0


def test_timed_out_click_is_marked_and_never_retried() -> None:
    """A timed-out click is pre-marked, so the same person is not clicked twice."""
    settings = make_settings(max_passes_per_session=2, max_no_match_passes=10)
    card = FakeCard("Slow Click", "hr manager", [InviteStatus.TIMED_OUT])
    processed: set[str] = set()
    policy = FakeLimitPolicy(unregistered_limit=5)

    result = make_runner(
        FakeSource([card]),
        policy,
        settings=settings,
        processed=processed,
    ).run()

    assert result == SessionResult(invites=0, reason=StopReason.SCROLL_CAP)
    assert card.send_calls == 1
    assert "Slow Click" in processed
    assert policy.unregistered_calls == 1


def test_scrolls_and_sleeps_once_between_two_passes() -> None:
    """Passes are separated by exactly one scroll and one configured pause."""
    settings = make_settings(max_passes_per_session=2, scroll_pause_seconds=0.5)
    source = FakeSource([], card_counts=[0, 0])
    sleeps: list[float] = []

    result = make_runner(
        source,
        FakeLimitPolicy(),
        settings=settings,
        sleeps=sleeps,
    ).run()

    assert result == SessionResult(invites=0, reason=StopReason.SCROLL_CAP)
    assert source.open_calls == 2
    assert source.scroll_calls == [settings.scroll_pixels]
    assert sleeps == [0.5]


def test_scroll_cap_at_pass_cap() -> None:
    """The pass cap returns SCROLL_CAP rather than looping forever."""
    settings = make_settings(max_passes_per_session=2, max_no_match_passes=10)
    source = FakeSource([FakeCard("Plain Person", "software engineer")], card_counts=[1, 1])

    result = make_runner(source, FakeLimitPolicy(), settings=settings).run()

    assert result == SessionResult(invites=0, reason=StopReason.SCROLL_CAP)
    assert source.open_calls == 2


def test_unavailable_card_warns_and_continues() -> None:
    """A card without a visible Connect button does not stop the pass."""
    unavailable = FakeCard("No Button", "hr manager", [InviteStatus.UNAVAILABLE])
    registered = FakeCard("Has Button", "hr manager")

    result = make_runner(
        FakeSource([unavailable, registered]),
        FakeLimitPolicy(),
    ).run()

    assert result == SessionResult(invites=1, reason=StopReason.SCROLL_CAP)
    assert unavailable.send_calls == 1
    assert registered.send_calls == 1
