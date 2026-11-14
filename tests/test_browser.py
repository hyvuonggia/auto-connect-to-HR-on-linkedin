"""Tests for the Playwright adapter that do not need a real browser.

The stubs model only the Playwright calls the adapter makes, which is enough to
pin the behaviours that do not depend on a live LinkedIn page: label parsing,
card enumeration, and the invite verification state machine.
"""

from __future__ import annotations

from typing import Any

from linkedin_auto_connect.browser import (
    CARD_SELECTOR,
    CONNECT_BUTTON_SELECTOR,
    LinkedInNetworkPage,
    PlaywrightCard,
    parse_invite_label,
)
from linkedin_auto_connect.config import Settings
from linkedin_auto_connect.models import InviteStatus


class StubButton:
    """A Connect button that is present, visible and clickable."""

    def __init__(self, label: str = "Invite Jane Doe to connect", *, detaches: bool = True) -> None:
        self.label = label
        self.detaches = detaches
        self.clicks = 0

    def count(self) -> int:
        return 1

    @property
    def first(self) -> StubButton:
        return self

    def get_attribute(self, name: str) -> str | None:
        return self.label if name == "aria-label" else None

    def is_visible(self) -> bool:
        return True

    def click(self, timeout: int) -> None:
        self.clicks += 1

    def wait_for(self, *, state: str, timeout: int) -> None:
        if not self.detaches:
            from playwright.sync_api import TimeoutError as PlaywrightTimeout

            raise PlaywrightTimeout("not detached")

    def locator(self, selector: str) -> Any:
        raise AssertionError("the Pending fallback must not be queried when the button detaches")


class StubCard:
    """One rendered card, optionally containing a Connect button."""

    def __init__(
        self,
        text: str = "Jane Doe — HR Manager",
        button: StubButton | None = None,
        *,
        with_button: bool = True,
    ) -> None:
        self.text = text
        self.button = button if button is not None else StubButton()
        self.with_button = with_button

    def locator(self, selector: str) -> Any:
        if selector == CONNECT_BUTTON_SELECTOR and self.with_button:
            return self.button
        return _Empty()

    def inner_text(self) -> str:
        return self.text


class _Empty:
    """A locator that matches nothing."""

    def count(self) -> int:
        return 0

    @property
    def first(self) -> _Empty:
        return self


class StubCards:
    """The result of ``page.locator('[role="listitem"]')``."""

    def __init__(self, cards: list[StubCard]) -> None:
        self._cards = cards

    def count(self) -> int:
        return len(self._cards)

    def nth(self, index: int) -> StubCard:
        return self._cards[index]


class StubPage:
    """A page exposing only what the adapter touches."""

    def __init__(self, cards: list[StubCard]) -> None:
        self._cards = cards
        self.waits: list[float] = []

    def locator(self, selector: str) -> Any:
        assert selector == CARD_SELECTOR
        return StubCards(self._cards)

    def wait_for_timeout(self, milliseconds: float) -> None:
        self.waits.append(milliseconds)


def test_parse_invite_label_extracts_the_name() -> None:
    """The aria-label wrapper is removed and whitespace collapsed."""
    assert parse_invite_label("Invite Jane Doe to connect") == "Jane Doe"
    assert parse_invite_label("  Invite   Jane   Doe   to connect  ") == "Jane Doe"
    assert parse_invite_label("Invite Ask about to connect to connect") == "Ask about to connect"
    assert parse_invite_label("") == ""
    assert parse_invite_label("Jane Doe") == "Jane Doe"


def test_open_cards_returns_a_handle_per_card_with_a_connect_button() -> None:
    """A rendered card with a Connect button becomes a live card handle."""
    page = StubPage([StubCard(), StubCard(text="Bob Stone — engineer")])

    cards = LinkedInNetworkPage(page, Settings()).open_cards()

    assert len(cards) == 2
    assert isinstance(cards[0], PlaywrightCard)


def test_open_cards_skips_cards_without_a_connect_button() -> None:
    """Cards with no Connect button (already pending, or not a person) are dropped."""
    page = StubPage([StubCard(with_button=False)])

    assert LinkedInNetworkPage(page, Settings()).open_cards() == []


def test_candidate_parses_the_name_and_keeps_the_card_text() -> None:
    """The card exposes the parsed name plus the raw text the matcher sees."""
    page = StubPage([StubCard()])

    candidate = LinkedInNetworkPage(page, Settings()).open_cards()[0].candidate

    assert candidate.name == "Jane Doe"
    assert "HR Manager" in candidate.text


def test_send_invite_reports_registered_when_the_button_detaches() -> None:
    """The Connect button turning into Pending is the registration signal."""
    page = StubPage([StubCard()])
    card = LinkedInNetworkPage(page, Settings()).open_cards()[0]

    result = card.send_invite()

    assert result.status is InviteStatus.REGISTERED
    assert page.waits  # the click pause was honoured


def test_send_invite_reports_unregistered_when_nothing_changes() -> None:
    """A button that never detaches and shows no Pending state means dropped invite."""
    card = StubCard(button=StubButton(detaches=False))
    page = StubPage([card])

    result = LinkedInNetworkPage(page, Settings()).open_cards()[0].send_invite()

    assert result.status is InviteStatus.UNREGISTERED


def test_send_invite_reports_unavailable_without_a_button() -> None:
    """A card without a visible Connect button is reported, not raised."""
    card = PlaywrightCard(StubPage([]), StubCard(with_button=False), Settings())

    assert card.send_invite().status is InviteStatus.UNAVAILABLE
