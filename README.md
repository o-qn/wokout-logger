# Terminal Workout Logger

A keyboard-first Linux terminal workout logger for one-set full-body training. It stores the real data in SQLite, autosaves during sessions, supports reusable Workout A/B/C templates, and regenerates an Excel workbook after saved workouts.

## What it does

- Arrow-key + Enter terminal menus; exercise names do not need to be typed during a workout.
- Date and decimal bodyweight logging.
- Reusable Workout A/B/C/... templates.
- New-workout mode where exercises disappear from the selection list after being logged.
- Existing-template mode where the saved order is followed automatically.
- Previous performance and previous note display.
- Fast reuse of saved load/settings, with an explicit **Update load/settings** option.
- RIR choices: `F`, `0`, `1`, `2`.
- Optional notes; press Enter on an empty notes field to skip.
- Exercise modes: external weight, bodyweight, BW + load, assistance, isometric, resistance units, AMRAP, and custom.
- Skip, undo, edit, and **Call it a day**.
- In-progress-session recovery after terminal interruption.
- Workout, exercise, and bodyweight history.
- Exercise add/rename/archive/configuration controls.
- Atomic `.xlsx` export with Workouts, Exercise History, Bodyweight, and Raw Data sheets.
- Decimal values are stored as strings / `Decimal` values rather than binary floats.

## Architecture

The project is intentionally split instead of using one large script:

- `workout_logger/database.py` — SQLite initialization and low-level database access.
- `workout_logger/models.py` — measurement modes and entry data structures.
- `workout_logger/services/workout_service.py` — workout/template/session behavior.
- `workout_logger/ui/prompts.py` — terminal input and exercise-specific prompts.
- `workout_logger/notation.py` — compact notation such as `BW+15kg × 7 @1`.
- `workout_logger/exports/excel_export.py` — `.xlsx` generation.
- `workout_logger/main.py` — interactive application flow.
- `tests/` — service, notation, persistence, and export tests.

## SQLite schema

The versioned schema uses these main tables:

- `exercises`
- `workout_templates`
- `workout_template_exercises`
- `workout_sessions`
- `workout_entries`
- `bodyweight_entries`

Workout entries snapshot the exercise name, measurement type, units, load/settings, bodyweight, reps/time, RIR, notes, order, and skipped state. This means renaming or reconfiguring an exercise later does not rewrite historical data.

Templates also snapshot measurement behavior and their remembered load/settings. When an existing template is repeated, you normally only enter reps, RIR, and optional notes unless you select **Update load/settings**.

## First run

Python 3.10+ is recommended.

```bash
cd workout_logger_app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m workout_logger
```

On first run, the database is created automatically and the 11 supplied exercises are seeded with appropriate measurement modes.

By default:

- SQLite database: `~/.local/share/workout_logger/workouts.db`
- Excel export: `~/workout_history.xlsx`

You can override them:

```bash
export WORKOUT_LOGGER_DATA_DIR="$HOME/somewhere/workout-data"
export WORKOUT_LOGGER_EXPORT="$HOME/somewhere/training.xlsx"
python -m workout_logger
```

## Install the `workout` command

The package defines a console command. From the project directory and inside your virtual environment:

```bash
pip install -e .
workout
```

There is also a local launcher:

```bash
./workout
```

## Typical repeated-workout flow

```text
Start workout
→ Workout A
→ date (Enter accepts today)
→ bodyweight
→ first exercise appears
→ previous performance is shown
→ Log using previous load/settings
→ reps
→ RIR
→ notes (Enter skips)
→ next exercise
```

For an exercise where the load changed, choose **Update load/settings** first.

## New workout flow

Choose the final `Workout <next letter> — create new workout order` option. The application shows all active exercises. Pick whichever one you want to perform next. Once saved, it disappears from that session's remaining list and its position becomes part of the new template's order.

**Call it a day** can save the session before all available exercises are completed. The session is marked incomplete while the logged subset remains safely stored.

## Recovery and autosave

A session row and bodyweight entry are written as soon as the workout begins. Every completed exercise entry, edit, skip, and undo is committed immediately. If the terminal is interrupted, the main menu exposes **Resume unfinished workout** with options to resume, save as incomplete, or discard.

`Ctrl+C` exits safely; already completed entries remain stored. A value that was only half-typed at the exact moment of interruption is not considered a completed entry.

## Excel layout

The exporter rebuilds the workbook from SQLite, so the spreadsheet is a view rather than the database.

### Workouts

Exercises are rows and saved sessions are columns. Top rows show date, bodyweight, session status, and Workout A/B/C label. Explicit skips show `SKIPPED`; exercises not visited remain blank.

### Exercise History

One row per saved exercise performance.

### Bodyweight

One row per saved workout bodyweight.

### Raw Data

A structured flat export of the workout-entry fields for analysis or migration.

## Tests

Run:

```bash
python -m unittest discover -s tests -v
```

The tests create temporary databases and workbooks; they do not touch your real workout data.

## Initial exercises

1. Supinated Single-Arm Extension — external weight
2. Single-Arm Preacher Curl — external weight
3. Super Wide-Grip Pull-Up — bodyweight
4. RTO Push-Up — bodyweight
5. Single-Arm Lateral Raise Robot — external weight
6. Kelso Shrug Isometric — isometric
7. Neck Extension Isometric + Neck Flexion Isometric — isometric
8. Single-Leg Hyperextension — external weight
9. Assisted Reverse Nordic — assistance
10. Chest Expander Above — resistance units (`springs`)
11. Explosive Sagittal-Plane Pull-Up — AMRAP

The seed creates exercise definitions only; it does **not** invent RIR values or fabricate a historical workout from the example log.
