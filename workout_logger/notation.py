from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Mapping, Any, Optional


def parse_decimal(value: str) -> Decimal:
    cleaned = value.strip().replace(",", ".")
    if not cleaned:
        raise InvalidOperation("blank")
    return Decimal(cleaned)


def decimal_text(value: Any) -> str:
    if value is None or value == "":
        return ""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def rir_suffix(code: Optional[str]) -> str:
    if code is None or code == "":
        return ""
    code = str(code).upper()
    return f" @{code}"


def format_entry(row: Mapping[str, Any]) -> str:
    if bool(row.get("skipped")):
        return "SKIPPED"

    mode = row.get("measurement_type")
    reps = row.get("reps")
    rir = rir_suffix(row.get("rir_code"))
    notes = (row.get("notes") or "").strip()
    suffix = f" — {notes}" if notes else ""

    if mode == "external_weight":
        text = f"{decimal_text(row.get('external_load'))}{row.get('unit') or 'kg'} × {reps}{rir}"
    elif mode == "bodyweight":
        text = f"BW × {reps}{rir}"
    elif mode == "bodyweight_plus":
        text = f"BW+{decimal_text(row.get('external_load'))}{row.get('unit') or 'kg'} × {reps}{rir}"
    elif mode == "assistance":
        text = f"{decimal_text(row.get('assistance'))}{row.get('unit') or 'kg'} assistance × {reps}{rir}"
    elif mode == "isometric":
        text = f"{decimal_text(row.get('duration_seconds'))}s × {row.get('rounds')}"
        if row.get("rest_seconds") not in (None, ""):
            text += f" / {decimal_text(row.get('rest_seconds'))}s rest"
        text += rir
    elif mode == "resistance_units":
        unit = row.get("resistance_unit") or row.get("unit") or "units"
        text = f"{decimal_text(row.get('resistance_value'))} {unit} × {reps}{rir}"
    elif mode == "amrap":
        text = f"AMRAP: {reps}"
        if rir:
            text += rir
    elif mode == "custom":
        text = str(row.get("custom_result") or "")
        if rir:
            text += rir
    else:
        text = str(row.get("custom_result") or "")

    return text + suffix


def load_signature(row: Mapping[str, Any]) -> tuple:
    """Comparable settings, preserving missing loads separately from zero.

    Bodyweight affects loading for bodyweight and assisted movements, so a
    change is reported as context instead of being counted as a rep PR.
    """
    mode = row.get("measurement_type")
    if mode == "external_weight":
        return (mode, _number_key(row.get("external_load")), _unit(row))
    if mode == "bodyweight_plus":
        return (mode, _number_key(row.get("bodyweight")), _number_key(row.get("external_load")), _unit(row))
    if mode == "bodyweight":
        return (mode, _number_key(row.get("bodyweight")), _unit(row))
    if mode == "assistance":
        return (mode, _number_key(row.get("bodyweight")), _number_key(row.get("assistance")), _unit(row))
    if mode == "isometric":
        return (mode, _number_key(row.get("duration_seconds")), _number_key(row.get("rounds")), _number_key(row.get("rest_seconds")))
    if mode == "resistance_units":
        return (mode, _number_key(row.get("resistance_value")), _unit(row))
    return (mode,)


def _number(value: Any) -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        return None
    try:
        result = parse_decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _number_key(value: Any) -> tuple:
    number = _number(value)
    if number is not None:
        return ("number", number)
    if value is None or str(value).strip() == "":
        return ("missing",)
    return ("invalid", str(value))


def _unit(row: Mapping[str, Any]) -> str:
    if row.get("measurement_type") == "resistance_units":
        return str(row.get("resistance_unit") or row.get("unit") or "units").strip()
    return str(row.get("unit") or "kg").strip()


def _rir(value: Any) -> tuple[Optional[Decimal], str]:
    label = str(value).strip().upper() if value is not None else ""
    if label == "F":
        return Decimal(0), "F"
    number = _number(value)
    if number is not None and number >= 0:
        return number, decimal_text(number)
    return None, label or "—"


def _display_number(value: Any) -> str:
    number = _number(value)
    return decimal_text(number) if number is not None else str(value) if value not in (None, "") else "—"


def _result(row: Mapping[str, Any]) -> str:
    # Notes describe the set but do not change the performance comparison.
    try:
        return format_entry({**row, "notes": ""}) or "—"
    except (InvalidOperation, ValueError, TypeError):
        return "result unavailable"


def _raw_rep_change(current: Mapping[str, Any], previous: Mapping[str, Any]) -> str:
    _, cur_rir = _rir(current.get("rir_code"))
    _, prev_rir = _rir(previous.get("rir_code"))
    return (
        f"reps: {_display_number(previous.get('reps'))} → {_display_number(current.get('reps'))}; "
        f"RIR: {prev_rir} → {cur_rir}"
    )


def _settings_change(current: Mapping[str, Any], previous: Mapping[str, Any]) -> str:
    parts = []
    mode = current.get("measurement_type")
    if mode in {"bodyweight", "bodyweight_plus", "assistance"} and _number_key(current.get("bodyweight")) != _number_key(previous.get("bodyweight")):
        # Bodyweight is entered in kg; the exercise unit describes external load.
        parts.append(f"BW: {_display_number(previous.get('bodyweight'))} → {_display_number(current.get('bodyweight'))} kg")
    if _unit(current) != _unit(previous):
        parts.append(f"unit: {_unit(previous)} → {_unit(current)}")
    parts.append(f"{_result(previous)} → {_result(current)}")
    return "Settings changed: " + "; ".join(parts) + " vs previous session"


def comparison_message(current: Mapping[str, Any], previous: Optional[Mapping[str, Any]]) -> str:
    """Describe performance at comparable settings and effort.

    Equal RIR permits a plain rep delta. Different valid RIR uses the user's
    technical metric, reps + RIR (failure is numerically zero), while retaining
    both raw transitions so failure and zero RIR never become interchangeable.
    """
    if not previous or bool(current.get("skipped")) or bool(previous.get("skipped")):
        return ""

    mode = current.get("measurement_type")
    if mode != previous.get("measurement_type"):
        old_mode = str(previous.get("measurement_type") or "unknown").replace("_", " ")
        new_mode = str(mode or "unknown").replace("_", " ")
        return f"Measurement changed: {old_mode} → {new_mode}; {_result(previous)} → {_result(current)}"
    if load_signature(current) != load_signature(previous):
        return _settings_change(current, previous)

    if mode not in {"external_weight", "bodyweight", "bodyweight_plus", "assistance", "resistance_units", "amrap"}:
        old_result, new_result = _result(previous), _result(current)
        if old_result == new_result:
            return "No change vs previous session"
        return f"Result: {old_result} → {new_result} vs previous session"

    required = {
        "external_weight": ("external_load",),
        "bodyweight": ("bodyweight",),
        "bodyweight_plus": ("bodyweight", "external_load"),
        "assistance": ("bodyweight", "assistance"),
        "resistance_units": ("resistance_value",),
    }.get(mode, ())
    if any(_number(row.get(field)) is None for row in (current, previous) for field in required):
        return f"Loading unavailable; {_raw_rep_change(current, previous)}; performance comparison unavailable"

    cur_reps, prev_reps = _number(current.get("reps")), _number(previous.get("reps"))
    cur_rir, cur_label = _rir(current.get("rir_code"))
    prev_rir, prev_label = _rir(previous.get("rir_code"))
    if any(value is None or value < 0 or value != value.to_integral_value() for value in (cur_reps, prev_reps)) or cur_rir is None or prev_rir is None:
        return f"{_raw_rep_change(current, previous)}; performance comparison unavailable"

    if cur_label == prev_label:
        diff = int(cur_reps - prev_reps)
        if diff == 0:
            return "No change vs previous session"
        return f"{diff:+d} rep{'s' if abs(diff) != 1 else ''} vs previous session"

    technical = cur_reps + cur_rir - prev_reps - prev_rir
    change = f"{'+' if technical > 0 else ''}{decimal_text(technical)} technical" if technical else "No technical change"
    return f"{change}; {_raw_rep_change(current, previous)} vs previous session"
