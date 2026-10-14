import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from workout_logger.backups import BackupError, BackupManager
from workout_logger.database import Database
from workout_logger.services.workout_service import WorkoutService


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "workouts.db")
        self.exercise = self.db.execute("INSERT INTO exercises(name,measurement_type) VALUES('Curl','external_weight')")
        self.template = self.db.execute("INSERT INTO workout_templates(label) VALUES('A')")
        self.manager = BackupManager(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def session(self, day="2026-10-01", started="2026-10-01T09:00:00", reps=6):
        sid = self.db.execute(
            "INSERT INTO workout_sessions(template_id,session_date,bodyweight,status,started_at) VALUES(?,?,?,'complete',?)",
            (self.template, day, "80", started),
        )
        self.db.execute(
            "INSERT INTO workout_entries(session_id,exercise_id,exercise_name_snapshot,measurement_type,reps,external_load,exercise_order) VALUES(?,?,'Curl','external_weight',?,'10',1)",
            (sid, self.exercise, reps),
        )
        self.db.execute("INSERT INTO bodyweight_entries(session_id,entry_date,bodyweight) VALUES(?,?,'80')", (sid, day))
        return sid

    def test_daily_once_and_unique_manual_paths(self):
        self.session()
        first = self.manager.daily_backup()
        self.assertTrue(first.is_file())
        self.assertIsNone(self.manager.daily_backup())
        second = self.manager.create_backup()
        third = self.manager.create_backup()
        self.assertNotEqual(second, third)
        infos = self.manager.list_backups()
        self.assertEqual(len(infos), 3)
        self.assertTrue(all(i.valid and i.sessions == 1 and i.entries == 1 for i in infos))

    def test_missing_and_invalid_files_are_safe(self):
        missing = BackupManager(Path(self.temp.name) / "missing.db")
        self.assertIsNone(missing.daily_backup())
        with self.assertRaises(BackupError):
            missing.create_backup()
        self.assertFalse(missing.path.exists())
        with self.assertRaises(BackupError):
            self.manager.recover_backup(Path(self.temp.name) / "unknown.db")
        self.manager.backup_dir.mkdir(exist_ok=True)
        bad = self.manager.backup_dir / "invalid.db"
        bad.write_text("not sqlite")
        self.assertFalse(self.manager.list_backups()[0].valid)
        with self.assertRaises(BackupError):
            self.manager.recover_backup(bad)
        with self.assertRaises(BackupError):
            self.manager.recover_backup(self.db.path)

    def test_recovery_restores_missing_history_keeps_newer_and_corrected(self):
        missing = self.session()
        corrected = self.session("2026-10-02", "2026-10-02T09:00:00", reps=7)
        snapshot = self.manager.create_backup()
        before_source = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM workout_sessions WHERE id=?", (missing,))
            conn.execute("UPDATE workout_entries SET reps=9 WHERE session_id=?", (corrected,))
        newer = self.session("2026-10-03", "2026-10-03T09:00:00", reps=10)
        result = self.manager.recover_backup(snapshot)
        self.assertEqual((result.sessions, result.entries, result.bodyweights), (1, 1, 1))
        self.assertTrue(result.pre_restore_backup.exists())
        self.assertEqual(self.db.fetchone("SELECT COUNT(*) FROM workout_sessions")[0], 3)
        self.assertEqual(self.db.fetchone("SELECT reps FROM workout_entries WHERE session_id=?", (corrected,))[0], 9)
        self.assertEqual(self.db.fetchone("SELECT reps FROM workout_entries WHERE session_id=?", (newer,))[0], 10)
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), before_source)
        repeated = self.manager.recover_backup(snapshot)
        self.assertEqual((repeated.sessions, repeated.entries, repeated.bodyweights), (0, 0, 0))

    def test_missing_result_recovers_without_overwriting_existing_session(self):
        sid = self.session()
        snapshot = self.manager.create_backup()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM workout_entries WHERE session_id=?", (sid,))
            conn.execute("UPDATE workout_sessions SET bodyweight='81',session_date='2026-10-04' WHERE id=?", (sid,))
        result = self.manager.recover_backup(snapshot)
        self.assertEqual((result.sessions, result.entries), (0, 1))
        row = self.db.fetchone("SELECT * FROM workout_sessions WHERE id=?", (sid,))
        self.assertEqual((row["bodyweight"], row["session_date"]), ("81", "2026-10-04"))

    def test_id_collisions_import_both_snapshot_sessions_and_are_idempotent(self):
        self.session(started="2026-10-01T09:00:00", reps=5)
        self.session("2026-10-02", "2026-10-01T09:00:00", reps=6)
        snapshot = self.manager.create_backup()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM workout_sessions")
            conn.execute("DELETE FROM sqlite_sequence WHERE name='workout_sessions'")
        live = self.session("2026-10-03", "2026-10-03T09:00:00", reps=9)
        result = self.manager.recover_backup(snapshot)
        self.assertEqual((result.sessions, result.entries), (2, 2))
        self.assertEqual(self.db.fetchone("SELECT reps FROM workout_entries WHERE session_id=?", (live,))[0], 9)
        self.assertEqual(self.db.fetchone("SELECT COUNT(*) FROM workout_sessions")[0], 3)
        self.assertEqual(self.manager.recover_backup(snapshot).sessions, 0)

    def test_foreign_database_and_broken_references_are_rejected(self):
        self.session()
        snapshot = self.manager.create_backup()
        with closing(sqlite3.connect(snapshot)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT OR REPLACE INTO app_settings VALUES('database_uuid','different-database')")
        with self.db.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            conn.execute("INSERT OR REPLACE INTO app_settings VALUES('database_uuid','live-database')")
        with self.assertRaisesRegex(BackupError, "different workout database"):
            self.manager.recover_backup(snapshot)
        bad_fk = self.manager.create_backup()
        with closing(sqlite3.connect(bad_fk)) as conn, conn:
            conn.execute("UPDATE workout_entries SET session_id=9999")
        with self.assertRaisesRegex(BackupError, "broken record references"):
            self.manager.recover_backup(bad_fk)

    def test_pre_recovery_snapshot_has_unmodified_live_data(self):
        sid = self.session()
        snapshot = self.manager.create_backup()
        self.db.execute("UPDATE workout_entries SET reps=12 WHERE session_id=?", (sid,))
        result = self.manager.recover_backup(snapshot)
        with closing(sqlite3.connect(result.pre_restore_backup)) as before:
            self.assertEqual(before.execute("SELECT reps FROM workout_entries").fetchone()[0], 12)

    def test_snapshot_without_database_uuid_recovers_and_keeps_live_identity(self):
        sid = self.session()
        snapshot = self.manager.create_backup()
        live_uuid = self.db.fetchone("SELECT value FROM app_settings WHERE key='database_uuid'")[0]
        with closing(sqlite3.connect(snapshot)) as conn, conn:
            conn.execute("DELETE FROM app_settings WHERE key='database_uuid'")
        with self.db.connect() as conn:
            conn.execute("DELETE FROM workout_sessions WHERE id=?", (sid,))
        result = self.manager.recover_backup(snapshot)
        self.assertEqual(result.sessions, 1)
        self.assertEqual(self.db.fetchone("SELECT value FROM app_settings WHERE key='database_uuid'")[0], live_uuid)

    def test_revision_history_is_recovered_once(self):
        sid = self.session()
        entry = self.db.fetchone("SELECT id FROM workout_entries WHERE session_id=?", (sid,))[0]
        # A snapshot has an audit record which is absent from live storage.
        snapshot = self.manager.create_backup()
        with closing(sqlite3.connect(snapshot)) as conn, conn:
            conn.execute("INSERT INTO workout_revisions(session_id,entry_id,change_type,before_json,after_json) VALUES(?,?,'entry','{\"reps\":5}','{\"reps\":6}')", (sid, entry))
        self.manager.recover_backup(snapshot)
        self.manager.recover_backup(snapshot)
        self.assertEqual(self.db.fetchone("SELECT COUNT(*) FROM workout_revisions")[0], 1)

    def test_paused_workout_recovery_preserves_timer_and_remaps_intervals(self):
        sid = self.session(started="2026-10-01T09:00:00+00:00")
        self.db.execute(
            "UPDATE workout_sessions SET status='in_progress',paused_at='2026-10-01T09:05:00+00:00',paused_seconds='60',rest_started_at='2026-10-01T09:03:00+00:00' WHERE id=?",
            (sid,),
        )
        self.db.execute(
            "INSERT INTO workout_pause_intervals(session_id,paused_at,resumed_at) VALUES(?,'2026-10-01T09:01:00+00:00','2026-10-01T09:02:00+00:00')", (sid,),
        )
        self.db.execute(
            "INSERT INTO workout_pause_intervals(session_id,paused_at) VALUES(?,'2026-10-01T09:05:00+00:00')", (sid,),
        )
        snapshot = self.manager.create_backup()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM workout_pause_intervals WHERE session_id=?", (sid,))
            conn.execute("DELETE FROM workout_sessions WHERE id=?", (sid,))
        self.manager.recover_backup(snapshot)
        restored = self.db.fetchone("SELECT id FROM workout_sessions")[0]
        self.assertNotEqual(restored, sid)
        now = datetime(2026, 10, 2, 9, tzinfo=timezone.utc)
        service = WorkoutService(self.db)
        self.assertEqual(service.session_elapsed_seconds(restored, now), 240)
        self.assertEqual(service.rest_elapsed_seconds(restored, now), 120)
        self.assertEqual(self.db.fetchone("SELECT COUNT(*) FROM workout_pause_intervals WHERE session_id=?", (restored,))[0], 2)
        self.manager.recover_backup(snapshot)
        self.assertEqual(self.db.fetchone("SELECT COUNT(*) FROM workout_pause_intervals")[0], 2)

    def test_old_snapshot_cannot_reopen_current_resumed_pause(self):
        sid = self.session(started="2026-10-01T09:00:00+00:00")
        self.db.execute("UPDATE workout_sessions SET status='in_progress',paused_at='2026-10-01T09:05:00+00:00' WHERE id=?", (sid,))
        self.db.execute("INSERT INTO workout_pause_intervals(session_id,paused_at) VALUES(?,'2026-10-01T09:05:00+00:00')", (sid,))
        snapshot = self.manager.create_backup()
        self.db.execute("UPDATE workout_sessions SET paused_at=NULL,paused_seconds='60' WHERE id=?", (sid,))
        self.db.execute("UPDATE workout_pause_intervals SET resumed_at='2026-10-01T09:06:00+00:00' WHERE session_id=?", (sid,))
        self.manager.recover_backup(snapshot)
        self.assertEqual(self.db.fetchone("SELECT resumed_at FROM workout_pause_intervals WHERE session_id=?", (sid,))[0], "2026-10-01T09:06:00+00:00")
        # Even if the live interval is missing, a stale open pause is not added.
        self.db.execute("DELETE FROM workout_pause_intervals WHERE session_id=?", (sid,))
        self.manager.recover_backup(snapshot)
        self.assertEqual(self.db.fetchone("SELECT COUNT(*) FROM workout_pause_intervals")[0], 0)

    def test_separate_recovery_preserves_snapshot_and_unreadable_original(self):
        self.session()
        snapshot = self.manager.create_backup()
        snapshot_digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        unreadable = Path(self.temp.name) / "unreadable.db"
        unreadable.write_bytes(b"broken original bytes remain available")
        wal = Path(str(unreadable) + "-wal")
        wal.write_bytes(b"original wal")
        target = Path(self.temp.name) / "recovered" / "workouts.db"
        result = BackupManager.recover_to_new_database(snapshot, target)
        self.assertEqual(result, target)
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workout_sessions").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(unreadable.read_bytes(), b"broken original bytes remain available")
        self.assertEqual(wal.read_bytes(), b"original wal")
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(), snapshot_digest)
        with self.assertRaisesRegex(BackupError, "already exists"):
            BackupManager.recover_to_new_database(snapshot, target)

    def test_separate_recovery_rejects_existing_destination_and_sidecars(self):
        self.session()
        snapshot = self.manager.create_backup()
        with self.assertRaisesRegex(BackupError, "already exists"):
            self.manager.recover_to_new_database(snapshot, self.db.path)
        target = Path(self.temp.name) / "new.db"
        sidecar = Path(str(target) + "-wal")
        sidecar.write_bytes(b"keep this")
        with self.assertRaisesRegex(BackupError, "already exists"):
            self.manager.recover_to_new_database(snapshot, target)
        self.assertFalse(target.exists())
        self.assertEqual(sidecar.read_bytes(), b"keep this")
        invalid = Path(self.temp.name) / "invalid.db"
        invalid.write_text("invalid")
        absent = Path(self.temp.name) / "still-absent.db"
        with self.assertRaises(BackupError):
            self.manager.recover_to_new_database(invalid, absent)
        self.assertFalse(absent.exists())


if __name__ == "__main__":
    unittest.main()
