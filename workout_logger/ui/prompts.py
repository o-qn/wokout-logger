from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit
from prompt_toolkit.shortcuts import prompt
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Label, RadioList
from rich.console import Console
from rich.panel import Panel

from ..models import MEASUREMENT_TYPES, WorkoutEntryData
from ..notation import parse_decimal, decimal_text, format_entry

console = Console()


@dataclass(frozen=True)
class Choice:
    title: str
    value: Any


PT_STYLE = Style.from_dict({
    "question": "bold",
    "radio-selected": "fg:#5f87ff bold",
    "radio-checked": "fg:#5fd787",
    "instruction": "fg:#888888",
})


def _normalize_choices(choices):
    result = []
    for c in choices:
        if isinstance(c, Choice):
            result.append(c)
        elif isinstance(c, tuple) and len(c) == 2:
            result.append(Choice(str(c[0]), c[1]))
        else:
            result.append(Choice(str(c), c))
    return result


def select(message: str, choices, default=None):
    normalized = _normalize_choices(choices)
    if not normalized:
        return None
    values = [(c.value, c.title) for c in normalized]
    valid_values = [c.value for c in normalized]
    radio_default = default if default in valid_values else normalized[0].value
    radio = RadioList(values=values, default=radio_default, select_on_focus=True)
    kb = KeyBindings()

    @kb.add("enter")
    def _accept(event):
        event.app.exit(result=radio.current_value)

    @kb.add("escape")
    def _cancel(event):
        event.app.exit(result=None)

    @kb.add("c-c")
    def _interrupt(event):
        event.app.exit(exception=KeyboardInterrupt())

    root = HSplit([
        Label(text=message, style="class:question"),
        radio,
        Label(text="↑/↓ move • Enter select • Esc back", style="class:instruction"),
    ])
    app = Application(
        layout=Layout(root, focused_element=radio),
        key_bindings=kb,
        style=PT_STYLE,
        full_screen=False,
        mouse_support=False,
        erase_when_done=True,
    )
    return app.run()


def text(message: str, default: str = "") -> Optional[str]:
    return prompt(f"{message} ", default=default)


def confirm(message: str, default: bool = True) -> Optional[bool]:
    choices = [Choice("Yes", True), Choice("No", False)]
    return select(message, choices, default=default)


def ask_date(default: Optional[str] = None) -> Optional[str]:
    default = default or date.today().isoformat()
    while True:
        raw = text("Workout date:", default)
        if raw is None:
            return None
        raw = raw.strip()
        for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                pass
        console.print("[red]Enter a date like 2026-09-16, 16 Sep 2026, or press Enter for the default.[/red]")


def ask_decimal(message: str, default: Optional[Any] = None, allow_blank: bool = False) -> Optional[Decimal]:
    default_text = decimal_text(default) if default not in (None, "") else ""
    while True:
        raw = text(message, default_text)
        if raw is None:
            return None
        raw = raw.strip()
        if not raw and allow_blank:
            return None
        try:
            value = parse_decimal(raw)
            if value < 0:
                raise InvalidOperation("negative")
            return value
        except (InvalidOperation, ValueError):
            console.print("[red]Enter a valid non-negative number, e.g. 19.5.[/red]")


def ask_int(message: str, default: Optional[Any] = None, minimum: int = 0) -> Optional[int]:
    default_text = str(default) if default not in (None, "") else ""
    while True:
        raw = text(message, default_text)
        if raw is None:
            return None
        try:
            value = int(raw.strip())
            if value < minimum:
                raise ValueError
            return value
        except ValueError:
            console.print(f"[red]Enter a whole number ≥ {minimum}.[/red]")


def ask_rir(default: Optional[str] = None) -> Optional[str]:
    options = [
        Choice("F — failure", "F"),
        Choice("0 — 0 RIR", "0"),
        Choice("1 — 1 RIR", "1"),
        Choice("2 — 2 RIR", "2"),
    ]
    return select("RIR:", options, default=(default.upper() if default else None))


def show_previous(previous: Optional[Mapping[str, Any]], bodyweight: str):
    if previous:
        performance = format_entry(previous)
        note = previous.get("notes") or "—"
        previous_bw = previous.get("bodyweight") or "—"
        content = (
            f"[bold]Previous:[/bold] {performance}\n"
            f"[bold]Previous BW:[/bold] {previous_bw} kg\n"
            f"[bold]Previous note:[/bold] {note}\n"
            f"[bold]Today BW:[/bold] {bodyweight} kg"
        )
    else:
        content = f"[bold]Previous:[/bold] no earlier entry\n[bold]Today BW:[/bold] {bodyweight} kg"
    console.print(Panel(content, border_style="blue"))


def _value(source: Optional[Mapping[str, Any]], *keys):
    if not source:
        return None
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return None


def prompt_entry(
    exercise: Mapping[str, Any],
    bodyweight: str,
    defaults: Optional[Mapping[str, Any]] = None,
    update_settings: bool = True,
    existing: Optional[Mapping[str, Any]] = None,
) -> Optional[WorkoutEntryData]:
    """Prompt a complete entry. Returns None only for an explicit menu cancellation."""
    mode = (existing or {}).get("measurement_type") or exercise.get("measurement_type")
    unit = (existing or {}).get("unit") or exercise.get("default_unit") or "kg"
    r_unit = exercise.get("resistance_unit") or _value(defaults, "resistance_unit", "resistance_unit_snapshot") or (existing or {}).get("resistance_unit")

    base = existing or defaults or {}
    data = WorkoutEntryData(
        exercise_id=int(exercise["id"]),
        exercise_name=str(exercise["name"]),
        measurement_type=str(mode),
        unit=str(unit),
        bodyweight=Decimal(str(bodyweight)),
        resistance_unit=r_unit,
    )

    if mode == "external_weight":
        prev = _value(base, "external_load", "default_external_load")
        data.external_load = ask_decimal("Load (kg):", prev) if update_settings or prev is None else Decimal(str(prev))
        data.reps = ask_int("Reps:", _value(existing, "reps"), 0)
    elif mode == "bodyweight":
        data.reps = ask_int("Reps:", _value(existing, "reps"), 0)
    elif mode == "bodyweight_plus":
        prev = _value(base, "external_load", "default_external_load")
        data.external_load = ask_decimal("Added load (kg):", prev) if update_settings or prev is None else Decimal(str(prev))
        data.reps = ask_int("Reps:", _value(existing, "reps"), 0)
    elif mode == "assistance":
        prev = _value(base, "assistance", "default_assistance")
        data.assistance = ask_decimal("Assistance (kg):", prev) if update_settings or prev is None else Decimal(str(prev))
        data.reps = ask_int("Reps:", _value(existing, "reps"), 0)
    elif mode == "isometric":
        dur = _value(base, "duration_seconds", "default_duration_seconds")
        rounds = _value(base, "rounds", "default_rounds")
        rest = _value(base, "rest_seconds", "default_rest_seconds")
        if update_settings or dur is None:
            data.duration_seconds = ask_decimal("Hold duration (seconds):", dur)
            data.rounds = ask_int("Rounds:", rounds, 1)
            data.rest_seconds = ask_decimal("Rest between rounds (seconds):", rest if rest is not None else 0)
        else:
            data.duration_seconds = Decimal(str(dur))
            data.rounds = int(rounds) if rounds is not None else 1
            data.rest_seconds = Decimal(str(rest)) if rest is not None else Decimal("0")
    elif mode == "resistance_units":
        rv = _value(base, "resistance_value", "default_resistance_value")
        if not r_unit:
            r_unit = text("Resistance unit (e.g. springs):", "springs")
            data.resistance_unit = (r_unit or "units").strip() or "units"
        data.resistance_value = ask_decimal(f"Resistance ({data.resistance_unit}):", rv) if update_settings or rv is None else Decimal(str(rv))
        data.reps = ask_int("Reps:", _value(existing, "reps"), 0)
    elif mode == "amrap":
        data.reps = ask_int("AMRAP result (reps):", _value(existing, "reps"), 0)
    else:
        result = text("Result:", str(_value(existing, "custom_result") or ""))
        data.custom_result = (result or "").strip()

    rir = ask_rir(_value(existing, "rir_code"))
    if rir is None:
        return None
    data.rir_code = rir
    data.failure = rir == "F"
    note = text("Notes (Enter to skip):", str(_value(existing, "notes") or ""))
    data.notes = (note or "").strip()
    return data


def measurement_type_choices():
    return [Choice(label, key) for key, label in MEASUREMENT_TYPES.items()]
