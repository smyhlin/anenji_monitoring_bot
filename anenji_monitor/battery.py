"""Battery state-of-charge from pack voltage.

The inverter's own SOC register drifts from the BMS reading, so the percentage the
bot reports is derived from pack voltage along a piecewise-linear curve that is
calibrated against the BMS. The register value stays available for comparison.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

# 24 V LiFePO4 (8S) pack, anchored on the BMS: 25.9 V ≈ 55 %.
DEFAULT_CURVE: tuple[tuple[float, float], ...] = (
    (22.40, 0),
    (24.00, 8),
    (24.40, 15),
    (24.80, 25),
    (25.20, 38),
    (25.50, 47),
    (25.70, 51),
    (25.90, 55),
    (26.10, 68),
    (26.30, 82),
    (26.50, 93),
    (26.70, 100),
)

SOURCE_VOLTAGE = "voltage"
SOURCE_REGISTER = "register"


@dataclass(frozen=True)
class Curve:
    points: tuple[tuple[float, float], ...]

    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ValueError("SOC curve needs at least two points")
        ordered = sorted(self.points)
        for (_, low), (_, high) in pairwise(ordered):
            if high < low:
                raise ValueError("SOC curve must not decrease with voltage")
        object.__setattr__(self, "points", tuple(ordered))

    def percent(self, voltage: float) -> float:
        points = self.points
        if voltage <= points[0][0]:
            return round(points[0][1], 0)
        if voltage >= points[-1][0]:
            return round(points[-1][1], 0)
        for (low_v, low_soc), (high_v, high_soc) in pairwise(points):
            if low_v <= voltage <= high_v:
                span = high_v - low_v
                share = 0.0 if span == 0 else (voltage - low_v) / span
                return round(low_soc + (high_soc - low_soc) * share, 0)
        return round(points[-1][1], 0)


class SocEstimator:
    """Puts the SOC the bot displays into `values["soc"]`."""

    def __init__(
        self,
        *,
        source: str = SOURCE_VOLTAGE,
        curve: Curve | None = None,
        voltage_key: str = "battery_voltage",
        register_key: str = "battery_soc",
    ) -> None:
        if source not in {SOURCE_VOLTAGE, SOURCE_REGISTER}:
            raise ValueError(f"unknown soc source {source!r}")
        self.source = source
        self.curve = curve or Curve(DEFAULT_CURVE)
        self._voltage_key = voltage_key
        self._register_key = register_key

    def estimate(self, values: dict[str, float]) -> float | None:
        if self.source == SOURCE_REGISTER:
            return values.get(self._register_key)
        voltage = values.get(self._voltage_key)
        if voltage is None:
            return values.get(self._register_key)
        return self.curve.percent(voltage)


def curve_from_config(points: list[list[float]]) -> Curve:
    return Curve(tuple((float(voltage), float(percent)) for voltage, percent in points))
