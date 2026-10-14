"""Pause controls and optional rest-reminder preferences."""

from __future__ import annotations

from .prompts import Choice, ask_int, confirm, console, select
from .timers import format_elapsed, refresh_active_timer


def rest_settings_menu(service, session_id: int | None = None) -> None:
    """Edit defaults for future sessions or a single current workout."""
    while True:
        settings = service.get_rest_reminder_settings(session_id)
        target = settings["target_seconds"]
        sound = settings["sound"]
        status = "Off" if target is None else f"{format_elapsed(target)} ({'sound on' if sound else 'visible reminder'})"
        scope = "this workout" if session_id is not None else "new workouts"
        choice = select(f"Rest reminder for {scope}: {status}", [
            Choice("Set target rest time", "set"),
            Choice("Turn reminders off", "off"),
            Choice("← Back", "back"),
        ])
        if choice in (None, "back"):
            return
        if choice == "off":
            service.configure_rest_reminder(session_id, target_seconds=None, sound=False)
            refresh_active_timer(session_id)
            console.print("[green]Rest reminders off. The rest timer keeps running.[/green]")
            continue
        seconds = ask_int("Target rest time (seconds):", default=target or 120, minimum=1)
        if seconds is None:
            continue
        use_sound = confirm("Also ring the terminal bell when rest is complete?", default=sound)
        if use_sound is None:
            continue
        service.configure_rest_reminder(session_id, target_seconds=seconds, sound=use_sound)
        refresh_active_timer(session_id)
        console.print("[green]Rest reminder saved. The counter continues after the target.[/green]")


def pause_workout_menu(service, session_id: int) -> str:
    """Keep a workout paused until Resume or return to the main menu.

    Returns ``resume`` after resuming and ``leave`` when the saved workout should
    stay paused. Escape leaves the overlay open so logging cannot continue while
    paused accidentally. Pauses remain saved if the app is closed.
    """
    session = service.get_session(session_id)
    if not session or session["status"] != "in_progress":
        return "leave"
    service.pause_session(session_id)
    refresh_active_timer(session_id)
    while True:
        choice = select("Workout paused. Workout and rest timers are stopped.", [
            Choice("Resume workout", "resume"),
            Choice("Rest reminder settings", "rest"),
            Choice("Keep paused and return to main menu", "leave"),
        ])
        if choice == "resume":
            service.resume_session_timer(session_id)
            refresh_active_timer(session_id)
            return "resume"
        if choice == "rest":
            rest_settings_menu(service, session_id)
        elif choice == "leave":
            return "leave"
