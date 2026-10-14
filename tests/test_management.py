import sqlite3
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from workout_logger.database import Database
from workout_logger.models import WorkoutEntryData
from workout_logger.services.workout_service import WorkoutService
from workout_logger.ui.management_menu import _edit_template, _revision_changes, saved_workout_menu


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "workouts.db")
        self.db.seed_exercises()
        self.service = WorkoutService(self.db)
        self.exercises = self.service.active_exercises()
        self.session_id = self.service.start_new_template_session("2026-09-14", "84.6")
        self.data = WorkoutEntryData(
            exercise_id=self.exercises[0]["id"], exercise_name=self.exercises[0]["name"],
            measurement_type="external_weight", unit="kg", bodyweight=Decimal("84.6"),
            external_load=Decimal("19.5"), reps=6, rir_code="0", notes="Original note",
        )
        self.entry_id = self.service.save_entry(self.session_id, self.data)
        self.service.save_entry(self.session_id, replace(
            self.data, exercise_id=self.exercises[1]["id"], exercise_name=self.exercises[1]["name"],
            external_load=Decimal("12"),
        ))
        self.service.finalize_session(self.session_id, force_incomplete=True)
        self.template_id = self.service.get_session(self.session_id)["template_id"]
        self.backup = Mock()
        self.service._backup_before_change = self.backup

    def tearDown(self):
        self.tmp.cleanup()

    def test_template_reorder_add_remove_only_changes_future_session(self):
        history = self.service.session_entries(self.session_id)
        snapshots = self.service.session_exercises(self.session_id)
        active = self.service.start_existing_template_session(self.template_id, "2026-09-16", "85")
        frozen = self.service.session_exercises(active)
        ids = [self.exercises[1]["id"], self.exercises[2]["id"]]
        self.service.set_template_exercises(self.template_id, ids)
        rows = self.service.template_exercises(self.template_id)
        self.assertEqual([row["exercise_id"] for row in rows], ids)
        self.assertEqual(rows[0]["default_external_load"], "12")
        self.assertIsNone(rows[1]["default_external_load"])
        self.assertEqual(history, self.service.session_entries(self.session_id))
        self.assertEqual(snapshots, self.service.session_exercises(self.session_id))
        self.assertEqual(frozen, self.service.session_exercises(active))
        self.service.save_entry(active, replace(self.data, reps=7))
        self.assertEqual([row["exercise_id"] for row in self.service.template_exercises(self.template_id)], ids)
        next_id = self.service.start_existing_template_session(self.template_id, "2026-09-17", "85")
        self.assertEqual([row["exercise_id"] for row in self.service.session_exercises(next_id)], ids)
        self.assertEqual(self.db.fetchone("SELECT revision FROM workout_templates WHERE id=?", (self.template_id,))["revision"], 1)
        self.backup.assert_called_once()

    def test_template_invalid_inputs_and_no_op_keep_original(self):
        original = self.service.template_exercises(self.template_id)
        ids = [row["exercise_id"] for row in original]
        self.service.set_template_exercises(self.template_id, ids)
        self.backup.assert_not_called()
        for invalid in ([], [ids[0], ids[0]], [999999], [True], [str(ids[0])]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.service.set_template_exercises(self.template_id, invalid)
        self.assertEqual(original, self.service.template_exercises(self.template_id))
        self.service.update_exercise(self.exercises[2]["id"], archived=True)
        with self.assertRaises(ValueError):
            self.service.set_template_exercises(self.template_id, [self.exercises[2]["id"]])

    def test_template_keeps_only_compatible_remembered_defaults(self):
        self.db.execute("UPDATE exercises SET default_unit='lb' WHERE id=?", (self.data.exercise_id,))
        self.service.set_template_exercises(self.template_id, [self.exercises[1]["id"], self.data.exercise_id])
        changed = self.service.template_exercises(self.template_id)[1]
        self.assertEqual(changed["unit_snapshot"], "lb")
        self.assertIsNone(changed["default_external_load"])
        self.assertEqual(self.service.get_entry(self.entry_id)["unit"], "kg")

    def test_existing_archived_template_member_can_be_retained_and_reordered(self):
        archived_id = self.exercises[0]["id"]
        self.service.update_exercise(archived_id, archived=True)
        self.backup.reset_mock()
        original_history = self.service.session_entries(self.session_id)
        ids = [self.exercises[1]["id"], archived_id]
        self.service.set_template_exercises(self.template_id, ids)
        rows = self.service.template_exercises(self.template_id)
        self.assertEqual([row["exercise_id"] for row in rows], ids)
        self.assertEqual(rows[1]["default_external_load"], "19.5")
        self.assertTrue(rows[1]["archived"])
        self.assertEqual(original_history, self.service.session_entries(self.session_id))
        self.backup.assert_called_once()
        self.service.set_template_exercises(self.template_id, [self.exercises[1]["id"]])
        with self.assertRaises(ValueError):
            self.service.set_template_exercises(self.template_id, ids)

    def test_entry_correction_keeps_original_snapshot_and_all_timer_template_state(self):
        original = self.service.get_entry(self.entry_id)
        session = self.service.get_session(self.session_id)
        template = self.service.template_exercises(self.template_id)
        self.service.update_exercise(self.data.exercise_id, name="New exercise name", measurement_type="bodyweight", default_unit="lb")
        template = self.service.template_exercises(self.template_id)
        revision_id = self.service.correct_entry(self.entry_id, replace(
            self.data, exercise_name="Changed input name", external_load=Decimal("20"),
            reps=7, rir_code="F", notes="Corrected note",
        ), reason="Mistyped reps and load")
        current = self.service.get_entry(self.entry_id)
        self.assertEqual(current["exercise_name_snapshot"], original["exercise_name_snapshot"])
        self.assertEqual((current["measurement_type"], current["unit"]), ("external_weight", "kg"))
        self.assertEqual((current["reps"], current["external_load"], current["rir_code"], current["failure"]), (7, "20", "F", 1))
        self.assertEqual(session, self.service.get_session(self.session_id))
        self.assertEqual(template, self.service.template_exercises(self.template_id))
        revisions = self.service.revisions(self.session_id)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["id"], revision_id)
        self.assertEqual(revisions[0]["before"], original)
        self.assertEqual(revisions[0]["after"], current)
        self.assertEqual(revisions[0]["reason"], "Mistyped reps and load")
        self.assertIn(("reps", 6, 7), list(_revision_changes(revisions[0])))

    def test_session_correction_updates_all_bodyweight_rows_with_full_original(self):
        original_session = self.service.get_session(self.session_id)
        original_entries = self.service.session_entries(self.session_id)
        original_bw = dict(self.db.fetchone("SELECT * FROM bodyweight_entries WHERE session_id=?", (self.session_id,)))
        template = self.service.template_exercises(self.template_id)
        self.service.correct_session(self.session_id, session_date="2026-09-13", bodyweight=Decimal("85.2"), reason="Scale correction")
        current = self.service.get_session(self.session_id)
        self.assertEqual((current["session_date"], current["bodyweight"]), ("2026-09-13", "85.2"))
        self.assertEqual(current["last_activity_at"], original_session["last_activity_at"])
        self.assertEqual(current["workout_duration_seconds"], original_session["workout_duration_seconds"])
        self.assertTrue(all(row["bodyweight"] == "85.2" for row in self.service.session_entries(self.session_id)))
        bodyweight = dict(self.db.fetchone("SELECT * FROM bodyweight_entries WHERE session_id=?", (self.session_id,)))
        self.assertEqual((bodyweight["entry_date"], bodyweight["bodyweight"]), ("2026-09-13", "85.2"))
        revision = self.service.revisions(self.session_id)[0]
        self.assertEqual(revision["before"]["bodyweight_entries"], [original_bw])
        self.assertEqual(revision["before"]["entries"], original_entries)
        self.assertEqual(template, self.service.template_exercises(self.template_id))

    def test_date_only_correction_preserves_entry_bodyweights(self):
        before = self.service.session_entries(self.session_id)
        self.service.correct_session(self.session_id, session_date="2026-09-12")
        self.assertEqual(before, self.service.session_entries(self.session_id))

    def test_multiple_corrections_keep_every_original_revision(self):
        self.service.correct_entry(self.entry_id, replace(self.data, reps=7))
        self.service.correct_entry(self.entry_id, replace(self.data, reps=8))
        revisions = self.service.revisions(self.session_id)
        self.assertEqual([row["before"]["reps"] for row in revisions], [7, 6])
        for statement in ("UPDATE workout_revisions SET reason='altered'", "DELETE FROM workout_revisions"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(statement)
        self.assertEqual(len(self.service.revisions(self.session_id)), 2)

    def test_recovered_older_revision_is_sorted_by_original_timestamp(self):
        newest = self.service.correct_entry(self.entry_id, replace(self.data, reps=7))
        recovered = self.db.execute(
            """INSERT INTO workout_revisions(session_id,entry_id,change_type,before_json,after_json,reason,created_at)
            SELECT session_id,entry_id,change_type,before_json,after_json,'Recovered older correction',
                   '2000-01-01T12:00:00+03:00' FROM workout_revisions WHERE id=?""", (newest,),
        )
        self.assertGreater(recovered, newest)
        self.assertEqual([row["id"] for row in self.service.revisions(self.session_id)], [newest, recovered])

    def test_invalid_entry_corrections_cannot_change_history(self):
        before = self.service.get_entry(self.entry_id)
        invalid = [
            replace(self.data, reps=-1), replace(self.data, reps=1.5),
            replace(self.data, external_load=Decimal("NaN")), replace(self.data, external_load=Decimal("Infinity")),
            replace(self.data, rir_code="-1"), replace(self.data, rir_code="1.5"),
            replace(self.data, rir_code=None), replace(self.data, external_load=None),
            replace(self.data, unit="lb"), replace(self.data, measurement_type="bodyweight"),
            replace(self.data, exercise_id=self.exercises[1]["id"]), replace(self.data, bodyweight=Decimal("90")),
        ]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.service.correct_entry(self.entry_id, data)
        self.assertEqual(before, self.service.get_entry(self.entry_id))
        self.assertEqual(self.service.revisions(self.session_id), [])
        self.backup.assert_not_called()

    def test_invalid_session_corrections_cannot_change_history(self):
        before = self.service.get_session(self.session_id)
        for changes in ({"session_date": "2026-02-30"}, {"session_date": "bad"}, {"bodyweight": "-1"},
                        {"bodyweight": "NaN"}, {"bodyweight": "Infinity"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.service.correct_session(self.session_id, **changes)
        self.assertEqual(before, self.service.get_session(self.session_id))
        self.assertEqual(self.service.revisions(self.session_id), [])
        self.backup.assert_not_called()

    def test_corrections_require_saved_workout(self):
        active = self.service.start_existing_template_session(self.template_id, "2026-09-17", "84.6")
        active_entry = self.service.save_entry(active, self.data)
        with self.assertRaises(ValueError):
            self.service.correct_entry(active_entry, replace(self.data, reps=7))
        with self.assertRaises(ValueError):
            self.service.correct_session(active, bodyweight="85")
        self.service.discard_session(active)
        with self.assertRaises(ValueError):
            self.service.correct_session(active, bodyweight="85")

    def test_revision_failure_rolls_back_performance_and_session_edits(self):
        old_entry = self.service.get_entry(self.entry_id)
        old_session = self.service.get_session(self.session_id)
        with patch.object(self.service, "_append_revision", side_effect=RuntimeError("cannot write revision")):
            with self.assertRaises(RuntimeError):
                self.service.correct_entry(self.entry_id, replace(self.data, reps=7))
            with self.assertRaises(RuntimeError):
                self.service.correct_session(self.session_id, bodyweight="85")
        self.assertEqual(old_entry, self.service.get_entry(self.entry_id))
        self.assertEqual(old_session, self.service.get_session(self.session_id))
        self.assertEqual(self.service.revisions(self.session_id), [])

    def test_no_op_creates_neither_backup_nor_revision(self):
        self.assertIsNone(self.service.correct_entry(self.entry_id, self.data))
        self.assertIsNone(self.service.correct_session(self.session_id))
        self.backup.assert_not_called()
        self.assertEqual(self.service.revisions(self.session_id), [])

    def test_skipped_saved_entry_can_be_corrected_to_a_result(self):
        skipped = self.service.start_existing_template_session(self.template_id, "2026-09-18", "84.6")
        entry_id = self.service.save_entry(skipped, replace(self.data, skipped=True, external_load=None, reps=None, rir_code=None))
        self.service.finalize_session(skipped, force_incomplete=True)
        self.service.correct_entry(entry_id, replace(self.data, reps=5))
        self.assertFalse(self.service.get_entry(entry_id)["skipped"])
        self.assertTrue(self.service.revisions(skipped)[0]["before"]["skipped"])

    def test_template_cancel_does_not_save_draft(self):
        template = self.service.templates()[0]
        original = self.service.template_exercises(self.template_id)
        with patch("workout_logger.ui.management_menu.select", side_effect=[
            ("exercise", self.data.exercise_id), "remove", ("cancel", None),
        ]):
            _edit_template(self.service, template)
        self.assertEqual(original, self.service.template_exercises(self.template_id))
        self.backup.assert_not_called()

    def test_saved_ui_correction_exports_after_confirmation(self):
        exporter = Mock()
        summary = Mock()
        with patch("workout_logger.ui.management_menu.select", side_effect=["entry", self.entry_id, None]), \
             patch("workout_logger.ui.management_menu.prompt_entry", return_value=replace(self.data, reps=7)), \
             patch("workout_logger.ui.management_menu.text", return_value="Typo"), \
             patch("workout_logger.ui.management_menu.confirm", return_value=True):
            saved_workout_menu(self.service, exporter, Path("history.xlsx"), self.session_id, summary)
        self.assertEqual(self.service.get_entry(self.entry_id)["reps"], 7)
        exporter.export.assert_called_once_with(Path("history.xlsx"))
        self.assertEqual(summary.call_count, 2)

    def test_saved_ui_cancel_does_not_correct_or_export(self):
        exporter = Mock()
        with patch("workout_logger.ui.management_menu.select", side_effect=["entry", self.entry_id, None]), \
             patch("workout_logger.ui.management_menu.prompt_entry", return_value=replace(self.data, reps=7)), \
             patch("workout_logger.ui.management_menu.text", return_value="Typo"), \
             patch("workout_logger.ui.management_menu.confirm", return_value=False):
            saved_workout_menu(self.service, exporter, Path("history.xlsx"), self.session_id, Mock())
        self.assertEqual(self.service.get_entry(self.entry_id)["reps"], 6)
        self.assertEqual(self.service.revisions(self.session_id), [])
        exporter.export.assert_not_called()


if __name__ == "__main__":
    unittest.main()
