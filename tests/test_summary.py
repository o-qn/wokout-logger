import io
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from workout_logger.database import Database
from workout_logger.main import session_summary
from workout_logger.models import WorkoutEntryData
from workout_logger.services.workout_service import WorkoutService


class SummaryTests(unittest.TestCase):
    def test_summary_uses_prior_effort_and_keeps_removed_workout_identity(self):
        with tempfile.TemporaryDirectory() as td:
            service = WorkoutService(Database(Path(td) / 'history.db'))
            eid = service.add_exercise('Curl', 'external_weight')
            first = service.start_new_template_session('2026-10-01', '80')
            def log(sid, reps, rir):
                service.save_entry(sid, WorkoutEntryData(
                    exercise_id=eid, exercise_name='Curl', measurement_type='external_weight',
                    external_load=Decimal('10'), reps=reps, rir_code=rir,
                ))
                service.finalize_session(sid)
            log(first, 6, '0')
            tid = service.get_session(first)['template_id']
            second = service.start_existing_template_session(tid, '2026-10-02', '80')
            log(second, 6, '2')
            future = service.start_existing_template_session(tid, '2026-10-03', '80')
            log(future, 20, '0')
            service.remove_template(tid)
            output = io.StringIO()
            with patch('workout_logger.main.console', Console(file=output, width=180, color_system=None)):
                session_summary(service, second)
            rendered = output.getvalue()
            self.assertIn('Removed Workout A', rendered)
            self.assertIn('2026-10-02', rendered)
            self.assertIn('Performance check', rendered)
            self.assertIn('+2 technical', rendered)
            self.assertIn('0 → 2', rendered)
            self.assertNotIn('-14', rendered)


if __name__ == '__main__':
    unittest.main()
