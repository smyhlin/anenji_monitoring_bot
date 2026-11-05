# Troubleshooting

## Startup recovery

Durations only make sense if the bot knows what it missed, so the first reading after a
start is compared with `last_seen` and the stored grid state:

| downtime | last grid | grid now | card |
|---|---|---|---|
| no history | — | any | 🟢 Моніторинг запущено |
| < 5 min | any | any | silent |
| 5–30 min | any | any | 🔄 Бот був вимкнений · 🕐 без моніторингу 15хв |
| ≥ 30 min | 🔴 не було | 🟢 є | 🪫 Інвертор вимкнувся — батарея розрядилась |
| ≥ 30 min | other | any | 😴 Довга пауза моніторингу |

Each card states the current grid state, whether it changed while the bot was down, and
the SOC. The observed state is then adopted silently (`rebase`) so the stream does not
re-post the same transition a second later. `rebase` keeps the stored transition time
when the state is unchanged — that is why "Світла немає · 4год" survives restarts.

Thresholds: `[recovery] reboot_after` (300 s) and `drained_after` (1800 s).

## Symptom → cause

| symptom | cause / fix |
|---|---|
| `inverter connected` then repeated `dongle closed the connection` | connection churn: the dongle refuses a session started in the same instant as the previous drop. The link backs off 1→2→4→8→16→30 s; wait, or `/reconnect` |
| `... did not connect within 30s` | wrong `[inverter] host`, wrong subnet, or the dongle cannot reach `listen_port` on this host |
| `cannot listen on port 8899` | another instance (or an HA bridge) owns the dongle |
| `409 Conflict` on Telegram | two pollers running with the same token |
| all readings plausible but grid is always 0 | the grid really is down, or `grid_voltage` points at an unused register — check with `/raw` against the inverter display |
| SOC disagrees with the BMS | expected for register 229; the bot shows the calibrated value plus `інв. N%`. Re-anchor the `[battery] curve` |
| no «світло з'явилося» post while the grid is flapping | expected: a restore must hold `grid_settle` (120 s); shorter "on" windows are the relay's failed retries |
| battery alerts never fire | they use the **calibrated** SOC with a 3-point re-arm margin, and levels already crossed at startup stay silent (the startup card covers it) |
| group posts missing | chat not subscribed (`/subscribe`), event toggled off (`/chats`), or the bot was removed |
| chat photo never changes | bot lacks **Change chat info**, `🖼 фото` is off, or the photo already shows the current state (by design) |
| «updated group photo» spam | was a real bug; now pictures change only on state change / cold start / 🔄 |
| `chat not found` in logs | the id in `[telegram] default_chats` is wrong, or the bot was never added. Supergroup ids look like `-100…`; a basic group is `-<id>` |
| service restart-loops | `journalctl -u anenji-monitor -n 50`; an invalid `config.toml` is rejected loudly (`ConfigError`) — that is fail-fast working |

## Checking state by hand

```bash
cd /opt/anenji-monitor
.venv/bin/python -c "
from anenji_monitor.store import Store
import time
s = Store('state.db')
print('grid:', s.grid_state(), '| last_seen age:', time.time() - s.last_seen())
print('chats:', s.chats())
print('events:', len(s.grid_events(0, 9e9)))
s.close()"
```

## Dongle diagnostics

Single clean probe, no service running (stop it first — one session at a time):

```bash
systemctl stop anenji-monitor
.venv/bin/python - <<'EOF'
from anenji_monitor.modbus import AnenjiLink, build_read_request
link = AnenjiLink("192.168.1.42", connect_timeout=20, notify_interval=5, request_timeout=5)
link.notify(); srv = link._make_server(); srv.settimeout(20)
conn, addr = srv.accept()
conn.sendall(build_read_request(200, 30))
print(addr, conn.recv(128).hex(" "))
EOF
systemctl start anenji-monitor
```

A healthy answer is 65 bytes starting `01 03 3c …` (30 registers) within ~0.5 s.

## Restoring service

```bash
systemctl status anenji-monitor
systemctl restart anenji-monitor
journalctl -u anenji-monitor -f
```

If `state.db` was lost, subscriptions and history are gone; re-subscribe with
`/subscribe` (or `[telegram] default_chats`) and re-import a cloud export — see
[statistics.md](statistics.md#importing-a-dessmonitor-export).
