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
    mode = row.get("measurement_type")
    if mode in {"external_weight", "bodyweight_plus"}:
        return (mode, str(row.get("external_load") or ""), row.get("unit") or "kg")
    if mode == "bodyweight":
        return (mode,)
    if mode == "assistance":
        return (mode, str(row.get("assistance") or ""), row.get("unit") or "kg")
    if mode == "isometric":
        return (mode, str(row.get("duration_seconds") or ""), row.get("rounds"), str(row.get("rest_seconds") or ""))
    if mode == "resistance_units":
        return (mode, str(row.get("resistance_value") or ""), row.get("resistance_unit") or "units")
    return (mode,)


def comparison_message(current: Mapping[str, Any], previous: Optional[Mapping[str, Any]]) -> str:
    if not previous or bool(current.get("skipped")) or bool(previous.get("skipped")):
        return ""

    mode = current.get("measurement_type")
    cur_reps = current.get("reps")
    prev_reps = previous.get("reps")

    if cur_reps is not None and prev_reps is not None and load_signature(current) == load_signature(previous):
        diff = int(cur_reps) - int(prev_reps)
        if diff > 0:
            return f"+{diff} rep{'s' if diff != 1 else ''} vs previous session"
        if diff < 0:
            return f"{diff} rep{'s' if diff != -1 else ''} vs previous session"

    field = None
    if mode in {"external_weight", "bodyweight_plus"}:
        field = "external_load"
    elif mode == "assistance":
        field = "assistance"
    elif mode == "resistance_units":
        field = "resistance_value"

    if field and current.get(field) not in (None, "") and previous.get(field) not in (None, ""):
        cur = Decimal(str(current.get(field)))
        prev = Decimal(str(previous.get(field)))
        diff = cur - prev
        if diff != 0:
            sign = "+" if diff > 0 else ""
            unit = current.get("resistance_unit") if mode == "resistance_units" else current.get("unit") or "kg"
            return f"{sign}{decimal_text(diff)} {unit} vs previous session"

    return ""
