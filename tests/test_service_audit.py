import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from workout_logger.database import Database
from workout_logger.models import WorkoutEntryData
from workout_logger.services.workout_service import WorkoutService


class ServiceAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'history.db')
        self.service = WorkoutService(self.db)
        self.ids = [self.service.add_exercise(name, 'external_weight') for name in ('Curl', 'Row')]
        self.data = WorkoutEntryData(
            exercise_id=self.ids[0], exercise_name='Curl', measurement_type='external_weight',
            external_load=Decimal('10'), reps=6, rir_code='1',
        )

    def tearDown(self):
        self.tmp.cleanup()

    def new_session(self):
        return self.service.start_new_template_session('2026-10-01', '80')

    def existing_session(self):
        first = self.new_session()
        self.service.save_entry(first, self.data)
        self.service.save_entry(first, replace(self.data, exercise_id=self.ids[1], exercise_name='Row', external_load=Decimal('15')))
        self.service.finalize_session(first)
        tid = self.service.get_session(first)['template_id']
        return tid, self.service.start_existing_template_session(tid, '2026-10-02', '80')

    def test_new_session_failure_does_not_leave_empty_template(self):
        with patch.object(self.service, 'get_rest_reminder_settings', side_effect=RuntimeError('settings unavailable')):
            with self.assertRaises(RuntimeError):
                self.new_session()
        self.assertEqual(self.service.templates(), [])
        self.assertEqual(self.service.unfinished_sessions(), [])
        self.assertEqual(self.db.fetchone('SELECT COUNT(*) AS n FROM bodyweight_entries')['n'], 0)
        self.assertEqual(self.service.next_template_label(), 'A')

    def test_invalid_start_values_do_not_create_templates_sessions_or_weigh_ins(self):
        for day, weight in [('2026-02-30', '80'), ('bad', '80'), ('2026-10-01', '-1'), ('2026-10-01', 'NaN')]:
            with self.subTest(day=day, weight=weight), self.assertRaises(ValueError):
                self.service.start_new_template_session(day, weight)
        self.assertEqual(self.service.templates(), [])
        self.assertEqual(self.service.unfinished_sessions(), [])
        self.assertEqual(self.db.fetchone('SELECT COUNT(*) AS n FROM bodyweight_entries')['n'], 0)

    def test_active_edit_cannot_overwrite_another_exercises_defaults(self):
        tid, sid = self.existing_session()
        eid = self.service.save_entry(sid, self.data)
        before_entry = self.service.get_entry(eid)
        before_template = self.service.template_exercises(tid)
        with self.assertRaises(ValueError):
            self.service.edit_entry(eid, replace(self.data, exercise_id=self.ids[1], external_load=Decimal('99')))
        self.assertEqual(self.service.get_entry(eid), before_entry)
        self.assertEqual(self.service.template_exercises(tid), before_template)

    def test_invalid_active_values_cannot_be_saved_or_edit_a_valid_result(self):
        sid = self.new_session()
        invalid = [replace(self.data, external_load=Decimal('NaN')),
                   replace(self.data, external_load=Decimal('Infinity')),
                   replace(self.data, external_load=Decimal('-1')), replace(self.data, reps=-1),
                   replace(self.data, reps=1.5), replace(self.data, external_load=None),
                   replace(self.data, rir_code='-1'), replace(self.data, rir_code='1.5')]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.service.save_entry(sid, data)
        self.assertEqual(self.service.session_entries(sid), [])
        self.assertIsNone(self.service.get_session(sid)['rest_started_at'])
        eid = self.service.save_entry(sid, self.data)
        before = self.service.get_entry(eid)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.service.edit_entry(eid, data)
        self.assertEqual(self.service.get_entry(eid), before)

    def test_failure_flag_matches_normalized_rir_on_save_and_edit(self):
        sid = self.new_session()
        eid = self.service.save_entry(sid, replace(self.data, rir_code=' f ', failure=False))
        self.assertEqual((self.service.get_entry(eid)['rir_code'], self.service.get_entry(eid)['failure']), ('F', 1))
        self.service.edit_entry(eid, replace(self.data, rir_code='0', failure=True))
        self.assertEqual((self.service.get_entry(eid)['rir_code'], self.service.get_entry(eid)['failure']), ('0', 0))

    def test_logging_rechecks_pause_state_before_writing(self):
        sid = self.new_session()
        original_get = self.service.get_session

        def read_then_pause(session_id):
            snapshot = original_get(session_id)
            self.service.pause_session(session_id)
            return snapshot

        with patch.object(self.service, 'get_session', side_effect=read_then_pause):
            with self.assertRaises(ValueError):
                self.service.save_entry(sid, self.data)
        self.assertEqual(self.service.session_entries(sid), [])
        self.assertIsNone(original_get(sid)['rest_started_at'])

    def test_active_edits_and_undo_recheck_saved_state_before_writing(self):
        for action in ('edit', 'undo'):
            with self.subTest(action=action):
                sid = self.new_session()
                eid = self.service.save_entry(sid, self.data)
                original = self.service.get_entry(eid)
                original_get = self.service.get_session

                def read_then_finish(session_id):
                    snapshot = original_get(session_id)
                    # Simulate another process finishing after the menu read.
                    self.db.execute("UPDATE workout_sessions SET status='complete' WHERE id=?", (session_id,))
                    return snapshot

                with patch.object(self.service, 'get_session', side_effect=read_then_finish):
                    with self.assertRaises(ValueError):
                        if action == 'edit':
                            self.service.edit_entry(eid, replace(self.data, reps=99))
                        else:
                            self.service.undo_last_entry(sid)
                self.assertEqual(self.service.get_entry(eid), original)

    def test_undo_existing_entry_restores_original_next_session_defaults(self):
        tid, sid = self.existing_session()
        before = self.service.template_exercises(tid)
        self.service.save_entry(sid, replace(self.data, external_load=Decimal('99')))
        self.service.undo_last_entry(sid)
        self.assertEqual(self.service.template_exercises(tid), before)
        next_sid = self.service.start_existing_template_session(tid, '2026-10-03', '80')
        self.assertEqual(self.service.get_session_exercise(next_sid, self.ids[0])['default_external_load'], '10')

    def test_undo_does_not_overwrite_defaults_logged_in_another_session(self):
        tid, sid = self.existing_session()
        other = self.service.start_existing_template_session(tid, '2026-10-03', '80')
        self.service.save_entry(sid, replace(self.data, external_load=Decimal('20')))
        self.service.save_entry(other, replace(self.data, external_load=Decimal('30')))
        self.service.undo_last_entry(sid)
        self.assertEqual(self.service.template_exercises(tid)[0]['default_external_load'], '30')

    def test_undo_preserves_same_weight_logged_later_in_another_session(self):
        tid, sid = self.existing_session()
        other = self.service.start_existing_template_session(tid, '2026-10-03', '80')
        changed = replace(self.data, external_load=Decimal('20'))
        self.service.save_entry(sid, changed)
        self.service.save_entry(other, changed)
        self.service.undo_last_entry(sid)
        self.assertEqual(self.service.template_exercises(tid)[0]['default_external_load'], '20')

    def test_undo_uses_actual_logging_order_in_existing_template(self):
        tid, sid = self.existing_session()
        row_id = self.service.save_entry(sid, replace(self.data, exercise_id=self.ids[1], exercise_name='Row'))
        curl_id = self.service.save_entry(sid, self.data)
        self.assertEqual(self.service.undo_last_entry(sid)['id'], curl_id)
        self.assertEqual([row['id'] for row in self.service.session_entries(sid)], [row_id])

    def test_turning_last_logged_entry_into_skip_restores_previous_rest(self):
        sid = self.new_session()
        self.service.save_entry(sid, self.data)
        first_rest = self.service.get_session(sid)['rest_started_at']
        self.service.mark_rest_reminder_delivered(sid, first_rest)
        row_data = replace(self.data, exercise_id=self.ids[1], exercise_name='Row')
        eid = self.service.save_entry(sid, row_data)
        self.service.edit_entry(eid, replace(row_data, skipped=True))
        session = self.service.get_session(sid)
        self.assertEqual(session['rest_started_at'], first_rest)
        self.assertEqual(session['rest_alerted_for'], first_rest)
        self.assertIsNone(self.service.get_entry(eid)['logged_at'])

    def test_skip_then_relog_can_ring_rest_reminder_again(self):
        sid = self.new_session()
        eid = self.service.save_entry(sid, self.data)
        rest = self.service.get_session(sid)['rest_started_at']
        self.assertTrue(self.service.mark_rest_reminder_delivered(sid, rest))
        self.service.edit_entry(eid, replace(self.data, skipped=True))
        self.assertIsNone(self.service.get_session(sid)['rest_started_at'])
        self.service.edit_entry(eid, self.data)
        self.assertEqual(self.service.get_entry(eid)['rest_alerted'], 0)
        new_rest = self.service.get_session(sid)['rest_started_at']
        self.assertTrue(self.service.mark_rest_reminder_delivered(sid, new_rest))

    def test_configuration_changed_away_and_back_does_not_accept_stale_defaults(self):
        tid, sid = self.existing_session()
        self.service.update_exercise(self.ids[0], default_unit='lb')
        self.service.update_exercise(self.ids[0], default_unit='kg')
        self.service.save_entry(sid, replace(self.data, external_load=Decimal('99')))
        self.assertIsNone(self.service.template_exercises(tid)[0]['default_external_load'])

    def test_removed_template_is_not_modified_by_further_logging(self):
        sid = self.new_session()
        tid = self.service.get_session(sid)['template_id']
        self.service.remove_template(tid)
        before = self.service.template_exercises(tid)
        self.service.save_entry(sid, self.data)
        self.assertEqual(self.service.template_exercises(tid), before)
        self.assertEqual(len(self.service.session_entries(sid)), 1)

    def test_config_change_during_new_template_does_not_stop_later_membership_or_undo(self):
        sid = self.new_session()
        tid = self.service.get_session(sid)['template_id']
        first = self.service.save_entry(sid, self.data)
        self.service.update_exercise(self.ids[0], default_unit='lb')
        self.service.update_exercise(self.ids[0], default_unit='kg')
        self.service.edit_entry(first, replace(self.data, external_load=Decimal('99')))
        self.assertIsNone(self.service.template_exercises(tid)[0]['default_external_load'])
        row_data = replace(self.data, exercise_id=self.ids[1], exercise_name='Row')
        row = self.service.save_entry(sid, row_data)
        self.assertEqual([entry['exercise_id'] for entry in self.service.template_exercises(tid)], self.ids)
        self.assertEqual(self.service.undo_last_entry(sid)['id'], row)
        self.assertEqual([entry['exercise_id'] for entry in self.service.template_exercises(tid)], [self.ids[0]])
        self.service.save_entry(sid, row_data)
        self.service.finalize_session(sid)
        self.assertEqual([entry['exercise_id'] for entry in self.service.template_exercises(tid)], self.ids)

    def test_finalization_uses_one_timestamp_for_duration_and_completion(self):
        start = datetime(2026, 10, 1, 10, tzinfo=timezone(timedelta(hours=3)))
        with patch.object(self.service, '_timing_now', return_value=start):
            sid = self.new_session()
            self.service.save_entry(sid, self.data)
        finish = start + timedelta(seconds=91, microseconds=123000)
        with patch.object(self.service, '_timing_now', return_value=finish):
            self.service.finalize_session(sid, force_incomplete=True)
        session = self.service.get_session(sid)
        self.assertEqual(session['completed_at'], finish.isoformat(timespec='microseconds'))
        self.assertEqual(Decimal(session['workout_duration_seconds']), Decimal('91.123'))

    def test_discard_freezes_duration_and_closes_pause_without_deleting_entries(self):
        start = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
        with patch.object(self.service, '_timing_now', return_value=start):
            sid = self.new_session()
        with patch.object(self.service, '_timing_now', return_value=start + timedelta(seconds=10)):
            self.service.save_entry(sid, self.data)
        before = self.service.session_entries(sid)
        with patch.object(self.service, '_timing_now', return_value=start + timedelta(seconds=20)):
            self.service.pause_session(sid)
        finish = start + timedelta(seconds=80)
        with patch.object(self.service, '_timing_now', return_value=finish):
            self.service.discard_session(sid)
        session = self.service.get_session(sid)
        self.assertEqual(session['completed_at'], finish.isoformat(timespec='microseconds'))
        self.assertIsNone(session['paused_at'])
        self.assertEqual(self.service.session_elapsed_seconds(sid, now=finish + timedelta(hours=1)), 20)
        self.assertEqual(self.service.rest_elapsed_seconds(sid, now=finish + timedelta(hours=1)), 10)
        self.assertEqual(self.service.session_entries(sid), before)

    def test_all_history_is_visible_beyond_one_hundred_sessions(self):
        with patch.object(self.service.backups, 'create_backup'):
            first = self.new_session()
            self.service.save_entry(first, self.data)
            self.service.finalize_session(first, force_incomplete=True)
            tid = self.service.get_session(first)['template_id']
            for index in range(100):
                sid = self.service.start_existing_template_session(tid, '2026-10-02', '80')
                self.service.save_entry(sid, self.data)
                self.service.finalize_session(sid)
        self.assertEqual(len(self.service.workout_history()), 101)
        self.assertEqual(len(self.service.exercise_history(self.ids[0])), 101)
        self.assertEqual(len(self.service.bodyweight_history()), 101)
        self.assertEqual(self.service.workout_history()[-1]['id'], first)
        self.assertEqual(len(self.service.workout_history(limit=3)), 3)
        self.assertEqual(len(self.service.exercise_history(self.ids[0], limit=2)), 2)
        self.assertEqual(len(self.service.bodyweight_history(limit=1)), 1)

    def test_discard_restores_future_defaults_without_deleting_session_or_entries(self):
        tid, sid = self.existing_session()
        before = self.service.template_exercises(tid)
        self.service.save_entry(sid, replace(self.data, external_load=Decimal('99')))
        entries = self.service.session_entries(sid)
        self.service.discard_session(sid)
        self.assertEqual(self.service.template_exercises(tid), before)
        self.assertEqual(self.service.session_entries(sid), entries)
        self.assertEqual(self.service.get_session(sid)['status'], 'discarded')


if __name__ == '__main__':
    unittest.main()
