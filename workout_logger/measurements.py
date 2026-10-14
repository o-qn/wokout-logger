"""Units for new exercise configuration, separate from frozen history."""

from __future__ import annotations


_DEFAULT_UNITS = {
    "external_weight": "kg",
    "bodyweight": "kg",
    "bodyweight_plus": "kg",
    "assistance": "kg",
    "isometric": "sec",
    "resistance_units": "units",
    "amrap": "reps",
    "custom": "units",
}

_MASS_MODES = frozenset({"external_weight", "bodyweight", "bodyweight_plus", "assistance"})
_RETAINED_MASS_MODES = _MASS_MODES - {"bodyweight"}
_MASS_ALIASES = {
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg",
    "kilogram": "kg", "kilograms": "kg",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "g": "g", "gram": "g", "grams": "g",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
}


def _mass_unit(value: str | None) -> str | None:
    return _MASS_ALIASES.get(value.strip().casefold()) if isinstance(value, str) else None


def measurement_unit(mode: str, requested: str | None = None, previous: str | None = None) -> str:
    """Choose or validate a unit for a new or updated exercise configuration.

    ``None`` means no unit was requested; an explicitly blank unit is invalid.
    Compatible previous mass units can survive a change between loaded modes.
    Historical entry and session units must remain outside this normalization.
    """
    if not isinstance(mode, str) or mode not in _DEFAULT_UNITS:
        raise ValueError(f"Unknown measurement type: {mode!r}")

    if requested is None:
        if mode in _RETAINED_MASS_MODES:
            return _mass_unit(previous) or _DEFAULT_UNITS[mode]
        return _DEFAULT_UNITS[mode]

    if not isinstance(requested, str):
        raise ValueError("Unit must be text")
    unit = requested.strip()
    if not unit:
        raise ValueError("Unit must not be blank")
    if mode in _MASS_MODES:
        canonical = _mass_unit(unit)
        if canonical is None:
            raise ValueError("Weight units must be kg, lb, g, or oz")
        return canonical
    return unit
