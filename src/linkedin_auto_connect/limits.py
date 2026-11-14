"""Invitation-limit bookkeeping for one run.

LinkedIn exposes no reliable client-side counter for the weekly invitation
limit, so the run watches two cheaper signals: the configured per-run invite
cap and a streak of consecutive clicks that failed to register. This module is
the pure state machine behind both -- no I/O, no logging.
"""

from __future__ import annotations

from .models import UNLIMITED_INVITES


class InviteLimitPolicy:
    """Track registered invites and consecutive unregistered attempts.

    ``max_invites`` at or below :data:`UNLIMITED_INVITES` means the per-run
    invite cap is disabled. When the streak of unregistered attempts reaches
    ``max_unregistered`` the caller treats the weekly limit as reached and
    stops the run.
    """

    def __init__(self, *, max_invites: int, max_unregistered: int) -> None:
        """Create a policy with a per-run invite cap and a failure-streak limit."""
        self._max_invites = max_invites
        self._max_unregistered = max_unregistered
        self._sent = 0
        self._unregistered_streak = 0

    @property
    def sent(self) -> int:
        """Registered invites so far in this run."""
        return self._sent

    @property
    def unregistered_streak(self) -> int:
        """Consecutive attempts whose invite did not register."""
        return self._unregistered_streak

    @property
    def at_invite_cap(self) -> bool:
        """True when the per-run invite cap is enabled and has been reached."""
        if self._max_invites <= UNLIMITED_INVITES:
            return False
        return self._sent >= self._max_invites

    def record_registered(self) -> None:
        """Count one accepted invite and reset the unregistered streak."""
        self._sent += 1
        self._unregistered_streak = 0

    def record_unregistered(self) -> bool:
        """Count one failed attempt; True once the streak limit is reached."""
        self._unregistered_streak += 1
        return self._unregistered_streak >= self._max_unregistered
