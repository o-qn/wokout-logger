import unittest
from decimal import Decimal

from workout_logger.notation import format_entry, parse_decimal, comparison_message


class NotationTests(unittest.TestCase):
    def test_decimal_exact(self):
        self.assertEqual(parse_decimal("84.6"), Decimal("84.6"))
        self.assertEqual(parse_decimal("19,5"), Decimal("19.5"))

    def test_formats(self):
        self.assertEqual(format_entry({
            "measurement_type": "bodyweight_plus", "external_load": "15.5", "unit": "kg",
            "reps": 7, "rir_code": "1", "notes": "clean", "skipped": 0,
        }), "BW+15.5kg × 7 @1 — clean")
        self.assertEqual(format_entry({
            "measurement_type": "isometric", "duration_seconds": "5", "rounds": 5,
            "rest_seconds": "3", "rir_code": "0", "notes": "", "skipped": 0,
        }), "5s × 5 / 3s rest @0")
        self.assertEqual(format_entry({"skipped": 1}), "SKIPPED")

    def test_rep_comparison(self):
        previous = {
            "measurement_type": "external_weight", "external_load": "19.5", "unit": "kg",
            "reps": 4, "skipped": 0,
        }
        current = {
            "measurement_type": "external_weight", "external_load": "19.5", "unit": "kg",
            "reps": 5, "skipped": 0,
        }
        self.assertEqual(comparison_message(current, previous), "+1 rep vs previous session")


if __name__ == "__main__":
    unittest.main()
