"""Choose a chronological, clearly identified comparison for each result."""
from ..notation import load_signature

COMPARISON_MODES = {
    'previous': 'Previous session',
    'matching': 'Last session with matching load/settings',
}


class ComparisonMixin:
    def set_comparison_mode(self, mode, session_id=None):
        if mode not in COMPARISON_MODES:
            raise ValueError('Choose previous session or matching load/settings')
        if session_id is None:
            self.set_setting('comparison_mode', mode)
        else:
            if not self.get_session(session_id):
                raise ValueError('Workout not found')
            self.db.execute('UPDATE workout_sessions SET comparison_mode=? WHERE id=?', (mode, session_id))

    def comparison_entry(self, current, session_id, mode=None):
        reference = self.get_session(session_id)
        if not reference:
            return None
        mode = mode or reference.get('comparison_mode') or 'previous'
        if mode not in COMPARISON_MODES:
            raise ValueError('Unknown comparison baseline')
        previous = self.db.fetchall(
            """
            SELECT we.*, ws.session_date
            FROM workout_entries we JOIN workout_sessions ws ON ws.id=we.session_id
            WHERE we.exercise_id=? AND we.skipped=0 AND ws.status IN ('complete','incomplete')
              AND (ws.session_date<? OR (ws.session_date=? AND ws.id<?))
            ORDER BY ws.session_date DESC,ws.id DESC,we.id DESC
            """,
            (current['exercise_id'], reference['session_date'], reference['session_date'], session_id),
        )
        signature = load_signature(current)
        for row in previous:
            candidate = dict(row)
            if mode == 'previous' or load_signature(candidate) == signature:
                return candidate
        return None

    def comparison_preview(self, exercise, defaults, session_id):
        session = self.get_session(session_id)
        candidate = {
            'exercise_id': exercise['id'], 'measurement_type': exercise['measurement_type'],
            'unit': exercise.get('default_unit') or 'kg', 'bodyweight': session['bodyweight'],
            'resistance_unit': exercise.get('resistance_unit'),
        }
        defaults = defaults or {}
        for field in ('external_load','assistance','duration_seconds','rounds','rest_seconds','resistance_value'):
            candidate[field] = defaults.get(field, defaults.get('default_' + field))
        return self.comparison_entry(candidate, session_id)
