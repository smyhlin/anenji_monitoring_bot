# Deployment

## Where it runs

| | |
|---|---|
| host | Ubiquiti CloudKey Gen2+ (`uck-g2-plus`, tailnet `100.81.10.73`) |
| LAN | `192.168.1.249`, Debian 11, aarch64, kernel 3.18 |
| path | `/opt/anenji-monitor` |
| unit | `anenji-monitor.service` (`enabled`, `Restart=always`) |
| SSH | `ssh -i ~/.ssh/cloudkey_uckg2p_ed25519 root@192.168.1.249` |

It lives on the box **because the dongle dials back on the LAN**: `set>server=<ip>:8899`
has to point at a host the inverter can reach, and the CloudKey is that host. Running it
on a laptop breaks the moment the laptop sleeps or changes network.

Access instructions for the device live in `~/Projects/cloudkey` (`credentials/ACCESS.md`,
`CLAUDE.md`); do not duplicate credentials here.

## First-time setup

Debian 11 ships Python 3.9; the code needs 3.11+ (`tomllib`, runtime unions), so the
interpreter comes from `uv`:

```bash
HOST=root@192.168.1.249
KEY=~/.ssh/cloudkey_uckg2p_ed25519
DIR=/opt/anenji-monitor

ssh -i $KEY $HOST 'curl -LsSf https://astral.sh/uv/install.sh | sh'
ssh -i $KEY $HOST "export PATH=/root/.local/bin:\$PATH && uv python install 3.12"
ssh -i $KEY $HOST "mkdir -p $DIR"
rsync -az -e "ssh -i $KEY" ./ "$HOST:$DIR/"
ssh -i $KEY $HOST "cd $DIR && export PATH=/root/.local/bin:\$PATH && \
  uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python aiogram"

ssh -i $KEY $HOST "cp $DIR/deploy/anenji-monitor.service /etc/systemd/system/ && \
  systemctl daemon-reload && systemctl enable --now anenji-monitor"
```

## Updating

```bash
deploy/deploy.sh          # rsync + chown + systemctl restart
```

The script excludes `.venv`, `__pycache__`, `*.egg-info`, `.git` and `state.db*`.
`config.toml` **is** synced: edit the repo copy, or the next deploy overwrites the
server's.

## Service unit

`deploy/anenji-monitor.service`:

- `Restart=always`, `RestartSec=10`, `StartLimitIntervalSec=0` — crash loops keep
  retrying instead of giving up;
- `WantedBy=multi-user.target` — starts on every boot;
- runs as root with `WorkingDirectory=/opt/anenji-monitor`;
- `PYTHONUNBUFFERED=1` so journald gets lines immediately.

## Operating it

```bash
systemctl status anenji-monitor
journalctl -u anenji-monitor -f
systemctl restart anenji-monitor
journalctl -u anenji-monitor --since "1 hour ago" -o cat | grep -c "poll failed"
```

Healthy start looks like:

```
poller: polling 23 registers in 2 batch(es) every 10s
bot @invertor_light_bot ready, polling 192.168.1.42:8899
modbus: inverter connected from 192.168.1.42:xxxxx after 1 notify attempt(s)
```

## Things that bite

- **One instance only.** The dongle serves one TCP session and Telegram one long-poll.
  A local copy on a laptop stealing either produces connect/drop churn and `409 Conflict`.
- **`state.db` is data.** Keep it across deploys (the script does). It holds
  subscriptions, the grid anchor, history and statistics.
- **Timezone matters.** Message timestamps use the process timezone; the box is
  `Europe/Kiev`, matching the inverter's location.
- **Port 8899 must be free** for the dial-back; a competing bridge or a previous
  instance blocks it.
- **Device clock is +1 h** relative to local time; relevant when importing exports
  (`--shift-minutes -60`).

## Local development

```bash
uv venv .venv && uv pip install -e .        # or .venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -t .
uvx ruff check anenji_monitor main.py tests tools
.venv/bin/python main.py --config config.toml
```

Without hardware, run the simulator in another terminal:

```bash
.venv/bin/python -m anenji_monitor.simulator --notify-port 58899
# then set [inverter] host = "127.0.0.1"
```
