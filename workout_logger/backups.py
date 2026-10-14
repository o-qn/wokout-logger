"""Consistent SQLite snapshots and history-preserving recovery.

Recovery is intentionally additive: a snapshot can put missing records back,
but can never replace an existing result, correction, exercise, or setting.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .database import check_schema_version


class BackupError(ValueError):
    pass


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    created_at: str
    reason: str
    sessions: int = 0
    entries: int = 0
    valid: bool = True
    error: str = ""


@dataclass(frozen=True)
class RecoveryResult:
    sessions: int
    entries: int
    bodyweights: int
    exercises: int
    templates: int
    pre_restore_backup: Path


_REQUIRED = {
    "exercises": {"id", "name", "measurement_type", "default_unit"},
    "workout_templates": {"id", "label", "created_at"},
    "workout_template_exercises": {"id", "template_id", "exercise_id", "exercise_order"},
    "workout_sessions": {"id", "template_id", "session_date", "bodyweight", "started_at"},
    "workout_entries": {"id", "session_id", "exercise_id", "exercise_name_snapshot", "measurement_type", "exercise_order"},
    "bodyweight_entries": {"id", "session_id", "entry_date", "bodyweight"},
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # Table names are internal constants, never user input.
    return {r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise BackupError(f"Database file does not exist: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _validate(conn: sqlite3.Connection):
    for table, required in _REQUIRED.items():
        if not required <= _columns(conn, table):
            raise BackupError(f"This file is not a compatible workout backup (missing {table} fields).")
    checks = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    if checks != ["ok"]:
        raise BackupError("SQLite integrity check failed: " + "; ".join(checks[:3]))
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise BackupError("The backup contains broken record references.")


def _uuid(conn: sqlite3.Connection) -> str | None:
    if "app_settings" not in _tables(conn):
        return None
    row = conn.execute("SELECT value FROM app_settings WHERE key='database_uuid'").fetchone()
    return str(row[0]) if row and row[0] else None


def _legacy_source_key(conn: sqlite3.Connection) -> str:
    # Pre-UUID snapshots have no shared database identity. Namespace their
    # recovery mappings by contents so two unrelated snapshots cannot reuse
    # another snapshot's source IDs/timestamps. Original rows still deduplicate
    # by their natural identities when importing overlapping old snapshots.
    digest = hashlib.sha256()
    for statement in conn.iterdump():
        digest.update(statement.encode())
        digest.update(b"\0")
    return "legacy:" + digest.hexdigest()


class BackupManager:
    def __init__(self, db_or_path, backup_dir: Path | str | None = None):
        self.path = Path(getattr(db_or_path, "path", db_or_path)).expanduser()
        self.backup_dir = Path(backup_dir).expanduser() if backup_dir is not None else self.path.parent / "backups"

    def daily_backup(self) -> Path | None:
        """Capture existing data once each local day, before startup migrations."""
        if not self.path.exists():
            return None
        prefix = f"workouts-{datetime.now().date().isoformat()}--daily--"
        # Startup only needs today's daily snapshot. Validating the entire
        # retained archive makes every launch progressively slower.
        if self.backup_dir.is_dir():
            for path in self.backup_dir.glob(f"{prefix}*.db"):
                try:
                    snapshot = _readonly(path)
                    try:
                        _validate(snapshot)
                    finally:
                        snapshot.close()
                    return None
                except (BackupError, sqlite3.Error, OSError):
                    continue
        return self.create_backup("daily")

    def create_backup(self, reason: str = "manual") -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        reason = re.sub(r"[^a-z0-9_-]+", "-", str(reason).lower()).strip("-") or "manual"
        stamp = datetime.now().strftime("%Y-%m-%d--%H%M%S-%f")
        day, time = stamp.split("--")
        # mkstemp also protects against collisions when the clock is unchanged.
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=self.backup_dir)
        os.close(fd)
        temporary_path = Path(temporary)
        destination = self.backup_dir / f"workouts-{day}--{reason}--{time}-{temporary_path.stem[-8:]}.db"
        try:
            source = _readonly(self.path)
            try:
                _validate(source)
                target = sqlite3.connect(temporary_path)
                try:
                    source.backup(target)
                    target.commit()
                    target.row_factory = sqlite3.Row
                    _validate(target)
                finally:
                    target.close()
            finally:
                source.close()
            temporary_path.replace(destination)
            return destination
        except (sqlite3.Error, OSError) as exc:
            raise BackupError(f"Could not create a safe backup: {exc}") from exc
        finally:
            temporary_path.unlink(missing_ok=True)

    def list_backups(self) -> list[BackupInfo]:
        if not self.backup_dir.is_dir():
            return []
        items = []
        for path in self.backup_dir.glob("*.db"):
            try:
                created = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
                parts = path.stem.split("--")
                reason = parts[1] if len(parts) > 2 else "external"
                conn = _readonly(path)
                try:
                    _validate(conn)
                    sessions = conn.execute("SELECT COUNT(*) FROM workout_sessions").fetchone()[0]
                    entries = conn.execute("SELECT COUNT(*) FROM workout_entries").fetchone()[0]
                finally:
                    conn.close()
                items.append(BackupInfo(path, created, reason, sessions, entries))
            except (BackupError, sqlite3.Error, OSError) as exc:
                items.append(BackupInfo(path, "", "invalid", valid=False, error=str(exc)))
        return sorted(items, key=lambda item: (item.created_at, item.path.name), reverse=True)

    @staticmethod
    def recover_to_new_database(snapshot: Path | str, new_path: Path | str) -> Path:
        """Build a separate usable database without touching an unreadable one.

        The destination must be new, including its SQLite sidecar paths. A
        complete, validated copy is published atomically without replacement.
        Startup may then migrate the new database normally.
        """
        source_path = Path(snapshot).expanduser()
        destination = Path(new_path).expanduser()
        source = None
        temporary_path = None
        try:
            occupied = [destination, Path(str(destination) + "-wal"), Path(str(destination) + "-shm")]
            if any(os.path.lexists(path) for path in occupied):
                raise BackupError(f"Recovery destination already exists: {destination}. Choose a new path.")
            source = _readonly(source_path)
            _validate(source)
            check_schema_version(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".recovery-", suffix=".tmp", dir=destination.parent)
            os.close(fd)
            temporary_path = Path(temporary)
            target = sqlite3.connect(temporary_path)
            try:
                source.backup(target)
                target.commit()
                target.row_factory = sqlite3.Row
                _validate(target)
            finally:
                target.close()
            # A hard link is atomic and, unlike replace(), cannot overwrite a
            # file created after the destination check above.
            os.link(temporary_path, destination)
            return destination
        except (sqlite3.Error, OSError) as exc:
            raise BackupError(f"Could not create a separate recovery database: {exc}") from exc
        finally:
            if source is not None:
                source.close()
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def recover_backup(self, path: Path | str) -> RecoveryResult:
        source_path = Path(path).expanduser()
        if source_path.resolve() == self.path.resolve():
            raise BackupError("Choose a snapshot file, rather than the live database.")
        source = None
        live = None
        try:
            source = _readonly(source_path)
            source.execute("BEGIN")
            _validate(source)
            check_schema_version(source)
            if not self.path.is_file():
                raise BackupError(f"Live database does not exist: {self.path}")
            live = sqlite3.connect(self.path)
            live.row_factory = sqlite3.Row
            live.execute("PRAGMA foreign_keys=ON")
            _validate(live)
            check_schema_version(live)
            source_uuid, live_uuid = _uuid(source), _uuid(live)
            if source_uuid and live_uuid and source_uuid != live_uuid:
                raise BackupError("This snapshot belongs to a different workout database. Recovery was not applied.")
            live.execute("BEGIN IMMEDIATE")
            pre_restore = self.create_backup("before-recovery")
            result = self._merge(source, live, source_uuid or _legacy_source_key(source), pre_restore)
            if live.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupError("Recovery could not preserve all record references; no recovery changes were saved.")
            live.commit()
            return result
        except (sqlite3.Error, OSError) as exc:
            raise BackupError(f"Could not recover this snapshot: {exc}") from exc
        finally:
            if live is not None:
                live.rollback()
                live.close()
            if source is not None:
                source.close()

    @staticmethod
    def _merge(source, live, source_key: str, pre_restore: Path) -> RecoveryResult:
        live.execute("""
            CREATE TABLE IF NOT EXISTS backup_recovery_map (
                source_key TEXT NOT NULL, table_name TEXT NOT NULL,
                source_id INTEGER NOT NULL, signature TEXT NOT NULL,
                target_id INTEGER NOT NULL,
                PRIMARY KEY(source_key, table_name, source_id, signature)
            )
        """)
        source_tables, live_tables = _tables(source), _tables(live)
        # Imported rows can take IDs used by other rows in the snapshot. Match
        # identities against the original live state, never newly inserted rows.
        original_rows = {
            table: {r["id"]: dict(r) for r in live.execute(f'SELECT * FROM "{table}"')}
            for table in ("exercises", "workout_templates", "workout_sessions")
        }
        legacy_exercises, legacy_templates = set(), set()
        if source_key.startswith("legacy:"):
            # A shared immutable entry snapshot ties renamed exercises and
            # relettered templates to their old IDs more strongly than names
            # or second-resolution creation times alone can do.
            source_sessions = {row["id"]: dict(row) for row in source.execute("SELECT * FROM workout_sessions")}
            live_entries = {
                (row["session_id"], row["exercise_id"]): dict(row)
                for row in live.execute("SELECT * FROM workout_entries")
            }
            for entry in source.execute("SELECT * FROM workout_entries"):
                source_session = source_sessions[entry["session_id"]]
                live_session = original_rows["workout_sessions"].get(entry["session_id"])
                live_entry = live_entries.get((entry["session_id"], entry["exercise_id"]))
                if (
                    live_session and live_entry
                    and live_session["started_at"] == source_session["started_at"]
                    and live_entry["exercise_name_snapshot"] == entry["exercise_name_snapshot"]
                ):
                    legacy_exercises.add(entry["exercise_id"])
                    if live_session["template_id"] == source_session["template_id"]:
                        legacy_templates.add(source_session["template_id"])
        counts = {"workout_sessions": 0, "workout_entries": 0, "bodyweight_entries": 0, "exercises": 0, "workout_templates": 0}
        maps: dict[str, dict[int, int]] = {}

        def signature(table, row):
            if table == "workout_revisions":
                # Audit records are immutable. Different corrections can share
                # an ID and timestamp in separate legacy databases.
                identity = json.dumps(row, sort_keys=True, separators=(",", ":"))
                return hashlib.sha256(identity.encode()).hexdigest()
            if table == "workout_sessions":
                identity = row.get("started_at")
            elif table == "workout_pause_intervals":
                identity = row.get("paused_at")
            else:
                identity = row.get("created_at")
            # The original ID and creation time survive later corrections.
            identity = identity or str(row.get("exercise_id", ""))
            return hashlib.sha256(str(identity).encode()).hexdigest()

        def remembered(table, row):
            entry = live.execute(
                "SELECT target_id FROM backup_recovery_map WHERE source_key=? AND table_name=? AND source_id=? AND signature=?",
                (source_key, table, row["id"], signature(table, row)),
            ).fetchone()
            if entry and live.execute(f'SELECT 1 FROM "{table}" WHERE id=?', (entry[0],)).fetchone():
                return entry[0]
            if source_key.startswith("legacy:"):
                # Older app versions used one shared namespace for all legacy
                # imports. Adopt an old mapping only when the referenced row
                # still corroborates it, preserving recovery idempotence after
                # upgrading without trusting another snapshot's ID alone.
                previous = live.execute(
                    "SELECT target_id FROM backup_recovery_map WHERE source_key='legacy' AND table_name=? AND source_id=? AND signature=?",
                    (table, row["id"], signature(table, row)),
                ).fetchone()
                target = live.execute(f'SELECT * FROM "{table}" WHERE id=?', (previous[0],)).fetchone() if previous else None
                if target:
                    if table == "exercises" and target["name"] == row["name"]:
                        return target["id"]
                    if table == "workout_templates" and target["created_at"] == row["created_at"]:
                        recovered_label = rf"Recovered {re.escape(row['label'])} \({row['id']}(?:-\d+)?\)"
                        if target["label"] == row["label"] or (not target["active"] and re.fullmatch(recovered_label, target["label"])):
                            return target["id"]
                    if table == "workout_sessions" and target["started_at"] == row["started_at"] and target["template_id"] == maps.get("workout_templates", {}).get(row["template_id"]):
                        return target["id"]
                    if table == "workout_pause_intervals" and target["paused_at"] == row["paused_at"] and target["session_id"] == maps.get("workout_sessions", {}).get(row["session_id"]):
                        return target["id"]
            return None

        def remember(table, row, target_id):
            maps.setdefault(table, {})[row["id"]] = target_id
            live.execute(
                "INSERT INTO backup_recovery_map(source_key,table_name,source_id,signature,target_id) VALUES(?,?,?,?,?) "
                "ON CONFLICT(source_key,table_name,source_id,signature) DO UPDATE SET target_id=excluded.target_id",
                (source_key, table, row["id"], signature(table, row), target_id),
            )

        def insert(table, row, changes=None):
            values = {k: v for k, v in row.items() if k != "id" and k in _columns(live, table)}
            values.update(changes or {})
            names = list(values)
            quoted = ",".join('"' + name + '"' for name in names)
            query = f'INSERT INTO "{table}" ({quoted}) VALUES ({",".join("?" for _ in names)})'
            new_id = live.execute(query, [values[name] for name in names]).lastrowid
            counts[table] = counts.get(table, 0) + 1
            return new_id

        for raw in source.execute("SELECT * FROM exercises ORDER BY id"):
            row = dict(raw)
            target_id = remembered("exercises", row)
            if target_id is None:
                existing = original_rows["exercises"].get(row["id"])
                if existing and (
                    not source_key.startswith("legacy:") or existing["name"] == row["name"]
                    or row["id"] in legacy_exercises
                ):
                    target_id = existing["id"]
                else:
                    named = live.execute("SELECT id FROM exercises WHERE name=?", (row["name"],)).fetchone()
                    target_id = named[0] if named else insert("exercises", row, {"archived": 1})
            remember("exercises", row, target_id)

        new_templates = set()
        template_labels = {}
        for raw in source.execute("SELECT * FROM workout_templates ORDER BY id"):
            row = dict(raw)
            template_labels[row["id"]] = row["label"]
            target_id = remembered("workout_templates", row)
            if target_id is None:
                existing = original_rows["workout_templates"].get(row["id"])
                if existing and existing["created_at"] == row["created_at"] and (
                    not source_key.startswith("legacy:") or existing["label"] == row["label"]
                    or row["id"] in legacy_templates
                ):
                    target_id = existing["id"]
                else:
                    label = row["label"]
                    if live.execute("SELECT 1 FROM workout_templates WHERE label=?", (label,)).fetchone():
                        label = f"Recovered {label} ({row['id']})"
                        index = 2
                        while live.execute("SELECT 1 FROM workout_templates WHERE label=?", (label,)).fetchone():
                            label = f"Recovered {row['label']} ({row['id']}-{index})"
                            index += 1
                    target_id = insert("workout_templates", row, {"label": label, "active": 0})
                    new_templates.add(row["id"])
            remember("workout_templates", row, target_id)

        # Only recovered templates receive snapshot definitions. Live templates
        # keep the user's current exercise order and settings.
        for raw in source.execute("SELECT * FROM workout_template_exercises ORDER BY id"):
            row = dict(raw)
            if row["template_id"] in new_templates:
                insert("workout_template_exercises", row, {
                    "template_id": maps["workout_templates"][row["template_id"]],
                    "exercise_id": maps["exercises"][row["exercise_id"]],
                })

        for raw in source.execute("SELECT * FROM workout_sessions ORDER BY id"):
            row = dict(raw)
            target_id = remembered("workout_sessions", row)
            if target_id is None:
                existing = original_rows["workout_sessions"].get(row["id"])
                if existing and existing["started_at"] == row["started_at"] and (
                    existing["template_id"] == maps.get("workout_templates", {}).get(row["template_id"])
                ):
                    target_id = existing["id"]
                else:
                    changes = {"template_id": maps.get("workout_templates", {}).get(row["template_id"])}
                    if "template_label_snapshot" in _columns(live, "workout_sessions"):
                        changes["template_label_snapshot"] = row.get("template_label_snapshot") or template_labels.get(row["template_id"])
                    target_id = insert("workout_sessions", row, changes)
            remember("workout_sessions", row, target_id)

        for table in ("workout_session_exercises", "workout_entries", "bodyweight_entries"):
            if table not in source_tables or table not in live_tables:
                continue
            for raw in source.execute(f'SELECT * FROM "{table}" ORDER BY id'):
                row = dict(raw)
                changes = {"session_id": maps["workout_sessions"][row["session_id"]]}
                if "exercise_id" in row:
                    changes["exercise_id"] = maps["exercises"][row["exercise_id"]]
                    existing = live.execute(f'SELECT id FROM "{table}" WHERE session_id=? AND exercise_id=?', (changes["session_id"], changes["exercise_id"])).fetchone()
                else:
                    existing = live.execute(f'SELECT id FROM "{table}" WHERE session_id=?', (changes["session_id"],)).fetchone()
                target_id = existing[0] if existing else insert(table, row, changes)
                remember(table, row, target_id)

        if "workout_pause_intervals" in source_tables and "workout_pause_intervals" in live_tables:
            for raw in source.execute("SELECT * FROM workout_pause_intervals ORDER BY id"):
                row = dict(raw)
                if remembered("workout_pause_intervals", row) is not None:
                    continue
                session_id = maps["workout_sessions"][row["session_id"]]
                existing = live.execute(
                    "SELECT id FROM workout_pause_intervals WHERE session_id=? AND paused_at=?",
                    (session_id, row["paused_at"]),
                ).fetchone()
                if existing:
                    target_id = existing[0]
                else:
                    session = live.execute("SELECT paused_at FROM workout_sessions WHERE id=?", (session_id,)).fetchone()
                    # An older snapshot can contain an open pause which the
                    # live session has since resumed. Never reopen that pause.
                    if row["resumed_at"] is None and session["paused_at"] != row["paused_at"]:
                        continue
                    target_id = insert("workout_pause_intervals", row, {"session_id": session_id})
                remember("workout_pause_intervals", row, target_id)

        if "workout_revisions" in source_tables and "workout_revisions" in live_tables:
            matched_revisions = set()
            for raw in source.execute("SELECT * FROM workout_revisions ORDER BY id"):
                row = dict(raw)
                remembered_id = remembered("workout_revisions", row)
                if remembered_id is not None:
                    matched_revisions.add(remembered_id)
                    continue
                changes = {"session_id": maps["workout_sessions"][row["session_id"]], "entry_id": maps.get("workout_entries", {}).get(row.get("entry_id"))}
                # Avoid duplicating an audit row already present in the live DB.
                # Match each live record only once: repeated corrections in the
                # same second are still distinct history records.
                candidates = live.execute(
                    "SELECT id FROM workout_revisions WHERE session_id=? AND entry_id IS ? AND change_type=? AND before_json=? AND after_json=? AND reason=? AND created_at=? ORDER BY id",
                    (changes["session_id"], changes["entry_id"], row["change_type"], row["before_json"], row["after_json"], row.get("reason", ""), row["created_at"]),
                ).fetchall()
                existing = next((item for item in candidates if item[0] not in matched_revisions), None)
                target_id = existing[0] if existing else insert("workout_revisions", row, changes)
                matched_revisions.add(target_id)
                remember("workout_revisions", row, target_id)

        return RecoveryResult(counts["workout_sessions"], counts["workout_entries"], counts["bodyweight_entries"], counts["exercises"], counts["workout_templates"], pre_restore)
