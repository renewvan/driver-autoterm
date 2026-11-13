# driver-autoterm

Autoterm AIR 2D diesel heater driver for the renewvan hub (serial UART to MQTT)

Native MQTT **Driver** for an Autoterm AIR 2D diesel heater. It reads the heater over a
9600 8N1 UART (`/dev/ttyAUTOTERM`), publishes the `heater` Entity
(`hub/schema/heater.schema.json`), and, only when `commands_enabled = true`, accepts six command
topics under the safety rules of `hub/docs/adr/0021-writable-heater-and-its-command-safety-rules.md`.
There is no simulated backend; Demo mode covers the no-hardware case.

> **WARNING: `commands_enabled` is `false` by default for a reason.** The hub's broker accepts
> anonymous clients, so with commands on, anyone who can reach port 1883 can start and stop the
> heater. Keep the broker on the van's own network. A typo in the value stops the Driver at startup.

## Topics published

All under `renewvan/heater/<id>/`, retained, JSON. Unknown is an empty retained payload.

- Identity, once: `driver`, `name` (omitted when empty), `commands_enabled`.
- Live, every poll: `phase`, `status_code`, `error_code`, `internal_temperature_c`,
  `heater_temperature_c` (byte - 15), `external_temperature_c` (absent on the `0x7F` sentinel),
  `voltage_v`, `fan_rpm` (byte x 60), `fuel_pump_hz` (byte / 100), `ventilating` (`true` only for
  status 3.35), and `mode`, `setpoint_c`, `power_level` (read back from the heater, never cached).
- `runtime_s` is not published: no verified source byte.
- After `stale_after_s` without a valid status, all live fields are cleared.

## Commands

Unretained, strict JSON, under `renewvan/heater/<id>/`: `set` (`true` start / `false` stop),
`mode/set` (`"heater_temperature"`, `"external_temperature"`, `"power"`), `setpoint_c/set` (integer
1-30), `power_level/set` (integer 0-9). `ventilating/set` (`true` starts fan-only from off at the read power level, `false` stops) is
**not yet bench-verified**. `clear_error/set` is **refused** (one WARNING, no frame sent) until its
frame is bench-verified, and the `0x0D` unblock is never
exposed. Every command, stop included, is also rejected earlier (WARNING, no frame) when
`commands_enabled` is false or the state is stale or not yet synced; the `3.35`/`3.4` phase defaults
are unverified. `commands_enabled/set` (`true`/`false`) is the one command accepted while commands are
off: it flips the switch for this run only and republishes `commands_enabled`; a restart returns to
`config.ini`. A rejected or unconfirmed command is logged at WARNING with the command and phase; there is
no reply topic. Stop is retried at most 3 times, 3 polls apart, then one ERROR, and is never gated
by phase. Confirmation is the retained field changing (settings: the next `0x02` read). Ventilation
is confirmed only by status `3.35`: if the heater answers `0x23` with any other start, one ERROR is
logged and the frame is not sent again. The Driver never stops or re-commands the heater on its own,
including after a restart.

Start, ventilate and every settings write read `0x02` fresh first (a failed read rejects the command)
and send back the whole payload with only the commanded field changed, so the bytes the Driver does
not understand (`use_work_time`, `work_time`, `wait_mode`) keep what was picked on the panel.

## Installing

The stable `/dev/ttyAUTOTERM` udev symlink and `dialout` membership are set up by the hub
bootstrap; see the hub docs (udev section) rather than a hand-written rule here.

Copy `config.ini.example` to `config.ini`, set `[mqtt] host`, and decide on `commands_enabled`.

### A second heater

Run a second Driver process with its own config: a distinct `[device] id`, `[mqtt] client_id` and
`serial_device`. Two heaters never share one process or one failure.

## Unverified assumptions

Verified on a real AIR 2D (2026-11-02): the `0x0F` status poll, the `0x02` settings read, and one
full start-run-stop cycle with single frames (status codes `0.1`-`2.6`, `3.0`, `4.0`; error 0
throughout). Init is not needed, so `send_init` defaults off.

Still unverified; each is blocked or refused until a human-present bench check:

- status codes `3.35` (fan-only) and `3.4` (kept as flagged defaults), and the `0x23` ventilate frame
  behind `ventilating/set`, so the first real send needs a human-present bench check;
- the `0x0D` unblock (never exposed) and the `clear_error` frame (refused);
- live settings changes (`mode`, `setpoint_c`, `power_level`), the 1-30 setpoint bounds, and
  confirmation by the next `0x02` read;
- stop during `1.x`/`2.x` (early stop);
- what a rejected frame looks like (none seen; the heater has no NACK);
- error byte versus displayed code (`0x1E` = 30 is High confidence from the manual, wire equals
  display is Medium);
- which fan byte is right (11 commanded, 12 measured; `fan_rpm` uses 12);
- `external_temperature` mode and the probe reading (no probe installed).

## Finding the bytes of other modes

The settings (`0x02`) are re-read every 5 polls, so a change made on the panel is published within
about 5 s. The reply is logged raw: `Heater settings changed: <old> -> <new>` at INFO whenever it
differs from the last read, and every payload at DEBUG (`-d`). Pick Thermostat or Heat + ventilation on
the Comfort Control panel with the heater off and read the changed bytes, in the order `use_work_time,
work_time, mode, setpoint, wait_mode, power`. The driver only sends mode codes 1, 3 and 4.

## Development

```bash
git clone git@github.com:renewvan/driver-autoterm.git
cd driver-autoterm
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-test.txt
```

Keep conversion/decision logic in pure functions tested against fixture
values — no MQTT broker or real hardware required for any of it. Before
opening a PR: `pytest tests/ -v` must pass (same command CI runs), and any
change to a published field needs a matching
`hub/schema/heater.schema.json` update in a `hub` PR (per
`hub/docs/adr/0001-compose-services-via-pinned-images-not-git-submodules.md`,
the two repos are never coupled at build time, only at the schema-version
level).

## Configuration

`config.default.ini` ships every key with a default. Copy
`config.ini.example` to `config.ini` (gitignored) and override only what
differs.

## Running

```bash
pip install -r requirements.txt
python -m driver_autoterm.main --config config.ini
```

Install `driver_autoterm/systemd/driver-autoterm.service` to run at boot
(`Restart=on-failure`).

## Testing

```bash
pip install -r requirements.txt -r requirements-test.txt
pytest tests/ -v
```

## Releasing

Tagging a GitHub release builds and publishes
`ghcr.io/<owner>/driver-autoterm:<tag>`, which `hub`'s deployment compose file pins
by tag (never builds from source — see
`hub/docs/adr/0001-compose-services-via-pinned-images-not-git-submodules.md`).
