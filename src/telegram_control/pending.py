"""Ephemeral, owner-bound, single-use Telegram confirmations."""

# Direct errors are rendered through a safe user-facing mapping.
# ruff: noqa: ANN401, PLR0913, TRY003

from __future__ import annotations

import secrets
from dataclasses import replace
from threading import RLock
from time import monotonic
from typing import Any

from telegram_control.models import PendingAction, PendingActionState, PendingActionType


class PendingActionError(ValueError):
    """Safe confirmation failure identified by a stable reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PendingActionStore:
    """Keep confirmation state in memory; restart invalidates every nonce."""

    def __init__(self, maximum_pending: int = 64) -> None:
        self.maximum_pending = maximum_pending
        self.maximum_terminal_history = maximum_pending * 4
        self._actions: dict[str, PendingAction] = {}
        self._lock = RLock()

    def create(
        self,
        *,
        owner_user_id: int,
        chat_id: int,
        action_type: PendingActionType,
        payload: Any,
        summary: str,
        ttl_seconds: float,
    ) -> PendingAction:
        with self._lock:
            self._expire_locked(monotonic())
            active = sum(
                item.state == PendingActionState.PENDING
                for item in self._actions.values()
            )
            if active >= self.maximum_pending:
                raise PendingActionError("pending_limit")
            nonce = secrets.token_urlsafe(9)
            now = monotonic()
            action = PendingAction(
                nonce,
                owner_user_id,
                chat_id,
                action_type,
                payload,
                summary,
                now,
                now + ttl_seconds,
            )
            self._actions[nonce] = action
            self._prune_locked()
            return action

    def claim(self, nonce: str, *, owner_user_id: int, chat_id: int) -> PendingAction:
        """Atomically move one valid nonce to processing before any await."""
        with self._lock:
            action = self._validated(nonce, owner_user_id, chat_id)
            processing = replace(action, state=PendingActionState.PROCESSING)
            self._actions[nonce] = processing
            return processing

    def cancel(self, nonce: str, *, owner_user_id: int, chat_id: int) -> PendingAction:
        with self._lock:
            action = self._validated(nonce, owner_user_id, chat_id)
            cancelled = replace(action, state=PendingActionState.CANCELLED)
            self._actions[nonce] = cancelled
            self._prune_locked()
            return cancelled

    def finish(self, nonce: str, state: PendingActionState) -> None:
        if state not in {PendingActionState.CONFIRMED, PendingActionState.FAILED}:
            raise ValueError("pending action can finish only confirmed or failed")
        with self._lock:
            action = self._actions.get(nonce)
            if action is not None and action.state == PendingActionState.PROCESSING:
                self._actions[nonce] = replace(action, state=state)
                self._prune_locked()

    def expire(self) -> int:
        with self._lock:
            return self._expire_locked(monotonic())

    def cancel_all(self) -> int:
        with self._lock:
            changed = 0
            for nonce, action in tuple(self._actions.items()):
                if action.state == PendingActionState.PENDING:
                    self._actions[nonce] = replace(
                        action, state=PendingActionState.CANCELLED
                    )
                    changed += 1
            self._prune_locked()
            return changed

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(
                item.state == PendingActionState.PENDING
                for item in self._actions.values()
            )

    def _validated(self, nonce: str, owner_user_id: int, chat_id: int) -> PendingAction:
        action = self._actions.get(nonce)
        if action is None:
            raise PendingActionError("unknown")
        if action.owner_user_id != owner_user_id:
            raise PendingActionError("owner_mismatch")
        if action.chat_id != chat_id:
            raise PendingActionError("chat_mismatch")
        if action.state != PendingActionState.PENDING:
            raise PendingActionError("replayed")
        if monotonic() >= action.expires_mono:
            self._actions[nonce] = replace(action, state=PendingActionState.EXPIRED)
            self._prune_locked()
            raise PendingActionError("expired")
        return action

    def _expire_locked(self, now: float) -> int:
        changed = 0
        for nonce, action in tuple(self._actions.items()):
            if (
                action.state == PendingActionState.PENDING
                and now >= action.expires_mono
            ):
                self._actions[nonce] = replace(action, state=PendingActionState.EXPIRED)
                changed += 1
        self._prune_locked()
        return changed

    def _prune_locked(self) -> None:
        terminal = sorted(
            (
                action
                for action in self._actions.values()
                if action.state
                not in {PendingActionState.PENDING, PendingActionState.PROCESSING}
            ),
            key=lambda action: action.created_mono,
        )
        remove_count = max(0, len(terminal) - self.maximum_terminal_history)
        for action in terminal[:remove_count]:
            self._actions.pop(action.nonce, None)
