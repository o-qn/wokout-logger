import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from workout_logger.backups import BackupError
from workout_logger.database import Database
from workout_logger.main import run
from workout_logger.models import WorkoutEntryData
from workout_logger.services.workout_service import WorkoutService


class UndoRestTests(unittest.TestCase):
    def test_undo_restores_last_surviving_log_and_clears_after_last_set(self):
        with tempfile.TemporaryDirectory() as td:
            service = WorkoutService(Database(Path(td) / 'history.db'))
            ids = [service.add_exercise(name, 'external_weight') for name in ('Curl', 'Row')]
            sid = service.start_new_template_session('2026-10-14', '80')
            clock = [datetime(2026, 10, 14, 15, tzinfo=timezone.utc)]
            service._timing_now = lambda: clock[0]
            def data(index, skipped=False):
                return WorkoutEntryData(exercise_id=ids[index], exercise_name=('Curl', 'Row')[index], measurement_type='external_weight', external_load=Decimal('10'), reps=6, rir_code='0', skipped=skipped)
            service.save_entry(sid, data(0))
            first_rest = service.get_session(sid)['rest_started_at']
            clock[0] += timedelta(seconds=60)
            service.save_entry(sid, data(1))
            self.assertNotEqual(service.get_session(sid)['rest_started_at'], first_rest)
            service.undo_last_entry(sid)
            self.assertEqual(service.get_session(sid)['rest_started_at'], first_rest)
            self.assertEqual(service.rest_elapsed_seconds(sid), 60)
            service.undo_last_entry(sid)
            self.assertIsNone(service.get_session(sid)['rest_started_at'])
            self.assertIsNone(service.rest_elapsed_seconds(sid))

    def test_backup_failure_returns_to_main_menu(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with patch('workout_logger.main.app_paths', return_value=(td/'history.db', td/'history.xlsx')), patch('workout_logger.main.select', side_effect=['manage','exit']) as menu, patch('workout_logger.main.manage_exercises', side_effect=BackupError('disk full')), patch('workout_logger.main.console'), patch('builtins.input', return_value=''):
                run()
            self.assertEqual(menu.call_count, 2)


if __name__ == '__main__':
    unittest.main()
