import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from workout_logger.database import Database
from workout_logger.models import WorkoutEntryData
from workout_logger.services.workout_service import WorkoutService


class LiveTemplateTests(unittest.TestCase):
    def test_template_edit_during_new_workout_survives_further_logging_and_undo(self):
        with tempfile.TemporaryDirectory() as td:
            service = WorkoutService(Database(Path(td) / 'history.db'))
            ids = [service.add_exercise(name, 'external_weight') for name in ('One', 'Two', 'Three')]
            sid = service.start_new_template_session('2026-10-01', '80')
            def log(index):
                service.save_entry(sid, WorkoutEntryData(
                    exercise_id=ids[index], exercise_name=('One', 'Two', 'Three')[index],
                    measurement_type='external_weight', external_load=Decimal('10'), reps=6, rir_code='0',
                ))
            log(0)
            tid = service.get_session(sid)['template_id']
            service.set_template_exercises(tid, [ids[2], ids[1]])
            log(1)
            service.undo_last_entry(sid)
            self.assertEqual([r['exercise_id'] for r in service.template_exercises(tid)], [ids[2], ids[1]])
            self.assertEqual([r['exercise_id'] for r in service.remaining_for_session(sid)], ids[1:])
            log(1)
            log(2)
            service.finalize_session(sid)
            next_sid = service.start_existing_template_session(tid, '2026-10-02', '80')
            self.assertEqual([r['exercise_id'] for r in service.remaining_for_session(next_sid)], [ids[2], ids[1]])
            self.assertEqual([r['exercise_id'] for r in service.session_entries(sid)], ids)


if __name__ == '__main__':
    unittest.main()
