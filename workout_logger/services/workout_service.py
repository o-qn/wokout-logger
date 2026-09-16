from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Optional, Iterable

from ..database import Database
from ..models import WorkoutEntryData


def _label_from_index(index: int) -> str:
    # 1 -> A, 26 -> Z, 27 -> AA
    result = ""
    n = index
    while n:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


class WorkoutService:
    def __init__(self, db: Database):
        self.db = db

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

    def add_exercise(self, name: str, measurement_type: str, default_unit: str = "kg", resistance_unit: str | None = None):
        row = self.db.fetchone("SELECT COALESCE(MAX(display_order),0)+1 AS n FROM exercises")
        return self.db.execute(
            "INSERT INTO exercises(name, measurement_type, default_unit, resistance_unit, display_order) VALUES(?,?,?,?,?)",
            (name.strip(), measurement_type, default_unit.strip() or "kg", resistance_unit, row["n"]),
        )

    def update_exercise(self, exercise_id: int, **changes):
        allowed = {"name", "measurement_type", "default_unit", "resistance_unit", "aliases", "display_order", "archived"}
        pairs = [(k, v) for k, v in changes.items() if k in allowed]
        if not pairs:
            return
        sql = ", ".join(f"{k}=?" for k, _ in pairs) + ", updated_at=CURRENT_TIMESTAMP"
        values = [int(v) if k == "archived" else v for k, v in pairs]
        values.append(exercise_id)
        self.db.execute(f"UPDATE exercises SET {sql} WHERE id=?", values)

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
        rows = self.db.fetchall("SELECT label FROM workout_templates")
        labels = {r["label"] for r in rows}
        idx = 1
        while _label_from_index(idx) in labels:
            idx += 1
        return _label_from_index(idx)

    def create_template(self) -> int:
        label = self.next_template_label()
        return self.db.execute("INSERT INTO workout_templates(label) VALUES(?)", (label,))

    def template_exercises(self, template_id: int):
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
        template_id = self.create_template()
        planned_total = len(self.active_exercises())
        session_id = self.db.execute(
            """
            INSERT INTO workout_sessions(template_id, session_date, bodyweight, created_new_template, planned_total)
            VALUES(?,?,?,?,?)
            """,
            (template_id, session_date, bodyweight, 1, planned_total),
        )
        self.db.execute(
            "INSERT INTO bodyweight_entries(session_id, entry_date, bodyweight) VALUES(?,?,?)",
            (session_id, session_date, bodyweight),
        )
        return session_id

    def start_existing_template_session(self, template_id: int, session_date: str, bodyweight: str) -> int:
        count = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM workout_template_exercises WHERE template_id=?",
            (template_id,),
        )["n"]
        session_id = self.db.execute(
            """
            INSERT INTO workout_sessions(template_id, session_date, bodyweight, created_new_template, planned_total)
            VALUES(?,?,?,?,?)
            """,
            (template_id, session_date, bodyweight, 0, count),
        )
        self.db.execute(
            "INSERT INTO bodyweight_entries(session_id, entry_date, bodyweight) VALUES(?,?,?)",
            (session_id, session_date, bodyweight),
        )
        return session_id

    def get_session(self, session_id: int):
        row = self.db.fetchone(
            """
            SELECT s.*, t.label AS template_label
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
            SELECT s.*, t.label AS template_label
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
            extra = " AND we.session_id<>?"
            params.append(exclude_session_id)
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
        session = self.get_session(session_id)
        if not session or session["status"] != "in_progress":
            raise ValueError("Session is not active")

        if session["created_new_template"]:
            order = self._next_entry_order(session_id)
        else:
            order = self._template_order_for_exercise(session["template_id"], data.exercise_id)
            if order is None:
                raise ValueError("Exercise is not part of this template")

        values = (
            session_id,
            data.exercise_id,
            data.exercise_name,
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
        )
        with self.db.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO workout_entries(
                    session_id, exercise_id, exercise_name_snapshot, measurement_type, unit, bodyweight,
                    external_load, assistance, reps, duration_seconds, rounds, rest_seconds,
                    resistance_value, resistance_unit, custom_result, rir_code, failure, notes,
                    exercise_order, skipped
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            entry_id = int(cur.lastrowid)
            if session["created_new_template"]:
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
                        data.resistance_unit, str(data.external_load) if data.external_load is not None else None,
                        str(data.assistance) if data.assistance is not None else None,
                        str(data.duration_seconds) if data.duration_seconds is not None else None,
                        data.rounds, str(data.rest_seconds) if data.rest_seconds is not None else None,
                        str(data.resistance_value) if data.resistance_value is not None else None,
                    ),
                )
            elif not data.skipped:
                self._update_template_defaults_conn(conn, session["template_id"], data)
            conn.execute(
                "UPDATE workout_sessions SET last_activity_at=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), session_id),
            )
        return entry_id

    def _update_template_defaults_conn(self, conn, template_id: int, data: WorkoutEntryData):
        conn.execute(
            """
            UPDATE workout_template_exercises SET
                default_external_load=?, default_assistance=?, default_duration_seconds=?, default_rounds=?,
                default_rest_seconds=?, default_resistance_value=?, resistance_unit_snapshot=?
            WHERE template_id=? AND exercise_id=?
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
            ),
        )

    def edit_entry(self, entry_id: int, data: WorkoutEntryData):
        old = self.get_entry(entry_id)
        if not old:
            raise ValueError("Entry not found")
        session = self.get_session(old["session_id"])
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE workout_entries SET
                    exercise_name_snapshot=?, measurement_type=?, unit=?, bodyweight=?, external_load=?,
                    assistance=?, reps=?, duration_seconds=?, rounds=?, rest_seconds=?, resistance_value=?,
                    resistance_unit=?, custom_result=?, rir_code=?, failure=?, notes=?, skipped=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    data.exercise_name, data.measurement_type, data.unit,
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
                    int(data.skipped),
                    entry_id,
                ),
            )
            if not data.skipped and session and session["template_id"]:
                self._update_template_defaults_conn(conn, session["template_id"], data)
            conn.execute(
                "UPDATE workout_sessions SET last_activity_at=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), old["session_id"]),
            )

    def undo_last_entry(self, session_id: int) -> Optional[dict]:
        session = self.get_session(session_id)
        row = self.db.fetchone(
            "SELECT * FROM workout_entries WHERE session_id=? ORDER BY exercise_order DESC, id DESC LIMIT 1",
            (session_id,),
        )
        if not row:
            return None
        entry = dict(row)
        with self.db.connect() as conn:
            conn.execute("DELETE FROM workout_entries WHERE id=?", (entry["id"],))
            if session and session["created_new_template"]:
                conn.execute(
                    "DELETE FROM workout_template_exercises WHERE template_id=? AND exercise_id=?",
                    (session["template_id"], entry["exercise_id"]),
                )
            conn.execute("UPDATE workout_sessions SET last_activity_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), session_id))
        return entry

    def remaining_for_session(self, session_id: int):
        session = self.get_session(session_id)
        done_ids = {r["exercise_id"] for r in self.session_entries(session_id)}
        if session["created_new_template"]:
            return [e for e in self.active_exercises() if e["id"] not in done_ids]
        template_rows = self.template_exercises(session["template_id"])
        return [r for r in template_rows if r["exercise_id"] not in done_ids]

    def finalize_session(self, session_id: int, force_incomplete: bool = False):
        session = self.get_session(session_id)
        if not session:
            return
        entries = self.session_entries(session_id)
        completed = sum(1 for e in entries if not e["skipped"])
        visited = len(entries)
        remaining = len(self.remaining_for_session(session_id))
        incomplete = force_incomplete or remaining > 0 or completed < session["planned_total"]
        status = "incomplete" if incomplete else "complete"
        now = datetime.now().isoformat(timespec="seconds")
        with self.db.connect() as conn:
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
                else:
                    conn.execute(
                        "UPDATE workout_templates SET last_used_at=?, active=1 WHERE id=?",
                        (now, session["template_id"]),
                    )
        return status

    def save_unfinished_as_incomplete(self, session_id: int):
        return self.finalize_session(session_id, force_incomplete=True)

    def discard_session(self, session_id: int):
        session = self.get_session(session_id)
        if not session:
            return
        with self.db.connect() as conn:
            conn.execute("DELETE FROM workout_sessions WHERE id=?", (session_id,))
            if session["created_new_template"] and session["template_id"]:
                uses = conn.execute(
                    "SELECT COUNT(*) FROM workout_sessions WHERE template_id=?",
                    (session["template_id"],),
                ).fetchone()[0]
                if uses == 0:
                    conn.execute("DELETE FROM workout_templates WHERE id=?", (session["template_id"],))

    # ---------- history ----------
    def workout_history(self, limit: int = 100):
        rows = self.db.fetchall(
            """
            SELECT s.*, t.label AS template_label,
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
            (limit,),
        )
        return [dict(r) for r in rows]

    def exercise_history(self, exercise_id: int, limit: int = 100):
        rows = self.db.fetchall(
            """
            SELECT we.*, ws.session_date, ws.bodyweight AS session_bodyweight, t.label AS template_label
            FROM workout_entries we
            JOIN workout_sessions ws ON ws.id=we.session_id
            LEFT JOIN workout_templates t ON t.id=ws.template_id
            WHERE we.exercise_id=? AND ws.status IN ('complete','incomplete')
            ORDER BY ws.session_date DESC, ws.id DESC
            LIMIT ?
            """,
            (exercise_id, limit),
        )
        return [dict(r) for r in rows]

    def bodyweight_history(self, limit: int = 100):
        rows = self.db.fetchall(
            """
            SELECT bw.*, s.status
            FROM bodyweight_entries bw
            JOIN workout_sessions s ON s.id=bw.session_id
            WHERE s.status IN ('complete','incomplete')
            ORDER BY bw.entry_date DESC, bw.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]
