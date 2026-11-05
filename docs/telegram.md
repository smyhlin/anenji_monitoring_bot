# Telegram surface

## Roles

| role | config | commands |
|---|---|---|
| admin | `[telegram] admin_ids` | everything |
| moderator | `[telegram] moderator_ids` | `/status`, `/stats`, `/battery`, `/pv`, `/regs`, `/help` |
| chat admin | — | `/subscribe` in a chat they administer |
| anyone else | — | nothing in groups; «Цей бот приватний» in PM |

Filters are per handler (`FromIDs` in `bot.py`), not router-wide, so a moderator asking
for an admin command receives an explicit «Ця команда доступна лише адміністратору
бота» instead of silence or "unknown command".

## Commands

| command | notes |
|---|---|
| `/status` | verdict + emoji rows, sent as the bulb photo with a caption |
| `/stats [period]` | `day`, `7 days`, `2 weeks`, `3m`, `2 years`, Ukrainian words too |
| `/battery` | labelled battery section plus the calibration comparison |
| `/pv` | PV section (empty unless the profile defines PV registers) |
| `/regs` | the loaded register profile |
| `/all` | admin: every register, raw and scaled |
| `/raw <addr> [count]` | admin: arbitrary holding registers, hex + signed + unsigned |
| `/reconnect` | admin: drop the dongle session, the poller redials |
| `/subscribe` | register the current chat, or `/subscribe <chat_id>` from PM |
| `/chats` | admin: per-chat toggles and the 🔄 refresh button |
| `/help` | command list |

## Message formats

Status card (`/status`):

```
🔴 Світла немає · 4год 36хв
│ 🔋 55% ██████░░░░ · 25.9 В · -8.4 А · -217 Вт · інв. 74%
│ ⚡ 230.0 В · 1.2 А · 50.00 Гц
│ 🏠 204 Вт · 230 ВА
│ 🌡️ 21 °C · 25 °C
🕐 15:42 · 01.11.2026
```

Grid transitions, posted to subscribed chats:

```
🔴 11:16 Світло зникло            🟢 11:42 Світло з'явилося
🕐 Воно було 19год 14хв           🕐 Його не було 25хв
```

The input relay opens **immediately** on a spike, so an outage is published as soon as
it is seen (after `[events] confirm_samples`). Its reconnect is a verification: it
re-enables the light and drops it again if the grid is still bad. Therefore
`[events] grid_settle` (default 120 s) gates only the **restore** — a "light on" that
lasts less than the window is a failed retry, not a restore, and produces no post.
Timestamps are the first sample of each state, because the relay acted then and the
inverter reported later. While a restore is unverified, `/status` shows the new state
without a duration instead of presenting the old uptime as the outage length.

Battery thresholds (`alerts.soc_levels`, default `[50, 25]`):

```
🟡 Батарея: поріг 50%
🔴 світла немає · заряд 49% · інв. 72%
```

A level is posted only after the SOC stayed **at or below it for
`alerts.soc_settle`** seconds (default 600). The calibrated SOC follows pack
voltage, so a load spike sags it for a few minutes and it pops back up — without
the settle window that produced duplicate "поріг 50%" posts minutes apart. Any
reading above the level cancels the pending post and restarts the timer; the
level re-arms only after SOC climbs `+3` points above it, and the next crossing
waits for its own window.

Startup recovery cards are described in [troubleshooting.md](troubleshooting.md#startup-recovery)
and [architecture.md](architecture.md); the classifier itself lives in `recovery.py`.

## Delivery rules

- Transitions respect per-chat `grid_lost` / `grid_restored` toggles.
- Battery alerts respect the per-chat `battery` toggle.
- Inverter-offline warnings go to `admin_ids` only.
- A chat that removed the bot fails gracefully: the warning is logged one line, the
  stream continues.

## Chat picture

`sync_photos()` sets the chat photo to `assets/bulb_on.png` / `bulb_off.png`. It repaints
only when the state the chat's picture shows differs from the live state, or on a cold
start (no history, or downtime ≥ `recovery.reboot_after`), or when 🔄 is pressed in
`/chats`. The applied state is stored in `chats.photo_powered`, so quick restarts,
deploys and dongle retries never repaint. A failed call leaves the value unset and the
30-minute guard retries it.

The bot needs the **Change chat info** admin right in the chat; without it the call
fails with a one-line warning and everything else keeps working.

## Settings UI

`/chats` renders one block per chat with buttons:

```
🔴 зникло ✅ | 🟢 з'явилось ✅
🖼 фото ✅ | 🔋 батарея ✅ | 🗑
🔄 Оновити назви й фото
```

Toggles write to `chats`; 🔄 re-resolves titles and repaints the photo immediately,
reporting per-chat failures (`chat not found`, missing rights).
