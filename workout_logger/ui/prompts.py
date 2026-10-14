from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

from prompt_toolkit import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit
from prompt_toolkit.shortcuts import prompt
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Label, RadioList, TextArea
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from ..models import MEASUREMENT_TYPES, WorkoutEntryData
from ..notation import parse_decimal, decimal_text, format_entry
from .timers import timer_text

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
    "timer": "fg:#5fd787 bold",
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
        event.app.exit(result=values[radio._selected_index][0])

    @kb.add("escape")
    def _cancel(event):
        event.app.exit(result=None)

    @kb.add("c-c")
    def _interrupt(event):
        event.app.exit(exception=KeyboardInterrupt())

    def _move(offset):
        def move(event):
            radio._selected_index = (radio._selected_index + offset) % len(values)
            radio.current_value = values[radio._selected_index][0]
        return move

    # RadioList has its own Enter binding which only marks an item as checked.
    # Install our accept/cancel bindings directly on the focused control as well
    # so they take priority and Enter actually closes the menu with a result.
    radio.control.key_bindings.add("enter", eager=True)(_accept)
    radio.control.key_bindings.add("escape", eager=True)(_cancel)
    radio.control.key_bindings.add("c-c", eager=True)(_interrupt)
    for key in ("up", "k"):
        radio.control.key_bindings.add(key, eager=True)(_move(-1))
    for key in ("down", "j"):
        radio.control.key_bindings.add(key, eager=True)(_move(1))

    controls = [
        Label(text=message, style="class:question"),
        radio,
        Label(text="↑/↓ move • Enter select • Esc back", style="class:instruction"),
    ]
    if timer_text():
        controls.insert(0, Label(text=timer_text, style="class:timer"))
    root = HSplit(controls)
    app = Application(
        layout=Layout(root, focused_element=radio),
        key_bindings=kb,
        style=PT_STYLE,
        full_screen=False,
        mouse_support=False,
        erase_when_done=True,
        refresh_interval=1 if timer_text() else None,
    )
    return app.run()


def text(message: str, default: str = "") -> Optional[str]:
    kb = KeyBindings()

    @kb.add("escape")
    def cancel(event):
        event.app.exit(result=None)

    if not timer_text():
        return prompt(f"{message} ", default=default, key_bindings=kb)

    def accept(buffer):
        get_app().exit(result=buffer.text)
        return True

    # The timer occupies its own label. Redrawing it leaves the editable buffer,
    # cursor and completion state intact while a load/reps/note is being typed.
    field = TextArea(text=default, prompt=f"{message} ", multiline=False, height=1, accept_handler=accept)
    field.buffer.cursor_position = len(default)
    @kb.add("c-c")
    def interrupt(event):
        event.app.exit(exception=KeyboardInterrupt())

    @kb.add("c-d")
    def eof(event):
        event.app.exit(exception=EOFError())

    return Application(
        layout=Layout(HSplit([Label(text=timer_text, style="class:timer"), field]), focused_element=field),
        key_bindings=kb, style=PT_STYLE, full_screen=False,
        mouse_support=False, erase_when_done=True, refresh_interval=1,
    ).run()


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
            if not value.is_finite() or value < 0:
                raise InvalidOperation("non-finite or negative")
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
    if default and str(default).isdigit() and int(default) > 2:
        options.append(Choice(f"{int(default)} — {int(default)} RIR", str(int(default))))
    options.append(Choice("Other RIR value", "other"))
    result = select("RIR:", options, default=(default.upper() if default else None))
    if result == "other":
        value = ask_int("Reps in reserve:", minimum=0)
        return str(value) if value is not None else None
    return result


def show_previous(previous: Optional[Mapping[str, Any]], bodyweight: str):
    if previous:
        performance = escape(format_entry(previous))
        note = escape(str(previous.get("notes") or "—"))
        previous_bw = escape(str(previous.get("bodyweight") or "—"))
        previous_date = escape(str(previous.get("session_date") or "unknown date"))
        content = (
            f"[bold]Previous date:[/bold] {previous_date}\n"
            f"[bold]Previous:[/bold] {performance}\n"
            f"[bold]Previous BW:[/bold] {previous_bw} kg\n"
            f"[bold]Previous note:[/bold] {note}\n"
            f"[bold]Today BW:[/bold] {escape(str(bodyweight))} kg"
        )
    else:
        content = f"[bold]Previous:[/bold] no earlier entry\n[bold]Today BW:[/bold] {escape(str(bodyweight))} kg"
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
        data.external_load = ask_decimal(f"Load ({unit}):", prev) if update_settings or prev is None else Decimal(str(prev))
        if data.external_load is None:
            return None
        data.reps = ask_int("Reps:", _value(existing, "reps"), 0)
    elif mode == "bodyweight":
        data.reps = ask_int("Reps:", _value(existing, "reps"), 0)
    elif mode == "bodyweight_plus":
        prev = _value(base, "external_load", "default_external_load")
        data.external_load = ask_decimal(f"Added weight ({unit}):", prev) if update_settings or prev is None else Decimal(str(prev))
        if data.external_load is None:
            return None
        data.reps = ask_int("Reps:", _value(existing, "reps"), 0)
    elif mode == "assistance":
        prev = _value(base, "assistance", "default_assistance")
        data.assistance = ask_decimal(f"Assistance ({unit}):", prev) if update_settings or prev is None else Decimal(str(prev))
        if data.assistance is None:
            return None
        data.reps = ask_int("Reps:", _value(existing, "reps"), 0)
    elif mode == "isometric":
        dur = _value(base, "duration_seconds", "default_duration_seconds")
        rounds = _value(base, "rounds", "default_rounds")
        rest = _value(base, "rest_seconds", "default_rest_seconds")
        if update_settings or dur is None:
            data.duration_seconds = ask_decimal("Hold duration (seconds):", dur)
            if data.duration_seconds is None:
                return None
            data.rounds = ask_int("Rounds:", rounds, 1)
            if data.rounds is None:
                return None
            data.rest_seconds = ask_decimal("Rest between rounds (seconds):", rest if rest is not None else 0)
            if data.rest_seconds is None:
                return None
        else:
            data.duration_seconds = Decimal(str(dur))
            data.rounds = int(rounds) if rounds is not None else 1
            data.rest_seconds = Decimal(str(rest)) if rest is not None else Decimal("0")
    elif mode == "resistance_units":
        rv = _value(base, "resistance_value", "default_resistance_value")
        if not r_unit:
            r_unit = text("Resistance unit (e.g. springs):", "springs")
            if r_unit is None:
                return None
            data.resistance_unit = (r_unit or "units").strip() or "units"
        data.resistance_value = ask_decimal(f"Resistance ({data.resistance_unit}):", rv) if update_settings or rv is None else Decimal(str(rv))
        if data.resistance_value is None:
            return None
        data.reps = ask_int("Reps:", _value(existing, "reps"), 0)
    elif mode == "amrap":
        data.reps = ask_int("AMRAP result (reps):", _value(existing, "reps"), 0)
    else:
        result = text("Result:", str(_value(existing, "custom_result") or ""))
        if result is None:
            return None
        data.custom_result = result.strip()

    if mode in {"external_weight", "bodyweight", "bodyweight_plus", "assistance", "resistance_units", "amrap"} and data.reps is None:
        return None

    rir = ask_rir(_value(existing, "rir_code"))
    if rir is None:
        return None
    data.rir_code = rir
    data.failure = rir == "F"
    note = text("Notes (Enter to skip):", str(_value(existing, "notes") or ""))
    if note is None:
        return None
    data.notes = note.strip()
    return data


def measurement_type_choices():
    return [Choice(label, key) for key, label in MEASUREMENT_TYPES.items()]
