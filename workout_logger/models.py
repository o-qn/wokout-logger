from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


MEASUREMENT_TYPES = {
    "external_weight": "External weight × reps",
    "bodyweight": "Bodyweight × reps",
    "bodyweight_plus": "Bodyweight + external weight × reps",
    "assistance": "Assistance weight × reps",
    "isometric": "Time-based isometric",
    "resistance_units": "Springs/bands/resistance units × reps",
    "amrap": "AMRAP",
    "custom": "Custom measurement",
}


@dataclass
class Exercise:
    id: int
    name: str
    measurement_type: str
    default_unit: str = "kg"
    resistance_unit: Optional[str] = None
    aliases: str = ""
    archived: bool = False
    display_order: int = 0


@dataclass
class WorkoutEntryData:
    exercise_id: int
    exercise_name: str
    measurement_type: str
    unit: str = "kg"
    bodyweight: Optional[Decimal] = None
    external_load: Optional[Decimal] = None
    assistance: Optional[Decimal] = None
    reps: Optional[int] = None
    duration_seconds: Optional[Decimal] = None
    rounds: Optional[int] = None
    rest_seconds: Optional[Decimal] = None
    resistance_value: Optional[Decimal] = None
    resistance_unit: Optional[str] = None
    custom_result: Optional[str] = None
    rir_code: Optional[str] = None
    failure: bool = False
    notes: str = ""
    skipped: bool = False
