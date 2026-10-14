from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text
from rich.markup import escape

from ..notation import format_entry
from ..services.management import _historical_resistance_unit
from .prompts import Choice, ask_date, ask_decimal, confirm, prompt_entry, select, text


console = Console()


def manage_templates(service):
    while True:
        templates = service.templates()
        template_id = select("Edit workout templates (changes apply next session):", [
            Choice(f"Workout {row['label']} — {row['exercise_count']} exercises", row["id"])
            for row in templates
        ] + [Choice("← Back", None)])
        if template_id is None:
            return
        template = next(row for row in templates if row["id"] == template_id)
        _edit_template(service, template)


def _edit_template(service, template):
    original = [row["exercise_id"] for row in service.template_exercises(template["id"])]
    draft = original[:]
    while True:
        exercises = {row["id"]: row for row in service.all_exercises()}
        choices = [Choice(
            f"{index}. {exercises[exercise_id]['name']}" + (" [archived]" if exercises[exercise_id]["archived"] else ""),
            ("exercise", exercise_id),
        ) for index, exercise_id in enumerate(draft, start=1)]
        choices += [
            Choice("Add exercise", ("add", None)),
            Choice("Save template", ("save", None)),
            Choice("Cancel changes", ("cancel", None)),
        ]
        action = select(f"Workout {template['label']} — edit order for future sessions:", choices)
        if action is None or action[0] == "cancel":
            return
        if action[0] == "save":
            try:
                service.set_template_exercises(template["id"], draft)
            except ValueError as exc:
                console.print(f"[red]{escape(str(exc))}[/red]")
                continue
            console.print("[green]Template saved. Changes apply when the next workout starts.[/green]")
            return
        if action[0] == "add":
            available = [row for row in service.active_exercises() if row["id"] not in draft]
            exercise_id = select("Add exercise:", [Choice(row["name"], row["id"]) for row in available]
                                 + [Choice("← Back", None)])
            if exercise_id is not None:
                draft.append(exercise_id)
            continue
        exercise_id = action[1]
        index = draft.index(exercise_id)
        moves = []
        if index > 0:
            moves.append(Choice("Move up", "up"))
        if index < len(draft) - 1:
            moves.append(Choice("Move down", "down"))
        moves += [Choice("Remove from template", "remove"), Choice("← Back", None)]
        change = select(exercises[exercise_id]["name"], moves)
        if change == "remove":
            draft.remove(exercise_id)
        elif change in {"up", "down"}:
            other = index - 1 if change == "up" else index + 1
            draft[index], draft[other] = draft[other], draft[index]


def _export_after_correction(exporter, export_path, correction=True):
    try:
        exporter.export(export_path)
        console.print("[green]Correction saved and Excel updated. Original values remain in revision history.[/green]" if correction
                      else "[green]Comparison baseline saved and Excel updated.[/green]")
    except Exception as exc:
        console.print(f"[yellow]Changes are safely saved. Excel export failed: {escape(str(exc))}[/yellow]")


def saved_workout_menu(service, exporter, export_path, session_id, summary_callback):
    while True:
        summary_callback(service, session_id)
        action = select("Saved workout:", [
            Choice("Correct an entry", "entry"),
            Choice("Correct workout date", "date"),
            Choice("Correct bodyweight", "bodyweight"),
            Choice("Change comparison baseline", "comparison"),
            Choice("View revision history", "revisions"),
            Choice("← Back", None),
        ])
        if action is None:
            return
        if action == "revisions":
            _show_revisions(service, session_id)
            continue
        if action == "comparison":
            mode = select("Compare results with:", [
                Choice("Previous session", "previous"),
                Choice("Last session with matching load/settings", "matching"),
                Choice("← Back", None),
            ], default=service.get_session(session_id).get("comparison_mode", "previous"))
            if mode is not None:
                service.set_comparison_mode(mode, session_id=session_id)
                _export_after_correction(exporter, export_path, correction=False)
            continue
        try:
            if action == "entry":
                changed = _correct_entry(service, session_id)
            else:
                changed = _correct_session(service, session_id, action)
            if changed:
                _export_after_correction(exporter, export_path)
        except ValueError as exc:
            console.print(f"[red]Correction not saved: {escape(str(exc))}[/red]")


def _correct_entry(service, session_id):
    entries = service.session_entries(session_id)
    entry_id = select("Correct which entry?", [
        Choice(f"{row['exercise_name_snapshot']} — {format_entry(row)}", row["id"])
        for row in entries
    ] + [Choice("← Back", None)])
    if entry_id is None:
        return False
    old = service.get_entry(entry_id)
    if old["skipped"] and select("This exercise was skipped:", [
        Choice("Enter its result", "log"), Choice("← Back", None),
    ]) != "log":
        return False
    exercise = {
        "id": old["exercise_id"], "name": old["exercise_name_snapshot"],
        "measurement_type": old["measurement_type"], "default_unit": old["unit"],
        "resistance_unit": _historical_resistance_unit(old),
    }
    bodyweight = old["bodyweight"] or service.get_session(session_id)["bodyweight"]
    data = prompt_entry(exercise, bodyweight, defaults=old, update_settings=True, existing=old)
    if data is None:
        return False
    after = vars(data)
    table = Table(title=Text(f"Correction: {old['exercise_name_snapshot']}"))
    table.add_column("Before")
    table.add_column("After")
    table.add_row(Text(format_entry(old)), Text(format_entry(after)))
    console.print(table)
    reason = text("Reason (optional):", "")
    if reason is None or not confirm("Save this correction and retain the original in revision history?", default=False):
        return False
    return service.correct_entry(entry_id, data, reason=reason) is not None


def _correct_session(service, session_id, field):
    session = service.get_session(session_id)
    if field == "date":
        value = ask_date(session["session_date"])
        if value is None:
            return False
        before = session["session_date"]
        label = "Date"
        changes = {"session_date": value}
    else:
        value = ask_decimal("Correct bodyweight (kg):", session["bodyweight"])
        if value is None:
            return False
        before = session["bodyweight"]
        label = "Bodyweight (kg)"
        changes = {"bodyweight": value}
    console.print(f"{label}: {before} → {value}")
    reason = text("Reason (optional):", "")
    if reason is None or not confirm("Save this correction and retain the original in revision history?", default=False):
        return False
    return service.correct_session(session_id, reason=reason, **changes) is not None


def _display_value(value):
    return "—" if value is None or value == "" else str(value)


def _revision_changes(revision):
    before, after = revision["before"], revision["after"]
    if revision["change_type"] == "entry":
        for key in before.keys() | after.keys():
            if key != "updated_at" and before.get(key) != after.get(key):
                yield key.replace("_", " "), before.get(key), after.get(key)
        return
    for key in ("session_date", "bodyweight"):
        if before["session"].get(key) != after["session"].get(key):
            yield key.replace("_", " "), before["session"].get(key), after["session"].get(key)
    for collection, label in (("entries", "Entry"), ("bodyweight_entries", "Weigh-in")):
        previous = {row["id"]: row for row in before[collection]}
        current = {row["id"]: row for row in after[collection]}
        for row_id in sorted(previous.keys() | current.keys()):
            old, new = previous.get(row_id, {}), current.get(row_id, {})
            for key in ("entry_date", "bodyweight"):
                if old.get(key) != new.get(key):
                    yield f"{label} #{row_id} {key.replace('_', ' ')}", old.get(key), new.get(key)


def _show_revisions(service, session_id):
    revisions = service.revisions(session_id)
    if not revisions:
        console.print("[yellow]No corrections have been made to this workout.[/yellow]")
    for revision in revisions:
        target = f"Entry #{revision['entry_id']}" if revision["entry_id"] else "Workout"
        table = Table(title=f"{revision['created_at']} • {target} • revision #{revision['id']}")
        table.add_column("Field")
        table.add_column("Before")
        table.add_column("After")
        for field, before, after in sorted(_revision_changes(revision)):
            table.add_row(field, Text(_display_value(before)), Text(_display_value(after)))
        console.print(table)
        if revision["reason"]:
            console.print("Reason:", revision["reason"], markup=False)
    select("Revision history:", [Choice("← Back", None)])
