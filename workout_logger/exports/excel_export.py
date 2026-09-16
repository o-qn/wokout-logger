from __future__ import annotations

from pathlib import Path
from collections import OrderedDict

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter

from ..database import Database
from ..notation import format_entry


class ExcelExporter:
    def __init__(self, db: Database):
        self.db = db

    def export(self, output_path: Path | str) -> Path:
        output = Path(output_path).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)

        wb = Workbook()
        ws = wb.active
        ws.title = "Workouts"
        self._workouts_sheet(ws)
        self._exercise_history_sheet(wb.create_sheet("Exercise History"))
        self._bodyweight_sheet(wb.create_sheet("Bodyweight"))
        self._raw_sheet(wb.create_sheet("Raw Data"))

        for sheet in wb.worksheets:
            sheet.freeze_panes = "B2" if sheet.title == "Workouts" else "A2"
            self._autosize(sheet)

        tmp = output.with_suffix(output.suffix + ".tmp")
        wb.save(tmp)
        tmp.replace(output)
        return output

    def _sessions(self):
        return [dict(r) for r in self.db.fetchall(
            """
            SELECT s.*, t.label AS template_label
            FROM workout_sessions s
            LEFT JOIN workout_templates t ON t.id=s.template_id
            WHERE s.status IN ('complete','incomplete')
            ORDER BY s.session_date, s.id
            """
        )]

    def _workouts_sheet(self, ws):
        sessions = self._sessions()
        ws.cell(1, 1, "Exercise")
        ws.cell(2, 1, "Bodyweight")
        ws.cell(3, 1, "Status")
        ws.cell(4, 1, "Workout")
        for row in range(1, 5):
            ws.cell(row, 1).font = Font(bold=True)

        for col, session in enumerate(sessions, start=2):
            label = session.get("template_label") or "—"
            ws.cell(1, col, session["session_date"])
            ws.cell(2, col, f"{session['bodyweight']} kg")
            ws.cell(3, col, session["status"])
            ws.cell(4, col, f"Workout {label}")
            ws.cell(1, col).font = Font(bold=True)

        # Stable union of exercise snapshot names, then current display order for never-used exercises.
        names = OrderedDict()
        for r in self.db.fetchall(
            """
            SELECT we.exercise_name_snapshot
            FROM workout_entries we
            JOIN workout_sessions s ON s.id=we.session_id
            WHERE s.status IN ('complete','incomplete')
            ORDER BY s.session_date, s.id, we.exercise_order
            """
        ):
            names.setdefault(r["exercise_name_snapshot"], None)
        for r in self.db.fetchall("SELECT name FROM exercises ORDER BY display_order, id"):
            names.setdefault(r["name"], None)

        name_rows = {name: idx for idx, name in enumerate(names.keys(), start=5)}
        for name, row_idx in name_rows.items():
            ws.cell(row_idx, 1, name)

        session_col = {s["id"]: i for i, s in enumerate(sessions, start=2)}
        entries = self.db.fetchall(
            """
            SELECT we.* FROM workout_entries we
            JOIN workout_sessions s ON s.id=we.session_id
            WHERE s.status IN ('complete','incomplete')
            ORDER BY s.session_date, s.id, we.exercise_order
            """
        )
        for raw in entries:
            e = dict(raw)
            row_idx = name_rows[e["exercise_name_snapshot"]]
            col_idx = session_col[e["session_id"]]
            ws.cell(row_idx, col_idx, format_entry(e))

    def _exercise_history_sheet(self, ws):
        headers = ["Date", "Workout", "Exercise", "Performance", "Bodyweight", "Notes"]
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True)
        rows = self.db.fetchall(
            """
            SELECT we.*, s.session_date, s.bodyweight AS session_bodyweight, t.label AS template_label
            FROM workout_entries we
            JOIN workout_sessions s ON s.id=we.session_id
            LEFT JOIN workout_templates t ON t.id=s.template_id
            WHERE s.status IN ('complete','incomplete')
            ORDER BY we.exercise_name_snapshot, s.session_date, s.id
            """
        )
        for raw in rows:
            e = dict(raw)
            ws.append([
                e["session_date"],
                f"Workout {e['template_label']}" if e["template_label"] else "",
                e["exercise_name_snapshot"],
                format_entry(e),
                e["session_bodyweight"],
                e["notes"],
            ])

    def _bodyweight_sheet(self, ws):
        ws.append(["Date", "Bodyweight (kg)", "Workout Status"])
        for c in ws[1]:
            c.font = Font(bold=True)
        rows = self.db.fetchall(
            """
            SELECT bw.entry_date, bw.bodyweight, s.status
            FROM bodyweight_entries bw
            JOIN workout_sessions s ON s.id=bw.session_id
            WHERE s.status IN ('complete','incomplete')
            ORDER BY bw.entry_date, bw.id
            """
        )
        for r in rows:
            ws.append([r["entry_date"], r["bodyweight"], r["status"]])

    def _raw_sheet(self, ws):
        headers = [
            "session_id", "date", "workout", "status", "exercise_order", "exercise", "measurement_type",
            "unit", "bodyweight", "external_load", "assistance", "reps", "duration_seconds", "rounds",
            "rest_seconds", "resistance_value", "resistance_unit", "custom_result", "rir", "failure",
            "notes", "skipped",
        ]
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True)
        rows = self.db.fetchall(
            """
            SELECT we.*, s.session_date, s.status, t.label AS template_label
            FROM workout_entries we
            JOIN workout_sessions s ON s.id=we.session_id
            LEFT JOIN workout_templates t ON t.id=s.template_id
            WHERE s.status IN ('complete','incomplete')
            ORDER BY s.session_date, s.id, we.exercise_order
            """
        )
        for r in rows:
            ws.append([
                r["session_id"], r["session_date"], r["template_label"], r["status"], r["exercise_order"],
                r["exercise_name_snapshot"], r["measurement_type"], r["unit"], r["bodyweight"],
                r["external_load"], r["assistance"], r["reps"], r["duration_seconds"], r["rounds"],
                r["rest_seconds"], r["resistance_value"], r["resistance_unit"], r["custom_result"],
                r["rir_code"], r["failure"], r["notes"], r["skipped"],
            ])

    def _autosize(self, ws):
        widths = {}
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                widths[cell.column] = min(max(widths.get(cell.column, 0), len(str(cell.value)) + 2), 60)
                cell.alignment = Alignment(vertical="top")
        for col_idx, width in widths.items():
            ws.column_dimensions[get_column_letter(col_idx)].width = width
