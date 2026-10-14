import unittest

from workout_logger.measurements import measurement_unit
from workout_logger.models import MEASUREMENT_TYPES


class MeasurementUnitTests(unittest.TestCase):
    def test_omitted_units_follow_the_measurement_type(self):
        expected = {
            "external_weight": "kg", "bodyweight": "kg", "bodyweight_plus": "kg",
            "assistance": "kg", "isometric": "sec", "amrap": "reps",
            "resistance_units": "units", "custom": "units",
        }
        self.assertEqual(set(expected), set(MEASUREMENT_TYPES))
        for mode, unit in expected.items():
            with self.subTest(mode=mode):
                self.assertEqual(measurement_unit(mode), unit)

    def test_loaded_modes_retain_compatible_previous_mass_units(self):
        for mode in ("external_weight", "bodyweight_plus", "assistance"):
            for previous, expected in (("kg", "kg"), ("lb", "lb"), ("g", "g"), ("oz", "oz"),
                                       (" LBS ", "lb"), ("Kilograms", "kg")):
                with self.subTest(mode=mode, previous=previous):
                    self.assertEqual(measurement_unit(mode, previous=previous), expected)

    def test_loaded_modes_replace_incompatible_previous_units_with_kg(self):
        for mode in ("external_weight", "bodyweight_plus", "assistance"):
            for previous in (None, "", " ", "reps", "sec", "springs", "units", 5):
                with self.subTest(mode=mode, previous=previous):
                    self.assertEqual(measurement_unit(mode, previous=previous), "kg")

    def test_bodyweight_omitted_unit_always_defaults_to_kg(self):
        self.assertEqual(measurement_unit("bodyweight", previous="lb"), "kg")

    def test_non_mass_modes_use_their_own_default_instead_of_previous_unit(self):
        for mode, expected in (("amrap", "reps"), ("isometric", "sec"),
                               ("resistance_units", "units"), ("custom", "units")):
            with self.subTest(mode=mode):
                self.assertEqual(measurement_unit(mode, previous="lb"), expected)

    def test_explicit_mass_aliases_are_canonicalized(self):
        aliases = {
            "kg": "kg", "kgs": "kg", "KILO": "kg", "kilos": "kg",
            "kilogram": "kg", " Kilograms ": "kg",
            "lb": "lb", " LBS ": "lb", "pound": "lb", "Pounds": "lb",
            "g": "g", "gram": "g", " GRAMS ": "g",
            "oz": "oz", "ounce": "oz", " OUNCES ": "oz",
        }
        for mode in ("external_weight", "bodyweight", "bodyweight_plus", "assistance"):
            for requested, expected in aliases.items():
                with self.subTest(mode=mode, requested=requested):
                    self.assertEqual(measurement_unit(mode, requested, previous="oz"), expected)

    def test_explicit_invalid_mass_units_are_rejected(self):
        for mode in ("external_weight", "bodyweight", "bodyweight_plus", "assistance"):
            for requested in ("reps", "sec", "springs", "units", "stones"):
                with self.subTest(mode=mode, requested=requested), self.assertRaises(ValueError):
                    measurement_unit(mode, requested, previous="kg")

    def test_explicit_blank_units_are_rejected_in_every_mode(self):
        for mode in MEASUREMENT_TYPES:
            for requested in ("", " ", "\t\n"):
                with self.subTest(mode=mode, requested=requested), self.assertRaises(ValueError):
                    measurement_unit(mode, requested)

    def test_explicit_custom_unit_labels_are_preserved_for_other_modes(self):
        for mode in ("amrap", "isometric", "resistance_units", "custom"):
            with self.subTest(mode=mode):
                self.assertEqual(measurement_unit(mode, " Heavy Springs "), "Heavy Springs")
                self.assertEqual(measurement_unit(mode, "lbs"), "lbs")

    def test_non_text_requested_units_are_rejected(self):
        for mode in MEASUREMENT_TYPES:
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                measurement_unit(mode, 5)

    def test_unknown_measurement_type_is_rejected(self):
        for mode in ("unknown", "", None, ["amrap"]):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                measurement_unit(mode, "kg", previous="lb")


if __name__ == "__main__":
    unittest.main()
