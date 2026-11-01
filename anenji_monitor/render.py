"""Telegram message rendering for device state."""

from __future__ import annotations

import time
from html import escape
from pathlib import Path

from .poller import MonitorState, Reading
from .registers import RegisterMap, grid_present
from .text import battery_bar, clock, day_label, format_duration, soc_mark

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"

GROUP_TITLES = {
    "grid": "💡 Світло",
    "battery": "🔋 Батарея",
    "output": "⚡ Вихід",
    "load": "🏠 Навантаження",
    "ac": "🔌 AC",
    "pv": "☀️ PV",
    "temperature": "🌡️ Температура",
}
BATTERY_ORDER = ("battery_soc", "battery_voltage", "battery_current", "battery_power")


def bulb_path(powered: bool) -> Path | None:
    """PNG asset for the given grid state, when the assets are installed."""
    path = ASSET_DIR / ("bulb_on.png" if powered else "bulb_off.png")
    return path if path.exists() else None


def bulb_for(values: dict[str, float]) -> Path | None:
    powered = grid_present(values)
    return None if powered is None else bulb_path(powered)


def _age_text(seconds: float) -> str:
    return format_duration(seconds)


def _footer(state: MonitorState, reading: Reading, stale_after: float) -> list[str]:
    age = max(0.0, time.time() - reading.timestamp)
    lines = [f"<i>🕐 {clock(reading.timestamp)} · {day_label(reading.timestamp)}</i>"]
    if age > stale_after:
        lines.append(f"⚠️ дані застаріли ({_age_text(age)})")
    if state.last_error:
        lines.append(f"⚠️ остання помилка: <code>{escape(state.last_error)}</code>")
    return lines


def _no_data(state: MonitorState) -> str:
    lines = ["⚪ <b>Даних ще немає</b>"]
    if state.last_error:
        lines.append(f"⚠️ остання помилка: <code>{escape(state.last_error)}</code>")
    return "\n".join(lines)


def _grid_header(powered: bool | None, grid_since: float | None) -> str:
    if powered is None:
        return "⚪ <b>Стан мережі невідомий</b>"
    duration = ""
    if grid_since is not None:
        duration = " · " + format_duration(time.time() - grid_since)
    if powered:
        return f"🟢 <b>Світло є</b>{duration}"
    return f"🔴 <b>Світла немає</b>{duration}"


def _battery_line(registers: RegisterMap, values: dict[str, float]) -> str | None:
    if not registers.in_group("battery"):
        return None
    parts = []
    raw_soc = values.get("battery_soc")
    soc = values.get("soc", raw_soc)
    mark = soc_mark(soc) if soc is not None else "🔋"
    if soc is not None:
        parts.append(f"{soc:.0f}% {battery_bar(soc)}")
    for key in BATTERY_ORDER[1:]:
        register = registers.by_key.get(key)
        if register is not None and key != "battery_soc":
            parts.append(registers.format(register, values))
    if raw_soc is not None and soc is not None and abs(soc - raw_soc) >= 1:
        parts.append(f"інв. {raw_soc:.0f}%")
    return mark + " " + " · ".join(parts) if parts else None


def status(
    state: MonitorState,
    registers: RegisterMap,
    *,
    stale_after: float,
    grid_since: float | None = None,
) -> str:
    reading = state.latest
    if reading is None:
        return _no_data(state)

    values = reading.values
    powered = grid_present(values)
    details: list[str] = []

    if powered:
        details.append(
            "💡 "
            + " · ".join(
                registers.format(register, values) for register in registers.in_group("grid")
            )
        )

    battery = _battery_line(registers, values)
    if battery:
        details.append(battery)

    for group in registers.status_groups():
        if group in {"grid", "battery"}:
            continue
        emoji = GROUP_TITLES.get(group, group).split(" ", 1)[0]
        row = " · ".join(
            registers.format(register, values) for register in registers.in_group(group)
        )
        if row:
            details.append(f"{emoji} {row}")

    lines = [_grid_header(powered, grid_since)]
    if details:
        lines.append("<blockquote>" + "\n".join(details) + "</blockquote>")
    return "\n".join(lines + _footer(state, reading, stale_after))


def group(
    state: MonitorState, registers: RegisterMap, name: str, *, stale_after: float
) -> str:
    reading = state.latest
    if reading is None:
        return _no_data(state)
    members = registers.in_group(name)
    title = f"<b>{GROUP_TITLES.get(name, name.title())}</b>"
    if not members:
        return f"{title}\nУ профілі немає цих регістрів — див. /regs."
    lines = [title]
    lines += [
        f"{register.label}: <b>{registers.format(register, reading.values)}</b>"
        for register in members
    ]
    soc = reading.values.get("soc")
    raw_soc = reading.values.get("battery_soc")
    if soc is not None:
        note = f"🧮 Заряд (калібрування): <b>{soc:.0f} %</b>"
        if raw_soc is not None and abs(soc - raw_soc) >= 1:
            note += f" · регістр {raw_soc:.0f} %"
        lines.append(note)
    return "\n".join(lines + [""] + _footer(state, reading, stale_after))


def all_registers(state: MonitorState, registers: RegisterMap, *, stale_after: float) -> str:
    reading = state.latest
    if reading is None:
        return _no_data(state)
    rows = [("рег", "ключ", "сире", "значення")]
    rows += [
        (
            str(register.address),
            register.key,
            str(reading.raw.get(register.address, "—")),
            registers.format(register, reading.values),
        )
        for register in registers.registers
    ]
    width = max(len(row[1]) for row in rows)
    table = "\n".join(
        f"{address:>4}  {key:<{width}}  {raw:>6}  {value}" for address, key, raw, value in rows
    )
    return "\n".join(
        ["<b>🧾 Усі регістри</b>", f"<pre>{escape(table)}</pre>"] + _footer(state, reading, stale_after)
    )


def register_list(registers: RegisterMap) -> str:
    rows = [
        f"{register.address:>5}  {register.key:<20} {register.label} [{register.unit}] ×{register.scale:g}"
        for register in registers.registers
    ]
    return f"<b>📋 Профіль регістрів</b>\n<pre>{escape(chr(10).join(rows))}</pre>"


def raw_block(start: int, values: dict[int, int], registers: RegisterMap) -> str:
    rows = ["    рег   hex   signed  unsigned"]
    for address, value in sorted(values.items()):
        unsigned = value & 0xFFFF
        rows.append(f"{address:>7} 0x{unsigned:04X} {value:>7} {unsigned:>9}")
    known = ", ".join(
        f"{address}={registers.by_address[address].key}"
        for address in sorted(values)
        if address in registers.by_address
    )
    lines = [
        f"<b>🧾 Регістри {start}+{len(values)}</b>",
        f"<pre>{escape(chr(10).join(rows))}</pre>",
    ]
    if known:
        lines.append(f"<i>{escape(known)}</i>")
    return "\n".join(lines)
