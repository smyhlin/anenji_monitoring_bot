# Anenji inverter monitoring bot

Telegram bot that reads an Anenji / DessMonitor inverter over the LAN (Modbus RTU over
the WiFi dongle, no cloud), posts grid on/off signals into a group, and keeps uptime
statistics.

```text
🔴 Світла немає · 4год 36хв
│ 🔋 55% ████████░░ · 25.9 В · -8.4 А · -217 Вт · інв. 74%
│ ⚡ 230.0 В · 1.2 А · 50.00 Гц
│ 🏠 204 Вт · 230 ВА
│ 🌡️ 21 °C · 25 °C
🕐 15:42 · 01.11.2026
```

Runs on the CloudKey Gen2+ (`192.168.1.249`) as `anenji-monitor.service` because the
inverter dongle dials back on the LAN.

## Quick start

```bash
uv venv .venv && uv pip install -e .
cp config.example.toml config.toml        # token, chat ids
.venv/bin/python main.py --config config.toml
```

Open the bot in Telegram and press **Start** — a bot cannot message a user who never
started it. For the group feed, add the bot to the chat with **Change chat info**
rights and either send `/subscribe` there or list the chat in
`[telegram] default_chats`.

## Commands

| command | who | what |
|---|---|---|
| `/status` | admin, moderator | grid / battery / output / load / temps, with the bulb photo |
| `/stats [period]` | admin, moderator | uptime, outages, SOC range (`day`, `7 days`, `2 weeks`, `3m`, `2 years`) |
| `/battery`, `/pv` | admin, moderator | labelled sections |
| `/regs` | admin, moderator | register profile |
| `/all`, `/raw`, `/reconnect` | admin | registers, arbitrary reads, dongle reconnect |
| `/subscribe`, `/chats` | admin / chat admin | subscribe a chat, per-chat toggles, 🔄 refresh |

Moderators are read-only: asking for an admin command returns an explicit refusal.

## Documentation

| document | content |
|---|---|
| [docs/architecture.md](docs/architecture.md) | module map, data flow, storage schema, invariants |
| [docs/modbus.md](docs/modbus.md) | dongle protocol, framing, redial/backoff policy, simulator |
| [docs/registers.md](docs/registers.md) | `registers.toml` format, the shipped profile, how it was derived |
| [docs/telegram.md](docs/telegram.md) | roles, commands, message formats, chat picture policy |
| [docs/statistics.md](docs/statistics.md) | stored history, `/stats` windows, SOC calibration, XLSX import |
| [docs/deployment.md](docs/deployment.md) | CloudKey setup, systemd, `deploy/deploy.sh`, ops |
| [docs/troubleshooting.md](docs/troubleshooting.md) | startup recovery, symptom → cause, manual checks |

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
uvx ruff check anenji_monitor main.py tests tools
```

`anenji_monitor.simulator` provides a fake dongle for offline work; the integration
tests use it, including the full polling path. CI runs the same two commands on every
push (`.github/workflows/tests.yml`), plus a check that `config.example.toml` and
`registers.toml` stay loadable.

## License

MIT — see [LICENSE](LICENSE).
