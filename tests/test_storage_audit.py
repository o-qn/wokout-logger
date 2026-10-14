import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from workout_logger.backups import BackupError, BackupManager
from workout_logger import backups
from workout_logger.database import Database, SCHEMA_VERSION, UnsupportedSchemaError


class StorageAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "live.db")
        self.manager = BackupManager(self.db)
        self.exercise = self.db.execute(
            "INSERT INTO exercises(name,measurement_type,created_at) VALUES('Curl','external_weight','2026-10-01 09:00:00')"
        )
        self.template = self.db.execute(
            "INSERT INTO workout_templates(label,created_at) VALUES('A','2026-10-01 09:00:00')"
        )
        self.session = self.db.execute(
            """INSERT INTO workout_sessions(template_id,session_date,bodyweight,status,started_at)
               VALUES(?,'2026-10-01','80','complete','2026-10-01 10:00:00')""", (self.template,)
        )
        self.entry = self.db.execute(
            """INSERT INTO workout_entries(session_id,exercise_id,exercise_name_snapshot,
                   measurement_type,reps,external_load,exercise_order)
               VALUES(?,?,'Curl','external_weight',6,'10',1)""", (self.session, self.exercise)
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_late_migration_failure_rolls_back_columns_and_backfills(self):
        with self.db.connect() as conn:
            conn.execute("ALTER TABLE workout_entries DROP COLUMN rest_alerted")
            conn.execute("DROP TABLE workout_revisions")
            conn.execute("CREATE VIEW workout_revisions AS SELECT id,session_id FROM workout_entries")
            conn.execute("UPDATE workout_sessions SET template_label_snapshot=NULL,exercise_snapshot_initialized=0")
            conn.execute("UPDATE schema_meta SET value='2' WHERE key='schema_version'")
        with self.assertRaises(sqlite3.DatabaseError):
            Database(self.db.path)
        with closing(sqlite3.connect(self.db.path)) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(workout_entries)")]
            self.assertNotIn("rest_alerted", columns)
            self.assertEqual(conn.execute("SELECT template_label_snapshot,exercise_snapshot_initialized FROM workout_sessions").fetchone(), (None, 0))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workout_session_exercises").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0], "2")
            self.assertEqual(conn.execute("SELECT reps FROM workout_entries").fetchone()[0], 6)

    def test_daily_startup_checks_only_todays_daily_candidates(self):
        today = self.manager.create_backup("daily")
        stale = self.manager.backup_dir / "workouts-2000-01-01--daily--invalid.db"
        unrelated = self.manager.backup_dir / "unrelated.db"
        stale.write_bytes(b"old invalid snapshot kept")
        unrelated.write_bytes(b"unrelated file kept")
        with patch.object(backups, "_readonly", wraps=backups._readonly) as readonly:
            self.assertIsNone(self.manager.daily_backup())
        readonly.assert_called_once_with(today)
        self.assertEqual(stale.read_bytes(), b"old invalid snapshot kept")
        self.assertEqual(unrelated.read_bytes(), b"unrelated file kept")

    def test_invalid_daily_candidate_does_not_prevent_a_valid_new_snapshot(self):
        self.manager.backup_dir.mkdir()
        broken = self.manager.backup_dir / f"workouts-{datetime.now().date().isoformat()}--daily--invalid.db"
        broken.write_bytes(b"invalid daily snapshot retained")
        created = self.manager.daily_backup()
        self.assertTrue(created.is_file())
        self.assertNotEqual(created, broken)
        self.assertEqual(broken.read_bytes(), b"invalid daily snapshot retained")
        self.assertIsNone(self.manager.daily_backup())

    def test_future_schema_is_rejected_without_changing_file_or_journal(self):
        with closing(sqlite3.connect(self.db.path)) as conn, conn:
            conn.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION + 1),))
        with closing(sqlite3.connect(self.db.path)) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
        digest = hashlib.sha256(self.db.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(UnsupportedSchemaError, "newer app"):
            Database(self.db.path)
        self.assertEqual(hashlib.sha256(self.db.path.read_bytes()).hexdigest(), digest)
        with closing(sqlite3.connect(self.db.path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_successful_migration_is_idempotent(self):
        Database(self.db.path)
        with closing(sqlite3.connect(self.db.path)) as conn:
            before = list(conn.iterdump())
        Database(self.db.path)
        with closing(sqlite3.connect(self.db.path)) as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_future_snapshot_cannot_be_partially_merged_or_selected_as_recovery(self):
        snapshot = self.manager.create_backup()
        with closing(sqlite3.connect(snapshot)) as conn, conn:
            conn.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION + 1),))
        with self.assertRaisesRegex(BackupError, "newer app"):
            self.manager.recover_backup(snapshot)
        destination = self.root / "recovered.db"
        with self.assertRaisesRegex(BackupError, "newer app"):
            self.manager.recover_to_new_database(snapshot, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(len(self.manager.list_backups()), 1)
        self.assertEqual(self.db.fetchone("SELECT COUNT(*) FROM workout_entries")[0], 1)

    def test_pre_recovery_backup_is_created_with_exclusive_writer_reservation(self):
        snapshot = self.manager.create_backup()
        original_backup = self.manager.create_backup
        writer_was_blocked = []

        def backup_with_concurrent_write(reason):
            with closing(sqlite3.connect(self.db.path, timeout=0)) as writer:
                try:
                    writer.execute("UPDATE workout_entries SET reps=99")
                    writer.commit()
                    writer_was_blocked.append(False)
                except sqlite3.OperationalError as exc:
                    writer_was_blocked.append("locked" in str(exc))
            return original_backup(reason)

        with patch.object(self.manager, "create_backup", side_effect=backup_with_concurrent_write):
            result = self.manager.recover_backup(snapshot)
        self.assertEqual(writer_was_blocked, [True])
        with closing(sqlite3.connect(result.pre_restore_backup)) as conn:
            self.assertEqual(conn.execute("SELECT reps FROM workout_entries").fetchone()[0], 6)

    def test_failed_pre_recovery_backup_leaves_history_and_schema_untouched(self):
        snapshot = self.manager.create_backup()
        with closing(sqlite3.connect(self.db.path)) as conn:
            before = list(conn.iterdump())
        with patch.object(self.manager, "create_backup", side_effect=BackupError("disk full")):
            with self.assertRaisesRegex(BackupError, "disk full"):
                self.manager.recover_backup(snapshot)
        with closing(sqlite3.connect(self.db.path)) as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_recovery_preserves_multiplicity_and_reasons_of_immutable_revisions(self):
        sql = """INSERT INTO workout_revisions(session_id,entry_id,change_type,before_json,
                    after_json,reason,created_at) VALUES(?,?,'entry','{"reps":5}',
                    '{"reps":6}',?,'2026-10-01 10:00:00')"""
        self.db.execute(sql, (self.session, self.entry, "Original"))
        snapshot = self.manager.create_backup()
        with closing(sqlite3.connect(snapshot)) as conn, conn:
            conn.execute(sql, (self.session, self.entry, "Original"))
            conn.execute(sql, (self.session, self.entry, "Another correction"))
        self.manager.recover_backup(snapshot)
        self.manager.recover_backup(snapshot)
        reasons = [row[0] for row in self.db.fetchall("SELECT reason FROM workout_revisions ORDER BY id")]
        self.assertEqual(reasons, ["Original", "Original", "Another correction"])
        for operation in ("UPDATE workout_revisions SET reason='altered'", "DELETE FROM workout_revisions"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(operation)

    def legacy_snapshot(self, name, label):
        source = Database(self.root / f"{name}.db")
        source.execute("DELETE FROM app_settings WHERE key='database_uuid'")
        ex = source.execute("INSERT INTO exercises(name,measurement_type,created_at) VALUES(?,'external_weight','2026-10-01 09:00:00')", (name,))
        template = source.execute("INSERT INTO workout_templates(label,created_at) VALUES(?,'2026-10-01 09:00:00')", (label,))
        sid = source.execute("INSERT INTO workout_sessions(template_id,session_date,bodyweight,status,started_at) VALUES(?,'2026-10-02','80','complete','2026-10-02 10:00:00')", (template,))
        source.execute("INSERT INTO workout_entries(session_id,exercise_id,exercise_name_snapshot,measurement_type,reps,exercise_order) VALUES(?,?,?,'external_weight',7,1)", (sid, ex, name))
        return source.path

    def test_unrelated_legacy_snapshots_cannot_share_exercise_or_template_identity(self):
        squat = self.legacy_snapshot("Squat", "B")
        press = self.legacy_snapshot("Press", "C")
        self.manager.recover_backup(squat)
        self.manager.recover_backup(press)
        self.manager.recover_backup(squat)
        self.manager.recover_backup(press)
        rows = self.db.fetchall("""SELECT we.exercise_name_snapshot,e.name,t.label FROM workout_entries we
                                  JOIN exercises e ON e.id=we.exercise_id
                                  JOIN workout_sessions s ON s.id=we.session_id
                                  JOIN workout_templates t ON t.id=s.template_id ORDER BY we.id""")
        self.assertEqual([tuple(row) for row in rows], [("Curl", "Curl", "A"), ("Squat", "Squat", "B"), ("Press", "Press", "C")])

    def test_legacy_snapshot_matches_shared_history_after_names_and_labels_change(self):
        snapshot = self.manager.create_backup()
        with closing(sqlite3.connect(snapshot)) as conn, conn:
            conn.execute("DELETE FROM app_settings WHERE key='database_uuid'")
        self.db.execute("UPDATE exercises SET name='Renamed Curl' WHERE id=?", (self.exercise,))
        self.db.execute("UPDATE workout_templates SET label='B' WHERE id=?", (self.template,))
        result = self.manager.recover_backup(snapshot)
        self.assertEqual((result.sessions, result.entries, result.exercises, result.templates), (0, 0, 0, 0))
        self.assertEqual(self.db.fetchone("SELECT name FROM exercises WHERE id=?", (self.exercise,))[0], "Renamed Curl")
        self.assertEqual(self.db.fetchone("SELECT label FROM workout_templates WHERE id=?", (self.template,))[0], "B")

    def test_previously_recovered_legacy_snapshot_stays_idempotent_after_upgrade(self):
        snapshot = self.manager.create_backup()
        with closing(sqlite3.connect(snapshot)) as conn, conn:
            conn.execute("DELETE FROM app_settings WHERE key='database_uuid'")
        self.db.execute("DELETE FROM workout_sessions WHERE id=?", (self.session,))
        self.assertEqual(self.manager.recover_backup(snapshot).sessions, 1)
        # Earlier releases stored exactly these maps under a shared key.
        self.db.execute("UPDATE backup_recovery_map SET source_key='legacy'")
        result = self.manager.recover_backup(snapshot)
        self.assertEqual((result.sessions, result.entries, result.exercises, result.templates), (0, 0, 0, 0))

    def test_old_legacy_mapping_keeps_recovered_template_name_collisions_idempotent(self):
        snapshot = self.legacy_snapshot("Squat", "A")
        with closing(sqlite3.connect(snapshot)) as conn, conn:
            conn.execute("UPDATE workout_templates SET created_at='2025-01-01 09:00:00'")
        self.assertEqual(self.manager.recover_backup(snapshot).templates, 1)
        self.db.execute("UPDATE backup_recovery_map SET source_key='legacy'")
        result = self.manager.recover_backup(snapshot)
        self.assertEqual((result.sessions, result.entries, result.exercises, result.templates), (0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
