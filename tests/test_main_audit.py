import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rich.console import Console

from workout_logger.database import Database, SCHEMA_VERSION
from workout_logger.main import manage_exercises, run, session_summary
from workout_logger.models import WorkoutEntryData
from workout_logger.services.workout_service import WorkoutService
from workout_logger.ui.management_menu import _show_revisions


class MainAuditTests(unittest.TestCase):
    def test_cancelling_resistance_unit_does_not_create_an_exercise(self):
        service = Mock()
        with patch('workout_logger.main.select', side_effect=['add','resistance_units','back']), patch('workout_logger.main.text', side_effect=['Band press','springs',None]), patch('workout_logger.main.header'):
            manage_exercises(service)
        service.add_exercise.assert_not_called()

    def test_literal_brackets_in_history_names_results_notes_and_revisions(self):
        with tempfile.TemporaryDirectory() as td:
            service = WorkoutService(Database(Path(td) / 'history.db'))
            eid = service.add_exercise('Curl [/bad]', 'custom')
            sid = service.start_new_template_session('2026-10-01', '80')
            data = WorkoutEntryData(exercise_id=eid, exercise_name='Curl [/bad]', measurement_type='custom', unit='units', custom_result='Result [/bad]', notes='Note [/bad]', rir_code='1')
            entry = service.save_entry(sid,data)
            service.finalize_session(sid)
            data.notes = 'Corrected [/bad]'
            service.correct_entry(entry,data)
            output = io.StringIO()
            console = Console(file=output, width=180, color_system=None)
            with patch('workout_logger.main.console', console):
                session_summary(service,sid)
            with patch('workout_logger.ui.management_menu.console',console), patch('workout_logger.ui.management_menu.select',return_value=None):
                _show_revisions(service,sid)
            rendered = output.getvalue()
            for literal in ('Curl [/bad]','Result [/bad]','Corrected [/bad]','Note [/bad]'):
                self.assertIn(literal,rendered)

    def test_future_schema_does_not_offer_corruption_recovery_or_modify_database(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = Database(root / 'workouts.db')
            db.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'",(str(SCHEMA_VERSION+1),))
            original = hashlib.sha256(db.path.read_bytes()).hexdigest()
            with patch('workout_logger.main.app_paths',return_value=(db.path,root/'history.xlsx')), patch('workout_logger.main.recover_startup_database') as recovery, patch('workout_logger.main.select') as menu, patch('workout_logger.main.console'):
                run()
            recovery.assert_not_called()
            menu.assert_not_called()
            self.assertEqual(hashlib.sha256(db.path.read_bytes()).hexdigest(),original)

    def test_missing_or_invalid_active_recovery_does_not_create_empty_default_database(self):
        for content in (json.dumps({'database':'recovered-missing.db'}),'broken json',b'\xff'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                if isinstance(content,bytes):
                    (root/'active-recovery.json').write_bytes(content)
                else:
                    (root/'active-recovery.json').write_text(content)
                with patch.dict('os.environ',{'WORKOUT_LOGGER_DATA_DIR':str(root)}), patch('workout_logger.main.recover_startup_database',return_value=None) as recovery, patch('workout_logger.main.console'):
                    run()
                recovery.assert_called_once()
                self.assertFalse((root/'workouts.db').exists())

    def test_interrupt_during_startup_recovery_exits_without_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = root/'workouts.db'
            bad.write_bytes(b'broken database preserved')
            with patch('workout_logger.main.app_paths',return_value=(bad,root/'history.xlsx')), patch('workout_logger.main.select',side_effect=KeyboardInterrupt()), patch('workout_logger.main.console'):
                run()
            self.assertEqual(bad.read_bytes(),b'broken database preserved')


if __name__ == '__main__':
    unittest.main()
