import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from workout_logger.database import Database, SCHEMA_VERSION
from workout_logger.models import WorkoutEntryData
from workout_logger.services.workout_service import WorkoutService


class HistorySessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "history.db"
        self.db = Database(self.path)
        self.service = WorkoutService(self.db)
        self.exercise_ids = [self.service.add_exercise(name, "external_weight", "kg") for name in ("Curl", "Press", "Row")]

    def tearDown(self):
        self.tmp.cleanup()

    def data(self, exercise_id=None, **changes):
        exercise_id = exercise_id or self.exercise_ids[0]
        exercise = self.service.get_exercise(exercise_id)
        values = dict(
            exercise_id=exercise_id, exercise_name=exercise["name"], measurement_type="external_weight",
            unit="kg", external_load=Decimal("20"), reps=6, rir_code="0",
        )
        values.update(changes)
        return WorkoutEntryData(**values)

    def saved_session(self, session_date="2026-10-01"):
        session_id = self.service.start_new_template_session(session_date, "80")
        self.service.save_entry(session_id, self.data())
        self.service.finalize_session(session_id, force_incomplete=True)
        return session_id, self.service.get_session(session_id)["template_id"]

    def counts(self):
        return {table: self.db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")["n"] for table in (
            "workout_templates", "workout_template_exercises", "workout_sessions", "workout_entries", "bodyweight_entries"
        )}

    def test_remove_preserves_every_history_row_and_original_letters(self):
        first, template_a = self.saved_session("2026-10-01")
        second, template_b = self.saved_session("2026-10-02")
        third, template_c = self.saved_session("2026-10-03")
        before_counts = self.counts()
        before_entries = [dict(r) for r in self.db.fetchall("SELECT * FROM workout_entries ORDER BY id")]
        before_bodyweights = [dict(r) for r in self.db.fetchall("SELECT * FROM bodyweight_entries ORDER BY id")]

        self.service.remove_template(template_a)
        self.assertEqual([(t["id"], t["label"]) for t in self.service.templates()], [(template_b, "A"), (template_c, "B")])
        self.assertEqual(self.service.get_session(first)["workout_name"], "Removed Workout A")
        self.assertEqual(self.service.get_session(second)["workout_name"], "Workout B")
        self.assertEqual(self.service.get_session(third)["workout_name"], "Workout C")
        self.assertEqual(self.service.get_session(first)["session_date"], "2026-10-01")
        self.assertEqual(self.counts(), before_counts)
        self.assertEqual([dict(r) for r in self.db.fetchall("SELECT * FROM workout_entries ORDER BY id")], before_entries)
        self.assertEqual([dict(r) for r in self.db.fetchall("SELECT * FROM bodyweight_entries ORDER BY id")], before_bodyweights)

        renamed = self.service.start_existing_template_session(template_b, "2026-10-04", "81")
        self.assertEqual(self.service.get_session(renamed)["template_label"], "A")
        self.service.remove_template(template_b)
        self.assertEqual(self.service.get_session(second)["workout_name"], "Removed Workout B")
        self.assertEqual(self.service.get_session(renamed)["workout_name"], "Removed Workout A")
        with self.assertRaises(ValueError):
            self.service.start_existing_template_session(template_b, "2026-10-05", "81")
        self.service.save_entry(renamed, self.data())
        self.service.finalize_session(renamed)
        self.assertEqual([t["id"] for t in self.service.templates()], [template_c])
        self.assertEqual(self.service.next_template_label(), "B")
        new_id = self.service.create_template()
        self.assertEqual([(t["id"], t["label"]) for t in self.service.templates()], [(template_c, "A"), (new_id, "B")])
        history = {s["id"]: s for s in self.service.workout_history()}
        self.assertEqual(history[first]["workout_name"], "Removed Workout A")
        self.assertEqual(next(e for e in self.service.exercise_history(self.exercise_ids[0]) if e["session_id"] == second)["workout_name"], "Removed Workout B")

    def test_labels_continue_past_z_after_removing_a_middle_template(self):
        templates = [self.service.create_template() for _ in range(28)]
        self.assertEqual([t["label"] for t in self.service.templates()][-2:], ["AA", "AB"])
        self.service.remove_template(templates[12])
        labels = [t["label"] for t in self.service.templates()]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(labels[-1], "AA")
        self.assertEqual(self.service.next_template_label(), "AB")
        self.service.create_template()
        self.assertEqual(self.service.templates()[-1]["label"], "AB")

    def test_config_changes_apply_next_session_and_keep_started_session_frozen(self):
        first, template_id = self.saved_session()
        before = self.service.session_entries(first)[0]
        running = self.service.start_existing_template_session(template_id, "2026-10-02", "80")
        self.service.update_exercise(self.exercise_ids[0], name="Renamed Curl", default_unit="lb")

        frozen = self.service.get_session_exercise(running, self.exercise_ids[0])
        self.assertEqual((frozen["name"], frozen["default_unit"], frozen["default_external_load"]), ("Curl", "kg", "20"))
        next_session = self.service.start_existing_template_session(template_id, "2026-10-03", "80")
        next_exercise = self.service.remaining_for_session(next_session)[0]
        self.assertEqual((next_exercise["name"], next_exercise["unit_snapshot"]), ("Renamed Curl", "lb"))
        self.assertIsNone(next_exercise["default_external_load"])

        # Resuming after restarting the app must still use the frozen kg config.
        self.service = WorkoutService(Database(self.path))
        self.assertEqual(self.service.get_session_exercise(running, self.exercise_ids[0])["default_unit"], "kg")
        self.service.save_entry(running, self.data(exercise_name="Curl", external_load=Decimal("21")))
        self.assertIsNone(self.service.template_exercises(template_id)[0]["default_external_load"])
        self.assertEqual(self.service.session_entries(first)[0], before)

        self.service.update_exercise(self.exercise_ids[0], measurement_type="isometric", default_unit="sec")
        later = self.service.start_existing_template_session(template_id, "2026-10-04", "80")
        row = self.service.remaining_for_session(later)[0]
        self.assertEqual((row["measurement_type_snapshot"], row["unit_snapshot"]), ("isometric", "sec"))
        self.assertIsNone(row["default_external_load"])
        self.assertEqual(self.service.session_entries(first)[0], before)

    def test_new_session_freezes_exercises_names_modes_and_compatible_defaults(self):
        self.saved_session()
        running = self.service.start_new_template_session("2026-10-02", "80")
        self.assertEqual(self.service.get_session_exercise(running, self.exercise_ids[0])["default_external_load"], "20")
        self.service.update_exercise(self.exercise_ids[0], name="New Curl", measurement_type="isometric", default_unit="sec")
        self.service.update_exercise(self.exercise_ids[1], archived=True)
        added = self.service.add_exercise("New exercise", "amrap", "reps")
        remaining = self.service.remaining_for_session(running)
        self.assertEqual([e["id"] for e in remaining], self.exercise_ids)
        self.assertNotIn(added, [e["id"] for e in remaining])
        self.assertEqual((remaining[0]["name"], remaining[0]["measurement_type"]), ("Curl", "external_weight"))
        self.service.save_entry(running, self.data(exercise_name="Curl"))
        entry = self.service.session_entries(running)[0]
        self.assertEqual((entry["exercise_name_snapshot"], entry["unit"]), ("Curl", "kg"))
        template_id = self.service.get_session(running)["template_id"]
        template = self.service.template_exercises(template_id)[0]
        self.assertEqual(template["measurement_type_snapshot"], "isometric")
        self.assertIsNone(template["default_external_load"])

    def test_rest_timer_changes_only_for_a_successful_new_logged_set(self):
        session_id = self.service.start_new_template_session("2026-10-01", "80")
        self.assertIsNone(self.service.get_session(session_id)["rest_started_at"])
        entry_id = self.service.save_entry(session_id, self.data())
        self.assertIsNotNone(self.service.get_session(session_id)["rest_started_at"])
        sentinel = "2020-01-01T01:00:00+00:00"
        self.db.execute("UPDATE workout_sessions SET rest_started_at=? WHERE id=?", (sentinel, session_id))
        self.service.edit_entry(entry_id, self.data(reps=7))
        self.assertEqual(self.service.get_session(session_id)["rest_started_at"], sentinel)
        self.service.save_entry(session_id, self.data(self.exercise_ids[1], skipped=True))
        self.assertEqual(self.service.get_session(session_id)["rest_started_at"], sentinel)
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.save_entry(session_id, self.data())
        self.assertEqual(self.service.get_session(session_id)["rest_started_at"], sentinel)
        self.service.save_entry(session_id, self.data(self.exercise_ids[2]))
        self.assertNotEqual(self.service.get_session(session_id)["rest_started_at"], sentinel)

    def test_logging_a_skipped_set_starts_rest_but_regular_edits_do_not(self):
        session_id = self.service.start_new_template_session("2026-10-01", "80")
        entry_id = self.service.save_entry(session_id, self.data(skipped=True))
        self.assertIsNone(self.service.get_session(session_id)["rest_started_at"])
        self.service.edit_entry(entry_id, self.data(skipped=False))
        self.assertIsNotNone(self.service.get_session(session_id)["rest_started_at"])
        sentinel = "2020-01-01T01:00:00+00:00"
        self.db.execute("UPDATE workout_sessions SET rest_started_at=? WHERE id=?", (sentinel, session_id))
        self.service.edit_entry(entry_id, self.data(reps=7))
        self.assertEqual(self.service.get_session(session_id)["rest_started_at"], sentinel)

    def test_resistance_mode_and_cleared_unit_have_stable_session_units(self):
        _, template_id = self.saved_session()
        self.service.update_exercise(self.exercise_ids[0], measurement_type="resistance_units", resistance_unit=None)
        running = self.service.start_existing_template_session(template_id, "2026-10-02", "80")
        exercise = self.service.get_session_exercise(running, self.exercise_ids[0])
        self.assertEqual(exercise["resistance_unit"], "units")
        self.service.save_entry(running, self.data(
            measurement_type="resistance_units", resistance_unit=exercise["resistance_unit"],
            external_load=None, resistance_value=Decimal("2"),
        ))
        self.service.update_exercise(self.exercise_ids[0], default_unit="springs", resistance_unit=None)
        next_session = self.service.start_existing_template_session(template_id, "2026-10-03", "80")
        exercise = self.service.get_session_exercise(next_session, self.exercise_ids[0])
        self.assertEqual((exercise["default_unit"], exercise["resistance_unit"]), ("springs", "springs"))
        self.assertIsNone(exercise["default_resistance_value"])
        self.service.save_entry(next_session, self.data(
            measurement_type="resistance_units", unit="springs", resistance_unit="springs",
            external_load=None, resistance_value=Decimal("3"),
        ))

    def test_previous_entry_uses_strict_chronological_order_even_for_old_history(self):
        first, template_id = self.saved_session("2026-10-01")
        second = self.service.start_existing_template_session(template_id, "2026-10-02", "80")
        self.service.save_entry(second, self.data(reps=7))
        same_day_later = self.service.start_existing_template_session(template_id, "2026-10-02", "80")
        self.service.save_entry(same_day_later, self.data(reps=8))
        future = self.service.start_existing_template_session(template_id, "2026-10-05", "80")
        self.service.save_entry(future, self.data(reps=9))
        backdated = self.service.start_existing_template_session(template_id, "2026-09-30", "80")
        self.assertIsNone(self.service.latest_previous_entry(self.exercise_ids[0], exclude_session_id=backdated))
        self.assertIsNone(self.service.latest_previous_entry(self.exercise_ids[0], exclude_session_id=first))
        self.assertEqual(self.service.latest_previous_entry(self.exercise_ids[0], exclude_session_id=second)["session_id"], first)
        self.assertEqual(self.service.latest_previous_entry(self.exercise_ids[0], exclude_session_id=same_day_later)["session_id"], second)
        self.assertEqual(self.service.latest_previous_entry(self.exercise_ids[0], exclude_session_id=future)["session_id"], same_day_later)

    def test_discard_keeps_all_rows_and_saved_history_cannot_be_discarded_or_undone(self):
        saved, _ = self.saved_session()
        unfinished = self.service.start_new_template_session("2026-10-02", "80")
        self.service.save_entry(unfinished, self.data())
        before = self.counts()
        self.service.discard_session(unfinished)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.service.get_session(unfinished)["status"], "discarded")
        self.assertEqual(len(self.service.session_entries(unfinished)), 1)
        with self.assertRaises(ValueError):
            self.service.discard_session(saved)
        with self.assertRaises(ValueError):
            self.service.undo_last_entry(saved)
        self.assertEqual(self.counts(), before)

    def test_additive_v1_migration_preserves_data_and_is_idempotent(self):
        session_id, template_id = self.saved_session()
        before_entry = self.service.session_entries(session_id)[0]
        before_bw = dict(self.db.fetchone("SELECT * FROM bodyweight_entries WHERE session_id=?", (session_id,)))
        # Recreate the old database shape while retaining a real old session.
        with self.db.connect() as conn:
            conn.execute("DROP TABLE workout_session_exercises")
            for column in ("template_label_snapshot", "rest_started_at", "exercise_snapshot_initialized"):
                conn.execute(f"ALTER TABLE workout_sessions DROP COLUMN {column}")
            conn.execute("UPDATE schema_meta SET value='1' WHERE key='schema_version'")
            conn.execute("UPDATE exercises SET name='Current Name', measurement_type='isometric', default_unit='sec' WHERE id=?", (self.exercise_ids[0],))

        self.db = Database(self.path)
        self.service = WorkoutService(self.db)
        self.assertEqual(self.db.fetchone("SELECT value FROM schema_meta WHERE key='schema_version'")["value"], str(SCHEMA_VERSION))
        self.assertEqual(self.service.session_entries(session_id)[0], before_entry)
        self.assertEqual(dict(self.db.fetchone("SELECT * FROM bodyweight_entries WHERE session_id=?", (session_id,))), before_bw)
        frozen = self.service.get_session_exercise(session_id, self.exercise_ids[0])
        self.assertEqual((frozen["name"], frozen["measurement_type"], frozen["default_unit"]), ("Curl", "external_weight", "kg"))
        self.assertEqual(self.service.get_session(session_id)["template_label"], "A")
        self.assertIsNotNone(self.service.get_session(session_id)["rest_started_at"])
        self.service.remove_template(template_id)
        self.db.initialize()
        self.assertEqual(self.service.get_session(session_id)["workout_name"], "Removed Workout A")
        self.assertEqual(self.service.get_session_exercise(session_id, self.exercise_ids[0]), frozen)
        self.assertEqual(self.service.session_entries(session_id)[0], before_entry)


if __name__ == "__main__":
    unittest.main()
