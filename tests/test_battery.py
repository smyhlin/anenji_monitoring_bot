import unittest

from anenji_monitor.battery import (
    DEFAULT_CURVE,
    SOURCE_REGISTER,
    SOURCE_VOLTAGE,
    Curve,
    SocEstimator,
    curve_from_config,
)


class CurveTest(unittest.TestCase):
    def setUp(self):
        self.curve = Curve(DEFAULT_CURVE)

    def test_hits_anchor_points(self):
        self.assertEqual(self.curve.percent(25.90), 55)
        self.assertEqual(self.curve.percent(26.70), 100)
        self.assertEqual(self.curve.percent(22.40), 0)

    def test_interpolates_between_points(self):
        # 26.00 V sits halfway between (25.90, 55) and (26.10, 68)
        self.assertEqual(self.curve.percent(26.00), 62)

    def test_clamps_outside_the_table(self):
        self.assertEqual(self.curve.percent(21.00), 0)
        self.assertEqual(self.curve.percent(28.00), 100)

    def test_accepts_unsorted_points(self):
        curve = Curve(((26.0, 80), (25.0, 20), (25.5, 50)))
        self.assertEqual(curve.points[0], (25.0, 20))
        self.assertEqual(curve.percent(25.5), 50)

    def test_rejects_single_point(self):
        with self.assertRaises(ValueError):
            Curve(((25.0, 50),))

    def test_rejects_decreasing_curve(self):
        with self.assertRaises(ValueError):
            Curve(((25.0, 60), (26.0, 40)))

    def test_curve_from_config(self):
        curve = curve_from_config([[25.0, 20], [26.0, 80]])
        self.assertEqual(curve.percent(25.5), 50)


class EstimatorTest(unittest.TestCase):
    def test_voltage_source_uses_the_curve(self):
        estimator = SocEstimator(source=SOURCE_VOLTAGE)
        self.assertEqual(estimator.estimate({"battery_voltage": 25.9}), 55)
        self.assertEqual(estimator.estimate({"battery_voltage": 26.4}), 87)

    def test_voltage_source_ignores_the_register(self):
        estimator = SocEstimator(source=SOURCE_VOLTAGE)
        values = {"battery_voltage": 25.9, "battery_soc": 78}
        self.assertEqual(estimator.estimate(values), 55)

    def test_register_source_keeps_the_raw_value(self):
        estimator = SocEstimator(source=SOURCE_REGISTER)
        self.assertEqual(estimator.estimate({"battery_voltage": 25.9, "battery_soc": 78}), 78)

    def test_falls_back_to_register_without_voltage(self):
        estimator = SocEstimator(source=SOURCE_VOLTAGE)
        self.assertEqual(estimator.estimate({"battery_soc": 78}), 78)

    def test_returns_none_without_any_input(self):
        self.assertIsNone(SocEstimator().estimate({}))

    def test_rejects_unknown_source(self):
        with self.assertRaises(ValueError):
            SocEstimator(source="magic")

    def test_custom_curve_is_honoured(self):
        estimator = SocEstimator(curve=Curve(((25.0, 90), (26.0, 95))))
        self.assertEqual(estimator.estimate({"battery_voltage": 25.5}), 92)


if __name__ == "__main__":
    unittest.main()
