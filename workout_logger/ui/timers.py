"""Live, cached timers shared by the workout menus and entry prompts."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import math
import sys
import time
from typing import Any, Callable, Iterator, Mapping, Protocol

from ..services.timing import session_elapsed_from_record, timestamp_elapsed


class SessionReader(Protocol):
    def get_session(self, session_id: int) -> Mapping[str, Any] | None: ...


def format_elapsed(seconds: float) -> str:
    """Render elapsed time, allowing workouts longer than 24 hours."""
    total = max(0, int(seconds)) if math.isfinite(seconds) else 0
    hours, remaining = divmod(total, 3600)
    minutes, seconds = divmod(remaining, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


# Retain the helper used by older callers.
_timestamp_elapsed = timestamp_elapsed


def _terminal_bell() -> None:
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


class SessionTimer:
    """Anchor persisted times once, then tick without querying SQLite.

    Pausing freezes both counters. Refresh only reanchors when timing state
    changes, so unrelated redraws and wall-clock adjustments do not cause jumps.
    """

    def __init__(
        self,
        service: SessionReader,
        session_id: int,
        *,
        now: Callable[[], datetime] = datetime.now,
        monotonic: Callable[[], float] = time.monotonic,
        notify: Callable[[], None] = _terminal_bell,
    ):
        self.service = service
        self.session_id = session_id
        self._now = now
        self._monotonic = monotonic
        self._notify = notify
        self._started_value: Any = object()
        self._rest_value: Any = object()
        self._timing_signature: Any = object()
        self._workout_seconds = 0.0
        self._rest_seconds: float | None = None
        self._workout_anchor = monotonic()
        self._rest_anchor = self._workout_anchor
        self._active = False
        self._paused = False
        self._target: int | None = None
        self._sound = False
        self._alerted = False
        self.refresh()

    def refresh(self) -> None:
        session = self.service.get_session(self.session_id)
        self._active = bool(session and session.get("status") == "in_progress")
        if not self._active:
            return
        now = self._now()
        tick = self._monotonic()
        self._paused = bool(session.get("paused_at"))
        try:
            target = int(session.get("rest_target_seconds") or 0)
            self._target = target if target > 0 else None
        except (TypeError, ValueError, OverflowError):
            self._target = None
        self._sound = str(session.get("rest_alert_sound")) in {"1", "True", "true"}
        started_value = session.get("started_at")
        signature = (started_value, session.get("paused_at"), session.get("paused_seconds", "0"))
        timing_changed = signature != self._timing_signature
        if timing_changed:
            self._timing_signature = signature
            self._started_value = started_value
            self._workout_seconds = session_elapsed_from_record(session, now)
            self._workout_anchor = tick
        rest_value = session.get("rest_started_at")
        rest_changed = rest_value != self._rest_value
        if rest_changed or timing_changed:
            self._rest_value = rest_value
            if hasattr(self.service, "rest_elapsed_seconds"):
                self._rest_seconds = self.service.rest_elapsed_seconds(self.session_id, now=now)
            else:
                end = session.get("paused_at")
                raw = timestamp_elapsed(rest_value, now) if rest_value else None
                paused = timestamp_elapsed(end, now) if end else 0.0
                self._rest_seconds = max(0.0, raw - (paused or 0.0)) if raw is not None else None
            self._rest_anchor = tick
        if rest_changed:
            self._alerted = bool(rest_value and session.get("rest_alerted_for") == rest_value)
        elif session.get("rest_alerted_for") == rest_value and rest_value:
            self._alerted = True

    def text(self) -> str:
        if not self._active:
            return ""
        tick = self._monotonic()
        workout_seconds = self._workout_seconds + (0.0 if self._paused else max(0.0, tick - self._workout_anchor))
        rest_seconds = (
            self._rest_seconds + (0.0 if self._paused else max(0.0, tick - self._rest_anchor))
            if self._rest_seconds is not None else None
        )
        workout = format_elapsed(workout_seconds)
        rest = format_elapsed(rest_seconds) if rest_seconds is not None else "--:--"
        result = f"Workout time: {workout}  •  Rest time: {rest}"
        if self._paused:
            result += "  •  PAUSED"
        if self._target and rest_seconds is not None:
            if rest_seconds >= self._target:
                result += "  •  Rest complete"
                if self._sound and not self._alerted and not self._paused:
                    self._alerted = True
                    marker = getattr(self.service, "mark_rest_reminder_delivered", None)
                    if marker is None or marker(self.session_id, self._rest_value):
                        self._notify()
            else:
                result += f"  •  Rest target: {format_elapsed(self._target)}"
        return result


_active_timer: ContextVar[SessionTimer | None] = ContextVar("active_workout_timer", default=None)


@contextmanager
def active_session_timer(
    service: SessionReader,
    session_id: int,
    *,
    now: Callable[[], datetime] = datetime.now,
    monotonic: Callable[[], float] = time.monotonic,
    notify: Callable[[], None] = _terminal_bell,
) -> Iterator[SessionTimer]:
    """Show timers only while an active workout is being entered or resumed."""
    timer = SessionTimer(service, session_id, now=now, monotonic=monotonic, notify=notify)
    token = _active_timer.set(timer)
    try:
        yield timer
    finally:
        _active_timer.reset(token)


def timer_text() -> str:
    """Callable text suitable for a prompt-toolkit Label or prompt message."""
    timer = _active_timer.get()
    return timer.text() if timer else ""


def refresh_active_timer(session_id: int | None = None) -> None:
    """Refresh cached timestamps after entries, pauses or reminder settings change."""
    timer = _active_timer.get()
    if timer and (session_id is None or session_id == timer.session_id):
        timer.refresh()
