# Register profile (`registers.toml`)

The register layout is model-specific, so it is **data, not code**. `registers.toml`
is loaded into a `RegisterMap` (`anenji_monitor/registers.py`) at startup.

## Entry format

```toml
[[register]]
address = 215
key = "battery_voltage"
label = "Напруга батареї"   # shown in /battery, /all, /regs
unit = "В"
scale = 0.1                  # value = raw * scale
group = "battery"            # section used by /status and the /battery view
signed = true                # false for bitfields/identity registers
```

- `group` values drive the status card: `grid`, `battery`, `output`, `load`, `ac`,
  `pv`, `temperature`, plus `diagnostics` (visible only in `/all`).
- Grouping for reads is automatic: addresses within `max_gap` (20) and inside
  `max_batch` (50) go out in one Modbus request.
- Grid presence = `grid_frequency ≥ 40 Hz` **or** `grid_voltage ≥ 100 V`
  (`registers.grid_present`), whichever keys the profile provides.

## The shipped profile

Unit: Anenji, equipment type `0x7804` (30724), 24 V LiFePO4 bank. Live block:

| address | key | reading (example) |
|---|---|---|
| 171 | `equipment_type` | 30724, unsigned |
| 200 | `status_flags` | 0xB000, unsigned |
| 201 | `status_code` | 3 |
| 202 / 203 / 204 | `grid_voltage` / `grid_frequency` / `grid_power` | 0 when the grid is down |
| 205 / 206 / 207 | `ac_voltage` / `ac_current` / `ac_frequency` (block 1) | 229.9 V · 2.2 A · 50 Hz |
| 208 | `unknown_208` | ~42, unidentified |
| 210 / 211 / 212 | AC block 2, unidentified duplicates | 229.9 V · 1.8 A · 50 Hz |
| 213 / 214 | `load_power` / `load_apparent_power` | 381 W · 413 VA |
| 215 / 216 / 217 | `battery_voltage` / `battery_current` / `battery_power` | 25.9 V · −9.8 A · −254 W |
| 218 / 219 | `dc_voltage` / `dc_current`, unidentified | 360.5 V · 14.9 A |
| 226 / 227 | `inverter_temp` / `dc_temp` | 21 °C · 26 °C |
| 229 | `battery_soc` | the inverter's own SOC — drifts, see below |

## How the profile was derived

The public `attilagyurman/anenji-local-modbus` map does **not** apply to this unit:
203/252–255/277–281 read `0` here. The shipped profile comes from:

1. scanning holding registers `0..1200`;
2. repeating the scan 8 s apart — drifting addresses carry telemetry, static ones are
   settings or identity;
3. cross-checking the live block against
   [`Millerderek/anenji-ha-bridge`](https://github.com/Millerderek/anenji-ha-bridge),
   which decodes the same 200..239 window.

Self-consistency pinned the meaning: `battery_power ≈ battery_voltage ×
battery_current` (−254 W ≈ 25.9 V × −9.8 A) and `load_apparent_power ≈ ac_voltage ×
ac_current_2` (413 VA ≈ 229.9 V × 1.8 A).

Registers that remain unidentified stay in `diagnostics` so they can be watched without
being presented as facts.

## Adding a register

1. find the address and scaling (probe with `/raw 240 20` while the inverter is doing
   the thing you want to measure — values drift when they are real);
2. add an entry, restart, and it appears in `/status` (if its group is a known one) and
   `/all`;
3. keep `signed = false` for bitfields and identity registers.

`battery_soc` (229) is kept for comparison only: the percentage the bot reports is
computed from pack voltage — see [statistics.md](statistics.md#battery-percentage).
