# Terminal Workout Logger

A keyboard-first Linux terminal workout logger for one-set full-body training. It stores the real data in SQLite, autosaves during sessions, supports reusable Workout A/B/C templates, and regenerates an Excel workbook after saved workouts.

## What it does

- Arrow-key + Enter terminal menus; exercise names do not need to be typed during a workout.
- Up from the first menu item wraps to the last; Down from the last wraps to the first.
- Date and decimal bodyweight logging.
- Reusable Workout A/B/C/... templates.
- New-workout mode where exercises disappear from the selection list after being logged.
- Existing-template mode where the saved order is followed automatically.
- Previous performance and previous note display.
- Fast reuse of saved load/settings, with an explicit **Update load/settings** option.
- RIR choices: `F`, `0`, `1`, `2`, plus **Other RIR value** for any non-negative whole number.
- Live workout time and rest time above the active menu or input; rest restarts after each logged set.
- RIR-aware performance checks beside each exercise in the workout summary.
- Editable template membership and order, applied to the next session.
- Saved-workout corrections with immutable before/after revision history.
- Automatic SQLite backups and recovery that preserves newer workouts.
- Pause/resume for both timers; saved workout durations in history and Excel.
- Optional target-rest reminders, with a visible indicator and terminal bell.
- Optional notes; press Enter on an empty notes field to skip.
- Exercise modes: external weight, bodyweight, BW + load, assistance, isometric, resistance units, AMRAP, and custom.
- Skip, undo, edit, and **Call it a day**.
- In-progress-session recovery after terminal interruption.
- Workout, exercise, and bodyweight history.
- Exercise add/rename/archive/configuration controls.
- Complete history views without a 100-workout cutoff.
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
- `workout_session_exercises`
- `workout_revisions`
- `workout_pause_intervals`
- `app_settings`

Workout entries snapshot the exercise name, measurement type, units, load/settings, bodyweight, reps/time, RIR, notes, order, and skipped state. This means renaming or reconfiguring an exercise later does not rewrite historical data.

Starting a workout saves its template, session, and weigh-in together. Invalid input or a failed save leaves no partial workout template. Active edits and Undo require an unpaused active session. Undo follows the most recent logged or skipped result, restores the previous remembered settings when appropriate, and preserves settings supplied by a later session.

Templates remember load/settings. Each session snapshots the current exercise configuration when it starts, so changes in **Manage exercises** apply to the next session, including repeats of existing templates. Sessions already started and historical entries retain their original names, measurements and units. Changing a measurement type or unit clears incompatible remembered settings and prompts for new values. When an existing template is repeated, you normally only enter reps, RIR, and optional notes unless you select **Update load/settings**.

Weight-based modes use weight units (`kg`, `lb`, `g`, or `oz`). Changing AMRAP to bodyweight plus external weight defaults the new unit to `kg`, and prompts separately for **Added weight (kg)** and **Reps**. Switching between weight-based modes can retain a compatible weight unit. An earlier AMRAP result remains AMRAP in history; the first comparison after changing modes reports **Measurement changed**.

**Start workout → Remove workout template** hides a template and reletters the remaining active templates A/B/C. It preserves every saved session, exercise result and weigh-in. Sessions belonging to a removed template appear as **Removed Workout A** (or their original session letter), alongside their date, in history and Excel. Historical letters stay fixed even when active templates receive new letters.

Workout time starts at zero when the session is created. Rest time begins after the first logged set and restarts after each subsequent logged set; skips and ordinary corrections do not restart it. Undo restores the most recent surviving set's rest timer or clears it when no sets remain. Use **Pause workout** to stop both timers, then resume or return to the main menu with the pause saved. A pause survives closing the app. Time while the app is closed is counted only when the workout was left running. Completed workout duration excludes pauses and appears in history and Excel. Older workouts without a recorded duration show **Not recorded**.

The summary compares each result with the latest earlier non-skipped result for that exercise. At matching load/settings and RIR, an extra rep is **+1 rep**. When RIR differs, the **technical** change is `(current reps + current RIR) − (previous reps + previous RIR)`: for example, 6 reps at 0 RIR or F followed by 6 at 1 RIR is **+1 technical**; 6 at 2 RIR is **+2 technical**. F counts as zero in this calculation, but transitions between F and 0 RIR are explicitly shown. Missing RIR or changed load, units, measurement mode, or bodyweight for bodyweight-based exercises produces descriptive context instead of an improvement claim.

Use **Change comparison baseline** during a session or when viewing a saved workout to choose **Previous session** or **Last session with matching load/settings**. Summary comparisons show the source result and RIR without a date prefix, and exclude later workouts. The workout date remains in the summary heading. **Preferences → Comparison baseline** sets the default for new sessions.

## Templates and saved corrections

Open **Edit workout templates** from the main menu or Start workout. Select an exercise to move it up/down or remove it, or choose **Add exercise**. **Save template** applies the draft to future sessions; **Cancel changes** leaves the template unchanged. Workouts already started retain their original exercise order and configuration.

Open a workout in **Workout history** to correct an entry, workout date, or bodyweight. Each correction is reviewed before saving and keeps immutable before/after values, a timestamp, and an optional reason under **View revision history**. Bodyweight corrections update the session, weigh-in, and entry bodyweight values together. Historical exercise names, units, and measurement modes remain fixed. Corrections refresh Excel and do not change template defaults or workout timing.

## Backups and recovery

Backups are stored in a `backups/` folder beside the database. The app makes one daily snapshot on startup, a snapshot after every saved workout, and a snapshot before template edits, exercise-setting changes, saved corrections, or recovery. SQLite's backup API captures committed data consistently, including WAL data. Snapshots are validated and are not automatically pruned.

Use **Backups and recovery** to create or inspect snapshots, or recover missing history. Recovery adds missing sessions, results, weigh-ins, and audit records while preserving existing workouts, corrections, and preferences. Running recovery again does not duplicate the imported records. It also makes a backup before recovery.

If the database cannot be opened, startup offers a validated backup as a separate recovery copy. The original database and its journal files remain available. Subsequent launches use the recovery copy selected in `active-recovery.json` beside the database.

A missing or damaged recovery selection offers recovery again instead of silently opening an older or empty database. Database upgrades apply atomically; failed upgrades roll back. A database created by a newer app requires that newer version and is never downgraded automatically.

## Rest reminders

Reminders start **off**. In **Preferences → Rest reminder target and sound**, choose a target in seconds and optionally enable the terminal bell for new sessions. **Rest reminder settings** during a workout changes only that workout. The display shows **Rest complete** at the target; the counter continues upward. A sound-enabled reminder rings once for each logged set, including across closing and reopening the app. Audible playback depends on terminal bell settings.

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

A session row and bodyweight entry are written as soon as the workout begins. Every completed exercise entry, edit, skip, and undo is committed immediately. If the terminal is interrupted, the main menu exposes **Resume unfinished workout** with options to resume, save as incomplete, or discard. Discarding hides an unfinished workout from recovery while retaining its stored rows. Undo is available only during an active workout.

`Ctrl+C` exits safely; already completed entries remain stored. A value that was only half-typed at the exact moment of interruption is not considered a completed entry.

## Excel layout

The exporter rebuilds the workbook from one consistent SQLite snapshot, so the spreadsheet is a view rather than the database. Names, notes, and custom results remain literal text, including strings beginning with `=`. All five workout metadata rows stay frozen while scrolling. A failed export preserves the previous workbook, and the export path cannot overwrite the live database or its journal files.

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


## Creating a permanent workout command that uses the app’s virtual environment directly.
```
mkdir -p ~/.local/bin

printf '%s\n' '#!/usr/bin/env bash' \
'exec "/home/qn/Documents/workout logger/workout_logger_app/.venv/bin/python" -m workout_logger "$@"' \
> ~/.local/bin/workout

chmod +x ~/.local/bin/workout
```
And make sure ``` ~/.local/bin ``` is in you local path
```fish
fish_add_path ~/.local/bin
```
and now from any directory you can type workout and the logger should open directly

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
