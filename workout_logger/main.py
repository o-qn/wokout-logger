from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .database import Database
from .exports.excel_export import ExcelExporter
from .models import WorkoutEntryData, MEASUREMENT_TYPES
from .notation import format_entry, comparison_message, decimal_text
from .services.workout_service import WorkoutService
from .ui.prompts import (
    select, text, confirm, ask_date, ask_decimal, prompt_entry, show_previous,
    measurement_type_choices, Choice,
)

console = Console()


def app_paths():
    data_dir = Path(os.environ.get("WORKOUT_LOGGER_DATA_DIR", "~/.local/share/workout_logger")).expanduser()
    export_path = Path(os.environ.get("WORKOUT_LOGGER_EXPORT", "~/workout_history.xlsx")).expanduser()
    return data_dir / "workouts.db", export_path


def header():
    console.clear()
    console.print(Panel.fit("[bold]Terminal Workout Logger[/bold]", border_style="blue"))


def export_now(exporter: ExcelExporter, export_path: Path):
    try:
        path = exporter.export(export_path)
        console.print(f"[green]Excel updated:[/green] {path}")
    except Exception as exc:
        console.print(f"[yellow]Workout is safely stored in SQLite, but Excel export failed:[/yellow] {exc}")


def session_summary(service: WorkoutService, session_id: int):
    session = service.get_session(session_id)
    entries = service.session_entries(session_id)
    remaining = service.remaining_for_session(session_id)
    table = Table(title=f"Workout {session.get('template_label') or '—'} • {session['session_date']} • BW {session['bodyweight']} kg")
    table.add_column("#", justify="right")
    table.add_column("Exercise")
    table.add_column("Performance")
    for i, entry in enumerate(entries, start=1):
        table.add_row(str(i), entry["exercise_name_snapshot"], format_entry(entry))
    if not entries:
        table.add_row("—", "No entries yet", "")
    console.print(table)
    completed = sum(1 for e in entries if not e["skipped"])
    skipped = sum(1 for e in entries if e["skipped"])
    console.print(f"Completed: [bold]{completed}[/bold] • Skipped: [bold]{skipped}[/bold] • Not visited: [bold]{len(remaining)}[/bold]")


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
    choices.append(Choice("← Back", ("back", None)))
    return select("Choose workout:", choices)


def start_workout(service: WorkoutService, exporter: ExcelExporter, export_path: Path):
    header()
    choice = choose_template(service)
    if not choice or choice[0] == "back":
        return
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


def _exercise_from_template_row(service: WorkoutService, row: dict):
    ex = service.get_exercise(row["exercise_id"])
    if not ex:
        raise ValueError("Exercise no longer exists")
    # A template snapshots its measurement behavior so later exercise-config edits
    # do not silently change how an existing Workout A/B/C is logged.
    ex = dict(ex)
    ex["measurement_type"] = row.get("measurement_type_snapshot") or ex["measurement_type"]
    ex["default_unit"] = row.get("unit_snapshot") or ex["default_unit"]
    ex["resistance_unit"] = row.get("resistance_unit_snapshot") or ex.get("resistance_unit")
    return ex


def _entry_action(service: WorkoutService, session_id: int, exercise: dict, defaults: Optional[dict], allow_skip: bool):
    previous = service.latest_previous_entry(exercise["id"], exclude_session_id=session_id)
    session = service.get_session(session_id)
    header()
    console.print(f"[bold cyan]{exercise['name']}[/bold cyan]")
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
            if undone:
                console.print(f"[yellow]Undid:[/yellow] {undone['exercise_name_snapshot']}")
            return "restart"
        if action == "end":
            return "end"
        if action == "skip":
            service.save_entry(session_id, _make_skipped(exercise, service.get_session(session_id)["bodyweight"]))
            return "logged"

        update = action == "update"
        base = defaults or previous
        data = prompt_entry(exercise, service.get_session(session_id)["bodyweight"], defaults=base, update_settings=update)
        if data is None:
            continue
        service.save_entry(session_id, data)
        current = service.session_entries(session_id)[-1]
        comparison = comparison_message(current, previous)
        if comparison:
            console.print(f"[green]{comparison}[/green]")
        return "logged"


def run_session(service: WorkoutService, exporter: ExcelExporter, export_path: Path, session_id: int):
    while True:
        session = service.get_session(session_id)
        if not session or session["status"] != "in_progress":
            return
        remaining = service.remaining_for_session(session_id)

        if session["created_new_template"]:
            header()
            console.print(f"Workout {session['template_label']} • {session['session_date']} • BW {session['bodyweight']} kg")
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
            choices += [Choice("✓ Call it a day", "__end__")]
            picked = select("Choose next exercise:", choices)
            if picked is None:
                continue
            if picked == "__edit__":
                edit_session_entry(service, session_id)
                continue
            if picked == "__undo__":
                service.undo_last_entry(session_id)
                continue
            if picked == "__end__":
                if end_session_menu(service, exporter, export_path, session_id, force_incomplete=bool(remaining)):
                    return
                continue
            exercise = service.get_exercise(int(picked))
            result = _log_one(service, session_id, exercise, None, allow_skip=False)
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
    ex = service.get_exercise(old["exercise_id"])
    header()
    console.print(f"[bold]Editing {old['exercise_name_snapshot']}[/bold]")
    if old["skipped"]:
        action = select("This entry is SKIPPED:", [Choice("Log it now", "log"), Choice("Keep skipped", "back")])
        if action != "log":
            return
    data = prompt_entry(ex, service.get_session(session_id)["bodyweight"], defaults=old, update_settings=True, existing=old)
    if data:
        service.edit_entry(entry_id, data)
        console.print("[green]Entry updated.[/green]")


def end_session_menu(service: WorkoutService, exporter: ExcelExporter, export_path: Path, session_id: int, force_incomplete: bool):
    while True:
        header()
        session_summary(service, session_id)
        choice = select("Finish workout:", [
            Choice("Save workout", "save"),
            Choice("Edit entries", "edit"),
            Choice("Continue workout", "continue"),
        ])
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
        Choice(f"{s['session_date']} • Workout {s.get('template_label') or '—'} • BW {s['bodyweight']} kg", s["id"])
        for s in unfinished
    ] + [Choice("← Back", None)]
    session_id = select("Unfinished workouts:", choices)
    if not session_id:
        return
    action = select("What do you want to do?", [
        Choice("Resume workout", "resume"),
        Choice("Save as incomplete", "save"),
        Choice("Discard workout", "discard"),
        Choice("← Back", "back"),
    ])
    if action == "resume":
        run_session(service, exporter, export_path, session_id)
    elif action == "save":
        service.save_unfinished_as_incomplete(session_id)
        export_now(exporter, export_path)
    elif action == "discard":
        if confirm("Discard this unfinished workout permanently?", default=False):
            service.discard_session(session_id)
            console.print("[yellow]Unfinished workout discarded.[/yellow]")


def workout_history(service: WorkoutService):
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
                f"{s['session_date']} | Workout {s.get('template_label') or '—'} | "
                f"{s.get('completed_count') or 0}/{s['planned_total']} completed | BW {s['bodyweight']} kg | {s['status']}",
                s["id"],
            ))
        choices.append(Choice("← Back", None))
        sid = select("Workout history:", choices)
        if not sid:
            return
        header()
        session_summary(service, sid)
        input("Press Enter to return...")


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
    table = Table(title=ex["name"])
    table.add_column("Date")
    table.add_column("Workout")
    table.add_column("Performance")
    for r in rows:
        table.add_row(r["session_date"], f"Workout {r.get('template_label') or '—'}", format_entry(r))
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
            unit = text("Default unit:", "kg" if mode not in {"isometric", "amrap", "custom"} else ("sec" if mode == "isometric" else "reps"))
            r_unit = None
            if mode == "resistance_units":
                r_unit = text("Resistance unit (e.g. springs):", "springs")
            try:
                service.add_exercise(name.strip(), mode, unit or "kg", r_unit.strip() if r_unit else None)
                console.print("[green]Exercise added.[/green]")
            except Exception as exc:
                console.print(f"[red]Could not add exercise:[/red] {exc}")
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
                if v: service.update_exercise(ex_id, measurement_type=v)
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


def run():
    db_path, export_path = app_paths()
    db = Database(db_path)
    db.seed_exercises()
    service = WorkoutService(db)
    exporter = ExcelExporter(db)

    try:
        while True:
            header()
            unfinished = service.unfinished_sessions()
            choices = [Choice("Start workout", "start")]
            if unfinished:
                choices.append(Choice(f"Resume unfinished workout ({len(unfinished)})", "resume"))
            choices += [
                Choice("Workout history", "workouts"),
                Choice("Exercise history", "exercise_history"),
                Choice("Bodyweight history", "bodyweight"),
                Choice("Manage exercises", "manage"),
                Choice("Export data", "export"),
                Choice("Exit", "exit"),
            ]
            action = select("Main menu:", choices)
            if action in (None, "exit"):
                return
            if action == "start":
                start_workout(service, exporter, export_path)
            elif action == "resume":
                resume_menu(service, exporter, export_path)
            elif action == "workouts":
                workout_history(service)
            elif action == "exercise_history":
                exercise_history(service)
            elif action == "bodyweight":
                bodyweight_history(service)
            elif action == "manage":
                manage_exercises(service)
            elif action == "export":
                export_now(exporter, export_path)
                input("Press Enter to return...")
    except KeyboardInterrupt:
        console.print("\n[yellow]Safe exit. Completed exercise entries and in-progress sessions are already autosaved.[/yellow]")


if __name__ == "__main__":
    run()
