import unittest
from datetime import datetime, timedelta, timezone

from workout_logger.ui.timers import (
    SessionTimer,
    active_session_timer,
    format_elapsed,
    refresh_active_timer,
    timer_text,
)


class FakeClock:
    def __init__(self):
        self.wall = datetime(2026, 10, 14, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
        self.tick = 100.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.tick

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.tick += seconds


class FakeService:
    def __init__(self, **changes):
        self.session = {
            "status": "in_progress",
            "started_at": "2026-10-14T15:00:00",
            "rest_started_at": None,
            **changes,
        }
        self.reads = 0

    def get_session(self, session_id):
        self.reads += 1
        return self.session.copy() if self.session else None


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = FakeService()

    def timer(self, **changes):
        self.service.session.update(changes)
        return SessionTimer(self.service, 1, now=self.clock.now, monotonic=self.clock.monotonic)

    def test_new_workout_starts_at_zero_and_ticks_without_database_reads(self):
        timer = self.timer()
        self.assertEqual(timer.text(), "Workout time: 00:00:00  •  Rest time: --:--")
        self.clock.advance(65.9)
        self.assertEqual(timer.text(), "Workout time: 00:01:05  •  Rest time: --:--")
        timer.text()
        self.assertEqual(self.service.reads, 1)

    def test_resume_reads_elapsed_workout_and_rest_times(self):
        timer = self.timer(started_at="2026-10-14T13:59:55", rest_started_at="2026-10-14T14:59:40")
        self.assertEqual(timer.text(), "Workout time: 01:00:05  •  Rest time: 00:00:20")
        self.clock.advance(1)
        self.assertIn("Rest time: 00:00:21", timer.text())

    def test_logging_a_set_restarts_rest_without_restarting_workout(self):
        timer = self.timer()
        self.clock.advance(90)
        self.service.session["rest_started_at"] = self.clock.now().isoformat()
        timer.refresh()
        self.assertEqual(timer.text(), "Workout time: 00:01:30  •  Rest time: 00:00:00")
        self.clock.advance(45)
        self.service.session["rest_started_at"] = self.clock.now().isoformat()
        timer.refresh()
        self.assertEqual(timer.text(), "Workout time: 00:02:15  •  Rest time: 00:00:00")

    def test_undo_can_restore_previous_rest_or_clear_it(self):
        timer = self.timer(rest_started_at="2026-10-14T14:59:50")
        self.service.session["rest_started_at"] = "2026-10-14T14:59:00"
        timer.refresh()
        self.assertIn("Rest time: 00:01:00", timer.text())
        self.service.session["rest_started_at"] = None
        timer.refresh()
        self.assertIn("Rest time: --:--", timer.text())

    def test_running_counters_ignore_wall_clock_adjustments(self):
        timer = self.timer(rest_started_at="2026-10-14T14:59:50")
        self.clock.advance(5)
        self.clock.wall -= timedelta(hours=3)
        timer.refresh()
        self.assertEqual(timer.text(), "Workout time: 00:00:05  •  Rest time: 00:00:15")

    def test_iso_timezone_offsets_are_respected(self):
        self.clock.wall = datetime(2026, 10, 14, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
        timer = self.timer(started_at="2026-10-14T11:00:00Z", rest_started_at="2026-10-14T14:59:00+03:00")
        self.assertEqual(timer.text(), "Workout time: 01:00:00  •  Rest time: 00:01:00")

    def test_legacy_sqlite_timestamps_are_utc(self):
        self.clock.wall = datetime(2026, 10, 14, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
        timer = self.timer(started_at="2026-10-14 11:00:00", rest_started_at="2026-10-14 11:59:30")
        self.assertEqual(timer.text(), "Workout time: 01:00:00  •  Rest time: 00:00:30")

    def test_future_timestamps_start_at_zero_and_still_tick(self):
        timer = self.timer(started_at="2099-01-01T00:00:00", rest_started_at="2099-01-01T00:00:00")
        self.assertEqual(timer.text(), "Workout time: 00:00:00  •  Rest time: 00:00:00")
        self.clock.advance(2)
        self.assertEqual(timer.text(), "Workout time: 00:00:02  •  Rest time: 00:00:02")

    def test_malformed_timestamps_do_not_break_prompts(self):
        timer = self.timer(started_at="invalid", rest_started_at="also invalid")
        self.assertEqual(timer.text(), "Workout time: 00:00:00  •  Rest time: --:--")
        self.clock.advance(3)
        self.assertIn("Workout time: 00:00:03", timer.text())

    def test_inactive_or_missing_sessions_show_no_timer(self):
        for status in ("complete", "incomplete", "discarded"):
            self.assertEqual(self.timer(status=status).text(), "")
        self.service.session = None
        timer = SessionTimer(self.service, 1, now=self.clock.now, monotonic=self.clock.monotonic)
        self.assertEqual(timer.text(), "")

    def test_completion_hides_timer_on_refresh(self):
        timer = self.timer()
        self.service.session["status"] = "complete"
        timer.refresh()
        self.assertEqual(timer.text(), "")

    def test_context_cleanup_after_exception_and_nested_context(self):
        self.assertEqual(timer_text(), "")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            with active_session_timer(self.service, 1, now=self.clock.now, monotonic=self.clock.monotonic):
                self.assertIn("Workout time", timer_text())
                outer = timer_text()
                with active_session_timer(FakeService(status="complete"), 2):
                    self.assertEqual(timer_text(), "")
                self.assertEqual(timer_text(), outer)
                raise RuntimeError("cancelled")
        self.assertEqual(timer_text(), "")

    def test_refresh_only_changes_matching_active_context(self):
        with active_session_timer(self.service, 1, now=self.clock.now, monotonic=self.clock.monotonic):
            self.service.session["rest_started_at"] = self.clock.now().isoformat()
            refresh_active_timer(2)
            self.assertIn("Rest time: --:--", timer_text())
            refresh_active_timer(1)
            self.assertIn("Rest time: 00:00:00", timer_text())
        reads = self.service.reads
        refresh_active_timer()
        self.assertEqual(self.service.reads, reads)

    def test_duration_formatting_clamps_negative_values_and_supports_long_sessions(self):
        self.assertEqual(format_elapsed(-10), "00:00:00")
        self.assertEqual(format_elapsed(25 * 3600 + 61), "25:01:01")
        self.assertEqual(format_elapsed(float("nan")), "00:00:00")


if __name__ == "__main__":
    unittest.main()
