# Local Modbus link

The inverter's Wi-Fi dongle normally dials out to the **DessMonitor** portal — the
vendor's cloud service that parses its telemetry over the internet. To read the device
locally we take over that connection:

1. a UDP datagram `set>server=<our-ip>:8899;` is sent to the dongle on port **58899**;
2. the dongle opens a TCP session to us on port **8899**;
3. bare Modbus RTU frames travel over that socket — unit id 1, function 3 (read
   holding registers), CRC16/MODBUS, no MBAP header.

`AnenjiLink` (`anenji_monitor/modbus.py`) owns the whole exchange: notify, `accept()`,
frame assembly, CRC check, redial policy.

## Framing

Requests are `unit | func | addr_hi | addr_lo | count_hi | count_lo | crc_lo | crc_hi`.
Responses are read by length, not by one `recv()`: read 3 bytes, then `byte_count + 2`
more; an exception response (`func | 0x80`) is 5 bytes. Partial reads are accumulated.
Values are decoded as signed 16-bit two's complement (`to_signed`), with the profile's
`scale` applied on top.

## Timing and failure policy

| knob | default | why |
|---|---|---|
| `poll_interval` | 10 s | dongle is happy with this; faster buys nothing for a house |
| `connect_timeout` | 30 s | how long one dial-back attempt waits before giving up |
| `request_timeout` | 5 s | normal reply arrives in 0.3–0.5 s |
| `FIRST_REQUEST_TIMEOUT` | 2 s | a session that is dead on arrival answers nothing |
| paced redial | 1 s → 2 → 4 → 8 → 16 → 30 s cap | the dongle refuses a session started in the same instant as the previous drop |

Observed behaviour worth knowing:

- The dongle serves **one session at a time** and may close it right after a batch.
- Redialling instantly makes it accept and immediately close; paced redial fixes it.
- A clean probe after ~20 s of silence answers in ~0.4 s, so the device is healthy —
  it is our own connection churn that breaks it.
- A failed session (zero bytes) increments the backoff; any successful read resets it.
- `read_cycle` retries the failed batch once **only if earlier batches already
  returned data**; a cycle that read nothing propagates `LinkDown` and the poller
  waits `poll_interval` instead of hammering the dongle.

## Reading the device

The shipped profile reads 23 registers in **2 requests per cycle** (`171`, `200+30`).
Registers and their meaning are documented in [registers.md](registers.md); unknown
addresses can be probed at runtime with `/raw <addr> [count]`.

## Offline development

`python -m anenji_monitor.simulator` listens on `127.0.0.1:58899`, waits for the notify
datagram, dials back and serves synthetic registers. Point `[inverter] host` at
`127.0.0.1` and run `main.py` to exercise the whole stack without hardware. The
integration tests use the same `FakeDongle`.
