from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Any

SCHEMA_VERSION = 3


class UnsupportedSchemaError(sqlite3.DatabaseError):
    """The database was written by a newer version of the application."""


def check_schema_version(conn: sqlite3.Connection):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'").fetchone():
        return
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if row is None:
        return
    try:
        version = int(row[0])
    except (TypeError, ValueError) as exc:
        raise sqlite3.DatabaseError("The workout database has an invalid schema version") from exc
    if version > SCHEMA_VERSION:
        raise UnsupportedSchemaError(
            f"This workout database needs a newer app (schema {version}; supported {SCHEMA_VERSION}). "
            "Update the app to open it."
        )


def _execute_schema(conn: sqlite3.Connection, script: str):
    # sqlite3.executescript() commits any pending transaction before running.
    # Execute complete statements individually so all additive migrations,
    # backfills, indexes and triggers commit or roll back together.
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        conn.execute(statement)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        with self.connect() as conn:
            # Check before changing journal mode or starting any migration.
            check_schema_version(conn)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            check_schema_version(conn)
            _execute_schema(conn,
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS exercises (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    measurement_type TEXT NOT NULL,
                    default_unit TEXT NOT NULL DEFAULT 'kg',
                    resistance_unit TEXT,
                    aliases TEXT NOT NULL DEFAULT '',
                    archived INTEGER NOT NULL DEFAULT 0,
                    display_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS workout_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS workout_template_exercises (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_id INTEGER NOT NULL REFERENCES workout_templates(id) ON DELETE CASCADE,
                    exercise_id INTEGER NOT NULL REFERENCES exercises(id),
                    exercise_order INTEGER NOT NULL,
                    measurement_type_snapshot TEXT NOT NULL,
                    unit_snapshot TEXT NOT NULL DEFAULT 'kg',
                    resistance_unit_snapshot TEXT,
                    default_external_load TEXT,
                    default_assistance TEXT,
                    default_duration_seconds TEXT,
                    default_rounds INTEGER,
                    default_rest_seconds TEXT,
                    default_resistance_value TEXT,
                    UNIQUE(template_id, exercise_order),
                    UNIQUE(template_id, exercise_id)
                );

                CREATE TABLE IF NOT EXISTS workout_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_id INTEGER REFERENCES workout_templates(id),
                    session_date TEXT NOT NULL,
                    bodyweight TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'in_progress',
                    created_new_template INTEGER NOT NULL DEFAULT 0,
                    planned_total INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_activity_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS workout_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES workout_sessions(id) ON DELETE CASCADE,
                    exercise_id INTEGER NOT NULL REFERENCES exercises(id),
                    exercise_name_snapshot TEXT NOT NULL,
                    measurement_type TEXT NOT NULL,
                    unit TEXT NOT NULL DEFAULT 'kg',
                    bodyweight TEXT,
                    external_load TEXT,
                    assistance TEXT,
                    reps INTEGER,
                    duration_seconds TEXT,
                    rounds INTEGER,
                    rest_seconds TEXT,
                    resistance_value TEXT,
                    resistance_unit TEXT,
                    custom_result TEXT,
                    rir_code TEXT,
                    failure INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT '',
                    exercise_order INTEGER NOT NULL,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(session_id, exercise_id)
                );

                CREATE TABLE IF NOT EXISTS bodyweight_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL UNIQUE REFERENCES workout_sessions(id) ON DELETE CASCADE,
                    entry_date TEXT NOT NULL,
                    bodyweight TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_entries_exercise ON workout_entries(exercise_id, session_id);
                CREATE INDEX IF NOT EXISTS idx_sessions_date ON workout_sessions(session_date, id);
                CREATE INDEX IF NOT EXISTS idx_sessions_status ON workout_sessions(status);
                """
            )
            # Additive migrations keep every existing session, entry and weigh-in.
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(workout_sessions)")}
            for name, declaration in (
                ("template_label_snapshot", "TEXT"),
                ("rest_started_at", "TEXT"),
                ("exercise_snapshot_initialized", "INTEGER NOT NULL DEFAULT 0"),
                ("comparison_mode", "TEXT NOT NULL DEFAULT 'previous'"),
                ("paused_at", "TEXT"),
                ("paused_seconds", "TEXT NOT NULL DEFAULT '0'"),
                ("workout_duration_seconds", "TEXT"),
                ("rest_target_seconds", "INTEGER"),
                ("rest_alert_sound", "INTEGER NOT NULL DEFAULT 0"),
                ("rest_alerted_for", "TEXT"),
                ("template_revision", "INTEGER NOT NULL DEFAULT 0"),
                ("template_config_revision", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE workout_sessions ADD COLUMN {name} {declaration}")
            template_columns = {r["name"] for r in conn.execute("PRAGMA table_info(workout_templates)")}
            if "revision" not in template_columns:
                conn.execute("ALTER TABLE workout_templates ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            if "config_revision" not in template_columns:
                conn.execute("ALTER TABLE workout_templates ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 0")
            entry_columns = {r["name"] for r in conn.execute("PRAGMA table_info(workout_entries)")}
            if "logged_at" not in entry_columns:
                conn.execute("ALTER TABLE workout_entries ADD COLUMN logged_at TEXT")
                conn.execute("UPDATE workout_entries SET logged_at=created_at WHERE skipped=0")
            if "rest_alerted" not in entry_columns:
                conn.execute("ALTER TABLE workout_entries ADD COLUMN rest_alerted INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workout_session_exercises (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES workout_sessions(id) ON DELETE CASCADE,
                    exercise_id INTEGER NOT NULL REFERENCES exercises(id),
                    exercise_name_snapshot TEXT NOT NULL,
                    exercise_order INTEGER NOT NULL,
                    measurement_type_snapshot TEXT NOT NULL,
                    unit_snapshot TEXT NOT NULL,
                    resistance_unit_snapshot TEXT,
                    default_external_load TEXT,
                    default_assistance TEXT,
                    default_duration_seconds TEXT,
                    default_rounds INTEGER,
                    default_rest_seconds TEXT,
                    default_resistance_value TEXT,
                    UNIQUE(session_id, exercise_id)
                )
                """
            )
            conn.execute(
                """
                UPDATE workout_sessions SET template_label_snapshot=(
                    SELECT label FROM workout_templates WHERE id=workout_sessions.template_id
                ) WHERE template_label_snapshot IS NULL
                """
            )
            # Freeze legacy sessions once. A logged row is the strongest source
            # for its historical name, units and measurement behavior.
            for session in conn.execute(
                "SELECT * FROM workout_sessions WHERE exercise_snapshot_initialized=0"
            ).fetchall():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO workout_session_exercises(
                        session_id, exercise_id, exercise_name_snapshot, exercise_order,
                        measurement_type_snapshot, unit_snapshot, resistance_unit_snapshot,
                        default_external_load, default_assistance, default_duration_seconds,
                        default_rounds, default_rest_seconds, default_resistance_value
                    )
                    SELECT ?, te.exercise_id, e.name, te.exercise_order,
                           te.measurement_type_snapshot, te.unit_snapshot, te.resistance_unit_snapshot,
                           te.default_external_load, te.default_assistance, te.default_duration_seconds,
                           te.default_rounds, te.default_rest_seconds, te.default_resistance_value
                    FROM workout_template_exercises te JOIN exercises e ON e.id=te.exercise_id
                    WHERE te.template_id=?
                    """,
                    (session["id"], session["template_id"]),
                )
                if session["created_new_template"] and session["status"] == "in_progress":
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO workout_session_exercises(
                            session_id, exercise_id, exercise_name_snapshot, exercise_order,
                            measurement_type_snapshot, unit_snapshot, resistance_unit_snapshot
                        ) SELECT ?, id, name, display_order, measurement_type, default_unit,
                                 resistance_unit FROM exercises WHERE archived=0
                        """,
                        (session["id"],),
                    )
                for entry in conn.execute(
                    "SELECT * FROM workout_entries WHERE session_id=?", (session["id"],)
                ).fetchall():
                    conn.execute(
                        """
                        INSERT INTO workout_session_exercises(
                            session_id, exercise_id, exercise_name_snapshot, exercise_order,
                            measurement_type_snapshot, unit_snapshot, resistance_unit_snapshot,
                            default_external_load, default_assistance, default_duration_seconds,
                            default_rounds, default_rest_seconds, default_resistance_value
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(session_id, exercise_id) DO UPDATE SET
                            exercise_name_snapshot=excluded.exercise_name_snapshot,
                            measurement_type_snapshot=excluded.measurement_type_snapshot,
                            unit_snapshot=excluded.unit_snapshot,
                            resistance_unit_snapshot=excluded.resistance_unit_snapshot
                        """,
                        (session["id"], entry["exercise_id"], entry["exercise_name_snapshot"],
                         entry["exercise_order"], entry["measurement_type"], entry["unit"],
                         entry["resistance_unit"], entry["external_load"], entry["assistance"],
                         entry["duration_seconds"], entry["rounds"], entry["rest_seconds"],
                         entry["resistance_value"]),
                    )
                conn.execute(
                    """
                    UPDATE workout_sessions SET exercise_snapshot_initialized=1,
                        rest_started_at=COALESCE(rest_started_at, (
                            SELECT created_at FROM workout_entries
                            WHERE session_id=? AND skipped=0 ORDER BY id DESC LIMIT 1
                        )) WHERE id=?
                    """,
                    (session["id"], session["id"]),
                )
            _execute_schema(conn,
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workout_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL REFERENCES workout_sessions(id),
                    entry_id INTEGER REFERENCES workout_entries(id),
                    change_type TEXT NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_revisions_session ON workout_revisions(session_id,id);
                CREATE TRIGGER IF NOT EXISTS immutable_revision_update BEFORE UPDATE ON workout_revisions
                BEGIN SELECT RAISE(ABORT,'Workout revision history is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_revision_delete BEFORE DELETE ON workout_revisions
                BEGIN SELECT RAISE(ABORT,'Workout revision history is immutable'); END;
                CREATE TABLE IF NOT EXISTS workout_pause_intervals (
                    id INTEGER PRIMARY KEY,
                    session_id INTEGER NOT NULL REFERENCES workout_sessions(id),
                    paused_at TEXT NOT NULL,
                    resumed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pauses_session ON workout_pause_intervals(session_id);
                """
            )
            conn.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('database_uuid',?)", (str(uuid.uuid4()),))
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def seed_exercises(self):
        seeds = [
            ("Supinated Single-Arm Extension", "external_weight", "kg", None),
            ("Single-Arm Preacher Curl", "external_weight", "kg", None),
            ("Super Wide-Grip Pull-Up", "bodyweight", "kg", None),
            ("RTO Push-Up", "bodyweight", "kg", None),
            ("Single-Arm Lateral Raise Robot", "external_weight", "kg", None),
            ("Kelso Shrug Isometric", "isometric", "sec", None),
            ("Neck Extension Isometric + Neck Flexion Isometric", "isometric", "sec", None),
            ("Single-Leg Hyperextension", "external_weight", "kg", None),
            ("Assisted Reverse Nordic", "assistance", "kg", None),
            ("Chest Expander Above", "resistance_units", "springs", "springs"),
            ("Explosive Sagittal-Plane Pull-Up", "amrap", "reps", None),
        ]
        with self.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM exercises").fetchone()[0]
            if count:
                return
            for idx, (name, mode, unit, r_unit) in enumerate(seeds, start=1):
                conn.execute(
                    "INSERT INTO exercises(name, measurement_type, default_unit, resistance_unit, display_order) VALUES(?,?,?,?,?)",
                    (name, mode, unit, r_unit, idx),
                )

    def fetchall(self, sql: str, params: Iterable[Any] = ()):
        with self.connect() as conn:
            return conn.execute(sql, tuple(params)).fetchall()

    def fetchone(self, sql: str, params: Iterable[Any] = ()):
        with self.connect() as conn:
            return conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.connect() as conn:
            cur = conn.execute(sql, tuple(params))
            return int(cur.lastrowid)

    def touch_session(self, session_id: int):
        with self.connect() as conn:
            conn.execute(
                "UPDATE workout_sessions SET last_activity_at=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), session_id),
            )
