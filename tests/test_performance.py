import unittest

from workout_logger.notation import comparison_message, load_signature


def entry(**changes):
    result = {
        "measurement_type": "external_weight",
        "external_load": "19.5",
        "unit": "kg",
        "bodyweight": "84.6",
        "reps": 6,
        "rir_code": "0",
    }
    result.update(changes)
    return result


class PerformanceTests(unittest.TestCase):
    def test_same_rir_rep_deltas(self):
        for rir in ("0", "F", "1", "12"):
            for reps, expected in ((7, "+1 rep"), (8, "+2 reps"), (5, "-1 rep"), (4, "-2 reps")):
                with self.subTest(rir=rir, reps=reps):
                    self.assertEqual(
                        comparison_message(entry(reps=reps, rir_code=rir), entry(rir_code=rir)),
                        f"{expected} vs previous session",
                    )

    def test_same_reps_and_rir_shows_no_change(self):
        self.assertEqual(comparison_message(entry(), entry()), "No change vs previous session")

    def test_user_technical_examples_from_zero_and_failure(self):
        for old_rir in ("0", "F"):
            for new_rir in (1, 2, 4, 12):
                with self.subTest(old_rir=old_rir, new_rir=new_rir):
                    self.assertEqual(
                        comparison_message(entry(rir_code=str(new_rir)), entry(rir_code=old_rir)),
                        f"+{new_rir} technical; reps: 6 → 6; RIR: {old_rir} → {new_rir} vs previous session",
                    )

    def test_technical_delta_combines_reps_and_rir(self):
        cases = (
            (7, "1", 6, "0", "+2 technical"),
            (7, "0", 6, "1", "No technical change"),
            (7, "0", 6, "2", "-1 technical"),
            (6, "1", 6, "3", "-2 technical"),
            (5, "3", 6, "0", "+2 technical"),
            (6, "1.5", 6, "0", "+1.5 technical"),
        )
        for cur_reps, cur_rir, prev_reps, prev_rir, expected in cases:
            with self.subTest(case=(cur_reps, cur_rir, prev_reps, prev_rir)):
                message = comparison_message(entry(reps=cur_reps, rir_code=cur_rir), entry(reps=prev_reps, rir_code=prev_rir))
                self.assertEqual(message, f"{expected}; reps: {prev_reps} → {cur_reps}; RIR: {prev_rir} → {cur_rir} vs previous session")
                self.assertNotIn("rep vs", message)

    def test_failure_and_zero_are_always_distinguished(self):
        for old, new in (("0", "F"), ("F", "0")):
            for reps, technical in ((6, "No technical change"), (7, "+1 technical")):
                with self.subTest(old=old, new=new, reps=reps):
                    self.assertEqual(
                        comparison_message(entry(reps=reps, rir_code=new), entry(rir_code=old)),
                        f"{technical}; reps: 6 → {reps}; RIR: {old} → {new} vs previous session",
                    )

    def test_equivalent_decimal_load_and_rir_are_comparable(self):
        self.assertEqual(
            comparison_message(entry(external_load="19.50", reps=7, rir_code="1.00"), entry(rir_code="1")),
            "+1 rep vs previous session",
        )
        self.assertEqual(load_signature(entry(external_load="19,500")), load_signature(entry()))
        self.assertNotEqual(load_signature(entry(external_load=0)), load_signature(entry(external_load=None)))

    def test_missing_or_invalid_rir_does_not_claim_rep_improvement(self):
        for old, new in ((None, None), ("", ""), (None, "0"), ("0", None), ("bad", "bad"), ("-1", "0"), ("NaN", "F"), ("Infinity", "0")):
            with self.subTest(old=old, new=new):
                message = comparison_message(entry(reps=7, rir_code=new), entry(rir_code=old))
                self.assertIn("reps: 6 → 7", message)
                self.assertIn("performance comparison unavailable", message)
                self.assertNotIn("+1", message)
                self.assertNotIn("technical", message)

    def test_invalid_reps_do_not_crash_or_produce_improvement(self):
        for reps in (None, "bad", "NaN", "Infinity", -1, "6.5"):
            with self.subTest(reps=reps):
                message = comparison_message(entry(reps=reps), entry())
                self.assertIn("performance comparison unavailable", message)
                self.assertNotIn("technical", message)

    def test_changed_load_or_unit_describes_settings_without_pr(self):
        for changes in ({"external_load": "20"}, {"external_load": "0"}, {"unit": "lb"}, {"external_load": None}):
            with self.subTest(changes=changes):
                message = comparison_message(entry(reps=7, **changes), entry())
                self.assertTrue(message.startswith("Settings changed:"))
                self.assertNotIn("+1 rep", message)
                self.assertNotIn("technical", message)

    def test_unknown_loading_does_not_count_a_pr(self):
        for load in (None, "bad", "NaN"):
            with self.subTest(load=load):
                message = comparison_message(entry(external_load=load, reps=7), entry(external_load=load))
                self.assertIn("Loading unavailable", message)
                self.assertNotIn("+1 rep", message)

    def test_measurement_change_does_not_compare_reps(self):
        message = comparison_message(entry(measurement_type="bodyweight", reps=7), entry())
        self.assertIn("Measurement changed: external weight → bodyweight", message)
        self.assertNotIn("+1 rep", message)

    def test_bodyweight_loading_changes_are_contextual(self):
        for mode in ("bodyweight", "bodyweight_plus", "assistance"):
            old = entry(measurement_type=mode, assistance="10")
            new = {**old, "bodyweight": "85", "reps": 7}
            with self.subTest(mode=mode):
                message = comparison_message(new, old)
                self.assertIn("BW: 84.6 → 85 kg", message)
                self.assertNotIn("+1 rep", message)
                self.assertEqual(comparison_message({**old, "bodyweight": "84.60", "reps": 7}, old), "+1 rep vs previous session")

    def test_resistance_unit_change_is_not_a_rep_pr(self):
        old = entry(measurement_type="resistance_units", resistance_value="3", resistance_unit="springs")
        self.assertEqual(comparison_message({**old, "resistance_value": "3.00", "reps": 7}, old), "+1 rep vs previous session")
        for changes in ({"resistance_value": "4"}, {"resistance_unit": "bands"}):
            with self.subTest(changes=changes):
                message = comparison_message({**old, **changes, "reps": 7}, old)
                self.assertIn("Settings changed:", message)
                self.assertNotIn("+1 rep", message)

    def test_bodyweight_delta_stays_in_kg_when_external_load_is_lb(self):
        for mode in ("bodyweight_plus", "assistance"):
            with self.subTest(mode=mode):
                old = entry(measurement_type=mode, unit="lb", assistance="20")
                message = comparison_message({**old, "bodyweight": "85", "reps": 7}, old)
                self.assertIn("BW: 84.6 → 85 kg", message)
                self.assertNotIn("85 lb", message)

    def test_assistance_change_is_described_without_pr(self):
        old = entry(measurement_type="assistance", assistance="10")
        message = comparison_message({**old, "assistance": "8", "reps": 7}, old)
        self.assertIn("Settings changed:", message)
        self.assertIn("10kg assistance", message)
        self.assertIn("8kg assistance", message)
        self.assertNotIn("+1 rep", message)

    def test_isometrics_and_custom_never_use_rep_or_technical_math(self):
        cases = (
            (entry(measurement_type="isometric", duration_seconds="5", rounds=5, rest_seconds="3"), {"rir_code": "2", "reps": 7}),
            (entry(measurement_type="isometric", duration_seconds="5", rounds=5, rest_seconds="3"), {"duration_seconds": "6"}),
            (entry(measurement_type="custom", custom_result="level 1"), {"custom_result": "level 2", "rir_code": "2", "reps": 7}),
        )
        for old, changes in cases:
            with self.subTest(mode=old["measurement_type"], changes=changes):
                message = comparison_message({**old, **changes}, old)
                self.assertNotIn("technical", message)
                self.assertNotIn("+1 rep", message)
                self.assertIn("→", message)
                self.assertEqual(comparison_message(old, old), "No change vs previous session")

    def test_amrap_respects_rir(self):
        old = entry(measurement_type="amrap", rir_code="F")
        self.assertEqual(comparison_message({**old, "reps": 7}, old), "+1 rep vs previous session")
        self.assertIn("+1 technical", comparison_message({**old, "rir_code": "1"}, old))

    def test_no_comparison_for_first_or_skipped_set(self):
        self.assertEqual(comparison_message(entry(), None), "")
        self.assertEqual(comparison_message(entry(skipped=1), entry()), "")
        self.assertEqual(comparison_message(entry(), entry(skipped=1)), "")


if __name__ == "__main__":
    unittest.main()
