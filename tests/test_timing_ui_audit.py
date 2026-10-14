import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.shortcuts import PromptSession
from rich.console import Console

from workout_logger.database import Database
from workout_logger.services.timing import session_elapsed_from_record, timestamp_elapsed
from workout_logger.services.workout_service import WorkoutService
from workout_logger.ui.prompts import show_previous, text
from workout_logger.ui.timers import SessionTimer


class TimingAuditTests(unittest.TestCase):
    def test_legacy_finished_workout_does_not_keep_counting_in_history(self):
        session = {
            "status": "complete",
            "started_at": "2026-10-01 12:00:00",
            "completed_at": "2026-10-01 13:00:00",
            "paused_seconds": "120",
            "workout_duration_seconds": None,
        }
        for now in (
            datetime(2026, 10, 14, tzinfo=timezone.utc),
            datetime(2027, 1, 1, tzinfo=timezone(timedelta(hours=3))),
        ):
            with self.subTest(now=now):
                self.assertEqual(session_elapsed_from_record(session, now), 3480)

    def test_legacy_local_iso_finish_and_open_pause_use_same_local_zone(self):
        zone = timezone(timedelta(hours=3))
        now = datetime(2026, 10, 14, tzinfo=zone)
        session = {
            "status": "incomplete",
            "started_at": "2026-10-01T12:00:00",
            "completed_at": "2026-10-01T13:00:00",
            "paused_at": "2026-10-01T12:50:00",
            "paused_seconds": "60",
        }
        self.assertEqual(session_elapsed_from_record(session, now), 2940)

    def test_elapsed_time_uses_real_seconds_over_dst_transitions(self):
        zone = ZoneInfo("America/New_York")
        examples = (
            ("2026-03-08T01:30:00", datetime(2026, 3, 8, 3, 30, tzinfo=zone), 3600),
            ("2026-11-01T00:30:00", datetime(2026, 11, 1, 2, 30, tzinfo=zone), 10800),
        )
        for start, finish, expected in examples:
            with self.subTest(start=start):
                self.assertEqual(timestamp_elapsed(start, finish), expected)
                self.assertEqual(timestamp_elapsed(datetime.fromisoformat(start).replace(tzinfo=zone), finish), expected)

    def test_rest_excludes_real_pause_seconds_over_dst_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WorkoutService(Database(Path(directory) / "workouts.db"))
            sid = service.start_new_template_session("2026-03-08", "80")
            service.db.execute(
                "UPDATE workout_sessions SET rest_started_at=? WHERE id=?",
                ("2026-03-08T01:00:00", sid),
            )
            service.db.execute(
                "INSERT INTO workout_pause_intervals(session_id,paused_at,resumed_at) VALUES(?,?,?)",
                (sid, "2026-03-08T01:30:00", "2026-03-08T03:00:00"),
            )
            self.assertEqual(
                service.rest_elapsed_seconds(sid, now=datetime(2026, 3, 8, 3, 30, tzinfo=ZoneInfo("America/New_York"))),
                3600,
            )

    def test_database_busy_at_rest_target_does_not_break_prompt_or_lose_bell(self):
        class Service:
            def __init__(self):
                self.claims = 0

            def get_session(self, sid):
                return {
                    "status": "in_progress", "started_at": "2026-10-14T12:00:00+00:00",
                    "rest_started_at": "2026-10-14T12:00:00+00:00",
                    "rest_target_seconds": 60, "rest_alert_sound": 1,
                }

            def mark_rest_reminder_delivered(self, sid, start):
                self.claims += 1
                if self.claims == 1:
                    raise sqlite3.OperationalError("database is locked")
                return True

        bells = []
        service = Service()
        timer = SessionTimer(
            service, 1,
            now=lambda: datetime(2026, 10, 14, 12, 1, tzinfo=timezone.utc),
            monotonic=lambda: 100,
            notify=lambda: bells.append("bell"),
        )
        self.assertIn("Rest complete", timer.text())
        self.assertEqual(bells, [])
        self.assertIn("Rest complete", timer.text())
        timer.text()
        self.assertEqual(bells, ["bell"])
        self.assertEqual(service.claims, 2)


class PromptAuditTests(unittest.TestCase):
    def test_escape_cancels_text_without_an_active_workout_timer(self):
        with create_pipe_input() as pipe:
            pipe.send_text("\x1b")
            session = PromptSession(input=pipe, output=DummyOutput())

            def interactive_prompt(*args, **kwargs):
                return session.prompt(*args, **kwargs)

            with patch("workout_logger.ui.prompts.prompt", side_effect=interactive_prompt):
                self.assertIsNone(text("Corrected note:", "Original note"))

    def test_previous_custom_results_and_notes_render_as_literal_text(self):
        output = io.StringIO()
        with patch("workout_logger.ui.prompts.console", Console(file=output, width=160)):
            show_previous({
                "measurement_type": "custom", "custom_result": "7 [/bad]",
                "notes": "[bold]literal[/bold]", "bodyweight": "80", "session_date": "2026-10-10",
            }, "81")
        rendered = output.getvalue()
        self.assertIn("7 [/bad]", rendered)
        self.assertIn("[bold]literal[/bold]", rendered)
        self.assertIn("Previous date: 2026-10-10", rendered)


if __name__ == "__main__":
    unittest.main()
