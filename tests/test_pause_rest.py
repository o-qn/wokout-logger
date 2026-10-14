import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.application import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from workout_logger.database import Database
from workout_logger.models import WorkoutEntryData
from workout_logger.services.workout_service import WorkoutService
from workout_logger.ui.prompts import ask_decimal, prompt_entry, text
from workout_logger.ui.timers import SessionTimer, active_session_timer
from workout_logger.ui.timing_menu import pause_workout_menu, rest_settings_menu


class Clock:
    def __init__(self):
        self.wall = datetime(2026, 10, 14, 15, 0, tzinfo=timezone(timedelta(hours=3)))
        self.tick = 0.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.tick

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.tick += seconds


class PauseRestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "workouts.db")
        self.db.seed_exercises()
        self.service = WorkoutService(self.db)
        self.clock = Clock()
        self.service._timing_now = self.clock.now
        self.sid = self.service.start_new_template_session("2026-10-14", "80")
        self.db.execute("UPDATE workout_sessions SET started_at=? WHERE id=?", (self.clock.now().isoformat(), self.sid))

    def tearDown(self):
        self.temp.cleanup()

    def timer(self, notify=lambda: None):
        return SessionTimer(self.service, self.sid, now=self.clock.now, monotonic=self.clock.monotonic, notify=notify)

    def set_rest_start(self):
        self.db.execute("UPDATE workout_sessions SET rest_started_at=? WHERE id=?", (self.clock.now().isoformat(), self.sid))

    def test_pause_resume_excludes_interruptions_from_both_counters(self):
        self.clock.advance(30)
        self.set_rest_start()
        self.clock.advance(15)
        self.assertTrue(self.service.pause_session(self.sid))
        timer = self.timer()
        self.assertIn("Workout time: 00:00:45", timer.text())
        self.assertIn("Rest time: 00:00:15", timer.text())
        self.assertIn("PAUSED", timer.text())
        self.clock.advance(3600)
        self.assertEqual(self.service.session_elapsed_seconds(self.sid), 45)
        self.assertEqual(self.service.rest_elapsed_seconds(self.sid), 15)
        self.assertIn("Rest time: 00:00:15", timer.text())
        self.assertTrue(self.service.resume_session_timer(self.sid))
        timer.refresh()
        self.clock.advance(10)
        self.assertEqual(self.service.session_elapsed_seconds(self.sid), 55)
        self.assertEqual(self.service.rest_elapsed_seconds(self.sid), 25)
        self.assertIn("Workout time: 00:00:55", timer.text())
        self.assertNotIn("PAUSED", timer.text())

    def test_pause_is_idempotent_and_survives_reopening(self):
        self.clock.advance(40)
        self.assertTrue(self.service.pause_session(self.sid))
        self.assertFalse(self.service.pause_session(self.sid))
        self.clock.advance(10000)
        reopened = WorkoutService(Database(self.db.path))
        reopened._timing_now = self.clock.now
        self.assertEqual(reopened.session_elapsed_seconds(self.sid), 40)
        self.assertTrue(reopened.resume_session_timer(self.sid))
        self.assertFalse(reopened.resume_session_timer(self.sid))
        self.clock.advance(5)
        self.assertEqual(reopened.session_elapsed_seconds(self.sid), 45)
        self.assertEqual(len(self.db.fetchall("SELECT * FROM workout_pause_intervals")), 1)

    def test_rest_excludes_only_overlapping_pauses_after_rest_rewinds(self):
        self.clock.advance(10)
        self.service.pause_session(self.sid)
        self.clock.advance(20)
        self.service.resume_session_timer(self.sid)
        self.set_rest_start()  # A set after the first pause.
        self.clock.advance(10)
        self.service.pause_session(self.sid)
        self.clock.advance(40)
        self.service.resume_session_timer(self.sid)
        self.clock.advance(20)
        self.assertEqual(self.service.rest_elapsed_seconds(self.sid), 30)
        # Undo may restore a rest start during/before an earlier pause.
        original = self.clock.now() - timedelta(seconds=85)
        self.db.execute("UPDATE workout_sessions SET rest_started_at=? WHERE id=?", (original.isoformat(), self.sid))
        self.assertEqual(self.service.rest_elapsed_seconds(self.sid), 30)

    def test_finalizing_while_paused_freezes_duration_and_preserves_entries(self):
        ex = self.service.session_exercises(self.sid)[0]
        self.service.save_entry(self.sid, WorkoutEntryData(
            exercise_id=ex["id"], exercise_name=ex["name"], measurement_type=ex["measurement_type"],
            unit=ex["default_unit"], bodyweight=Decimal("80"), external_load=Decimal("10"), reps=6, rir_code="1",
        ))
        before = self.service.session_entries(self.sid)
        self.clock.advance(91)
        self.service.pause_session(self.sid)
        self.clock.advance(1000)
        self.service.finalize_session(self.sid, force_incomplete=True)
        session = self.service.get_session(self.sid)
        self.assertEqual(float(session["workout_duration_seconds"]), 91)
        self.assertIsNone(session["paused_at"])
        self.assertEqual(before, self.service.session_entries(self.sid))
        self.clock.advance(999)
        self.assertEqual(self.service.session_elapsed_seconds(self.sid), 91)
        with self.assertRaises(ValueError):
            self.service.pause_session(self.sid)

    def test_paused_workout_rejects_logging_and_editing(self):
        ex = self.service.session_exercises(self.sid)[0]
        data = WorkoutEntryData(
            exercise_id=ex["id"], exercise_name=ex["name"], measurement_type=ex["measurement_type"],
            unit=ex["default_unit"], bodyweight=Decimal("80"), external_load=Decimal("10"), reps=6,
        )
        eid = self.service.save_entry(self.sid, data)
        self.service.pause_session(self.sid)
        with self.assertRaises(ValueError):
            self.service.edit_entry(eid, data)
        with self.assertRaises(ValueError):
            self.service.save_entry(self.sid, data)

    def test_reminders_are_off_by_default_and_future_defaults_are_snapshotted(self):
        self.assertEqual(self.service.get_rest_reminder_settings(), {"target_seconds": None, "sound": False})
        self.service.configure_rest_reminder(target_seconds=90, sound=True)
        self.assertEqual(self.service.get_rest_reminder_settings(self.sid), {"target_seconds": None, "sound": False})
        next_sid = self.service.start_new_template_session("2026-10-15", "80")
        self.assertEqual(self.service.get_rest_reminder_settings(next_sid), {"target_seconds": 90, "sound": True})
        self.service.configure_rest_reminder(target_seconds=None)
        self.assertEqual(self.service.get_rest_reminder_settings(next_sid), {"target_seconds": 90, "sound": True})
        for invalid in (0, -1, 1.2, "60", True):
            with self.assertRaises(ValueError):
                self.service.configure_rest_reminder(target_seconds=invalid)

    def test_visible_reminder_keeps_counting_and_sound_fires_once_per_rest_across_reopen(self):
        bells = []
        self.service.configure_rest_reminder(self.sid, target_seconds=20, sound=True)
        self.set_rest_start()
        timer = self.timer(lambda: bells.append("bell"))
        self.assertIn("Rest target: 00:00:20", timer.text())
        self.clock.advance(20)
        self.assertIn("Rest complete", timer.text())
        for _ in range(5):
            timer.text()
            timer.refresh()
        self.assertEqual(bells, ["bell"])
        self.clock.advance(5)
        self.assertIn("Rest time: 00:00:25", timer.text())
        reopened = self.timer(lambda: bells.append("reopen bell"))
        self.assertIn("Rest complete", reopened.text())
        self.assertEqual(bells, ["bell"])
        self.set_rest_start()
        reopened.refresh()
        self.clock.advance(20)
        reopened.text()
        self.assertEqual(bells, ["bell", "reopen bell"])

    def test_undo_restores_already_alerted_set_without_repeating_its_bell(self):
        bells = []
        self.service.configure_rest_reminder(self.sid, target_seconds=20, sound=True)
        exercises = self.service.session_exercises(self.sid)[:2]

        def log(exercise):
            return self.service.save_entry(self.sid, WorkoutEntryData(
                exercise_id=exercise["id"], exercise_name=exercise["name"],
                measurement_type=exercise["measurement_type"], unit=exercise["default_unit"],
                bodyweight=Decimal("80"), external_load=Decimal("10"), reps=6, rir_code="1",
            ))

        first_id = log(exercises[0])
        first_start = self.service.get_session(self.sid)["rest_started_at"]
        timer = self.timer(lambda: bells.append("bell"))
        self.clock.advance(20)
        self.assertIn("Rest complete", timer.text())
        self.assertEqual(self.service.get_entry(first_id)["rest_alerted"], 1)
        second_id = log(exercises[1])
        timer.refresh()
        self.clock.advance(20)
        self.assertIn("Rest complete", timer.text())
        self.assertEqual(self.service.get_entry(second_id)["rest_alerted"], 1)
        self.assertEqual(bells, ["bell", "bell"])
        self.assertEqual(self.service.undo_last_entry(self.sid)["id"], second_id)
        session = self.service.get_session(self.sid)
        self.assertEqual(session["rest_started_at"], first_start)
        self.assertEqual(session["rest_alerted_for"], first_start)
        timer.refresh()
        self.assertIn("Rest time: 00:00:40", timer.text())
        self.assertIn("Rest complete", timer.text())
        reopened = self.timer(lambda: bells.append("unexpected"))
        reopened.text()
        self.assertEqual(bells, ["bell", "bell"])
        self.assertEqual(self.service.get_entry(first_id)["rest_alerted"], 1)

    def test_visible_only_reminder_and_paused_rest_never_ring(self):
        bells = []
        self.service.configure_rest_reminder(self.sid, target_seconds=10, sound=False)
        self.set_rest_start()
        timer = self.timer(lambda: bells.append("bell"))
        self.clock.advance(10)
        self.assertIn("Rest complete", timer.text())
        self.assertEqual(bells, [])
        self.service.configure_rest_reminder(self.sid, target_seconds=20, sound=True)
        self.service.pause_session(self.sid)
        timer.refresh()
        self.clock.advance(100)
        self.assertNotIn("Rest complete", timer.text())
        self.assertEqual(bells, [])
        self.service.resume_session_timer(self.sid)
        timer.refresh()
        self.clock.advance(10)
        self.assertIn("Rest complete", timer.text())
        self.assertEqual(bells, ["bell"])

    def test_text_entry_retains_literal_p_and_default_in_live_timer_layout(self):
        with create_pipe_input() as pipe:
            def application(**kwargs):
                return Application(**kwargs, input=pipe, output=DummyOutput())
            pipe.send_text(" pizza\r")
            with patch("workout_logger.ui.prompts.Application", side_effect=application):
                with active_session_timer(self.service, self.sid, now=self.clock.now, monotonic=self.clock.monotonic):
                    self.assertEqual(text("Notes:", "p"), "p pizza")

    def test_pause_menu_requires_explicit_resume_or_saved_leave(self):
        with patch("workout_logger.ui.timing_menu.select", side_effect=[None, "resume"]):
            self.assertEqual(pause_workout_menu(self.service, self.sid), "resume")
        self.assertIsNone(self.service.get_session(self.sid)["paused_at"])
        with patch("workout_logger.ui.timing_menu.select", return_value="leave"):
            self.assertEqual(pause_workout_menu(self.service, self.sid), "leave")
        self.assertIsNotNone(self.service.get_session(self.sid)["paused_at"])

    def test_rest_menu_applies_target_and_can_turn_it_off(self):
        with patch("workout_logger.ui.timing_menu.select", side_effect=["set", "back"]), patch(
            "workout_logger.ui.timing_menu.ask_int", return_value=75,
        ), patch("workout_logger.ui.timing_menu.confirm", return_value=True):
            rest_settings_menu(self.service, self.sid)
        self.assertEqual(self.service.get_rest_reminder_settings(self.sid), {"target_seconds": 75, "sound": True})
        with patch("workout_logger.ui.timing_menu.select", side_effect=["off", "back"]):
            rest_settings_menu(self.service, self.sid)
        self.assertEqual(self.service.get_rest_reminder_settings(self.sid), {"target_seconds": None, "sound": False})


    def test_escape_from_actual_load_or_reps_prompt_never_saves_partial_entry(self):
        from workout_logger.main import _log_one

        exercise = self.service.session_exercises(self.sid)[0]
        for cancelled_field in ("load", "reps"):
            with self.subTest(cancelled_field=cancelled_field), create_pipe_input() as pipe, ExitStack() as stack:
                def application(**kwargs):
                    return Application(**kwargs, input=pipe, output=DummyOutput())

                pipe.send_text("\x1b")
                stack.enter_context(patch("workout_logger.ui.prompts.Application", side_effect=application))
                stack.enter_context(patch("workout_logger.main._entry_action", side_effect=[("update", None), ("end", None)]))
                save = stack.enter_context(patch.object(self.service, "save_entry", wraps=self.service.save_entry))
                if cancelled_field == "reps":
                    stack.enter_context(patch("workout_logger.ui.prompts.ask_decimal", return_value=Decimal("10")))
                stack.enter_context(active_session_timer(self.service, self.sid, now=self.clock.now, monotonic=self.clock.monotonic))
                self.assertEqual(_log_one(self.service, self.sid, exercise, exercise, True), "end")
                save.assert_not_called()
                self.assertEqual(self.service.session_entries(self.sid), [])

    def test_each_required_field_cancels_the_entry_immediately(self):
        base = {"id": 1, "name": "Exercise", "measurement_type": "external_weight", "default_unit": "kg"}
        cases = [
            ("external_weight", {"ask_decimal": None}),
            ("bodyweight_plus", {"ask_decimal": None}),
            ("assistance", {"ask_decimal": None}),
            ("bodyweight", {"ask_int": None}),
            ("amrap", {"ask_int": None}),
            ("isometric", {"ask_decimal": None}),
            ("isometric", {"ask_decimal": Decimal("20"), "ask_int": None}),
            ("resistance_units", {"text": None}),
            ("custom", {"text": None}),
            ("external_weight", {"ask_decimal": Decimal("10"), "ask_int": None}),
            ("external_weight", {"ask_decimal": Decimal("10"), "ask_int": 6, "ask_rir": None}),
            ("external_weight", {"ask_decimal": Decimal("10"), "ask_int": 6, "ask_rir": "1", "text": None}),
        ]
        for mode, answers in cases:
            with self.subTest(mode=mode, answers=answers), ExitStack() as stack:
                for name, answer in answers.items():
                    stack.enter_context(patch(f"workout_logger.ui.prompts.{name}", return_value=answer))
                self.assertIsNone(prompt_entry({**base, "measurement_type": mode}, "80"))
        with patch("workout_logger.ui.prompts.ask_decimal", side_effect=[Decimal("20"), None]), patch(
            "workout_logger.ui.prompts.ask_int", return_value=2,
        ):
            self.assertIsNone(prompt_entry({**base, "measurement_type": "isometric"}, "80"))
        # Blank notes are an intentional valid value, unlike Escape/None.
        with patch("workout_logger.ui.prompts.ask_decimal", return_value=Decimal("10")), patch(
            "workout_logger.ui.prompts.ask_int", return_value=6,
        ), patch("workout_logger.ui.prompts.ask_rir", return_value="1"), patch(
            "workout_logger.ui.prompts.text", return_value="",
        ):
            self.assertEqual(prompt_entry(base, "80").notes, "")

    def test_decimal_prompt_rejects_nan_and_infinity(self):
        with patch("workout_logger.ui.prompts.text", side_effect=["NaN", "Infinity", "-Infinity", "sNaN", "5.5"]), patch(
            "workout_logger.ui.prompts.console.print",
        ):
            self.assertEqual(ask_decimal("Load:"), Decimal("5.5"))


if __name__ == "__main__":
    unittest.main()
