import unittest

from workout_logger.notation import comparison_message, format_entry, load_signature


class ComparisonAuditTests(unittest.TestCase):
    def row(self,**changes):
        row={'measurement_type':'external_weight','unit':'lb','external_load':'20','reps':6,'rir_code':'1'}
        row.update(changes)
        return row

    def test_weight_unit_aliases_compare_without_rewriting_historical_display(self):
        old=self.row(unit='lbs')
        new=self.row(reps=7)
        self.assertEqual(load_signature(old),load_signature(new))
        self.assertEqual(comparison_message(new,old),'+1 rep vs previous session')
        self.assertEqual(format_entry(old),'20lbs × 6 @1')
        self.assertNotEqual(load_signature(old),load_signature(self.row(unit='kg')))

    def test_negative_legacy_loading_cannot_be_reported_as_rep_improvement(self):
        message=comparison_message(self.row(external_load='-20',reps=7),self.row(external_load='-20'))
        self.assertIn('comparison unavailable',message)
        self.assertNotIn('+1 rep',message)

if __name__ == '__main__':
    unittest.main()
