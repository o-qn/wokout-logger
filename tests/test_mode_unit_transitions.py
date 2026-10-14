import io
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from workout_logger.database import Database
from workout_logger.models import WorkoutEntryData
from workout_logger.notation import comparison_message, format_entry
from workout_logger.services.workout_service import WorkoutService
from workout_logger.ui.prompts import prompt_entry


class ModeUnitTransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = WorkoutService(Database(Path(self.tmp.name) / 'workouts.db'))

    def tearDown(self):
        self.tmp.cleanup()

    def test_amrap_to_added_weight_defaults_to_kg_preserving_old_history_and_active_session(self):
        s = self.service
        eid = s.add_exercise('Explosive pull-up', 'amrap')
        old = s.start_new_template_session('2026-10-10', '83.5')
        s.save_entry(old, WorkoutEntryData(exercise_id=eid, exercise_name='Explosive pull-up', measurement_type='amrap', unit='reps', reps=7, rir_code='1'))
        s.finalize_session(old)
        history_before = s.session_entries(old)
        tid = s.get_session(old)['template_id']
        running = s.start_existing_template_session(tid, '2026-10-11', '83.5')
        s.update_exercise(eid, measurement_type='bodyweight_plus')
        self.assertEqual(s.get_exercise(eid)['default_unit'], 'kg')
        self.assertEqual(s.session_entries(old), history_before)
        self.assertEqual(s.get_session_exercise(running, eid)['measurement_type'], 'amrap')
        self.assertEqual(s.get_session_exercise(running, eid)['default_unit'], 'reps')
        new = s.start_existing_template_session(tid, '2026-10-12', '83.5')
        frozen = s.get_session_exercise(new, eid)
        self.assertEqual((frozen['measurement_type'], frozen['default_unit']), ('bodyweight_plus','kg'))
        self.assertIsNone(frozen['default_external_load'])
        with patch('workout_logger.ui.prompts.ask_decimal', return_value=Decimal('0')) as weight, patch('workout_logger.ui.prompts.ask_int', return_value=7) as reps, patch('workout_logger.ui.prompts.ask_rir', return_value='1'), patch('workout_logger.ui.prompts.text', return_value=''):
            data = prompt_entry(frozen, '83.5', defaults=frozen, update_settings=False)
        weight.assert_called_once_with('Added weight (kg):', None)
        reps.assert_called_once_with('Reps:', None, 0)
        entry_id = s.save_entry(new, data)
        self.assertEqual(format_entry(s.get_entry(entry_id)), 'BW+0kg × 7 @1')
        message = comparison_message(s.get_entry(entry_id), history_before[0])
        self.assertIn('Measurement changed', message)
        self.assertIn('AMRAP: 7 @1', message)
        self.assertNotIn('0reps', message)

    def test_invalid_weight_unit_rejected_before_backup_or_change(self):
        s = self.service
        eid = s.add_exercise('Pull-up', 'amrap')
        before = s.get_exercise(eid)
        with patch.object(s, '_backup_before_change') as backup:
            for bad in ('reps','seconds','springs',''):
                with self.assertRaises(ValueError):
                    s.update_exercise(eid, measurement_type='bodyweight_plus', default_unit=bad)
            backup.assert_not_called()
        self.assertEqual(s.get_exercise(eid), before)
        with self.assertRaises(ValueError):
            s.add_exercise('Invalid', 'external_weight', 'reps')

    def test_switching_weight_modes_preserves_lb_and_clears_incompatible_defaults(self):
        s = self.service
        eid = s.add_exercise('Pull-up', 'external_weight', 'lbs')
        sid = s.start_new_template_session('2026-10-10','80')
        s.save_entry(sid, WorkoutEntryData(exercise_id=eid, exercise_name='Pull-up', measurement_type='external_weight', unit='lb', external_load=Decimal('20'), reps=7, rir_code='1'))
        tid = s.get_session(sid)['template_id']
        s.update_exercise(eid, measurement_type='bodyweight_plus')
        self.assertEqual(s.get_exercise(eid)['default_unit'],'lb')
        self.assertIsNone(s.template_exercises(tid)[0]['default_external_load'])

    def test_started_legacy_session_remains_loggable_after_configuration_repair(self):
        s = self.service
        eid = s.add_exercise('Pull-up', 'amrap')
        # Reproduce pre-fix configuration without using the new validated API.
        s.db.execute("UPDATE exercises SET measurement_type='bodyweight_plus',default_unit='reps' WHERE id=?", (eid,))
        running = s.start_new_template_session('2026-10-10','80')
        s.update_exercise(eid, default_unit='kg')
        reopened = WorkoutService(Database(s.db.path))
        self.assertEqual(reopened.get_session_exercise(running,eid)['default_unit'],'reps')
        reopened.save_entry(running, WorkoutEntryData(exercise_id=eid,exercise_name='Pull-up',measurement_type='bodyweight_plus',unit='reps',external_load=Decimal('0'),reps=7,rir_code='1'))
        self.assertEqual(reopened.session_entries(running)[0]['unit'],'reps')


if __name__ == '__main__':
    unittest.main()
