# Bench: read what the PU-27 writes to the heater's settings

Goal: find the `0x02` settings bytes the PU-27 panel stores for each of its air-heater settings,
above all its **Ventilation** toggle (byte 4, `wait_mode`), and then check on a run what each value
does. This settles whether `wait_mode` 1, which the Driver publishes as `thermostat`, is the
panel's "Ventilation on" (fan keeps blowing in standby) and which value is "Ventilation off" (fan
stops in standby).

What the PU-27 manual (DE, v1.0.0.1, §11) says the settings do:

| PU-27 item | Manual | Expected byte |
|---|---|---|
| By T heater | regulate on the heater's built-in sensor | mode (byte 2) = `01` |
| By T panel | regulate on the panel's built-in sensor | mode = `02` |
| By T air | regulate on the external sensor | mode = `03` |
| By power, Power value 0-9 | fixed power level | mode = `04`, power (byte 5) |
| T setpoint | goes to standby at the setpoint, leaves it when the temperature falls 5 °C below | setpoint (byte 3) |
| Ventilation on / off | in standby the fan runs / stops | `wait_mode` (byte 4), **unknown** |

**Someone stays at the heater for the whole session.** The panel's off button (or the dashboard's)
is the abort. Cut the heater's 12 V only when it is off (`0.1`), never while running or purging.

Rules from the PU-27 manual that apply to every step:

- connect or disconnect the panel **only with the heater off**;
- after a stop, wait 5-10 s before starting again;
- after two failed starts in a row, stop and do not retry.

The commands run from a machine with the `renewvan` SSH alias:

```bash
COMPOSE='docker compose -f /opt/renewvan/hub/docker-compose.yml --profile autoterm'
```

## Wiring

The panel and the FTDI adapter must never be on the heater's UART at the same time: both drive the
heater's one receive wire, and the heater then hears neither (seen 2026-11-13; only a heater power
cycle brought the link back). In this checklist that means **unplug the adapter from the heater
harness before plugging in the panel**, and the other way round. A stopped Driver is not enough:
the adapter's TX still holds the line.

## Reading the settings bytes

With the adapter connected, the panel unplugged and the Driver **stopped**, this reads `0x02` once
(a read, it changes nothing):

```bash
ssh renewvan "$COMPOSE run --rm --no-deps -T --entrypoint python driver-autoterm -" <<'EOF'
from driver_autoterm.link import HeaterLink, open_serial
from driver_autoterm.protocol import MSG_SETTINGS
link = HeaterLink(open_serial("/dev/ttyAUTOTERM"), 1.0)
for _ in range(3):
    reply = link.transact(MSG_SETTINGS)
    if reply:
        print("0x02:", reply.payload.hex(" "))
        break
else:
    print("no reply")
EOF
```

The six bytes are `[work time hi] [work time lo] [mode] [setpoint] [wait_mode] [power]`. `no reply`:
check the wiring, then power-cycle the heater (only while off) and try again.

## Part A: what each PU-27 setting writes

Needs: heater **off** (`0.1`).

1. **Baseline.** Stop the Driver and read the bytes:

   ```bash
   ssh renewvan "$COMPOSE stop driver-autoterm"
   ```

   Run the read above and write the result in the table (row A0).

2. **Switch to the panel.** Unplug the adapter from the harness, plug in the PU-27 and wait for its
   main screen (it shows its connection process first).

3. **Ventilation on.** In the settings menu set *By T heater*, *T setpoint* 18, *Ventilation* on.
   Leave the menu and wait 10 s.

4. **Switch back to the Pi.** Unplug the panel, plug in the adapter, read the bytes (row A1).

5. **Ventilation off.** Repeat steps 2-4 with only *Ventilation* changed to off (row A2).

6. **Optional, the modes.** Repeat with *By T panel* (A3), *By T air* (A4) and *By power* with Power
   value 5 (A5), changing one item each time.

If a row reads exactly like the one before it, the PU-27 did not write the setting from its menu;
it probably sends the settings only with a start. Then redo that row with **step 3b** instead:

- **3b.** Set the items, start the heater from the PU-27, wait for `3.0` (heating, about 3-5 min),
  stop it from the PU-27 and wait until the purge ends and the display shows the heater off (`0.1`),
  then go on to step 4. Note in the row that it came from a start.

| Row | PU-27 setting | 0x02 bytes | Via start? |
|---|---|---|---|
| A0 | (baseline, as found) | | |
| A1 | By T heater, 18 °C, Ventilation **on** | | |
| A2 | By T heater, 18 °C, Ventilation **off** | | |
| A3 | By T panel | | |
| A4 | By T air | | |
| A5 | By power, Power value 5 | | |

## Part B: what each `wait_mode` value does on a run

Needs: Part A done, the adapter connected, the panel unplugged, and the room clearly warmer than
the setpoint you pick, so the heater reaches standby soon after it starts heating.

The Driver keeps the `wait_mode` byte it reads, so the value comes from the panel: set Ventilation
on the PU-27 as in Part A, switch back to the Pi, then start from the dashboard.

1. **Start the Driver** and check the dashboard shows the heater `off` with error 0:

   ```bash
   ssh renewvan "$COMPOSE start driver-autoterm"
   ```

2. Set the mode to heater temperature and the setpoint a few degrees **below** the room, then start.

3. Once it heats (`3.0`), wait for standby: `3.4` then `3.5` (phase `standby`). Write down for that
   standby:
   - air from the outlet: **blowing / still** (hand at the outlet);
   - `fan_rpm` and `fuel_pump_hz` from the dashboard;
   - the time from `3.0` to `3.4`.

4. Raise the setpoint above the room and note whether and when it re-ignites (`2.x`). Then stop
   from the dashboard and wait for `0.1`.

5. Repeat 1-4 with the other Ventilation value from Part A (after a 5-10 s pause, and with the
   panel swap done with the heater off).

| Row | wait_mode | Air in standby | fan_rpm / fuel_pump_hz in standby | 3.0 to 3.4 | Re-ignites at |
|---|---|---|---|---|---|
| B1 | (Ventilation on) | | | | |
| B2 | (Ventilation off) | | | | |

## Part C: the run-time bytes (bytes 0-1)

The PU-27's *Laufzeit* (run time, 5 min steps) is stored in bytes 0-1. How to read them is not
settled: a 16-bit big-endian minute count (`00 78` = 120 min, `FF FF` = unlimited) fits one
Comfort Control capture, while the Driver reads them as `use_work_time, work_time`. The Comfort
Control also writes `FF` to mean "keep this byte". On this heater they read `01 00`, which as a
minute count is 256 min (4 h 16 min), so a long run might stop by itself.

1. Do it together with Part A: set *Laufzeit* to 30 min, then 60 min, then its longest value (or
   unlimited, if offered), reading the bytes after each (rows C1-C3).
2. During a Part B run, after 3 min at `3.0`, watch the Driver log for one minute. If bytes 0-1
   count down, it logs `Heater settings changed: <old> -> <new>` about once a minute (row C4):

   ```bash
   ssh renewvan "$COMPOSE logs -f --since 2m driver-autoterm"
   ```

| Row | Laufzeit / situation | Bytes 0-1 |
|---|---|---|
| C1 | 30 min | |
| C2 | 60 min | |
| C3 | longest / unlimited | |
| C4 | running, one minute apart | |

## Reading the result

- A1 shows `01` and B1 blows air in standby: `01` is the panel's "Ventilation on", that is Heat +
  ventilation. The Driver's `thermostat` (wait_mode 1) is then that mode, not a fan-off thermostat.
- A2 shows the fan-off value (`02`, `03` or another): if B2 also reaches standby, the fan-off
  thermostat exists on this heater at that value. If B2 keeps burning and only turns the fuel down
  (as `02` did on 2026-11-13), the PU-27's "off" means "no standby".
- A3-A5 confirm the mode codes 1-4 against the panel's names.
- C1-C3 give the run-time encoding. If C4 counts down, the heater enforces the run time itself and
  reports what is left, which is what an "off after N minutes" timer on the dashboard can use.

Afterwards leave the adapter connected and the Driver running; power-cycle the heater (while off)
if the Driver logs `Heater did not report its settings` for more than a few seconds. Record the
filled tables here or in the PR, and update `README.md` (*Unverified assumptions*) and hub ADR 0021
with what was found.
