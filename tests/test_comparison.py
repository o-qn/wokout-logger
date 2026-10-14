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


class ComparisonSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = WorkoutService(Database(Path(self.tmp.name) / 'workouts.db'))
        self.eid = self.service.add_exercise('Curl', 'external_weight')

    def tearDown(self):
        self.tmp.cleanup()

    def saved(self, day, load, reps=6):
        sid = self.service.start_new_template_session(f'2026-10-{day:02d}', '80')
        self.service.save_entry(sid, WorkoutEntryData(
            exercise_id=self.eid, exercise_name='Curl', measurement_type='external_weight',
            external_load=Decimal(load), reps=reps, rir_code='0',
        ))
        self.service.finalize_session(sid)
        return sid

    def test_choose_latest_or_last_matching_normalized_load_and_show_source(self):
        first = self.saved(1, '10.0')
        second = self.saved(2, '12')
        current = self.saved(3, '10.00', 7)
        self.saved(4, '10', 20)
        entry = self.service.session_entries(current)[0]
        self.assertEqual(self.service.comparison_entry(entry, current)['session_id'], second)
        self.service.set_comparison_mode('matching', current)
        self.assertEqual(self.service.comparison_entry(entry, current)['session_id'], first)
        output = io.StringIO()
        with patch('workout_logger.main.console', Console(file=output, width=220, color_system=None)):
            session_summary(self.service, current)
        text = output.getvalue()
        self.assertIn('2026-10-01', text)
        self.assertIn('10kg × 6 @0', text)
        self.assertIn('+1 rep', text)
        self.assertNotIn('20 @0', text)

    def test_matching_does_not_fall_back_to_different_settings(self):
        self.saved(1, '12')
        current = self.saved(2, '10')
        self.assertIsNone(self.service.comparison_entry(self.service.session_entries(current)[0], current, 'matching'))

    def test_default_is_snapshotted_and_saved_view_is_independent(self):
        self.service.set_comparison_mode('matching')
        first = self.saved(1, '10')
        self.service.set_comparison_mode('previous')
        second = self.saved(2, '10')
        self.assertEqual(self.service.get_session(first)['comparison_mode'], 'matching')
        self.assertEqual(self.service.get_session(second)['comparison_mode'], 'previous')
        self.service.set_comparison_mode('previous', first)
        self.assertEqual(self.service.get_session(first)['comparison_mode'], 'previous')
        with self.assertRaises(ValueError):
            self.service.set_comparison_mode('unknown')

    def test_preview_uses_frozen_defaults_instead_of_current_definition(self):
        first = self.saved(1, '10')
        current = self.service.start_existing_template_session(self.service.get_session(first)['template_id'], '2026-10-02', '80')
        frozen = self.service.get_session_exercise(current, self.eid)
        self.service.set_comparison_mode('matching', current)
        self.service.update_exercise(self.eid, measurement_type='isometric', default_unit='sec')
        self.assertEqual(self.service.comparison_preview(frozen, frozen, current)['session_id'], first)


if __name__ == '__main__':
    unittest.main()
