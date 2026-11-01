"""Register map: which holding registers to read, how to scale and label them.

The layout is model-specific, so it lives in ``registers.toml`` rather than in
code. ``/raw`` can probe addresses that are not in the profile yet.
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

GRID_FREQUENCY_MIN_HZ = 40.0
GRID_VOLTAGE_MIN_V = 100.0
STATUS_GROUP_ORDER = ("grid", "battery", "output", "load", "ac", "pv", "temperature")


class RegisterMapError(Exception):
    """registers.toml is missing or malformed."""


@dataclass(frozen=True)
class Register:
    address: int
    key: str
    label: str
    unit: str
    scale: float
    group: str
    signed: bool = True

    def convert(self, raw: int) -> float:
        value = raw if self.signed else raw & 0xFFFF
        return round(float(value) * self.scale, self.digits)

    @property
    def digits(self) -> int:
        return 0 if self.scale >= 1 else round(-math.log10(self.scale))


class RegisterMap:
    def __init__(self, registers: tuple[Register, ...]) -> None:
        if not registers:
            raise RegisterMapError("register map is empty")
        self.registers = tuple(sorted(registers, key=lambda register: register.address))
        self.by_address = {register.address: register for register in self.registers}
        self.by_key = {register.key: register for register in self.registers}
        if len(self.by_address) != len(self.registers):
            raise RegisterMapError("duplicate register address")
        if len(self.by_key) != len(self.registers):
            raise RegisterMapError("duplicate register key")

    @classmethod
    def load(cls, path: str | Path) -> RegisterMap:
        config_path = Path(path)
        try:
            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RegisterMapError(f"{config_path} not found") from exc
        except tomllib.TOMLDecodeError as exc:
            raise RegisterMapError(f"{config_path} is not valid TOML: {exc}") from exc
        entries = raw.get("register", [])
        if not entries:
            raise RegisterMapError(f"{config_path} defines no [[register]] entries")
        registers = []
        for entry in entries:
            try:
                registers.append(
                    Register(
                        address=int(entry["address"]),
                        key=str(entry["key"]),
                        label=str(entry.get("label", entry["key"])),
                        unit=str(entry.get("unit", "")),
                        scale=float(entry.get("scale", 1)),
                        group=str(entry.get("group", "other")),
                        signed=bool(entry.get("signed", True)),
                    )
                )
            except KeyError as exc:
                raise RegisterMapError(f"{config_path}: entry missing {exc}") from exc
        return cls(tuple(registers))

    def batches(self, *, max_gap: int = 20, max_batch: int = 50) -> tuple[tuple[int, int], ...]:
        """Group addresses into (start, count) ranges so one request covers several registers."""
        batches: list[tuple[int, int]] = []
        start: int | None = None
        end = 0
        for address in sorted(self.by_address):
            if start is None:
                start = end = address
            elif address - end <= max_gap and address - start + 1 <= max_batch:
                end = address
            else:
                batches.append((start, end - start + 1))
                start = end = address
        if start is not None:
            batches.append((start, end - start + 1))
        return tuple(batches)

    def convert_all(self, raw: dict[int, int]) -> dict[str, float]:
        return {
            register.key: register.convert(raw[register.address])
            for register in self.registers
            if register.address in raw
        }

    def format(self, register: Register, values: dict[str, float]) -> str:
        value = values.get(register.key)
        if value is None:
            return "—"
        text = f"{value:.{register.digits}f}"
        return f"{text} {register.unit}".strip()

    def groups(self) -> list[str]:
        return list(dict.fromkeys(register.group for register in self.registers))

    def in_group(self, group: str) -> list[Register]:
        return [register for register in self.registers if register.group == group]

    def status_groups(self) -> list[str]:
        present = set(self.groups())
        ordered = [group for group in STATUS_GROUP_ORDER if group in present]
        return ordered + sorted(present - set(ordered) - {"diagnostics"})


def grid_present(values: dict[str, float]) -> bool | None:
    """None when the profile has no grid readings to judge by."""
    frequency = values.get("grid_frequency")
    voltage = values.get("grid_voltage")
    if frequency is None and voltage is None:
        return None
    return (frequency or 0.0) >= GRID_FREQUENCY_MIN_HZ or (voltage or 0.0) >= GRID_VOLTAGE_MIN_V
