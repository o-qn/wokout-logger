from __future__ import annotations

from contextlib import nullcontext
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Iterable

from ..database import Database
from ..models import WorkoutEntryData
from ..measurements import measurement_unit
from ..backups import BackupManager, BackupError
from .management import ManagementMixin, _nonnegative, _validated_entry, _DEFAULT_FIELDS
from .timing import TimingMixin, current_time
from .comparison import ComparisonMixin


def _label_from_index(index: int) -> str:
    # 1 -> A, 26 -> Z, 27 -> AA
    result = ""
    n = index
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def _resistance_unit(mode: str, unit: str | None, resistance_unit: str | None):
    if mode != "resistance_units" or resistance_unit:
        return resistance_unit
    candidate = (unit or "").strip()
    return candidate if candidate.lower() not in {"", "kg", "lb", "lbs", "sec", "seconds", "reps"} else "units"


class WorkoutService(ManagementMixin, TimingMixin, ComparisonMixin):
    def __init__(self, db: Database):
        self.db = db
        self.backups = BackupManager(db)
        self.backup_warning = None
        with self.db.connect() as conn:
            for exercise in conn.execute(
                "SELECT * FROM exercises WHERE measurement_type='resistance_units' AND resistance_unit IS NULL"
            ).fetchall():
                conn.execute(
                    "UPDATE exercises SET resistance_unit=? WHERE id=?",
                    (_resistance_unit(exercise["measurement_type"], exercise["default_unit"], None), exercise["id"]),
                )
            self._relabel_templates_conn(conn)

    def _backup_before_change(self, reason: str):
        return self.backups.create_backup(reason)

    def get_setting(self, key: str, default=None):
        row = self.db.fetchone("SELECT value FROM app_settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value):
        self.db.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def _template_unchanged(self, conn, session, *, include_config=True):
        row = conn.execute("SELECT revision,config_revision FROM workout_templates WHERE id=? AND active=1", (session["template_id"],)).fetchone()
        return (row is not None and row["revision"] == session["template_revision"]
                and (not include_config or row["config_revision"] == session["template_config_revision"]))

    # ---------- exercises ----------
    def active_exercises(self):
        return [dict(r) for r in self.db.fetchall(
            "SELECT * FROM exercises WHERE archived=0 ORDER BY display_order, id"
        )]

    def all_exercises(self):
        return [dict(r) for r in self.db.fetchall(
            "SELECT * FROM exercises ORDER BY archived, display_order, id"
        )]

    def get_exercise(self, exercise_id: int):
        row = self.db.fetchone("SELECT * FROM exercises WHERE id=?", (exercise_id,))
        return dict(row) if row else None

    def add_exercise(self, name: str, measurement_type: str, default_unit: str | None = None, resistance_unit: str | None = None):
        default_unit = measurement_unit(measurement_type, requested=default_unit)
        row = self.db.fetchone("SELECT COALESCE(MAX(display_order),0)+1 AS n FROM exercises")
        return self.db.execute(
            "INSERT INTO exercises(name, measurement_type, default_unit, resistance_unit, display_order) VALUES(?,?,?,?,?)",
            (name.strip(), measurement_type, default_unit.strip() or "kg",
             _resistance_unit(measurement_type, default_unit, resistance_unit), row["n"]),
        )

    def update_exercise(self, exercise_id: int, **changes):
        allowed = {"name", "measurement_type", "default_unit", "resistance_unit", "aliases", "display_order", "archived"}
        exercise = self.get_exercise(exercise_id)
        if not exercise:
            raise ValueError("Exercise not found")
        mode = changes.get("measurement_type", exercise["measurement_type"])
        if "measurement_type" in changes or "default_unit" in changes:
            changes["default_unit"] = measurement_unit(
                mode, requested=changes.get("default_unit"), previous=exercise["default_unit"],
            )
        if "measurement_type" in changes and mode != exercise["measurement_type"]:
            # Resistance labels do not carry over into a different mode.
            changes.setdefault("resistance_unit", None)
        pairs = [(k, v) for k, v in changes.items() if k in allowed]
        if not pairs:
            return
        self._backup_before_change("exercise-settings")
        sql = ", ".join(f"{k}=?" for k, _ in pairs) + ", updated_at=CURRENT_TIMESTAMP"
        values = [int(v) if k == "archived" else v for k, v in pairs]
        values.append(exercise_id)
        with self.db.connect() as conn:
            conn.execute(f"UPDATE exercises SET {sql} WHERE id=?", values)
            exercise = conn.execute("SELECT * FROM exercises WHERE id=?", (exercise_id,)).fetchone()
            if exercise and exercise["measurement_type"] == "resistance_units" and not exercise["resistance_unit"]:
                conn.execute(
                    "UPDATE exercises SET resistance_unit=? WHERE id=?",
                    (_resistance_unit(exercise["measurement_type"], exercise["default_unit"], None), exercise_id),
                )
            self._sync_template_config_conn(conn, exercise_id=exercise_id)

    # ---------- templates ----------
    def templates(self):
        rows = self.db.fetchall(
            """
            SELECT t.*, COUNT(te.id) AS exercise_count
            FROM workout_templates t
            LEFT JOIN workout_template_exercises te ON te.template_id=t.id
            WHERE t.active=1
            GROUP BY t.id
            ORDER BY t.id
            """
        )
        return [dict(r) for r in rows]

    def next_template_label(self) -> str:
        row = self.db.fetchone("SELECT COUNT(*) AS n FROM workout_templates WHERE active=1")
        return _label_from_index(row["n"] + 1)

    def create_template(self) -> int:
        with self.db.connect() as conn:
            return self._create_template_conn(conn)

    def _create_template_conn(self, conn) -> int:
        self._relabel_templates_conn(conn)
        count = conn.execute("SELECT COUNT(*) FROM workout_templates WHERE active=1").fetchone()[0]
        return int(conn.execute(
            "INSERT INTO workout_templates(label) VALUES(?)", (_label_from_index(count + 1),)
        ).lastrowid)

    def _relabel_templates_conn(self, conn):
        # The unique label belongs to the current menu; history owns a separate
        # immutable label. Temporary labels prevent A/B swaps from colliding.
        rows = conn.execute("SELECT id, active FROM workout_templates ORDER BY id").fetchall()
        for row in rows:
            conn.execute(
                "UPDATE workout_templates SET label=? WHERE id=?",
                (f"__template_{row['id']}__", row["id"]),
            )
        active_index = 0
        for row in rows:
            if row["active"]:
                active_index += 1
                label = _label_from_index(active_index)
            else:
                label = f"__removed_{row['id']}__"
            conn.execute("UPDATE workout_templates SET label=? WHERE id=?", (label, row["id"]))

    def remove_template(self, template_id: int):
        self._backup_before_change("remove-template")
        with self.db.connect() as conn:
            row = conn.execute("SELECT id FROM workout_templates WHERE id=?", (template_id,)).fetchone()
            if not row:
                raise ValueError("Workout template not found")
            conn.execute("UPDATE workout_templates SET active=0 WHERE id=?", (template_id,))
            self._relabel_templates_conn(conn)

    def _sync_template_config_conn(self, conn, template_id=None, exercise_id=None):
        filters, params = [], []
        if template_id is not None:
            filters.append("te.template_id=?")
            params.append(template_id)
        if exercise_id is not None:
            filters.append("te.exercise_id=?")
            params.append(exercise_id)
        where = " AND ".join(filters) or "1=1"
        rows = conn.execute(
            f"""
            SELECT te.*, e.measurement_type, e.default_unit, e.resistance_unit
            FROM workout_template_exercises te JOIN exercises e ON e.id=te.exercise_id
            WHERE {where}
            """, params,
        ).fetchall()
        changed_templates = set()
        for row in rows:
            if (row["measurement_type_snapshot"], row["unit_snapshot"], row["resistance_unit_snapshot"]) == (
                row["measurement_type"], row["default_unit"], row["resistance_unit"]
            ):
                continue
            # Never reuse a numeric default under a different measurement or
            # unit. Entries and already-started sessions retain their snapshots.
            conn.execute(
                """
                UPDATE workout_template_exercises SET measurement_type_snapshot=?, unit_snapshot=?,
                    resistance_unit_snapshot=?, default_external_load=NULL, default_assistance=NULL,
                    default_duration_seconds=NULL, default_rounds=NULL, default_rest_seconds=NULL,
                    default_resistance_value=NULL WHERE id=?
                """,
                (row["measurement_type"], row["default_unit"], row["resistance_unit"], row["id"]),
            )
            changed_templates.add(row["template_id"])
        for changed_template_id in changed_templates:
            conn.execute("UPDATE workout_templates SET config_revision=config_revision+1 WHERE id=?", (changed_template_id,))

    def template_exercises(self, template_id: int):
        with self.db.connect() as conn:
            self._sync_template_config_conn(conn, template_id=template_id)
        rows = self.db.fetchall(
            """
            SELECT te.*, e.name AS current_name, e.archived
            FROM workout_template_exercises te
            JOIN exercises e ON e.id=te.exercise_id
            WHERE te.template_id=?
            ORDER BY te.exercise_order
            """,
            (template_id,),
        )
        return [dict(r) for r in rows]

    # ---------- sessions ----------
    def start_new_template_session(self, session_date: str, bodyweight: str) -> int:
        with self.db.connect() as conn:
            template_id = self._create_template_conn(conn)
            return self._start_session(template_id, session_date, bodyweight, created_new_template=True, conn=conn)

    def start_existing_template_session(self, template_id: int, session_date: str, bodyweight: str) -> int:
        return self._start_session(template_id, session_date, bodyweight, created_new_template=False)

    def _start_session(self, template_id: int, session_date: str, bodyweight: str, created_new_template: bool, conn=None):
        try:
            session_date = date.fromisoformat(str(session_date)).isoformat()
        except ValueError:
            raise ValueError("Use a valid workout date in YYYY-MM-DD format") from None
        bodyweight = str(_nonnegative(bodyweight, "Bodyweight"))
        now = current_time(self._timing_now()).isoformat(timespec="microseconds")
        with (nullcontext(conn) if conn is not None else self.db.connect()) as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            self._sync_template_config_conn(conn, template_id=template_id)
            template = conn.execute(
                "SELECT * FROM workout_templates WHERE id=? AND active=1", (template_id,)
            ).fetchone()
            if not template:
                raise ValueError("Workout template has been removed")
            if created_new_template:
                exercise_rows = conn.execute(
                    "SELECT * FROM exercises WHERE archived=0 ORDER BY display_order, id"
                ).fetchall()
            else:
                exercise_rows = conn.execute(
                    """
                    SELECT te.*, e.name FROM workout_template_exercises te
                    JOIN exercises e ON e.id=te.exercise_id
                    WHERE te.template_id=? ORDER BY te.exercise_order
                    """, (template_id,),
                ).fetchall()
            session_id = int(conn.execute(
                """
                INSERT INTO workout_sessions(
                    template_id, session_date, bodyweight, created_new_template, planned_total,
                    template_label_snapshot, exercise_snapshot_initialized, started_at, last_activity_at
                ) VALUES(?,?,?,?,?,?,1,?,?)
                """,
                (template_id, session_date, bodyweight, int(created_new_template), len(exercise_rows),
                 template["label"], now, now),
            ).lastrowid)
            reminder = self.get_rest_reminder_settings()
            conn.execute(
                "UPDATE workout_sessions SET comparison_mode=?, rest_target_seconds=?, rest_alert_sound=?, template_revision=?, template_config_revision=? WHERE id=?",
                (self.get_setting("comparison_mode", "previous"), reminder["target_seconds"], int(reminder["sound"]),
                 template["revision"], template["config_revision"], session_id),
            )
            for index, source in enumerate(exercise_rows, start=1):
                row = dict(source)
                if created_new_template:
                    exercise_id = row["id"]
                    mode, unit, resistance_unit = row["measurement_type"], row["default_unit"], row["resistance_unit"]
                    previous = conn.execute(
                        """
                        SELECT we.* FROM workout_entries we
                        JOIN workout_sessions ws ON ws.id=we.session_id
                        WHERE we.exercise_id=? AND we.skipped=0 AND ws.status<>'discarded'
                          AND (ws.session_date<? OR (ws.session_date=? AND ws.id<?))
                        ORDER BY ws.session_date DESC, ws.id DESC, we.id DESC LIMIT 1
                        """, (exercise_id, session_date, session_date, session_id),
                    ).fetchone()
                    if previous and (previous["measurement_type"], previous["unit"], previous["resistance_unit"]) == (
                        mode, unit, resistance_unit
                    ):
                        for field in ("external_load", "assistance", "duration_seconds", "rounds", "rest_seconds", "resistance_value"):
                            row[f"default_{field}"] = previous[field]
                else:
                    exercise_id = row["exercise_id"]
                    mode, unit, resistance_unit = row["measurement_type_snapshot"], row["unit_snapshot"], row["resistance_unit_snapshot"]
                conn.execute(
                    """
                    INSERT INTO workout_session_exercises(
                        session_id, exercise_id, exercise_name_snapshot, exercise_order,
                        measurement_type_snapshot, unit_snapshot, resistance_unit_snapshot,
                        default_external_load, default_assistance, default_duration_seconds,
                        default_rounds, default_rest_seconds, default_resistance_value
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (session_id, exercise_id, row["name"], index, mode, unit, resistance_unit,
                     row.get("default_external_load"), row.get("default_assistance"),
                     row.get("default_duration_seconds"), row.get("default_rounds"),
                     row.get("default_rest_seconds"), row.get("default_resistance_value")),
                )
            conn.execute(
                "INSERT INTO bodyweight_entries(session_id, entry_date, bodyweight) VALUES(?,?,?)",
                (session_id, session_date, bodyweight),
            )
        return session_id

    def session_exercises(self, session_id: int):
        rows = self.db.fetchall(
            "SELECT * FROM workout_session_exercises WHERE session_id=? ORDER BY exercise_order, id",
            (session_id,),
        )
        exercises = []
        for source in rows:
            row = dict(source)
            row["resistance_unit_snapshot"] = _resistance_unit(
                row["measurement_type_snapshot"], row["unit_snapshot"], row["resistance_unit_snapshot"]
            )
            row.update(
                id=row["exercise_id"], name=row["exercise_name_snapshot"],
                current_name=row["exercise_name_snapshot"], measurement_type=row["measurement_type_snapshot"],
                default_unit=row["unit_snapshot"], resistance_unit=row["resistance_unit_snapshot"],
            )
            exercises.append(row)
        return exercises

    def get_session_exercise(self, session_id: int, exercise_id: int):
        return next((e for e in self.session_exercises(session_id) if e["exercise_id"] == exercise_id), None)

    def get_session(self, session_id: int):
        row = self.db.fetchone(
            """
            SELECT s.*, COALESCE(s.template_label_snapshot, t.label) AS template_label,
                   CASE WHEN t.active=0 THEN 1 ELSE 0 END AS template_removed,
                   CASE WHEN t.active=0 THEN 'Removed Workout ' ELSE 'Workout ' END ||
                       COALESCE(s.template_label_snapshot, t.label, '?') AS workout_name
            FROM workout_sessions s
            LEFT JOIN workout_templates t ON t.id=s.template_id
            WHERE s.id=?
            """,
            (session_id,),
        )
        return dict(row) if row else None

    def unfinished_sessions(self):
        rows = self.db.fetchall(
            """
            SELECT s.*, COALESCE(s.template_label_snapshot, t.label) AS template_label,
                   CASE WHEN t.active=0 THEN 1 ELSE 0 END AS template_removed,
                   CASE WHEN t.active=0 THEN 'Removed Workout ' ELSE 'Workout ' END ||
                       COALESCE(s.template_label_snapshot, t.label, '?') AS workout_name
            FROM workout_sessions s
            LEFT JOIN workout_templates t ON t.id=s.template_id
            WHERE s.status='in_progress'
            ORDER BY s.id DESC
            """
        )
        return [dict(r) for r in rows]

    def session_entries(self, session_id: int):
        return [dict(r) for r in self.db.fetchall(
            "SELECT * FROM workout_entries WHERE session_id=? ORDER BY exercise_order, id",
            (session_id,),
        )]

    def get_entry(self, entry_id: int):
        row = self.db.fetchone("SELECT * FROM workout_entries WHERE id=?", (entry_id,))
        return dict(row) if row else None

    def latest_previous_entry(self, exercise_id: int, exclude_session_id: int | None = None):
        params: list[object] = [exercise_id]
        extra = ""
        if exclude_session_id is not None:
            reference = self.get_session(exclude_session_id)
            if not reference:
                return None
            extra = " AND (ws.session_date<? OR (ws.session_date=? AND ws.id<?))"
            params.extend([reference["session_date"], reference["session_date"], exclude_session_id])
        row = self.db.fetchone(
            f"""
            SELECT we.*, ws.session_date
            FROM workout_entries we
            JOIN workout_sessions ws ON ws.id=we.session_id
            WHERE we.exercise_id=? AND we.skipped=0 AND ws.status<>'discarded' {extra}
            ORDER BY ws.session_date DESC, ws.id DESC, we.id DESC
            LIMIT 1
            """,
            params,
        )
        return dict(row) if row else None

    def _next_entry_order(self, session_id: int) -> int:
        row = self.db.fetchone(
            "SELECT COALESCE(MAX(exercise_order),0)+1 AS n FROM workout_entries WHERE session_id=?",
            (session_id,),
        )
        return int(row["n"])

    def _template_order_for_exercise(self, template_id: int, exercise_id: int) -> Optional[int]:
        row = self.db.fetchone(
            "SELECT exercise_order FROM workout_template_exercises WHERE template_id=? AND exercise_id=?",
            (template_id, exercise_id),
        )
        return int(row["exercise_order"]) if row else None

    def save_entry(self, session_id: int, data: WorkoutEntryData) -> int:
        data = _validated_entry(data)
        session = self.get_session(session_id)
        if not session or session["status"] != "in_progress":
            raise ValueError("Session is not active")
        if session["paused_at"]:
            raise ValueError("Resume the workout before logging a set")
        frozen = self.get_session_exercise(session_id, data.exercise_id)
        if not frozen:
            raise ValueError("Exercise is not part of this session")
        if (data.measurement_type, data.unit, data.resistance_unit) != (
            frozen["measurement_type"], frozen["default_unit"], frozen["resistance_unit"]
        ):
            raise ValueError("Exercise settings differ from the settings at the start of this session")

        if session["created_new_template"]:
            order = self._next_entry_order(session_id)
        else:
            order = frozen["exercise_order"]

        now = self._timing_now().isoformat(timespec="microseconds")
        values = (
            session_id,
            data.exercise_id,
            frozen["name"],
            data.measurement_type,
            data.unit,
            str(data.bodyweight) if data.bodyweight is not None else session["bodyweight"],
            str(data.external_load) if data.external_load is not None else None,
            str(data.assistance) if data.assistance is not None else None,
            data.reps,
            str(data.duration_seconds) if data.duration_seconds is not None else None,
            data.rounds,
            str(data.rest_seconds) if data.rest_seconds is not None else None,
            str(data.resistance_value) if data.resistance_value is not None else None,
            data.resistance_unit,
            data.custom_result,
            data.rir_code.upper() if data.rir_code else None,
            int(data.failure),
            data.notes,
            order,
            int(data.skipped),
            now if not data.skipped else None,
        )
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._active_timing_session(conn, session_id)
            if current["paused_at"]:
                raise ValueError("Resume the workout before logging a set")
            cur = conn.execute(
                """
                INSERT INTO workout_entries(
                    session_id, exercise_id, exercise_name_snapshot, measurement_type, unit, bodyweight,
                    external_load, assistance, reps, duration_seconds, rounds, rest_seconds,
                    resistance_value, resistance_unit, custom_result, rir_code, failure, notes,
                    exercise_order, skipped, logged_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            entry_id = int(cur.lastrowid)
            if session["created_new_template"] and self._template_unchanged(conn, session, include_config=False):
                defaults = (
                    str(data.external_load) if data.external_load is not None else None,
                    str(data.assistance) if data.assistance is not None else None,
                    str(data.duration_seconds) if data.duration_seconds is not None else None,
                    data.rounds, str(data.rest_seconds) if data.rest_seconds is not None else None,
                    str(data.resistance_value) if data.resistance_value is not None else None,
                ) if self._template_unchanged(conn, session) else (None,) * 6
                conn.execute(
                    """
                    INSERT INTO workout_template_exercises(
                        template_id, exercise_id, exercise_order, measurement_type_snapshot, unit_snapshot,
                        resistance_unit_snapshot, default_external_load, default_assistance,
                        default_duration_seconds, default_rounds, default_rest_seconds, default_resistance_value
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        session["template_id"], data.exercise_id, order, data.measurement_type, data.unit,
                        data.resistance_unit, *defaults,
                    ),
                )
                self._sync_template_config_conn(conn, template_id=session["template_id"])
            elif not data.skipped and self._template_unchanged(conn, session):
                self._update_template_defaults_conn(conn, session["template_id"], data)
            conn.execute(
                """
                UPDATE workout_sessions SET last_activity_at=?,
                    rest_started_at=CASE WHEN ?=0 THEN ? ELSE rest_started_at END WHERE id=?
                """,
                (now, int(data.skipped), now, session_id),
            )
        return entry_id

    def _update_template_defaults_conn(self, conn, template_id: int, data: WorkoutEntryData):
        conn.execute(
            """
            UPDATE workout_template_exercises SET
                default_external_load=?, default_assistance=?, default_duration_seconds=?, default_rounds=?,
                default_rest_seconds=?, default_resistance_value=?, resistance_unit_snapshot=?
            WHERE template_id=? AND exercise_id=? AND measurement_type_snapshot=? AND unit_snapshot=?
                AND resistance_unit_snapshot IS ?
            """,
            (
                str(data.external_load) if data.external_load is not None else None,
                str(data.assistance) if data.assistance is not None else None,
                str(data.duration_seconds) if data.duration_seconds is not None else None,
                data.rounds,
                str(data.rest_seconds) if data.rest_seconds is not None else None,
                str(data.resistance_value) if data.resistance_value is not None else None,
                data.resistance_unit,
                template_id,
                data.exercise_id,
                data.measurement_type,
                data.unit,
                data.resistance_unit,
            ),
        )

    def edit_entry(self, entry_id: int, data: WorkoutEntryData):
        old = self.get_entry(entry_id)
        if not old:
            raise ValueError("Entry not found")
        if data.exercise_id != old["exercise_id"]:
            raise ValueError("Editing an entry must preserve its original exercise")
        if (data.measurement_type, data.unit, data.resistance_unit) != (
            old["measurement_type"], old["unit"],
            _resistance_unit(old["measurement_type"], old["unit"], old["resistance_unit"])
        ):
            raise ValueError("Editing an entry must preserve its original measurement and units")
        session = self.get_session(old["session_id"])
        if session["status"] in {"complete", "incomplete"}:
            return self.correct_entry(entry_id, data)
        if session["status"] != "in_progress" or session["paused_at"]:
            raise ValueError("Only an active, unpaused workout can be edited here")
        data = _validated_entry(data)
        now = self._timing_now().isoformat(timespec="microseconds")
        newly_logged = old["skipped"] and not data.skipped
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._active_timing_session(conn, old["session_id"])
            if current["paused_at"]:
                raise ValueError("Only an active, unpaused workout can be edited here")
            conn.execute(
                """
                UPDATE workout_entries SET
                    exercise_name_snapshot=?, measurement_type=?, unit=?, bodyweight=?, external_load=?,
                    assistance=?, reps=?, duration_seconds=?, rounds=?, rest_seconds=?, resistance_value=?,
                    resistance_unit=?, custom_result=?, rir_code=?, failure=?, notes=?, skipped=?,
                    logged_at=CASE WHEN ? THEN NULL WHEN ? THEN ? ELSE logged_at END,
                    rest_alerted=CASE WHEN ? OR ? THEN 0 ELSE rest_alerted END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    old["exercise_name_snapshot"], old["measurement_type"], old["unit"],
                    str(data.bodyweight) if data.bodyweight is not None else session["bodyweight"],
                    str(data.external_load) if data.external_load is not None else None,
                    str(data.assistance) if data.assistance is not None else None,
                    data.reps,
                    str(data.duration_seconds) if data.duration_seconds is not None else None,
                    data.rounds,
                    str(data.rest_seconds) if data.rest_seconds is not None else None,
                    str(data.resistance_value) if data.resistance_value is not None else None,
                    old["resistance_unit"],
                    data.custom_result,
                    data.rir_code.upper() if data.rir_code else None,
                    int(data.failure),
                    data.notes,
                    int(data.skipped),
                    int(data.skipped), int(newly_logged), now,
                    int(data.skipped), int(newly_logged),
                    entry_id,
                ),
            )
            if not data.skipped and session and session["template_id"] and session["status"] == "in_progress" and self._template_unchanged(conn, session):
                self._update_template_defaults_conn(conn, session["template_id"], data)
            elif data.skipped and not old["skipped"]:
                self._restore_template_defaults_conn(conn, session, old)
            conn.execute(
                """
                UPDATE workout_sessions SET last_activity_at=?,
                    rest_started_at=CASE WHEN ? THEN ? ELSE rest_started_at END WHERE id=?
                """,
                (now, int(newly_logged), now, old["session_id"]),
            )
            if data.skipped and not old["skipped"]:
                self._restore_rest_conn(conn, old["session_id"])

    def _restore_template_defaults_conn(self, conn, session, entry):
        """Undo only this entry's remembered defaults, preserving newer edits."""
        if session["created_new_template"] or entry["skipped"] or not self._template_unchanged(conn, session):
            return
        newer = conn.execute(
            "SELECT 1 FROM workout_entries e JOIN workout_sessions s ON s.id=e.session_id "
            "WHERE s.template_id=? AND s.status<>'discarded' AND e.exercise_id=? AND e.skipped=0 "
            "AND e.id>? AND e.measurement_type=? AND e.unit=? AND e.resistance_unit IS ? LIMIT 1",
            (session["template_id"], entry["exercise_id"], entry["id"], entry["measurement_type"],
             entry["unit"], entry["resistance_unit"]),
        ).fetchone()
        if newer:
            return
        frozen = conn.execute(
            "SELECT * FROM workout_session_exercises WHERE session_id=? AND exercise_id=?",
            (session["id"], entry["exercise_id"]),
        ).fetchone()
        if not frozen:
            return
        assignments = ", ".join(f"{field}=?" for field in _DEFAULT_FIELDS)
        matches = " AND ".join(f"{field} IS ?" for field in _DEFAULT_FIELDS)
        conn.execute(
            f"UPDATE workout_template_exercises SET {assignments} WHERE template_id=? AND exercise_id=? "
            "AND measurement_type_snapshot=? AND unit_snapshot=? AND resistance_unit_snapshot IS ? "
            f"AND {matches}",
            (*[frozen[field] for field in _DEFAULT_FIELDS], session["template_id"], entry["exercise_id"],
             entry["measurement_type"], entry["unit"], entry["resistance_unit"],
             *[entry[field.removeprefix("default_")] for field in _DEFAULT_FIELDS]),
        )

    @staticmethod
    def _restore_rest_conn(conn, session_id):
        remaining_set = conn.execute(
            "SELECT COALESCE(logged_at,created_at) AS logged_at,rest_alerted FROM workout_entries "
            "WHERE session_id=? AND skipped=0 ORDER BY julianday(COALESCE(logged_at,created_at)) DESC,id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        rest = remaining_set["logged_at"] if remaining_set else None
        alerted = rest if remaining_set and remaining_set["rest_alerted"] else None
        conn.execute(
            "UPDATE workout_sessions SET rest_started_at=?,rest_alerted_for=? WHERE id=?",
            (rest, alerted, session_id),
        )

    def undo_last_entry(self, session_id: int) -> Optional[dict]:
        session = self.get_session(session_id)
        if not session or session["status"] != "in_progress":
            raise ValueError("Only an active session can be undone")
        row = self.db.fetchone(
            "SELECT * FROM workout_entries WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        )
        if not row:
            return None
        entry = dict(row)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._active_timing_session(conn, session_id)
            self._restore_template_defaults_conn(conn, session, entry)
            conn.execute("DELETE FROM workout_entries WHERE id=?", (entry["id"],))
            if session and session["created_new_template"] and self._template_unchanged(conn, session, include_config=False):
                conn.execute(
                    "DELETE FROM workout_template_exercises WHERE template_id=? AND exercise_id=?",
                    (session["template_id"], entry["exercise_id"]),
                )
            self._restore_rest_conn(conn, session_id)
            conn.execute(
                "UPDATE workout_sessions SET last_activity_at=? WHERE id=?",
                (self._timing_now().isoformat(timespec="microseconds"), session_id),
            )
        return entry

    def remaining_for_session(self, session_id: int):
        session = self.get_session(session_id)
        if not session:
            return []
        done_ids = {r["exercise_id"] for r in self.session_entries(session_id)}
        return [e for e in self.session_exercises(session_id) if e["exercise_id"] not in done_ids]

    def finalize_session(self, session_id: int, force_incomplete: bool = False):
        session = self.get_session(session_id)
        if not session:
            return
        entries = self.session_entries(session_id)
        completed = sum(1 for e in entries if not e["skipped"])
        remaining = len(self.remaining_for_session(session_id))
        incomplete = force_incomplete or remaining > 0 or completed < session["planned_total"]
        status = "incomplete" if incomplete else "complete"
        moment = current_time(self._timing_now())
        now = moment.isoformat(timespec="microseconds")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._finish_session_timing_conn(conn, session_id, now=moment)
            conn.execute(
                "UPDATE workout_sessions SET status=?, completed_at=?, last_activity_at=? WHERE id=?",
                (status, now, now, session_id),
            )
            if session["template_id"]:
                # Hide empty templates; otherwise preserve partial/new templates.
                template_count = conn.execute(
                    "SELECT COUNT(*) FROM workout_template_exercises WHERE template_id=?",
                    (session["template_id"],),
                ).fetchone()[0]
                if template_count == 0:
                    conn.execute("UPDATE workout_templates SET active=0 WHERE id=?", (session["template_id"],))
                    self._relabel_templates_conn(conn)
                else:
                    conn.execute(
                        "UPDATE workout_templates SET last_used_at=? WHERE id=?",
                        (now, session["template_id"]),
                    )
        try:
            self.backups.create_backup("after-workout")
            self.backup_warning = None
        except (BackupError, OSError) as exc:
            # The workout has already committed; a failed copy must not turn a
            # successful save into a failed or duplicate workout.
            self.backup_warning = str(exc)
        return status

    def save_unfinished_as_incomplete(self, session_id: int):
        return self.finalize_session(session_id, force_incomplete=True)

    def discard_session(self, session_id: int):
        session = self.get_session(session_id)
        if not session:
            return
        if session["status"] != "in_progress":
            raise ValueError("Only an unfinished workout can be discarded")
        moment = current_time(self._timing_now())
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._finish_session_timing_conn(conn, session_id, now=moment)
            for entry in conn.execute("SELECT * FROM workout_entries WHERE session_id=?", (session_id,)).fetchall():
                self._restore_template_defaults_conn(conn, session, dict(entry))
            conn.execute(
                "UPDATE workout_sessions SET status='discarded', completed_at=?, last_activity_at=? WHERE id=?",
                (moment.isoformat(timespec="microseconds"), moment.isoformat(timespec="microseconds"), session_id),
            )
            if session["created_new_template"] and session["template_id"]:
                uses = conn.execute(
                    "SELECT COUNT(*) FROM workout_sessions WHERE template_id=? AND status<>'discarded'",
                    (session["template_id"],),
                ).fetchone()[0]
                if uses == 0:
                    conn.execute("UPDATE workout_templates SET active=0 WHERE id=?", (session["template_id"],))
                    self._relabel_templates_conn(conn)

    # ---------- history ----------
    def workout_history(self, limit: int | None = None):
        rows = self.db.fetchall(
            """
            SELECT s.*, COALESCE(s.template_label_snapshot, t.label) AS template_label,
                   CASE WHEN t.active=0 THEN 1 ELSE 0 END AS template_removed,
                   CASE WHEN t.active=0 THEN 'Removed Workout ' ELSE 'Workout ' END ||
                       COALESCE(s.template_label_snapshot, t.label, '?') AS workout_name,
                   SUM(CASE WHEN we.skipped=0 THEN 1 ELSE 0 END) AS completed_count,
                   COUNT(we.id) AS visited_count
            FROM workout_sessions s
            LEFT JOIN workout_templates t ON t.id=s.template_id
            LEFT JOIN workout_entries we ON we.session_id=s.id
            WHERE s.status IN ('complete','incomplete')
            GROUP BY s.id
            ORDER BY s.session_date DESC, s.id DESC
            LIMIT ?
            """,
            (-1 if limit is None else limit,),
        )
        return [dict(r) for r in rows]

    def exercise_history(self, exercise_id: int, limit: int | None = None):
        rows = self.db.fetchall(
            """
            SELECT we.*, ws.session_date, ws.bodyweight AS session_bodyweight,
                   COALESCE(ws.template_label_snapshot, t.label) AS template_label,
                   CASE WHEN t.active=0 THEN 1 ELSE 0 END AS template_removed,
                   CASE WHEN t.active=0 THEN 'Removed Workout ' ELSE 'Workout ' END ||
                       COALESCE(ws.template_label_snapshot, t.label, '?') AS workout_name
            FROM workout_entries we
            JOIN workout_sessions ws ON ws.id=we.session_id
            LEFT JOIN workout_templates t ON t.id=ws.template_id
            WHERE we.exercise_id=? AND ws.status IN ('complete','incomplete')
            ORDER BY ws.session_date DESC, ws.id DESC
            LIMIT ?
            """,
            (exercise_id, -1 if limit is None else limit),
        )
        return [dict(r) for r in rows]

    def bodyweight_history(self, limit: int | None = None):
        rows = self.db.fetchall(
            """
            SELECT bw.*, s.status
            FROM bodyweight_entries bw
            JOIN workout_sessions s ON s.id=bw.session_id
            WHERE s.status IN ('complete','incomplete')
            ORDER BY bw.entry_date DESC, bw.id DESC
            LIMIT ?
            """,
            (-1 if limit is None else limit,),
        )
        return [dict(r) for r in rows]
