"""Tests for the invitation-limit state machine."""

from __future__ import annotations

from linkedin_auto_connect.domain.limits import InviteLimitPolicy


def test_new_policy_starts_empty() -> None:
    """A fresh policy has sent nothing and is not at any limit."""
    policy = InviteLimitPolicy(max_invites=10, max_unregistered=3)
    assert policy.sent == 0
    assert policy.unregistered_streak == 0
    assert policy.at_invite_cap is False


def test_cap_is_reached_at_the_boundary() -> None:
    """The cap flips exactly when the number of registered invites equals it."""
    policy = InviteLimitPolicy(max_invites=2, max_unregistered=5)
    policy.record_registered()
    assert policy.at_invite_cap is False
    policy.record_registered()
    assert policy.sent == 2
    assert policy.at_invite_cap is True
    policy.record_registered()
    assert policy.at_invite_cap is True


def test_zero_invite_cap_is_unlimited() -> None:
    """A non-positive cap never reports ``at_invite_cap``."""
    policy = InviteLimitPolicy(max_invites=0, max_unregistered=5)
    for _ in range(50):
        policy.record_registered()
    assert policy.sent == 50
    assert policy.at_invite_cap is False


def test_negative_invite_cap_is_unlimited() -> None:
    """A negative cap is treated the same way as zero."""
    policy = InviteLimitPolicy(max_invites=-1, max_unregistered=5)
    for _ in range(10):
        policy.record_registered()
    assert policy.at_invite_cap is False


def test_streak_triggers_exactly_at_the_threshold() -> None:
    """Only the attempt that reaches ``max_unregistered`` reports the limit."""
    policy = InviteLimitPolicy(max_invites=100, max_unregistered=3)
    assert policy.record_unregistered() is False
    assert policy.record_unregistered() is False
    assert policy.unregistered_streak == 2
    assert policy.record_unregistered() is True
    assert policy.unregistered_streak == 3


def test_registration_resets_the_streak() -> None:
    """An accepted invite breaks a run of failures."""
    policy = InviteLimitPolicy(max_invites=100, max_unregistered=3)
    policy.record_unregistered()
    policy.record_unregistered()
    policy.record_registered()
    assert policy.unregistered_streak == 0
    assert policy.record_unregistered() is False


def test_streak_limit_of_one_triggers_on_first_failure() -> None:
    """With ``max_unregistered=1`` any failure means the limit is reached."""
    policy = InviteLimitPolicy(max_invites=100, max_unregistered=1)
    assert policy.record_unregistered() is True


def test_unregistered_attempts_do_not_count_as_sent() -> None:
    """Failed attempts affect only the streak, never the sent counter or cap."""
    policy = InviteLimitPolicy(max_invites=1, max_unregistered=5)
    policy.record_unregistered()
    policy.record_unregistered()
    assert policy.sent == 0
    assert policy.at_invite_cap is False
