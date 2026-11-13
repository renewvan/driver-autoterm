# Bench: capture and verify the clear-error frame

Goal: find the exact frame the heater's own panel sends to clear a fault, then prove that the Driver
sending that frame clears it and does nothing else. Until both are done, `clear_error/set` stays
refused (`clear_error_frame` empty).

**Someone stays at the heater for the whole session**, with the panel and the heater's power within
reach. The panel's off button is the abort for every step.

The commands run from a checkout of this repo on a machine with the `renewvan` SSH alias. The Pi's
compose file is `/opt/renewvan/hub/docker-compose.yml`; the Driver's config is
`/etc/renewvan/autoterm/config.ini`.

```bash
COMPOSE='docker compose -f /opt/renewvan/hub/docker-compose.yml --profile autoterm'
```

## Wiring for the capture

The panel and the Pi's FTDI adapter cannot both be on the heater's UART as wired for the Driver
(adapter GND, TX and RX on the heater harness; only power unused). Seen on 2026-11-13: connecting
the panel made the adapter lose the heater, a capture with both connected read 0 bytes, and only a
heater power cycle brought the link back. Most likely both transmitters drive the heater's one
receive line, so the heater hears neither (not measured).

So for Part A the adapter must only listen:

- adapter **GND** to the harness GND;
- adapter **RX** to the panel's transmit wire (the one that goes to the heater's receive pin);
- adapter **TX disconnected**.

The sniffer then sees the panel's requests, which is where the clear frame is, but not the heater's
replies. Afterwards disconnect the panel, wire the adapter back as before (TX and RX), start the
Driver, and power-cycle the heater if the Driver logs `Heater did not report its settings` for
more than a few seconds.

## Part A: capture the panel clearing the fault

Needs: heater **off** (`phase off`, status `0.1`) and showing a fault (today: error 30). No new
release: the sniffer runs from this checkout on the image already on the Pi.

1. **Stop the Driver**, so the sniffer is the only reader of the port and the Pi sends nothing:

   ```bash
   ssh renewvan "$COMPOSE stop driver-autoterm"
   ```

   The heater is off, so a few minutes unpolled is fine (error 30 on 2026-11-13 came from being
   unpolled *while running*). Keep Part A under ~5 minutes. The dashboard shows the heater as unknown.

2. **Wiring check, 30 s, touch nothing:**

   ```bash
   ssh renewvan "$COMPOSE run --rm --no-deps -T --entrypoint python driver-autoterm - --seconds 30" \
     < driver_autoterm/sniff.py
   ```

   - Panel polls (`request msg 0f`, and `request msg 02` every few seconds) about once a second:
     the adapter hears the panel. Go on to step 3. (Replies appear only if the adapter's RX is on
     the heater's transmit wire instead.)
   - `No frames`: the adapter does not hear the panel; check the listen-only wiring above (panel
     awake, adapter RX on the panel's transmit wire, adapter TX disconnected). Without it, skip to
     step 5 and clear the fault on the panel without a capture.
   - `bytes did not parse`: something talks in another format; rerun with `--raw` and keep the
     output.

3. **Capture, 120 s.** Start it, wait for `Listening on ...`, then at the panel clear the fault the
   way its manual says. Write down each button press and the clock time. Then touch nothing until
   it ends.

   ```bash
   ssh renewvan "$COMPOSE run --rm --no-deps -T --entrypoint python driver-autoterm - --seconds 120 --raw" \
     < driver_autoterm/sniff.py | tee "clear-error-capture-$(date +%Y%m%d-%H%M).log"
   ```

4. **Read the result.** The summary at the end lists non-poll frames first, rarest first. The
   candidate is a `request` frame marked `<-- not a poll`, seen once or a few times at the clock time
   of the press, followed by the heater's reply. Keep the whole log; note the line(s), for example:

   ```
   19:42:07.513 request msg 0d unblock   payload []   <-- not a poll
   19:42:07.540 ack     msg 0d unblock   payload []   <-- not a poll
   ```

5. **Restart the Driver** and check the fault:

   ```bash
   ssh renewvan "$COMPOSE start driver-autoterm"
   ```

   | `error_code` after restart | Capture                           | Meaning                                                                            |
   | -------------------------- | --------------------------------- | ---------------------------------------------------------------------------------- |
   | 0                          | one non-poll request at the press | The candidate frame. Go on to Part B.                                              |
   | 0                          | no non-poll frame                 | The panel cleared it without a frame we can see (or is wired elsewhere). No candidate. |
   | still 30                   | any                               | The panel did not clear it. Power-cycle the heater; keep the log for later.        |

## Part B: verify the candidate through the Driver

Needs: this Driver change released and deployed (`clear_error_frame` support), the candidate from
Part A, and a heater that is **off with a fault**. Clearing in Part A removed the fault, so one has
to be raised again. The one seen safely on 2026-11-13: run the heater, stop the Driver for a few
minutes, and the heater shuts itself down with error 30 (`4.0`, then `0.1`).

1. **Configure the candidate.** In `/etc/renewvan/autoterm/config.ini`, under `[device]`, set the
   message byte then the payload, in hex, exactly as captured (for the example above:
   `clear_error_frame = 0d`). Start (`01`), settings (`02`) and ventilate (`23`) are refused at
   load: the Driver exits with the reason instead of running.

   ```bash
   ssh renewvan "$COMPOSE restart driver-autoterm && sleep 5 && $COMPOSE logs --tail=20 driver-autoterm"
   ```

2. **Send it once**, with the heater off and the fault showing: publish `true` on
   `renewvan/heater/air2d/clear_error/set` as the `command` account (the dashboard's).

3. **Watch for 30 s.** Expected: the log line `Clearing error 30 with frame 0d`, then
   `error_code 0` within a few seconds, `phase off`, no fan, no fuel pump, no glow plug.

   | What happens                                   | Driver does                                    | Do                                                 |
   | ---------------------------------------------- | ---------------------------------------------- | -------------------------------------------------- |
   | `error_code` 0, stays off                      | confirms, nothing more                         | Verified. Record it (below).                       |
   | nothing changes                                | resends once after 3 polls, then one WARNING   | Not the frame. Empty `clear_error_frame`, restart. |
   | heater leaves off (fan, ignition, any 1.x/2.x) | one ERROR, never resends, does **not** stop it | Panel off now. Empty `clear_error_frame`, restart. |

4. **Record the result** when verified: this repo's README (Unverified assumptions), hub ADR 0021
   and `hub/schema/heater.schema.json` (move the clear_error frame to verified, with the date and
   frame), and decide whether the verified frame becomes the shipped default.
