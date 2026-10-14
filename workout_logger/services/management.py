from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ..models import WorkoutEntryData


_DECIMAL_FIELDS = (
    "external_load", "assistance", "duration_seconds", "rest_seconds", "resistance_value",
)
_DEFAULT_FIELDS = tuple(f"default_{field}" for field in _DECIMAL_FIELDS) + ("default_rounds",)


def _nonnegative(value, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{label} must be a finite, non-negative number") from None
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label} must be a finite, non-negative number")
    return result


def _whole(value, label: str, minimum: int = 0) -> int:
    result = _nonnegative(value, label)
    if isinstance(value, bool) or result != result.to_integral_value() or result < minimum:
        raise ValueError(f"{label} must be a whole number of at least {minimum}")
    return int(result)


def _historical_resistance_unit(entry):
    if entry["measurement_type"] != "resistance_units" or entry.get("resistance_unit"):
        return entry.get("resistance_unit")
    candidate = (entry.get("unit") or "").strip()
    return candidate if candidate.lower() not in {"", "kg", "lb", "lbs", "sec", "seconds", "reps"} else "units"


def _validated_entry(data: WorkoutEntryData, *, require_rir=False) -> WorkoutEntryData:
    """Validate writes before they can change entries, defaults, or timers."""
    changes = {}
    for field in (*_DECIMAL_FIELDS, "bodyweight"):
        value = getattr(data, field)
        if value is not None:
            changes[field] = _nonnegative(value, field.replace("_", " "))
    for field, minimum in (("reps", 0), ("rounds", 1)):
        value = getattr(data, field)
        if value is not None:
            changes[field] = _whole(value, field, minimum)
    if not isinstance(data.skipped, (bool, int)) or data.skipped not in (False, True, 0, 1):
        raise ValueError("Skipped must be true or false")
    rir = str(data.rir_code).strip().upper() if data.rir_code not in (None, "") else None
    if rir not in (None, "F"):
        if not rir.isascii() or not rir.isdigit():
            raise ValueError("RIR must be F or a non-negative whole number")
        rir = str(int(rir))
    changes.update(rir_code=rir, failure=rir == "F" if rir is not None else bool(data.failure))
    if not data.skipped:
        required = {
            "external_weight": ("external_load", "reps"), "bodyweight": ("reps",),
            "bodyweight_plus": ("external_load", "reps"), "assistance": ("assistance", "reps"),
            "isometric": ("duration_seconds", "rounds"),
            "resistance_units": ("resistance_value", "reps"), "amrap": ("reps",),
            "custom": ("custom_result",),
        }.get(data.measurement_type)
        if required is None or any(getattr(data, field) in (None, "") for field in required):
            raise ValueError("The entry needs all required performance values")
        if data.measurement_type == "custom" and not str(data.custom_result).strip():
            raise ValueError("The entry needs a non-blank result")
        if require_rir and rir is None:
            raise ValueError("Choose an RIR value for the corrected entry")
    return replace(data, **changes)


class ManagementMixin:
    """Future template edits and audited corrections of saved workouts."""

    def set_template_exercises(self, template_id: int, exercise_ids):
        exercise_ids = list(exercise_ids)
        if not exercise_ids:
            raise ValueError("A workout template needs at least one exercise")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in exercise_ids):
            raise ValueError("Exercise IDs must be whole numbers")
        if len(set(exercise_ids)) != len(exercise_ids):
            raise ValueError("Each exercise can appear only once in a template")
        with self.db.connect() as conn:
            template = conn.execute(
                "SELECT * FROM workout_templates WHERE id=? AND active=1", (template_id,),
            ).fetchone()
            if not template:
                raise ValueError("Workout template not found or removed")
            old_rows = [dict(row) for row in conn.execute(
                "SELECT * FROM workout_template_exercises WHERE template_id=? ORDER BY exercise_order", (template_id,),
            )]
            old_by_exercise = {row["exercise_id"]: row for row in old_rows}
            exercises = {}
            for exercise_id in exercise_ids:
                row = conn.execute("SELECT * FROM exercises WHERE id=?", (exercise_id,)).fetchone()
                if not row or (row["archived"] and exercise_id not in old_by_exercise):
                    raise ValueError("New template members must be existing, unarchived exercises")
                exercises[exercise_id] = dict(row)
            if exercise_ids == [row["exercise_id"] for row in old_rows]:
                return
            self._backup_before_change("Before editing workout template")
            # Only future-template rows change. Session snapshots and saved
            # performance rows keep their original membership and order.
            conn.execute("DELETE FROM workout_template_exercises WHERE template_id=?", (template_id,))
            for order, exercise_id in enumerate(exercise_ids, start=1):
                exercise = exercises[exercise_id]
                old = old_by_exercise.get(exercise_id, {})
                compatible = (
                    old.get("measurement_type_snapshot"), old.get("unit_snapshot"), old.get("resistance_unit_snapshot")
                ) == (exercise["measurement_type"], exercise["default_unit"], exercise["resistance_unit"])
                defaults = [old.get(field) if compatible else None for field in _DEFAULT_FIELDS]
                conn.execute(
                    """
                    INSERT INTO workout_template_exercises(
                        template_id, exercise_id, exercise_order, measurement_type_snapshot,
                        unit_snapshot, resistance_unit_snapshot, default_external_load,
                        default_assistance, default_duration_seconds, default_rest_seconds,
                        default_resistance_value, default_rounds
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (template_id, exercise_id, order, exercise["measurement_type"],
                     exercise["default_unit"], exercise["resistance_unit"], *defaults),
                )
            conn.execute("UPDATE workout_templates SET revision=revision+1 WHERE id=?", (template_id,))

    @staticmethod
    def _require_saved_session(conn, session_id):
        session = conn.execute("SELECT * FROM workout_sessions WHERE id=?", (session_id,)).fetchone()
        if not session or session["status"] not in {"complete", "incomplete"}:
            raise ValueError("Only saved complete or incomplete workouts can be corrected")
        return dict(session)

    @staticmethod
    def _append_revision(conn, session_id, entry_id, change_type, before, after, reason):
        return int(conn.execute(
            """
            INSERT INTO workout_revisions(session_id, entry_id, change_type, before_json,
                after_json, reason, created_at) VALUES(?,?,?,?,?,?,?)
            """,
            (session_id, entry_id, change_type, json.dumps(before, sort_keys=True),
             json.dumps(after, sort_keys=True), str(reason or "").strip(),
             datetime.now().astimezone().isoformat(timespec="seconds")),
        ).lastrowid)

    def correct_entry(self, entry_id: int, data: WorkoutEntryData, reason: str = ""):
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM workout_entries WHERE id=?", (entry_id,)).fetchone()
            if not row:
                raise ValueError("Entry not found")
            old = dict(row)
            session = self._require_saved_session(conn, old["session_id"])
            if data.exercise_id != old["exercise_id"]:
                raise ValueError("A correction must preserve the original exercise")
            if (data.measurement_type, data.unit, data.resistance_unit) != (
                old["measurement_type"], old["unit"], _historical_resistance_unit(old),
            ):
                raise ValueError("A correction must preserve the original measurement and units")
            data = _validated_entry(data, require_rir=True)
            if data.bodyweight is not None and _nonnegative(data.bodyweight, "Bodyweight") != _nonnegative(
                old["bodyweight"] if old["bodyweight"] is not None else session["bodyweight"], "Bodyweight",
            ):
                raise ValueError("Use the workout bodyweight correction to change bodyweight")
            changes = {}
            for field in _DECIMAL_FIELDS:
                value = getattr(data, field)
                changes[field] = str(_nonnegative(value, field.replace("_", " "))) if value is not None else None
            for field, minimum in (("reps", 0), ("rounds", 1)):
                value = getattr(data, field)
                changes[field] = _whole(value, field, minimum) if value is not None else None
            rir = str(data.rir_code).strip().upper() if data.rir_code not in (None, "") else None
            if rir not in (None, "F"):
                if not rir.isascii() or not rir.isdigit():
                    raise ValueError("RIR must be F or a non-negative whole number")
                rir = str(int(rir))
            if not isinstance(data.skipped, (bool, int)) or data.skipped not in (False, True, 0, 1):
                raise ValueError("Skipped must be true or false")
            changes.update(
                custom_result=str(data.custom_result) if data.custom_result is not None else None,
                rir_code=rir, failure=int(rir == "F"), notes=str(data.notes or ""), skipped=int(data.skipped),
            )
            if not data.skipped:
                required = {
                    "external_weight": ("external_load", "reps"), "bodyweight": ("reps",),
                    "bodyweight_plus": ("external_load", "reps"), "assistance": ("assistance", "reps"),
                    "isometric": ("duration_seconds", "rounds"),
                    "resistance_units": ("resistance_value", "reps"), "amrap": ("reps",),
                    "custom": ("custom_result",),
                }.get(old["measurement_type"])
                if required is None or any(changes[field] in (None, "") for field in required):
                    raise ValueError("The corrected entry needs all required performance values")
                if rir is None:
                    raise ValueError("Choose an RIR value for the corrected entry")
            if all(old[field] == value for field, value in changes.items()):
                return None
            self._backup_before_change("Before correcting saved workout entry")
            columns = ", ".join(f"{field}=?" for field in changes)
            conn.execute(
                f"UPDATE workout_entries SET {columns}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (*changes.values(), entry_id),
            )
            after = dict(conn.execute("SELECT * FROM workout_entries WHERE id=?", (entry_id,)).fetchone())
            return self._append_revision(conn, old["session_id"], entry_id, "entry", old, after, reason)

    @staticmethod
    def _session_correction_snapshot(conn, session_id):
        return {
            "session": dict(conn.execute("SELECT * FROM workout_sessions WHERE id=?", (session_id,)).fetchone()),
            "bodyweight_entries": [dict(row) for row in conn.execute(
                "SELECT * FROM bodyweight_entries WHERE session_id=? ORDER BY id", (session_id,),
            )],
            "entries": [dict(row) for row in conn.execute(
                "SELECT * FROM workout_entries WHERE session_id=? ORDER BY id", (session_id,),
            )],
        }

    def correct_session(self, session_id: int, session_date=None, bodyweight=None, reason: str = ""):
        with self.db.connect() as conn:
            old = self._require_saved_session(conn, session_id)
            new_date = old["session_date"]
            if session_date is not None:
                try:
                    new_date = date.fromisoformat(str(session_date)).isoformat()
                except ValueError:
                    raise ValueError("Use a valid workout date in YYYY-MM-DD format") from None
            new_bodyweight = old["bodyweight"] if bodyweight is None else str(_nonnegative(bodyweight, "Bodyweight"))
            if (new_date, new_bodyweight) == (old["session_date"], old["bodyweight"]):
                return None
            before = self._session_correction_snapshot(conn, session_id)
            self._backup_before_change("Before correcting saved workout date or bodyweight")
            conn.execute(
                "UPDATE workout_sessions SET session_date=?, bodyweight=? WHERE id=?",
                (new_date, new_bodyweight, session_id),
            )
            conn.execute(
                """INSERT INTO bodyweight_entries(session_id, entry_date, bodyweight) VALUES(?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET entry_date=excluded.entry_date, bodyweight=excluded.bodyweight""",
                (session_id, new_date, new_bodyweight),
            )
            if bodyweight is not None:
                conn.execute(
                    "UPDATE workout_entries SET bodyweight=?, updated_at=CURRENT_TIMESTAMP WHERE session_id=?",
                    (new_bodyweight, session_id),
                )
            after = self._session_correction_snapshot(conn, session_id)
            return self._append_revision(conn, session_id, None, "session", before, after, reason)

    def revisions(self, session_id: int):
        rows = self.db.fetchall(
            "SELECT * FROM workout_revisions WHERE session_id=? ORDER BY created_at DESC, id DESC", (session_id,),
        )
        revisions = []
        for source in rows:
            row = dict(source)
            row["before"] = json.loads(row["before_json"])
            row["after"] = json.loads(row["after_json"])
            revisions.append(row)
        return revisions
