# driver-heater

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
1-30), `power_level/set` (integer 0-9), `ventilating/set` (`true`/`false`), `clear_error/set`
(`true` only). A rejected command is logged at WARNING with its reason; there is no reply topic.
Confirmation is the retained field changing. The Driver never stops or re-commands the heater on
its own, including after a restart.

## Installing

```bash
# Stable device name, so a USB re-plug does not break the Driver. Adjust the ids to your adapter
# (udevadm info -a /dev/ttyUSB0).
echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", SYMLINK+="ttyAUTOTERM"' \
  | sudo tee /etc/udev/rules.d/99-autoterm.rules
sudo udevadm control --reload && sudo udevadm trigger
sudo usermod -aG dialout renewvan
```

Copy `config.ini.example` to `config.ini`, set `[mqtt] host`, and decide on `commands_enabled`.

### A second heater

Run a second Driver process with its own config: a distinct `[device] id`, `[mqtt] client_id` and
`serial_device`. Two heaters never share one process or one failure.

## Unverified assumptions

Only status codes `0.1` and `3.35` are backed by real AIR 2D frames. Everything below is a
research default to walk on the first bench session (`hub/docs/adr/0021-...`):

- the `0x1C` -> `0x04` -> `0x06` init sequence (`send_init`; may not be needed);
- reading `0x02` settings on connect (never seen on a real AIR 2D);
- the `0x0D` unblock behind `clear_error/set`;
- live settings changes (`mode`, `setpoint_c`, `power_level`) and the 1-30 setpoint bounds;
- every status code except `0.1` and `3.35`, and so the phase mapping;
- which of the two fan bytes (11 commanded, 12 measured) is right: `fan_rpm` uses byte 12;
- rejected-command behaviour: the heater has no NACK, so a rejected frame looks like a timeout and
  confirmation is always a read-back (one retry after three polls, then a WARNING).

## Development

```bash
git clone git@github.com:renewvan/driver-heater.git
cd driver-heater
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
python -m driver_heater.main --config config.ini
```

Install `driver_heater/systemd/driver-heater.service` to run at boot
(`Restart=on-failure`).

## Testing

```bash
pip install -r requirements.txt -r requirements-test.txt
pytest tests/ -v
```

## Releasing

Tagging a GitHub release builds and publishes
`ghcr.io/<owner>/driver-heater:<tag>`, which `hub`'s deployment compose file pins
by tag (never builds from source — see
`hub/docs/adr/0001-compose-services-via-pinned-images-not-git-submodules.md`).
