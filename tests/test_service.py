import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from workout_logger.database import Database
from workout_logger.models import WorkoutEntryData
from workout_logger.services.workout_service import WorkoutService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "workouts.db")
        self.db.seed_exercises()
        self.service = WorkoutService(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_and_template_letters(self):
        self.assertEqual(len(self.service.active_exercises()), 11)
        self.assertEqual(self.service.next_template_label(), "A")
        sid = self.service.start_new_template_session("2026-09-14", "84.6")
        self.assertEqual(self.service.get_session(sid)["template_label"], "A")
        self.assertEqual(self.service.next_template_label(), "B")

    def test_new_session_autosaves_and_repeats_template(self):
        sid = self.service.start_new_template_session("2026-09-14", "84.6")
        preacher = next(e for e in self.service.active_exercises() if e["name"] == "Single-Arm Preacher Curl")
        self.service.save_entry(sid, WorkoutEntryData(
            exercise_id=preacher["id"], exercise_name=preacher["name"], measurement_type="external_weight",
            unit="kg", bodyweight=Decimal("84.6"), external_load=Decimal("19.5"), reps=4, rir_code="1",
        ))
        self.assertEqual(self.service.session_entries(sid)[0]["external_load"], "19.5")
        self.service.finalize_session(sid, force_incomplete=True)

        template_id = self.service.get_session(sid)["template_id"]
        sid2 = self.service.start_existing_template_session(template_id, "2026-09-16", "84.8")
        remaining = self.service.remaining_for_session(sid2)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["default_external_load"], "19.5")

        self.service.save_entry(sid2, WorkoutEntryData(
            exercise_id=preacher["id"], exercise_name=preacher["name"], measurement_type="external_weight",
            unit="kg", bodyweight=Decimal("84.8"), external_load=Decimal("20"), reps=4, rir_code="1",
        ))
        self.service.finalize_session(sid2)
        template_row = self.service.template_exercises(template_id)[0]
        self.assertEqual(template_row["default_external_load"], "20")

    def test_skip_and_undo(self):
        sid = self.service.start_new_template_session("2026-09-14", "84.6")
        ex = self.service.active_exercises()[0]
        self.service.save_entry(sid, WorkoutEntryData(
            exercise_id=ex["id"], exercise_name=ex["name"], measurement_type=ex["measurement_type"],
            unit=ex["default_unit"], bodyweight=Decimal("84.6"), external_load=Decimal("16"), reps=8, rir_code="0",
        ))
        self.assertEqual(len(self.service.template_exercises(self.service.get_session(sid)["template_id"])), 1)
        undone = self.service.undo_last_entry(sid)
        self.assertEqual(undone["exercise_id"], ex["id"])
        self.assertEqual(len(self.service.session_entries(sid)), 0)
        self.assertEqual(len(self.service.template_exercises(self.service.get_session(sid)["template_id"])), 0)


if __name__ == "__main__":
    unittest.main()
