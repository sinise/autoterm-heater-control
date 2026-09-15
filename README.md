# Autoterm Heater Control

A Home Assistant app
and its panel, controlled from a Raspberry Pi wired inline between
the two -- built for a boat installation, but the protocol and app aren't
boat-specific. Confirmed against a real **Autoterm 5D / Flow 5** (internally
BINAR-5S) unit; the app's optional Heater profile setting extends
19 vendor models, but only the Flow 5/BINAR-5S
family which is independently confirmed -- see its DOCS.md.

Status: passive decoding and live command injection (start/stop, preheat,
thermostat) are **working and verified against real hardware**.

## Supported heaters

The base protocol (start/stop/preheat/thermostat, status, fault code,
temperatures) is confirmed against a real **Autoterm 5D / Flow 5**
(internally `BINAR-5S`) unit and should work unmodified on anything
sharing that same underlying hardware/firmware. The app's **Heater
profile** option additionally selects model-specific formulas for the
*extended telemetry* frame (fan speed, fuel pump frequency, more
temperatures, named operating mode) across 19 vendor models, extracted
directly from the vendor diagnostic tool's own per-model data files:

| Vendor display name | Tested against real hardware? | Internal codename | Config value |
|---|---|---|---|
| AUTOTERM FLOW 5 | ✅ Tested | BINAR-5S | `autoterm_flow_5` |
| BINAR-5S | ✅ Tested (byte-identical to Flow 5) | BINAR-5S | `binar_5s` |
| BINAR-5S-NEXT | ✅ Tested (byte-identical to Flow 5) | BINAR-5S | `binar_5s_next` |
| 14TC-10 MOLEX | Untested | 4TC-10 MOLEX | `14tc_10_molex` |
| AUTOTERM AIR 2D | Untested | PLANAR-2MK | `autoterm_air_2d` |
| AUTOTERM AIR 4D | Untested | PLANAR-44MK | `autoterm_air_4d` |
| AUTOTERM AIR 8D | Untested | PLANAR-8D | `autoterm_air_8d` |
| AUTOTERM AIR 9D | Untested | PLANAR-9D | `autoterm_air_9d` |
| PLANAR-2 with flame sensor | Untested | PLANAR-2 with flame sensor | `planar_2_with_flame_sensor` |
| PLANAR-2D | Untested | PLANAR-2D | `planar_2d` |
| PLANAR-2MK | Untested | PLANAR-2MK | `planar_2mk` |
| PLANAR-44D-S-P | Untested | PLANAR-44D-SP | `planar_44d_s_p` |
| PLANAR-44MK | Untested | PLANAR-44MK | `planar_44mk` |
| PLANAR-4D-S-P | Untested | PLANAR-4D | `planar_4d_s_p` |
| PLANAR-4D | Untested | PLANAR-4D | `planar_4d` |
| PLANAR-8D-S-P | Untested | PLANAR-8D | `planar_8d_s_p` |
| PLANAR-9D | Untested | PLANAR-9D | `planar_9d` |
| SPUTNIK-2 | Untested | SPUTNIK-2 | `sputnik_2` |
| SPUTNIK-3 | Untested | Sputnik-3 | `sputnik_3` |

**Only Flow 5, BINAR-5S, and BINAR-5S-NEXT are confirmed against real
hardware** -- those three share one internal codename in the vendor's own
data (byte-identical fields, not a separate guess). Every other row is
read straight from the vendor tool's own per-model files and has **never
been validated** against a real unit: byte offsets could be wrong, and
it isn't even confirmed the extended-telemetry mechanism works the same
way on that model at all. If you test one of the untested models,
[open an issue](https://github.com/sinise/autoterm-heater-control/issues)
with what you found -- see the app's `DOCS.md`, "Heater profile: other
models", for the full detail and known limitations.

## What's in here

| Path | Purpose |
|---|---|
| `autoterm/` | The Home Assistant app (Supervisor add-on) -- owns both UART ports directly, publishes status and exposes controls via MQTT discovery, plus optional extended-telemetry probing and a raw traffic capture log. See its `DOCS.md`. |
| `grafana/` | A Grafana dashboard for the app's entities (via Home Assistant's Prometheus integration + VictoriaMetrics). See its `README.md`. |
| `docs/PROTOCOL.md` | Full protocol writeup: frame format, CRC, device roles, message catalog, state machine, confirmed commands, open questions. |

## Hardware

- Raspberry Pi (any model with enough USB ports / a multi-port USB-serial
  adapter) running Home Assistant OS/Supervised.
- A USB-to-UART adapter exposing (at least) two independent **5V TTL**
  serial ports -- not RS-232, and not 3.3V-only unless it's confirmed
  5V-tolerant on its inputs.
- Two data wires spliced into the panel<->heater harness, **cut** so the
  Pi sits inline between panel and heater -- plus a shared ground with the
  heater/panel circuit. **A missing ground produces pure garbage, not
  silence** -- check this first if a capture looks like noise.
- On the reference harness, the two data wires are **yellow** (carries
  data from the heater to the panel) and **white** (carries data from the
  panel to the heater). There's also usually a **red +12V power** wire in
  the same harness -- **leave it alone**. It's not a data signal; feeding
  12V into a UART pin built for 3.3V/5V logic can permanently damage the
  adapter (and possibly the Pi behind it) if that input isn't rated for
  it. See [the app's DOCS.md, "Wiring"](autoterm/DOCS.md#wiring-connecting-the-pi-to-the-heater)
  for the full step-by-step and a diagram.

**Confirm which physical port reaches which device before trusting a
default port assignment.** USB-serial adapters can re-enumerate on replug
(port names shifting which physical connector they refer to), and getting
the device roles backwards produces silent no-op, not an error -- the most
misleading failure mode here. The app's `autodiscover_ports` option
handles this automatically (see its DOCS.md); otherwise verify by content
(read a few seconds of traffic on each port and check which one reports a
heater-shaped rich status frame vs. a simple cabin-temperature reading)
rather than trusting a port number.

## Installing

This is a standard Home Assistant app (a Supervisor add-on -- Home
Assistant's own developer-facing term for the mechanism is unchanged, only
the UI/user-facing name is "App" now) -- either:

- **Add this repository**: Settings -> Apps -> App Store -> ⋮ menu ->
  Repositories -> add `https://github.com/sinise/autoterm-heater-control`,
  then install "Autoterm Heater" from the store. (On a Home Assistant
  version older than 2026.2, this same menu is still labeled Add-ons ->
  Add-on Store.)
- **Or copy manually**: copy `autoterm/` into `/addons/` on the
  Home Assistant host, then install it from the local apps list.

See the app's `DOCS.md` for wiring, configuration options, and what you
get once it's running.

## Safety notes

- This controls a real combustion appliance. The physical panel keeps
  working normally the entire time the app runs (nothing about the
  passthrough relay is ever disabled) -- it's always available as a manual
  fallback.
- The **stop** command is well-confirmed (three independent real captures,
  verified live). **Start** is confirmed for preheat and thermostat modes
  but the duration encoding has at least one known inconsistency -- see
  `docs/PROTOCOL.md`. Watch the entities/log on first use of any command
  rather than assuming success.
- The auto-thermostat loop only acts on fresh cabin-temperature readings
  (it ignores stale/missing data) and rate-limits its own actions, but it
  is still code controlling a fuel-burning appliance unattended --
  supervise it through at least one full cycle before trusting it
  unattended overnight.
- Debug mode (extended telemetry probing) sends an experimental handshake
  frame toward the heater on the live bus -- off by default; read the
  app's DOCS.md before turning it on.

## Roadmap

- Nail down the `type04`/`type06` unidentified message pair.
- Resolve the preheat duration encoding inconsistency.

## Credits

Frame format and device-ID hunches for this family of heater protocols
drew on prior community reverse-engineering of similar Planar/Autoterm
units:
- github.com/kalutep/AutotermHeaterController
- github.com/prclm/AutotermHeaterController
- github.com/schroeder-robert/autoterm-air-2d-serial-control

Everything specific to the 5D variant here (CRC identification, frame
layout, device roles, state machine, confirmed commands) was independently
reverse-engineered from scratch against real hardware.

## License

MIT -- see `LICENSE`.
