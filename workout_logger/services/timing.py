"""Persisted workout timing and optional reminders.

Pause intervals are kept separately so rest time can exclude exactly the pauses
that overlap the latest set, including after an undo restores an earlier set.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Mapping


def timestamp(value: Any, now: datetime) -> datetime | None:
    try:
        raw = str(value).strip()
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            if isinstance(value, str) and " " in raw and "T" not in raw.upper():
                stamp = stamp.replace(tzinfo=timezone.utc)
            else:
                stamp = stamp.replace(tzinfo=now.tzinfo or datetime.now().astimezone().tzinfo)
        return stamp
    except (TypeError, ValueError, OverflowError):
        return None


def current_time(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now().astimezone()
    if isinstance(value, datetime):
        return value if value.tzinfo else value.astimezone()
    parsed = timestamp(value, datetime.now().astimezone())
    if parsed is None:
        raise ValueError("Invalid timing timestamp")
    return parsed


def timestamp_elapsed(value: Any, now: datetime) -> float | None:
    now = current_time(now)
    stamp = timestamp(value, now)
    # Python treats two datetimes with the same ZoneInfo as wall times;
    # normalize before subtraction so DST changes count real elapsed seconds.
    return max(0.0, (now.astimezone(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()) if stamp else None


def seconds_value(value: Any) -> float:
    try:
        result = float(value or 0)
        return max(0.0, result) if math.isfinite(result) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def session_elapsed_from_record(session: Mapping[str, Any], now: datetime) -> float:
    now = current_time(now)
    saved = session.get("workout_duration_seconds")
    if session.get("status") != "in_progress" and saved is not None:
        return seconds_value(saved)
    if session.get("status") != "in_progress":
        # Old workouts predate the saved-duration column. Their finish time is
        # still authoritative; viewing history must never keep them running.
        finished = timestamp(session.get("completed_at") or session.get("last_activity_at"), now)
        if finished is None:
            return 0.0
        now = finished.astimezone(now.tzinfo)
    total = timestamp_elapsed(session.get("started_at"), now) or 0.0
    pauses = seconds_value(session.get("paused_seconds"))
    if session.get("paused_at"):
        pauses += timestamp_elapsed(session["paused_at"], now) or 0.0
    return max(0.0, total - pauses)


def _seconds_text(value: float) -> str:
    return str(round(max(0.0, value), 3))


class TimingMixin:
    def _timing_now(self) -> datetime:
        return datetime.now().astimezone()

    @staticmethod
    def _active_timing_session(conn, session_id: int) -> dict:
        row = conn.execute("SELECT * FROM workout_sessions WHERE id=?", (session_id,)).fetchone()
        if not row or row["status"] != "in_progress":
            raise ValueError("Session is not active")
        return dict(row)

    def pause_session(self, session_id: int) -> bool:
        now = current_time(self._timing_now()).isoformat(timespec="microseconds")
        with self.db.connect() as conn:
            session = self._active_timing_session(conn, session_id)
            if session.get("paused_at"):
                return False
            conn.execute(
                "UPDATE workout_sessions SET paused_at=?, last_activity_at=? WHERE id=?",
                (now, now, session_id),
            )
            conn.execute(
                "INSERT INTO workout_pause_intervals(session_id, paused_at) VALUES(?,?)", (session_id, now),
            )
        return True

    def _resume_session_timing_conn(self, conn, session: Mapping[str, Any], now: datetime) -> float:
        paused = seconds_value(session.get("paused_seconds"))
        if session.get("paused_at"):
            paused += timestamp_elapsed(session["paused_at"], now) or 0.0
            conn.execute(
                "UPDATE workout_pause_intervals SET resumed_at=? WHERE session_id=? AND resumed_at IS NULL",
                (now.isoformat(timespec="microseconds"), session["id"]),
            )
            conn.execute(
                "UPDATE workout_sessions SET paused_at=NULL, paused_seconds=?, last_activity_at=? WHERE id=?",
                (_seconds_text(paused), now.isoformat(timespec="microseconds"), session["id"]),
            )
        return paused

    def resume_session_timer(self, session_id: int) -> bool:
        with self.db.connect() as conn:
            session = self._active_timing_session(conn, session_id)
            if not session.get("paused_at"):
                return False
            self._resume_session_timing_conn(conn, session, current_time(self._timing_now()))
        return True

    def session_elapsed_seconds(self, session_id: int, now: datetime | None = None) -> float:
        session = self.get_session(session_id)
        if not session:
            raise ValueError("Workout does not exist")
        return session_elapsed_from_record(session, current_time(now or self._timing_now()))

    def rest_elapsed_seconds(self, session_id: int, now: datetime | None = None) -> float | None:
        session = self.get_session(session_id)
        if not session:
            raise ValueError("Workout does not exist")
        moment = current_time(now or self._timing_now())
        rest_start = timestamp(session.get("rest_started_at"), moment)
        if rest_start is None:
            return None
        rest_start = rest_start.astimezone(timezone.utc)
        # A finished session's rest counter is only used for saved timing views.
        end = timestamp(session.get("completed_at"), moment) if session.get("status") != "in_progress" else moment
        end = (end or moment).astimezone(timezone.utc)
        elapsed = max(0.0, (end - rest_start).total_seconds())
        for interval in self.db.fetchall(
            "SELECT paused_at,resumed_at FROM workout_pause_intervals WHERE session_id=?", (session_id,),
        ):
            start = timestamp(interval["paused_at"], moment)
            finish = timestamp(interval["resumed_at"], moment) if interval["resumed_at"] else end
            if start is not None and finish is not None:
                start = start.astimezone(timezone.utc)
                finish = finish.astimezone(timezone.utc)
                elapsed -= max(0.0, (min(end, finish) - max(rest_start, start)).total_seconds())
        return max(0.0, elapsed)

    def _finish_session_timing_conn(self, conn, session_id: int, now: datetime | str | None = None) -> float:
        session = self._active_timing_session(conn, session_id)
        moment = current_time(now or self._timing_now())
        duration = session_elapsed_from_record(session, moment)
        self._resume_session_timing_conn(conn, session, moment)
        conn.execute(
            "UPDATE workout_sessions SET workout_duration_seconds=? WHERE id=?",
            (_seconds_text(duration), session_id),
        )
        return duration

    def finish_session_timing(self, session_id: int) -> float:
        with self.db.connect() as conn:
            return self._finish_session_timing_conn(conn, session_id)

    def get_rest_reminder_settings(self, session_id: int | None = None) -> dict:
        if session_id is not None:
            session = self.get_session(session_id)
            if not session:
                raise ValueError("Workout does not exist")
            raw_target = session.get("rest_target_seconds")
            sound = session.get("rest_alert_sound")
        else:
            values = {row["key"]: row["value"] for row in self.db.fetchall(
                "SELECT key,value FROM app_settings WHERE key IN ('rest_target_seconds','rest_alert_sound')"
            )}
            raw_target = values.get("rest_target_seconds")
            sound = values.get("rest_alert_sound")
        try:
            target = int(raw_target) if raw_target not in (None, "") else None
            if target is not None and target <= 0:
                target = None
        except (TypeError, ValueError, OverflowError):
            target = None
        return {"target_seconds": target, "sound": str(sound) in {"1", "True", "true"}}

    def configure_rest_reminder(
        self, session_id: int | None = None, target_seconds: int | None = None, sound: bool = False,
    ) -> dict:
        if target_seconds is not None and (isinstance(target_seconds, bool) or not isinstance(target_seconds, int) or target_seconds < 1):
            raise ValueError("Rest target must be a positive whole number of seconds")
        with self.db.connect() as conn:
            if session_id is None:
                for key, value in (
                    ("rest_target_seconds", str(target_seconds) if target_seconds is not None else ""),
                    ("rest_alert_sound", str(int(bool(sound)))),
                ):
                    conn.execute(
                        "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value),
                    )
            else:
                self._active_timing_session(conn, session_id)
                conn.execute(
                    "UPDATE workout_sessions SET rest_target_seconds=?,rest_alert_sound=? WHERE id=?",
                    (target_seconds, int(bool(sound)), session_id),
                )
        return {"target_seconds": target_seconds, "sound": bool(sound)}

    def mark_rest_reminder_delivered(self, session_id: int, rest_started_at: str) -> bool:
        """Claim this set's bell atomically so redraws/reopening cannot repeat it."""
        with self.db.connect() as conn:
            changed = conn.execute(
                "UPDATE workout_sessions SET rest_alerted_for=? "
                "WHERE id=? AND status='in_progress' AND paused_at IS NULL "
                "AND rest_started_at=? AND (rest_alerted_for IS NULL OR rest_alerted_for<>?)",
                (rest_started_at, session_id, rest_started_at, rest_started_at),
            ).rowcount
            if not changed:
                return False
            # Keep the claim on the logged set as well as the session. Undo can
            # then restore an older rest counter without repeating its bell.
            already_delivered = conn.execute(
                "SELECT 1 FROM workout_entries WHERE session_id=? AND skipped=0 "
                "AND COALESCE(logged_at,created_at)=? AND rest_alerted=1 LIMIT 1",
                (session_id, rest_started_at),
            ).fetchone()
            conn.execute(
                "UPDATE workout_entries SET rest_alerted=1 WHERE session_id=? AND skipped=0 "
                "AND COALESCE(logged_at,created_at)=?",
                (session_id, rest_started_at),
            )
        return already_delivered is None
