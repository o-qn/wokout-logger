from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Any

SCHEMA_VERSION = 1


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
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self):
        with self.connect() as conn:
            conn.executescript(
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
