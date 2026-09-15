# Autoterm Heater app

Controls an Autoterm-family diesel heater and its comfort panel over UART:
owns both serial ports directly, publishes live status, and exposes
start/stop/thermostat controls as Home Assistant entities via MQTT
discovery. Confirmed against a real Autoterm 5D / Flow 5 (internally
BINAR-5S) unit -- see docs/PROTOCOL.md and "Heater profile: other models"
below for the 18 other vendor models this can decode extended telemetry
for, none of which are hardware-confirmed.

On top of that, it includes tools for continuing the protocol
reverse-engineering, all off by default so day-to-day use is unaffected
unless you turn them on: optional extended-telemetry probing across 19
vendor heater profiles, a full raw-traffic capture log, and a Bypass mode
that suspends all command injection for capturing a clean baseline.

Full protocol derivation lives in `docs/PROTOCOL.md` in the
[main repo](https://github.com/sinise/autoterm-heater-control) -- read it,
especially "Extended diagnostic-mode telemetry", before enabling debug mode.

*A note on terminology:* this document says "app" throughout, matching
Home Assistant's own UI since the 2026.2 release (Settings -> Apps ->
App Store). It's the same thing Home Assistant (and Supervisor's own
developer-facing API/docs) still calls an "add-on" under the hood -- only
the name shown to users changed.

## Before you install

This app owns both UART ports directly -- only one process can hold a
serial port open, so don't run anything else against the same two ports at
the same time.

It's safe to install and run with debug mode, capture logging, and Bypass
all left **off** (the default) -- in that mode it's a plain relay plus
controls, nothing experimental. The extra risk described below only
applies once you actually turn debug mode on.

## Wiring: connecting the Pi to the heater

**Hardware needed:** a USB-to-serial adapter exposing **two independent
5V TTL UART interfaces** (not RS-232, and not a 3.3V-only adapter unless
it's confirmed 5V-tolerant on its inputs -- the panel/heater bus runs 5V
TTL logic). A single 4-port adapter (e.g. an FTDI/CP2108-based quad
adapter) works well since it gives you two spare ports beyond the two this
app needs.

**The panel-heater harness has (at least) four wires you care about:**

| Wire | Carries |
|---|---|
| Yellow | The panel/display's **RX** -- i.e. this is the wire the **heater transmits on** |
| White | The panel/display's **TX** -- i.e. this is the wire the **heater receives on** |
| Red | **+12V power**, not a data signal |
| Black (or similar) | Ground, common to the whole harness |

**Cut both the yellow and the white wire** (only those two -- leave red and
black intact) at a convenient point between the panel and the heater. Each
cut leaves a "panel-side" stub and a "heater-side" stub. Wire each stub to
the UART port on that same side -- one wire, one direction of travel, per
diagram:

```
YELLOW wire -- carries data FROM the heater TO the panel:

   HEATER >---[cut]---> HEATER_PORT's RX pin

        (app relays it here, in software)

   PANEL_PORT's TX pin >---[cut]---> PANEL / DISPLAY


WHITE wire -- carries data FROM the panel TO the heater:

   PANEL / DISPLAY >---[cut]---> PANEL_PORT's RX pin

        (app relays it here, in software)

   HEATER_PORT's TX pin >---[cut]---> HEATER
```

So: `heater_port` RX = yellow's heater-side stub, `heater_port` TX =
white's heater-side stub; `panel_port` RX = white's panel-side stub,
`panel_port` TX = yellow's panel-side stub. The app relays bytes
between `panel_port` and `heater_port` in software (see
`docs/PROTOCOL.md`), so the panel and heater talk exactly as before, just
through the Pi in the middle.

**Do not connect the red wire to anything on the Pi or the USB-serial
adapter.** It's +12V, not a logic-level signal -- feeding 12V into a UART
RX pin built for 3.3V/5V logic can permanently damage the adapter (and
possibly the Pi's USB port behind it) if that input isn't rated for it.
Leave it connected exactly as it already is between the panel and heater;
this app has no reason to touch the power wire at all.

**Ground is not optional.** Tie the Pi's GND (shared between both UART
ports is fine) to the harness's black/ground wire. A missing shared ground
produces pure garbage on the line, not silence -- if a capture looks like
noise, check this first.

Confirm which physical port ended up as `panel_port` vs `heater_port` by
**content, not by assumption** -- USB-serial adapters can re-enumerate
after a replug, silently swapping which physical connector a device path
refers to (see docs/PROTOCOL.md's device-role history note, and "Port
autodiscovery" below for an automated way to handle this).

## Debug mode: extended telemetry probing

**What it does:** periodically sends the vendor diagnostic tool's "PUBR0"
handshake frame out the heater port. On a direct PC<->heater connection,
that handshake makes the heater start streaming a much richer 58-byte
telemetry frame once per second -- fan speed (defined vs. measured),
fuel pump frequency, flame/liquid/overheat/board temperature, supply
voltage, and a named operating mode/sub-mode (Low/Middle/High/Ignition
stages/etc), instead of just the basic 18-byte status. See
`docs/PROTOCOL.md` for exactly which fields and their formulas.

**The 58-byte extended telemetry frame itself never reaches the physical
panel.** Confirmed directly on real hardware: the panel visibly gets
confused if it receives that frame (it's not something its own firmware
was ever designed to parse). Since 1.1.0, the app filters that specific
frame out of the heater->panel relay direction -- it's still decoded for
the sensors below, and still logged (marked "NOT forwarded (filtered)") if
capture logging is on, it just never lands on the panel's wire.

**A second, separate issue was found and (partially) fixed in 1.5.0:**
sending the handshake itself, while it's queued to go out the same
heater_port line the panel's own query/reply traffic is relayed over, can
corrupt that traffic -- confirmed by reconstructing this app's own
"Telemetry stale" logic against a real capture and matching it
second-for-second to Home Assistant's actual stale/OK history, and by
finding a real 18-byte heater reply missing 2 bytes immediately after a
handshake send. It happened on roughly half of handshake sends, not all --
consistent with a timing collision, not a guaranteed failure. 1.5.0 holds
the handshake until the bus has been quiet for 250ms before sending it,
which narrows the collision window, but this has not been re-validated
against a fresh long capture the way the panel-confusion fix was --
**treat debug mode as experimental**, not fully solved, and watch
`Telemetry stale`/the capture log after updating rather than assuming this
is now perfect.

There's also a **Send debug handshake now** button, for sending exactly one
handshake on demand instead of waiting for the periodic timer -- useful for
a single closely-watched test. It waits for the same quiet gap before
sending.

The **Extended telemetry active** binary sensor tells you whether the
heater is actually replying with the richer frame (turns on once a valid
58-byte extended frame has arrived within the last 5 seconds) -- if debug
mode is on but this stays off, the handshake isn't getting a reply, which
is itself useful information.

**If you're seeing "no communication" glitches on the panel or the heater
seeming to restart every 30-40 minutes with debug mode off**, that's not
this issue -- see the 2.1.0 entry in CHANGELOG.md. The same quiet-gap
protection above turned out to be missing from every other command this
app injects too (Start preheat/thermostat, Stop, Start pump, including
the automatic ones Auto thermostat/Prevent freezing send on their own),
which is a much more likely cause of periodic disruption with debug mode
off. Fixed in 2.1.0.

## Heater profile: other models

The extended telemetry frame's field formulas (byte offsets, state/mode
names, fault names) are model-specific. The **Heater profile** option
selects which model's formulas decode the frame -- 19 choices, extracted
from the vendor diagnostic tool's own per-model `Profiles/*.pfl` files
plus its `language.res` string table (plaintext data files read directly,
not a decompile of the tool itself), the same way `autoterm_flow_5`'s
fields were originally derived and cross-checked against a real capture.

**Only `autoterm_flow_5`, `binar_5s`, and `binar_5s_next` are confirmed
against real hardware** -- the latter two share the exact same internal
codename (`BINAR-5S`) as Flow 5 in the vendor's own `.pfl` files, meaning
byte-identical field data, not a separate guess. Every other option below
is read straight from the vendor tool's own data and has **never been
validated**: the byte offsets could be wrong, the field set could be
incomplete (some models expose 5-6 temperature-ish registers; only 4
slots are wired up here -- see "Known limitation" below), and it isn't
even confirmed that the extended-telemetry mechanism itself (the `PUBR0`
handshake, the `dev02`/`type01` frame) works the same way on that model,
or applies at all. The app logs a warning at startup, and a **Heater
profile** sensor shows the active selection with a `(NOT TESTED)` suffix,
for anything but those three.

| Option value | Vendor tool's display name | Internal codename | Status |
|---|---|---|---|
| `14tc_10_molex` | 14TC-10 MOLEX | 4TC-10 MOLEX | untested |
| `autoterm_air_2d` | AUTOTERM AIR 2D | PLANAR-2MK | untested |
| `autoterm_air_4d` | AUTOTERM AIR 4D | PLANAR-44MK | untested |
| `autoterm_air_8d` | AUTOTERM AIR 8D | PLANAR-8D | untested |
| `autoterm_air_9d` | AUTOTERM AIR 9D | PLANAR-9D | untested |
| `autoterm_flow_5` | AUTOTERM FLOW 5 | BINAR-5S | **tested** |
| `binar_5s_next` | BINAR-5S-NEXT | BINAR-5S | **tested** (byte-identical data to Flow 5, same internal codename) |
| `binar_5s` | BINAR-5S | BINAR-5S | **tested** (byte-identical data to Flow 5, same internal codename) |
| `planar_2_with_flame_sensor` | PLANAR-2 with flame sensor | PLANAR-2 with flame sensor | untested |
| `planar_2d` | PLANAR-2D | PLANAR-2D | untested |
| `planar_2mk` | PLANAR-2MK | PLANAR-2MK | untested |
| `planar_44d_s_p` | PLANAR-44D-S-P | PLANAR-44D-SP | untested |
| `planar_44mk` | PLANAR-44MK | PLANAR-44MK | untested |
| `planar_4d_s_p` | PLANAR-4D-S-P | PLANAR-4D | untested |
| `planar_4d` | PLANAR-4D | PLANAR-4D | untested |
| `planar_8d_s_p` | PLANAR-8D-S-P | PLANAR-8D | untested |
| `planar_9d` | PLANAR-9D | PLANAR-9D | untested |
| `sputnik_2` | SPUTNIK-2 | SPUTNIK-2 | untested |
| `sputnik_3` | SPUTNIK-3 | Sputnik-3 | untested |

Several of these share an "internal codename" -- e.g. `autoterm_air_4d`
and `planar_44mk` are the exact same underlying protocol under a different
market name in the vendor tool, confirmed from the `.pfl` files
themselves (not a guess). Selecting either gives identical decoding.

**What stays the same regardless of this setting:** the base 18-byte
`type0f` protocol, all confirmed commands (Start/Stop/Prevent freezing/
etc), and the panel-filter fix (the extended frame is still never
forwarded to the physical panel) -- none of that is profile-specific, all
of it stays exactly as already confirmed for the 5D/Flow 5 hardware this
whole project is built against. Only the *decoding* of the extended
58-byte frame's contents changes.

**Known limitation:** some models expose more temperature-ish registers
(e.g. separate "in"/"out"/"heat exchanger"/"external sensor" readings)
than the 4 fixed slots (Flame/Liquid/Overheat/Board temperature) this
app has entities for -- extras beyond the first 4 (prioritized by
closest name match to Flow 5's own fields) aren't currently exposed. Their
formulas are still in the app's source if you want to add sensors for
them.

## Raw traffic capture log

**What it does:** the **Capture raw traffic log** switch turns on a
plain-text log of every message seen -- every valid frame, every bad-CRC
frame, and every stray (unparsed) byte -- tagged with who sent it:

- `display` -- the physical comfort panel
- `heater` -- the heater
- `rpi` -- this app itself (injected commands, and the debug handshake
  if debug mode is on)

Each line has a timestamp, the sender, CRC status, decoded `dev`/`type`/
`len` where applicable, and the full frame in hex. Since 1.1.0, the app's
own log messages (info/warning/error -- MQTT status, serial errors,
commands sent, etc) are also written into this same file, tagged `log`,
interleaved chronologically with the traffic -- so one file is normally
everything needed for further analysis. You don't need to separately pull the Supervisor log tab unless
you're chasing something that happened *before* capture logging was turned
on, or something the app logs at a level below what gets mirrored here.

**Where the log goes:** `/config/autoterm_debug/capture_<timestamp>.log` --
deliberately `/config`, not `/share`, so it shows up right where the
**File editor** app (and most other file-browser apps) already opens
by default, with no extra navigation or config changes needed. Reachable
from outside the app itself via:

- The **File editor** / **Studio Code Server** app -- it's right there
  in the default file tree, under `autoterm_debug/`.
- The **Samba share** app (if installed and configured to expose
  `config`) -- browse to `\\<home-assistant-ip>\config\autoterm_debug\`
  from your PC.
- SSH into the Home Assistant host, if you have that set up.

Toggling the switch off closes the current file cleanly (with an end
marker) -- toggling it back on starts a **new** file rather than appending,
so each capture session is its own file. A capture is capped at
`capture_log_max_mb` (default 20MB, configurable) -- past that it stops
writing (logged as a warning) rather than filling up storage; toggle it off
and on again to start a fresh file if you hit the cap mid-session.

The **Capture log file** and **Capture log size** sensors show the current
file name and size without needing to go find it first.

**If you send a capture back for further analysis:** the whole point of
this feature is to make that loop easy -- grab the file, note roughly what
you did and when (e.g. "turned on Debug mode at the start, pressed Start
preheat around the 2 minute mark"), and it can be diffed against the
already-decoded fields the same way the extended-frame work was done.

## Bypass (disable all injection)

**What it does:** while this switch is on, the app stops sending
*anything* it wouldn't otherwise be asked to by the physical panel --
Start preheat/thermostat/Stop/Start pump (manual or automatic, including
Auto thermostat and Prevent freezing) and the debug handshake are all
suspended (each attempt is logged instead of sent). The real panel keeps
talking to the real heater exactly as it always does -- this only stops
the *app's own* commands, not the passive relay.

**Why you'd use it:** to capture a clean baseline showing what the heater
actually does entirely on its own (or driven only by the physical panel),
with zero chance that anything this app injects is a contributing
factor. This is exactly how it was used to settle an open question here:
the heater has been observed self-stopping (going idle, then restarting
itself within seconds) roughly every 30-40 minutes even under Auto
thermostat/Prevent freezing. A one-hour Bypass capture -- this app
sending zero commands, heater started and set to 26°C directly from the
physical display in its own unlimited-runtime thermostat mode -- showed
the exact same cycle. That rules out this app (and any command it
could ever send) as a cause: it's the heater/panel's own native behavior.
See docs/PROTOCOL.md for the full writeup.

**Logging:** turning Bypass on starts a separate log file,
`/config/autoterm_debug/bypass_<timestamp>.log` -- same format as the
normal capture log, but its own file and its own on/off state, so a
bypass test is captured cleanly regardless of whether the regular
**Capture raw traffic log** switch happens to be on or off. The **Bypass
log file** and **Bypass log size** sensors show the current file without
needing to go find it. Turning Bypass off closes that file (with an end
marker); turning it on again later starts a new one.

## Configuration

| Option | Meaning |
|---|---|
| `panel_port` | Serial device wired to the panel leg (default `/dev/ttyUSB1`) |
| `heater_port` | Serial device wired to the heater leg, commands are injected out this port (default `/dev/ttyUSB3`) |
| `baud` | UART baud rate (default `2400`, confirmed on the reference hardware) |
| `autodiscover_ports` | If `true`, probe for the correct ports on every startup instead of trusting `panel_port`/`heater_port` -- see below |
| `preheat_default_minutes` | Initial value of the Preheat duration entity |
| `auto_target_default` | Initial target for the auto-thermostat climate entity |
| `prevent_freezing_target_default` | Initial value of the Prevent freezing target entity (0-10°C) -- see below |
| `mqtt_host`/`mqtt_port`/`mqtt_username`/`mqtt_password` | Only used as a fallback if no MQTT service (e.g. the Mosquitto broker app) is auto-discovered |
| `heater_profile` | Which vendor model's extended-telemetry field formulas to decode with -- see "Heater profile: other models" above |
| `debug_mode_default` | Whether Debug mode starts on when the app (re)starts. Live-togglable from Home Assistant afterward -- this is just the boot default. |
| `debug_interval_seconds_default` | Initial value of the Debug probe interval number entity. |
| `capture_log_default` | Whether the capture log starts on when the app (re)starts. |
| `capture_log_max_mb` | Size cap per capture file, in MB. |
| `external_temp_sensor_entity` | Optional entity_id of an existing Home Assistant temperature sensor to use for Auto thermostat/Prevent freezing instead of the panel's own Temperature at display -- see "External temperature sensor" above. Leave blank to keep using the panel sensor. |

If you have the official **Mosquitto broker** app (or any app
providing the `mqtt` service) installed, this app finds it automatically
and the `mqtt_*` options can be left blank.

Debug mode, the probe interval, the capture log toggle, and **Use external
temperature sensor** are all live-controllable from Home Assistant
(switches/number entities below) and persisted to the app's `/data`
volume -- the `_default` options (and `external_temp_sensor_entity`, which
has no live equivalent since it's an entity_id, not a toggle) only take
effect on a fresh install, an app restart, or if `/data` is cleared.

## Port autodiscovery

USB-serial adapters can re-enumerate on replug, silently swapping which
physical connector `/dev/ttyUSB1` vs `/dev/ttyUSB3` refers to -- this has
bitten this exact project before (see docs/PROTOCOL.md's device-role
history note). Turning on `autodiscover_ports` runs a probe at every
startup instead of trusting the configured device paths:

1. **Find the panel** -- listen (read-only) on every `/dev/ttyUSB*` and
   `/dev/ttyACM*` device at once, up to 8 seconds, for a valid frame from
   dev03. The panel appears to report its cabin temperature on its own,
   without needing anything from the heater side, so this works from pure
   listening.
2. **Find the heater** -- on each remaining candidate, send the empty
   type0f status query (the one documented, non-actuating "poll" the panel
   itself sends -- see `docs/PROTOCOL.md`) and listen for the heater's
   18-byte dev04 reply. **Only this empty query is ever sent during
   discovery -- never a start (`type01`/`type02`) or stop (`type03`)
   command**, since those actually move the heater's state machine and must
   never be used just to probe a port.

If both are found, each is resolved to its stable `/dev/serial/by-id/*`
symlink where one exists (tied to that specific adapter's USB vendor/
product/serial number, not plug-order) before being saved back into this
app's own configuration -- so unlike plugging in some unrelated
USB-serial device and having `/dev/ttyUSB<N>` numbering shift under you
again, a `by-id` path keeps pointing at the same physical adapter no
matter what else gets plugged in later. If an adapter has no USB serial
number for udev to key on (some cheap chipsets don't), there's no `by-id`
symlink to resolve to -- the app logs a warning and saves the raw
`/dev/ttyUSB<N>` path instead, same as before, which can still shift.
Either way, the Configuration tab reflects what was actually found and
used for that run, and you can turn `autodiscover_ports` back off
afterward once it's saved a `by-id` path. If either discovery step fails
(nothing found within the timeout), the app logs why and falls back to
whatever `panel_port`/`heater_port` are currently configured -- it never
refuses to start over a failed discovery.

**Note:** since `autodiscover_ports` is a config option, not a live
toggle, a saved change only takes effect on the *next* app start --
after enabling it from the Configuration tab, click **Save**, then
explicitly **Start**/**Restart** the app (a crashed/stopped app
won't pick up a config change on its own).

This is a heuristic based on how the wiring has behaved on the reference
hardware (see `docs/PROTOCOL.md`'s notes on device roles), not a certainty
for every unit/firmware revision. Watch the app log on first use, and
cross-check with the physical panel that the labeled entities actually
track what you expect.

## Prevent freezing

An independent frost-protection safety net, separate from the auto-
thermostat climate entity. When the **Prevent freezing** switch is on, the
heater is started (thermostat mode) whenever cabin temperature reaches the
**Prevent freezing target** (0-10°C) -- **regardless of whether the
auto-thermostat climate entity is on or off, and regardless of a prior
manual Stop.** That's the point of the feature: it can't be silently
defeated by turning normal heating off or pressing Stop once -- only
turning the Prevent freezing switch itself off disables it, with one
exception: a heater-reported fault also turns it off automatically (see
"Faults" below). Re-enable it once the fault is dealt with.

It won't fight anything else, though: it never stops a heater run it
didn't start (so it doesn't interrupt the auto-thermostat's own comfort
run, or a manual preheat session, or another admin's separate Start), and
if you disable Prevent freezing while it's mid-run, that run is left
running rather than cut off abruptly -- something else (manual Stop, the
auto-thermostat) needs to end it.

**Practical implication:** if it's cold and Prevent freezing is on, a
plain Stop button press won't keep the heater off -- it'll restart within
seconds once cabin temperature is still at/below the floor. To actually
stop the heater in that situation, turn off Prevent freezing first (or
raise its target below the current cabin temperature).

## External temperature sensor

By default, Auto thermostat and Prevent freezing both read the panel's own
**Temperature at display** report. If you'd rather they used a different
temperature sensor already in Home Assistant (e.g. a dedicated sensor
sitting where you actually feel the temperature, not wherever the panel
happens to be mounted), set the **External temperature sensor entity ID**
option to that sensor's entity_id (find it under Developer Tools ->
States, e.g. `sensor.lacrosse_bedroom_temperature`).

There's no live dropdown of Home Assistant entities in this app's own
Configuration page -- Supervisor app options are a static schema with no
access to Home Assistant's entity registry, unlike an Integration's config
flow. Typing the entity_id once is the standard pattern other apps use
for this same limitation.

**How it works:** this app polls that entity's state every 15 seconds
via Home Assistant's own API (the `homeassistant_api: true` permission this
app requests). While a fresh reading is available, **both** Auto
thermostat and Prevent freezing use it instead of Temperature at display -- the
climate entity's displayed current temperature follows the same source.

**Fallback:** if the entity is left blank, the **Use external temperature
sensor** switch is off, Home Assistant reports it explicitly `unknown`/
`unavailable`, or 90 seconds pass without a successful poll, this app
falls back to the panel's own Temperature at display automatically (no action
needed) -- Prevent freezing in particular is a frost-protection safety net,
so it's deliberately built to keep working off the panel's own sensor
rather than going blind if the external sensor or Home Assistant's API has
a problem. **Using external temperature sensor** (binary sensor) shows
which source is actually driving control right now; **External temperature
(polled)** shows the raw last-polled value regardless of whether it's
currently being used.

## Faults

The heater reports a fault code in every status frame (`0` = no fault),
independent of debug mode. This app surfaces it two ways:

- **Fault** / **Fault code**: the *current* fault, named and numeric --
  `Fault` reads "No faults" (or the fault's name) whenever the heater isn't
  currently faulted.
- **Fault active** (binary_sensor, `device_class: problem`): on exactly
  while `Fault code` is non-zero. This is the entity to use as an
  automation trigger, below.
- **Last fault code** / **Last fault** / **Last fault time**: the most
  recent fault's code, name, and when it *started* (not when it cleared) --
  these keep their value even after the heater recovers and `Fault code`
  goes back to `0`, so a fault that already cleared by the time you check
  Home Assistant doesn't just look like it never happened.

If **Auto thermostat** or **Prevent freezing** is on when a fault appears,
that loop turns itself off rather than keep re-issuing `start thermostat`
every time it next sees the heater idle -- re-enable it yourself once
you've dealt with the fault. Manual buttons are unaffected (a fault
doesn't stop you from pressing Start/Stop).

The fault-code name table is the same partial, lower-confidence one used
for "Fault (extended, named)" below -- only "no fault" (code 0) was
actually observed in the reference capture; the rest come from the
vendor's own string table, unconfirmed against a real fault. An unknown
code still shows up as `unknown(<code>)` rather than being hidden.

**Getting notified:** Home Assistant doesn't have a dedicated "alarm"
entity for this (the `alarm_control_panel` domain is for arm/disarm
security-panel semantics, not a good fit) -- the standard way is a plain
Automation triggered on **Fault active** turning on, with a notify action.
For example, in Settings -> Automations -> New automation -> Edit in YAML:

```yaml
trigger:
  - platform: state
    entity_id: binary_sensor.autoterm_heater_fault_active
    to: "on"
action:
  - service: notify.mobile_app_<your_phone>
    data:
      title: "Autoterm heater fault"
      message: >
        {{ states('sensor.autoterm_heater_fault') }}
        (code {{ states('sensor.autoterm_heater_fault_code') }})
```

Swap `notify.mobile_app_<your_phone>` for whichever `notify.*` service you
have configured (the mobile app integration, Telegram, email, a
`persistent_notification.create` call, etc. -- any of them work the same
way, this is just a normal HA automation, nothing app-specific).

## What you get

A single "Autoterm Heater" device in Home Assistant with:

- **Sensors**: State (idle/running/late-run/cooldown/final-shutdown), Fault
  code, Fault (named), Last fault code, Last fault, Last fault time, Cabin
  temperature, Temperature external sensor, Coolant temperature, Elapsed
  run time
- **Binary sensors**: Burner active, Fault active, Telemetry stale
  (diagnostic -- turns on if no fresh status/cabin frames have arrived in
  25s, e.g. a wiring or port problem), Using external temperature sensor
  (diagnostic -- see "External temperature sensor" above)
- **Climate entity** ("Autoterm thermostat"): mode `off`/`heat` toggles the
  app's own software hysteresis loop (stops the heater at target+1°C,
  starts it at target-1°C in thermostat mode); shows the temperature
  currently driving control (Temperature at display, or the external sensor if
  active) and burner state as HVAC action
- **Number**: Preheat duration (minutes), used by the Start preheat button;
  Prevent freezing target (°C, 0-10)
- **Switch**: Prevent freezing -- see above; Use external temperature
  sensor -- see "External temperature sensor" above
- **Buttons**: Start preheat, Start thermostat (manual, one-shot -- distinct
  from the climate entity's automatic loop), Stop, Start pump (ventilation
  only, no combustion -- runs the circulation fan/pump without heat; stop it
  with the same Stop button)

All confirmed protocol commands (start preheat/thermostat, stop) and the
device-role/frame-format knowledge this relies on come from
`docs/PROTOCOL.md` in the main repo -- not guessed.

Plus, from the debug-only features above:

- **Switch**: Debug mode, Capture raw traffic log, Bypass (disable all
  injection)
- **Number**: Debug probe interval (seconds)
- **Button**: Send debug handshake now
- **Binary sensor**: Extended telemetry active, Glow plug
- **Sensors** (all extended-frame fields, populated only while Extended
  telemetry active is on): Mode of operation (named, e.g. "High", "middle",
  "glow plug warming up"), Mode code (numeric mirror, see below), Running
  time (extended), Defined revolutions, Measured revolutions, Fuel pump
  frequency, Heater output, Flame temperature, Liquid temperature
  (extended), Overheat sensor temperature, Board temperature, Supply
  voltage, Fault (extended, named), Fault code (extended, numeric mirror),
  Engine state, Relay state, Fan current, Capture log file, Capture log
  size, Bypass log file, Bypass log size
- **Sensor** (always available): State code (numeric mirror of `State`),
  Heater profile (shows the active selection, `(NOT TESTED)` for anything
  but Flow 5 -- see "Heater profile: other models" above)

`State`, `Mode of operation`, and `Fault (extended, named)` are declared as
`enum` sensors (a fixed `options` list of every possible value) rather than
plain text -- genuinely useful for HA's own UI (dropdown-style display),
**but does not make them exportable to Prometheus/VictoriaMetrics** as
originally hoped: confirmed against a real instance that HA's Prometheus
integration tracks an enum sensor's availability/last-updated/change-count,
but never exports its actual text value as a metric (unlike the climate
entity's `mode`/`action`, which do get a proper metric). `State code`,
`Mode code` (`state*10+substate`), and `Fault code (extended)` are the
numeric mirrors that actually export -- see `grafana/` in the main repo,
which name-maps them back to text using Grafana's own value mappings
instead of relying on HA's exporter for that.

Field formulas are the vendor's own (read from its plaintext `.pfl`
profile, not reverse-engineered from scratch) and cross-checked against a
real capture -- see `docs/PROTOCOL.md`. The fault-code name table is a
partial, lower-confidence addition -- only "no fault" (code 0) was actually
observed in the reference capture; the rest of the names come from the
vendor's own string table but haven't been confirmed against a real fault.

**Heater output** is the one exception to "vendor's own formulas" above --
it's this app's own assumption, not vendor data: output scales linearly
with Fuel pump frequency, with 4.2Hz taken as 100% (so 2.1Hz reads 50%),
clamped to 0-100%. Unconfirmed against any real spec; treat it as a rough
indicator, not a calibrated wattage reading.

## Troubleshooting

- **No entities appear in Home Assistant**: check MQTT is actually
  discovered (app log should say "Using MQTT service auto-discovery"
  rather than the fallback-options warning) and that the MQTT integration is
  set up in Home Assistant (Settings -> Devices & Services).
- **Entities show unavailable**: the app publishes an MQTT "offline" LWT
  on crash/stop -- check the app log for a serial error (wrong port,
  permission, or an unplugged adapter).
- **Commands have no visible effect**: this exact failure mode has happened
  before in this project from a wrong port/device-byte assumption -- see
  `docs/PROTOCOL.md`'s "Important history" note. Re-verify port assignment
  by content before assuming the command itself is wrong.
- **Panel briefly shows "no communication"**: narrowed (not proven
  eliminated) in 2.1.0 -- every command this app injects toward the
  heater, including the automatic stop/start-thermostat calls Auto
  thermostat/Prevent freezing make on their own, now waits for a quiet
  moment on the bus first rather than landing mid-exchange with the panel.
  See CHANGELOG.md and `docs/PROTOCOL.md`. A separate, still-unexplained
  source of brief byte-level noise on the heater leg was also found and is
  independent of anything this app sends -- see `docs/PROTOCOL.md`.
- **The heater seems to restart on its own every 30-40 minutes**: this is
  **not** a bug in this app -- confirmed with a Bypass-mode capture (see
  above) that the exact same cycle happens with this app sending zero
  commands, heater started and set purely from the physical display in its
  own unlimited-runtime thermostat mode. It's the heater/panel's own native
  behavior. See `docs/PROTOCOL.md`.
- **Extended sensors stay blank**: Debug mode is probably off, or the
  handshake isn't getting a reply -- check the **Extended telemetry
  active** binary sensor and the app log for "DEBUG sent PUBR0
  handshake" lines.
- **Capture log switch is on but no file appears**: check the app log
  for a "capture log started" line and the exact path logged, and confirm
  you actually have a way to browse `/config` (File editor/Studio Code
  Server app installed, or SSH). It should appear as `autoterm_debug/`
  right in the default file tree -- no extra navigation needed.
