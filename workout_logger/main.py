from __future__ import annotations

import os
import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.markup import escape

from .database import Database, UnsupportedSchemaError
from .backups import BackupManager, BackupError
from .exports.excel_export import ExcelExporter
from .models import WorkoutEntryData, MEASUREMENT_TYPES
from .measurements import measurement_unit
from .notation import format_entry, comparison_message, decimal_text
from .services.workout_service import WorkoutService
from .ui.prompts import (
    select, text, confirm, ask_date, ask_decimal, prompt_entry, show_previous,
    measurement_type_choices, Choice,
)
from .ui.timers import active_session_timer, refresh_active_timer, format_elapsed
from .ui.backup_menu import backup_menu
from .ui.management_menu import manage_templates, saved_workout_menu
from .ui.timing_menu import pause_workout_menu, rest_settings_menu
from .services.comparison import COMPARISON_MODES

console = Console()


class RecoverySelectionError(ValueError):
    def __init__(self, message, data_dir, export_path):
        super().__init__(message)
        self.data_dir = data_dir
        self.export_path = export_path


def app_paths():
    data_dir = Path(os.environ.get("WORKOUT_LOGGER_DATA_DIR", "~/.local/share/workout_logger")).expanduser()
    export_path = Path(os.environ.get("WORKOUT_LOGGER_EXPORT", "~/workout_history.xlsx")).expanduser()
    db_path = data_dir / "workouts.db"
    pointer = data_dir / "active-recovery.json"
    try:
        contents = pointer.read_text()
    except FileNotFoundError:
        return db_path, export_path
    except (OSError, UnicodeError) as exc:
        raise RecoverySelectionError(f"The selected recovery database could not be read: {exc}", data_dir, export_path) from exc
    try:
        filename = json.loads(contents)["database"]
        candidate = data_dir / filename
        if Path(filename).name != filename or not filename.startswith("recovered-") or not candidate.is_file():
            raise ValueError("the selected recovery file is missing or invalid")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RecoverySelectionError(f"Could not open the selected recovery database: {exc}", data_dir, export_path) from exc
    return candidate, export_path


def header():
    console.clear()
    console.print(Panel.fit("[bold]Terminal Workout Logger[/bold]", border_style="blue"))


def export_now(exporter: ExcelExporter, export_path: Path):
    try:
        path = exporter.export(export_path)
        console.print(f"[green]Excel updated:[/green] {escape(str(path))}")
    except Exception as exc:
        console.print(f"[yellow]Workout is safely stored in SQLite, but Excel export failed:[/yellow] {escape(str(exc))}")


def session_summary(service: WorkoutService, session_id: int):
    session = service.get_session(session_id)
    entries = service.session_entries(session_id)
    remaining = service.remaining_for_session(session_id)
    table = Table(title=f"{session['workout_name']} • {session['session_date']} • BW {session['bodyweight']} kg")
    table.add_column("#", justify="right")
    table.add_column("Exercise")
    table.add_column("Performance")
    table.add_column("Performance check")
    for i, entry in enumerate(entries, start=1):
        previous = service.comparison_entry(entry, session_id)
        check = "Skipped" if entry["skipped"] else comparison_message(entry, previous)
        if session.get("comparison_mode") == "matching":
            check = check.replace("vs previous session", "vs matching session")
        if previous and not entry["skipped"]:
            check = f"{format_entry({**previous, 'notes': ''})}\n{check}"
        empty = "No earlier matching load/settings" if session.get("comparison_mode") == "matching" else "No earlier performance"
        table.add_row(str(i), Text(entry["exercise_name_snapshot"]), Text(format_entry(entry)), Text(check or empty))
    if not entries:
        table.add_row("—", "No entries yet", "", "")
    console.print(table)
    completed = sum(1 for e in entries if not e["skipped"])
    skipped = sum(1 for e in entries if e["skipped"])
    console.print(f"Completed: [bold]{completed}[/bold] • Skipped: [bold]{skipped}[/bold] • Not visited: [bold]{len(remaining)}[/bold]")
    duration = session.get("workout_duration_seconds")
    if session["status"] == "in_progress":
        duration = service.session_elapsed_seconds(session_id)
    console.print(f"Workout duration: {format_elapsed(float(duration)) if duration is not None else 'Not recorded'} • Comparison: {COMPARISON_MODES.get(session.get('comparison_mode'), 'Previous session')}")


def choose_template(service: WorkoutService):
    templates = service.templates()
    choices = []
    for t in templates:
        detail = f"{t['exercise_count']} exercises"
        if t.get("last_used_at"):
            detail += f" • last used {str(t['last_used_at'])[:10]}"
        choices.append(Choice(f"Workout {t['label']} — {detail}", ("existing", t["id"])))
    new_label = service.next_template_label()
    choices.append(Choice(f"Workout {new_label} — create new workout order", ("new", None)))
    if templates:
        choices.append(Choice("Edit workout templates", ("edit_templates", None)))
        choices.append(Choice("Remove workout template", ("remove", None)))
    choices.append(Choice("← Back", ("back", None)))
    return select("Choose workout:", choices)


def start_workout(service: WorkoutService, exporter: ExcelExporter, export_path: Path):
    while True:
        header()
        choice = choose_template(service)
        if not choice or choice[0] == "back":
            return
        if choice[0] == "edit_templates":
            manage_templates(service)
            continue
        if choice[0] != "remove":
            break
        remove_workout_template(service, exporter, export_path)
    session_date = ask_date()
    if not session_date:
        return
    bw = ask_decimal("Bodyweight (kg):")
    if bw is None:
        return
    if choice[0] == "new":
        session_id = service.start_new_template_session(session_date, str(bw))
    else:
        session_id = service.start_existing_template_session(choice[1], session_date, str(bw))
    run_session(service, exporter, export_path, session_id)


def remove_workout_template(service: WorkoutService, exporter: ExcelExporter, export_path: Path):
    templates = service.templates()
    template_id = select("Remove which template? All workout history will be kept.", [
        Choice(f"Workout {t['label']} — {t['exercise_count']} exercises", t["id"])
        for t in templates
    ] + [Choice("← Back", None)])
    if template_id is None:
        return
    template = next(t for t in templates if t["id"] == template_id)
    if confirm(f"Remove Workout {template['label']} from Start workout? History stays saved; remaining templates are relettered.", default=False):
        service.remove_template(template_id)
        export_now(exporter, export_path)
        console.print("[green]Template removed. Its sessions remain in history as Removed Workout with their original labels and dates.[/green]")


def _exercise_from_template_row(service: WorkoutService, row: dict):
    ex = service.get_exercise(row["exercise_id"])
    if not ex:
        raise ValueError("Exercise no longer exists")
    # Session rows freeze the exercise configuration when this workout starts.
    ex = dict(ex)
    ex["name"] = row.get("name") or row.get("current_name") or ex["name"]
    ex["measurement_type"] = row.get("measurement_type_snapshot") or ex["measurement_type"]
    ex["default_unit"] = row.get("unit_snapshot") or ex["default_unit"]
    ex["resistance_unit"] = row.get("resistance_unit_snapshot")
    return ex


def _entry_action(service: WorkoutService, session_id: int, exercise: dict, defaults: Optional[dict], allow_skip: bool):
    previous = service.comparison_preview(exercise, defaults, session_id)
    session = service.get_session(session_id)
    header()
    console.print(f"[bold cyan]{escape(exercise['name'])}[/bold cyan]")
    show_previous(previous, session["bodyweight"])

    has_settings = exercise["measurement_type"] in {"external_weight", "bodyweight_plus", "assistance", "isometric", "resistance_units"}
    choices = []
    if has_settings:
        choices.append(Choice("Log using previous load/settings", "log"))
        choices.append(Choice("Update load/settings", "update"))
    else:
        choices.append(Choice("Log exercise", "log"))
    if allow_skip:
        choices.append(Choice("Skip this exercise", "skip"))
    if service.session_entries(session_id):
        choices.append(Choice("Edit earlier entry", "edit"))
        choices.append(Choice("Undo last entry", "undo"))
    choices.append(Choice("Call it a day", "end"))
    choices.append(Choice("Pause workout", "pause"))
    choices.append(Choice("Rest reminder settings", "rest_settings"))
    choices.append(Choice("Change comparison baseline", "comparison"))
    action = select("Action:", choices)
    return action, previous


def _make_skipped(exercise: dict, bodyweight: str):
    return WorkoutEntryData(
        exercise_id=exercise["id"],
        exercise_name=exercise["name"],
        measurement_type=exercise["measurement_type"],
        unit=exercise.get("default_unit") or "kg",
        bodyweight=Decimal(str(bodyweight)),
        resistance_unit=exercise.get("resistance_unit"),
        skipped=True,
    )


def _log_one(service: WorkoutService, session_id: int, exercise: dict, defaults: Optional[dict], allow_skip: bool):
    while True:
        action, previous = _entry_action(service, session_id, exercise, defaults, allow_skip)
        if action is None:
            return "continue"
        if action == "edit":
            edit_session_entry(service, session_id)
            continue
        if action == "undo":
            undone = service.undo_last_entry(session_id)
            refresh_active_timer(session_id)
            if undone:
                console.print(f"[yellow]Undid:[/yellow] {escape(undone['exercise_name_snapshot'])}")
            return "restart"
        if action == "end":
            return "end"
        if action == "pause":
            if pause_workout_menu(service, session_id) == "leave":
                return "leave"
            continue
        if action == "rest_settings":
            rest_settings_menu(service, session_id)
            refresh_active_timer(session_id)
            continue
        if action == "comparison":
            comparison_menu(service, session_id)
            continue
        if action == "skip":
            service.save_entry(session_id, _make_skipped(exercise, service.get_session(session_id)["bodyweight"]))
            return "logged"

        update = action == "update"
        # Defaults from a session are authoritative, even if a config change
        # deliberately cleared a previous load or changed its units.
        base = defaults if defaults is not None else previous
        if base and base.get("measurement_type", base.get("measurement_type_snapshot")) != exercise["measurement_type"]:
            base = None
        data = prompt_entry(exercise, service.get_session(session_id)["bodyweight"], defaults=base, update_settings=update)
        if data is None:
            continue
        entry_id = service.save_entry(session_id, data)
        refresh_active_timer(session_id)
        current = service.get_entry(entry_id)
        previous = service.comparison_entry(current, session_id)
        comparison = comparison_message(current, previous)
        if comparison:
            if service.get_session(session_id).get("comparison_mode") == "matching":
                comparison = comparison.replace("vs previous session", "vs matching session")
            comparison += f" (comparison date: {previous['session_date']})"
            console.print(f"[green]{escape(comparison)}[/green]")
        return "logged"


def run_session(service: WorkoutService, exporter: ExcelExporter, export_path: Path, session_id: int):
    with active_session_timer(service, session_id):
        if service.get_session(session_id).get("paused_at"):
            if pause_workout_menu(service, session_id) == "leave":
                return
        _run_session(service, exporter, export_path, session_id)


def _run_session(service: WorkoutService, exporter: ExcelExporter, export_path: Path, session_id: int):
    while True:
        session = service.get_session(session_id)
        if not session or session["status"] != "in_progress":
            return
        remaining = service.remaining_for_session(session_id)

        if session["created_new_template"]:
            header()
            console.print(f"{session['workout_name']} • {session['session_date']} • BW {session['bodyweight']} kg")
            if not remaining:
                if end_session_menu(service, exporter, export_path, session_id, force_incomplete=False):
                    return
                continue
            choices = [Choice(e["name"], e["id"]) for e in remaining]
            if service.session_entries(session_id):
                choices += [
                    Choice("✎ Edit earlier entry", "__edit__"),
                    Choice("↶ Undo last entry", "__undo__"),
                ]
            choices += [Choice("✓ Call it a day", "__end__"), Choice("Pause workout", "__pause__"),
                        Choice("Rest reminder settings", "__rest__"), Choice("Change comparison baseline", "__comparison__")]
            picked = select("Choose next exercise:", choices)
            if picked is None:
                continue
            if picked == "__edit__":
                edit_session_entry(service, session_id)
                continue
            if picked == "__undo__":
                service.undo_last_entry(session_id)
                refresh_active_timer(session_id)
                continue
            if picked == "__end__":
                if end_session_menu(service, exporter, export_path, session_id, force_incomplete=bool(remaining)):
                    return
                continue
            if picked == "__pause__":
                if pause_workout_menu(service, session_id) == "leave":
                    return
                continue
            if picked == "__rest__":
                rest_settings_menu(service, session_id)
                refresh_active_timer(session_id)
                continue
            if picked == "__comparison__":
                comparison_menu(service, session_id)
                continue
            exercise = service.get_session_exercise(session_id, int(picked))
            result = _log_one(service, session_id, exercise, exercise, allow_skip=False)
            if result == "leave":
                return
            if result == "end":
                if end_session_menu(service, exporter, export_path, session_id, force_incomplete=True):
                    return
            continue

        # Existing template: next unvisited exercise in saved order.
        if not remaining:
            if end_session_menu(service, exporter, export_path, session_id, force_incomplete=False):
                return
            continue
        template_row = remaining[0]
        exercise = _exercise_from_template_row(service, template_row)
        result = _log_one(service, session_id, exercise, template_row, allow_skip=True)
        if result == "leave":
            return
        if result == "end":
            if end_session_menu(service, exporter, export_path, session_id, force_incomplete=True):
                return
        # "restart" just loops and recalculates the correct next exercise.


def edit_session_entry(service: WorkoutService, session_id: int):
    entries = service.session_entries(session_id)
    if not entries:
        console.print("[yellow]Nothing to edit yet.[/yellow]")
        return
    choices = [Choice(f"{e['exercise_name_snapshot']} — {format_entry(e)}", e["id"]) for e in entries]
    choices.append(Choice("← Back", None))
    entry_id = select("Edit which entry?", choices)
    if not entry_id:
        return
    old = service.get_entry(entry_id)
    frozen = service.get_session_exercise(session_id, old["exercise_id"]) or {}
    ex = {
        "id": old["exercise_id"], "name": old["exercise_name_snapshot"],
        "measurement_type": old["measurement_type"], "default_unit": old["unit"],
        "resistance_unit": old["resistance_unit"] or frozen.get("resistance_unit"),
    }
    header()
    console.print(f"[bold]Editing {escape(old['exercise_name_snapshot'])}[/bold]")
    if old["skipped"]:
        action = select("This entry is SKIPPED:", [Choice("Log it now", "log"), Choice("Keep skipped", "back")])
        if action != "log":
            return
    data = prompt_entry(ex, service.get_session(session_id)["bodyweight"], defaults=old, update_settings=True, existing=old)
    if data:
        service.edit_entry(entry_id, data)
        refresh_active_timer(session_id)
        console.print("[green]Entry updated.[/green]")


def end_session_menu(service: WorkoutService, exporter: ExcelExporter, export_path: Path, session_id: int, force_incomplete: bool):
    while True:
        header()
        session_summary(service, session_id)
        choice = select("Finish workout:", [
            Choice("Save workout", "save"),
            Choice("Edit entries", "edit"),
            Choice("Continue workout", "continue"),
            Choice("Change comparison baseline", "comparison"),
            Choice("Rest reminder settings", "rest"),
            Choice("Pause workout", "pause"),
        ])
        if choice == "comparison":
            comparison_menu(service, session_id)
            continue
        if choice == "rest":
            rest_settings_menu(service, session_id)
            refresh_active_timer(session_id)
            continue
        if choice == "pause":
            if pause_workout_menu(service, session_id) == "leave":
                return True
            continue
        if choice == "edit":
            edit_session_entry(service, session_id)
            continue
        if choice in (None, "continue"):
            return False
        if choice == "save":
            status = service.finalize_session(session_id, force_incomplete=force_incomplete)
            export_now(exporter, export_path)
            console.print(f"[green]Workout saved as {status}.[/green]")
            return True


def resume_menu(service: WorkoutService, exporter: ExcelExporter, export_path: Path):
    unfinished = service.unfinished_sessions()
    if not unfinished:
        console.print("[yellow]No unfinished workouts.[/yellow]")
        return
    choices = [
        Choice(f"{s['session_date']} • {s['workout_name']} • BW {s['bodyweight']} kg", s["id"])
        for s in unfinished
    ] + [Choice("← Back", None)]
    session_id = select("Unfinished workouts:", choices)
    if not session_id:
        return
    action = select("What do you want to do?", [
        Choice("Resume workout", "resume"),
        Choice("Save as incomplete", "save"),
        Choice("Discard unfinished workout (keep stored data)", "discard"),
        Choice("← Back", "back"),
    ])
    if action == "resume":
        run_session(service, exporter, export_path, session_id)
    elif action == "save":
        service.save_unfinished_as_incomplete(session_id)
        export_now(exporter, export_path)
    elif action == "discard":
        if confirm("Hide this unfinished workout from recovery? Stored entries will be kept.", default=False):
            service.discard_session(session_id)
            console.print("[yellow]Unfinished workout discarded.[/yellow]")


def workout_history(service: WorkoutService, exporter: ExcelExporter, export_path: Path):
    while True:
        header()
        rows = service.workout_history()
        if not rows:
            console.print("[yellow]No saved workouts yet.[/yellow]")
            input("Press Enter to return...")
            return
        choices = []
        for s in rows:
            choices.append(Choice(
                f"{s['session_date']} | {s['workout_name']} | "
                f"{s.get('completed_count') or 0}/{s['planned_total']} completed | BW {s['bodyweight']} kg | {s['status']}",
                s["id"],
            ))
        choices.append(Choice("← Back", None))
        sid = select("Workout history:", choices)
        if not sid:
            return
        saved_workout_menu(service, exporter, export_path, sid, summary_callback=session_summary)


def exercise_history(service: WorkoutService):
    header()
    exercises = service.all_exercises()
    choices = [Choice(("[archived] " if e["archived"] else "") + e["name"], e["id"]) for e in exercises]
    choices.append(Choice("← Back", None))
    ex_id = select("Exercise:", choices)
    if not ex_id:
        return
    ex = service.get_exercise(ex_id)
    rows = service.exercise_history(ex_id)
    header()
    table = Table(title=Text(ex["name"]))
    table.add_column("Date")
    table.add_column("Workout")
    table.add_column("Performance")
    for r in rows:
        table.add_row(r["session_date"], r["workout_name"], Text(format_entry(r)))
    if not rows:
        table.add_row("—", "—", "No history")
    console.print(table)
    input("Press Enter to return...")


def bodyweight_history(service: WorkoutService):
    header()
    rows = service.bodyweight_history()
    table = Table(title="Bodyweight History")
    table.add_column("Date")
    table.add_column("Bodyweight", justify="right")
    table.add_column("Change", justify="right")
    # Display newest first, but compare each value to the chronologically previous weigh-in.
    chrono = list(reversed(rows))
    changes = {}
    previous_chrono = None
    for r in chrono:
        bw = Decimal(str(r["bodyweight"]))
        changes[r["id"]] = "—" if previous_chrono is None else ("+" if bw - previous_chrono > 0 else "") + decimal_text(bw - previous_chrono)
        previous_chrono = bw
    for r in rows:
        table.add_row(r["entry_date"], f"{r['bodyweight']} kg", changes[r["id"]] + (" kg" if changes[r["id"]] != "—" else ""))
    console.print(table)
    input("Press Enter to return...")


def comparison_menu(service: WorkoutService, session_id=None):
    current = service.get_session(session_id)["comparison_mode"] if session_id is not None else service.get_setting("comparison_mode", "previous")
    mode = select("Compare performance against:", [Choice(label, key) for key, label in COMPARISON_MODES.items()] + [Choice("← Back", None)], default=current)
    if mode is not None:
        service.set_comparison_mode(mode, session_id)


def preferences_menu(service: WorkoutService):
    while True:
        header()
        action = select("Defaults for new sessions:", [
            Choice("Comparison baseline", "comparison"),
            Choice("Rest reminder target and sound", "rest"),
            Choice("← Back", None),
        ])
        if action is None:
            return
        if action == "comparison":
            comparison_menu(service)
        elif action == "rest":
            rest_settings_menu(service)


def manage_exercises(service: WorkoutService):
    while True:
        header()
        action = select("Manage exercises:", [
            Choice("Add exercise", "add"),
            Choice("Edit exercise", "edit"),
            Choice("Archive / unarchive exercise", "archive"),
            Choice("← Back", "back"),
        ])
        if action in (None, "back"):
            return
        if action == "add":
            name = text("Exercise name:")
            if not name or not name.strip():
                continue
            mode = select("Measurement type:", measurement_type_choices())
            if not mode:
                continue
            unit = text("Default unit:", measurement_unit(mode))
            if unit is None:
                continue
            r_unit = None
            if mode == "resistance_units":
                r_unit = text("Resistance unit (e.g. springs):", "springs")
                if r_unit is None:
                    continue
            try:
                service.add_exercise(name.strip(), mode, unit.strip() or measurement_unit(mode), r_unit.strip() if r_unit else None)
                console.print("[green]Exercise added.[/green]")
            except Exception as exc:
                console.print(f"[red]Could not add exercise:[/red] {escape(str(exc))}")
        elif action == "edit":
            exercises = service.all_exercises()
            ex_id = select("Exercise:", [Choice(e["name"], e["id"]) for e in exercises] + [Choice("← Back", None)])
            if not ex_id:
                continue
            ex = service.get_exercise(ex_id)
            sub = select("Change:", [
                Choice("Name", "name"),
                Choice("Measurement type", "mode"),
                Choice("Default unit", "unit"),
                Choice("Resistance unit", "runit"),
                Choice("Aliases", "aliases"),
                Choice("Display order", "order"),
                Choice("← Back", "back"),
            ])
            if sub == "name":
                v = text("Name:", ex["name"])
                if v and v.strip(): service.update_exercise(ex_id, name=v.strip())
            elif sub == "mode":
                v = select("Measurement type:", measurement_type_choices(), default=ex["measurement_type"])
                if v and v != ex["measurement_type"]:
                    suggested_unit = measurement_unit(v, previous=ex["default_unit"])
                    unit = text("Default unit:", suggested_unit)
                    if unit is None:
                        continue
                    resistance_unit = None
                    if v == "resistance_units":
                        resistance_unit = text("Resistance unit (e.g. springs):", unit.strip() or "units")
                        if resistance_unit is None:
                            continue
                        resistance_unit = resistance_unit.strip() or "units"
                    service.update_exercise(ex_id, measurement_type=v, default_unit=unit.strip() or suggested_unit, resistance_unit=resistance_unit)
                    console.print("[green]Settings updated for the next session. Saved history is preserved.[/green]")
            elif sub == "unit":
                v = text("Default unit:", ex["default_unit"])
                if v: service.update_exercise(ex_id, default_unit=v.strip())
            elif sub == "runit":
                v = text("Resistance unit:", ex.get("resistance_unit") or "")
                if v is not None: service.update_exercise(ex_id, resistance_unit=v.strip() or None)
            elif sub == "aliases":
                v = text("Aliases (comma-separated):", ex.get("aliases") or "")
                if v is not None: service.update_exercise(ex_id, aliases=v.strip())
            elif sub == "order":
                v = text("Display order:", str(ex["display_order"]))
                if v and v.isdigit(): service.update_exercise(ex_id, display_order=int(v))
        elif action == "archive":
            exercises = service.all_exercises()
            ex_id = select("Exercise:", [Choice(("[archived] " if e["archived"] else "") + e["name"], e["id"]) for e in exercises] + [Choice("← Back", None)])
            if ex_id:
                ex = service.get_exercise(ex_id)
                service.update_exercise(ex_id, archived=not bool(ex["archived"]))


def recover_startup_database(database_path: Path, error):
    console.print(f"[yellow]The workout database could not be opened:[/yellow] {escape(str(error))}")
    manager = BackupManager(database_path)
    snapshots = [item for item in manager.list_backups() if item.valid]
    choices = [Choice(f"{item.created_at} • {item.sessions} workouts / {item.entries} results", item.path) for item in snapshots]
    selected = select("Open a recovered copy from a backup. The original database will be kept.", choices + [Choice("Choose a backup file", "custom"), Choice("Exit", None)])
    if selected is None:
        return None
    if selected == "custom":
        raw = text("Backup database path:")
        if not raw:
            return None
        selected = Path(raw.strip()).expanduser()
    destination = database_path.parent / f"recovered-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.db"
    try:
        manager.recover_to_new_database(selected, destination)
        # A small pointer selects the recovered copy on subsequent launches;
        # the original database and its journal files remain untouched.
        pointer = database_path.parent / "active-recovery.json"
        temporary = pointer.with_suffix(".tmp")
        temporary.write_text(json.dumps({"database": destination.name}) + "\n")
        temporary.replace(pointer)
        console.print("[green]Recovery copy opened. Your original database is still preserved.[/green]")
        return destination
    except (BackupError, OSError) as exc:
        console.print(f"[red]Recovery could not be opened:[/red] {escape(str(exc))}")
        return None


def run():
    try:
        _run()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Safe exit. Completed exercise entries and in-progress sessions are already autosaved.[/yellow]")
    except (OSError, sqlite3.Error) as exc:
        console.print(f"[red]The app could not complete this operation:[/red] {escape(str(exc))}")


def _run():
    try:
        db_path, export_path = app_paths()
    except RecoverySelectionError as exc:
        db_path = recover_startup_database(exc.data_dir / "workouts.db", exc)
        if db_path is None:
            return
        export_path = exc.export_path
    backup_warning = None
    try:
        BackupManager(db_path).daily_backup()
    except Exception as exc:
        backup_warning = str(exc)
    try:
        db = Database(db_path)
        if db.fetchone("PRAGMA quick_check")[0] != "ok":
            raise sqlite3.DatabaseError("Database integrity check failed")
    except UnsupportedSchemaError as exc:
        console.print(str(exc), style="red", markup=False)
        return
    except sqlite3.DatabaseError as exc:
        recovered = recover_startup_database(db_path, exc)
        if recovered is None:
            return
        db_path = recovered
        db = Database(db_path)
    db.seed_exercises()
    service = WorkoutService(db)
    exporter = ExcelExporter(db)
    try:
        service.backups.daily_backup()
        backup_warning = None
    except Exception as exc:
        backup_warning = str(exc)

    try:
        while True:
            header()
            warning = backup_warning or getattr(service, "backup_warning", None)
            if warning:
                console.print(f"[yellow]Automatic backup needs attention:[/yellow] {escape(str(warning))}")
            unfinished = service.unfinished_sessions()
            choices = [Choice("Start workout", "start")]
            if unfinished:
                choices.append(Choice(f"Resume unfinished workout ({len(unfinished)})", "resume"))
            choices += [
                Choice("Workout history", "workouts"),
                Choice("Exercise history", "exercise_history"),
                Choice("Bodyweight history", "bodyweight"),
                Choice("Manage exercises", "manage"),
                Choice("Edit workout templates", "templates"),
                Choice("Backups and recovery", "backups"),
                Choice("Preferences", "preferences"),
                Choice("Export data", "export"),
                Choice("Exit", "exit"),
            ]
            action = select("Main menu:", choices)
            if action in (None, "exit"):
                return
            try:
                if action == "start":
                    start_workout(service, exporter, export_path)
                elif action == "resume":
                    resume_menu(service, exporter, export_path)
                elif action == "workouts":
                    workout_history(service, exporter, export_path)
                elif action == "exercise_history":
                    exercise_history(service)
                elif action == "bodyweight":
                    bodyweight_history(service)
                elif action == "manage":
                    manage_exercises(service)
                elif action == "templates":
                    manage_templates(service)
                elif action == "backups":
                    backup_menu(service, exporter, export_path)
                    backup_warning = None
                elif action == "preferences":
                    preferences_menu(service)
                elif action == "export":
                    export_now(exporter, export_path)
                    input("Press Enter to return...")
            except (BackupError, OSError, ValueError, sqlite3.Error) as exc:
                console.print(f"[red]Could not complete this action:[/red] {escape(str(exc))}")
                input("Press Enter to return to the main menu...")
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Safe exit. Completed exercise entries and in-progress sessions are already autosaved.[/yellow]")


if __name__ == "__main__":
    run()
