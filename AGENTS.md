# AGENTS.md — Anenji inverter monitoring bot

Project-specific instructions. Shared style rules come from the global agent config;
this file only adds what an agent cannot infer from the code.

## What this is

A Python/aiogram Telegram bot that polls an Anenji/DessMonitor inverter over the LAN
(Modbus RTU over the WiFi dongle) and posts grid on/off signals plus uptime statistics.
It runs as a systemd service on the CloudKey Gen2+ (`192.168.1.249`), **not** on a
workstation, because the dongle dials back on the LAN.

Deep dives: [docs/architecture.md](docs/architecture.md),
[docs/modbus.md](docs/modbus.md), [docs/registers.md](docs/registers.md),
[docs/telegram.md](docs/telegram.md), [docs/statistics.md](docs/statistics.md),
[docs/deployment.md](docs/deployment.md), [docs/troubleshooting.md](docs/troubleshooting.md).
Read the relevant one before changing behaviour — most of it encodes observed hardware
reality, not guesswork.

## Commands

```bash
uv venv .venv && uv pip install -e .
.venv/bin/python -m unittest discover -s tests -t .     # 163 tests, must stay green
uvx ruff check anenji_monitor main.py tests tools       # must stay clean
.venv/bin/python main.py --config config.toml           # local run (needs the dongle)
.venv/bin/python -m anenji_monitor.simulator            # fake dongle for offline work
deploy/deploy.sh                                        # rsync + restart on the CloudKey
```

Only stdlib plus `aiogram` — do not add dependencies without a stated reason.

## Hard invariants

1. **One process owns the dongle.** The device serves a single TCP session on 8899 and
   Telegram allows one long-poll. Never run a local bot while the service is running;
   stop one first. Duplicate pollers produce `409 Conflict` and dongle connect/drop churn.
2. **`grid_events` stores transitions only.** Inserting the same state twice inflates
   outage counts; `Store.set_grid_state` dedupes and `compact_grid_events()` cleans up.
3. **`grid_since` survives restarts while the state is unchanged.** `GridWatcher.rebase`
   must not reset it, otherwise `/status` reports "no power for 2 min" after every start.
4. **Chat pictures repaint only on a state change, a cold start, or 🔄.** The applied
   state lives in `chats.photo_powered`; every repaint posts an "updated group photo"
   service message into the chat, so unconditional calls are a bug.
5. **Statistics use the calibrated SOC** (`values["soc"]`), never the raw register 229.
6. **`state.db` is user data**: subscriptions, history, the grid anchor. Never delete it
   to fix code, never exclude it from backups.
7. **`config.toml` is synced by `deploy.sh`.** Edit the repo copy; changes made only on
   the server disappear on the next deploy.
8. **All user-facing copy is Ukrainian**, built in `text.py` / `render.py` / `stats.py`.
   Do not sprinkle literals into handlers.

## Layout

| path | role |
|---|---|
| `main.py` | wiring, startup report, task supervision |
| `anenji_monitor/modbus.py` | dongle transport, CRC, framing, redial/backoff |
| `anenji_monitor/poller.py` | batched reads → `Reading`/`MonitorState` |
| `anenji_monitor/events.py` | grid/battery watchers, `SignalEngine`, stream loop |
| `anenji_monitor/store.py` | SQLite schema and all queries |
| `anenji_monitor/battery.py` | voltage→SOC curve, `SocEstimator` |
| `anenji_monitor/registers.py` + `registers.toml` | register profile (data, not code) |
| `anenji_monitor/bot.py` | routers, roles, settings UI, publishing |
| `anenji_monitor/recovery.py` | startup classification |
| `tools/import_dessmonitor.py` | backfill from a cloud `.xlsx` |
| `deploy/` | systemd unit and deploy script |

## Conventions

- **Registers are data.** New metric = entry in `registers.toml` (+ group), not new code.
- **Handlers stay thin**: parse, call a module, render. Business logic belongs in
  `events.py` / `stats.py` / `store.py` and must be unit-testable without Telegram.
- **Tests**: `unittest` only (no pytest), fakes over mocks — `FakeDongle` for the device,
  `RecordingSession` for aiogram. Every behavioural fix gets a test that failed before.
- **Fail fast on config**: invalid TOML raises `ConfigError`, which crash-loops the
  service loudly. Do not "tolerate" broken config.

## Hardware facts that are easy to get wrong

- The device is equipment type `0x7804`; the public `attilagyurman/anenji-local-modbus`
  map does **not** apply (203/252–255/277–281 read 0 here). Do not "fix" the profile back
  to it; see [docs/registers.md](docs/registers.md).
- The dongle refuses a session started in the same instant as the previous drop; hence
  the paced redial and exponential backoff. Do not remove them to "simplify".
- Replies arrive in ~0.4 s; a session that is dead on arrival answers nothing, hence the
  2 s deadline on the first request of a fresh session.
- The inverter clock runs **1 h ahead** of local time — matters for imports.
- Grid presence threshold: ≥ 40 Hz or ≥ 100 V.

## Workflow

1. Change code → run tests and ruff.
2. `deploy/deploy.sh` → verify `systemctl is-active anenji-monitor` and
   `journalctl -u anenji-monitor --since "-1 min"` for `poll failed` / tracebacks.
3. Verify the behaviour that changed, not just the process: read `state.db` through
   `Store`, or exercise handlers with a `RecordingSession`.
4. If you must stop the service for a one-off (imports, dongle probes), start it again
   in the same command — a forgotten stop means no monitoring at all.

## Do not

- run two pollers, or a local instance "just to test" against the live token;
- rewrite `grid_since`, drop `state.db`, or hand-edit `grid_events` timestamps;
- call `setChatPhoto` outside the documented triggers;
- re-pin tests to incidental wording: if copy in `text.py` changes, update the tests with
  it, but never weaken a behavioural assertion to make it pass;
- commit `config.toml` (token) or `state.db` — both are gitignored on purpose.
