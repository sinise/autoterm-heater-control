# Changelog

## 3.4.1

- **Turning Auto thermostat on no longer sends `start thermostat` if the
  cabin is already at or above target** -- 3.4.0 sent it unconditionally.
  There's nothing to do in that case, and starting anyway would just get
  stopped again on the next poll once the hysteresis loop notices, for no
  benefit. Falls back to sending it (the 3.4.0 behavior) if the cabin
  temperature isn't known or is stale -- unclear beats presumed-fine here.
  Turning it off still always sends `stop`, unconditionally.

## 3.4.0

- **Auto thermostat and Prevent freezing no longer disable themselves on
  a heater fault.** Previously either loop turned itself fully off the
  moment the heater reported any fault, requiring the user to notice and
  manually re-enable it -- for Prevent freezing in particular, a frost-
  protection safety net going silently unarmed on a fault seemed like the
  wrong failure mode. Instead, both loops now stay on and retry `start
  thermostat` on a fixed backoff whenever a fault is blocking them: wait
  5 minutes before the 1st retry, 15 minutes before the 2nd, 20 minutes
  before the 3rd, then stop retrying until the fault actually clears
  (which immediately resets the schedule for the next time). No longer
  spams `start thermostat` into a persistent fault every
  `MIN_ACTION_INTERVAL` (90s) either, which the old disable-on-fault
  behavior had also been guarding against. New diagnostic sensors **Auto
  thermostat note** / **Prevent freezing note** show which stage of the
  backoff each loop is in, or what it gave up on. See DOCS.md, "Faults".
- **Turning Auto thermostat on/off from Home Assistant now sends `start
  thermostat`/`stop` immediately**, instead of waiting for the next poll
  cycle's hysteresis check (which might not decide to act at all right
  now -- e.g. cabin temp already above target when switching on). Ignores
  the fault-retry backoff entirely: a direct on/off request from Home
  Assistant always goes straight to the heater.

## 3.3.0

- **The raw traffic capture log is now a rolling log, not a hard size
  cap.** Previously it stopped writing entirely once it hit
  `capture_log_max_mb` (default 20MB) until manually toggled off and back
  on -- silently going dark mid-incident was exactly the failure mode a
  capture log exists to avoid. It's now a chain of hourly files, rotated
  automatically, that keeps writing regardless of size; files older than
  the new **`capture_log_retention_hours`** option (default 24h) are
  deleted automatically on the next rotation. Applies to both the normal
  capture log and the Bypass log (they're the same mechanism).
  **Replaces** the `capture_log_max_mb` option -- if you'd customized it,
  that value is gone and `capture_log_retention_hours` reverts to its
  24h default; re-set it if you want something else.

## 3.2.5

- **`autodiscover_ports` now always logs which way it was read** --
  `autodiscover_ports: enabled (raw value '...')` or `disabled (raw value
  '...')`, the very first thing logged at startup. Previously the enabled
  path logged when it started probing but the disabled path logged
  nothing at all, so a toggle that silently didn't take effect (not
  saved, or the app not yet restarted since saving) looked identical in
  the log to discovery genuinely being off -- impossible to tell apart
  without this.
- **When enabled, autodiscovery now checks the already-configured
  `panel_port`/`heater_port` first** (a few-second listen/probe, same
  non-actuating query used for the full scan) and only falls back to
  scanning every `/dev/ttyUSB*`/`/dev/ttyACM*` candidate if that fails or
  nothing is configured yet -- faster in the common case (nothing
  actually changed), and the configured ports still get upgraded to a
  stable `/dev/serial/by-id/*` path on a successful quick check, not just
  a full scan.
- Added more progress logging during a full scan: which candidate ports
  were actually opened for panel listening, a heartbeat every 2s while
  waiting, and a line per candidate while probing for the heater --
  previously a scan could go several seconds with no log output at all,
  indistinguishable from being stuck.

## 3.2.4

- Renamed the repo folder `autoterm-addon/` -> `autoterm/` (the "-addon"
  suffix read oddly now that Home Assistant calls these apps -- see
  3.2.3). **Not** an install-breaking change like the 3.0.0 slug rename:
  Supervisor identifies an add-on by the `slug` field in `config.yaml`
  (`autoterm_heater`, unchanged), not by the folder it lives in, so this
  should reach existing installs as a normal update rather than requiring
  a fresh reinstall. `repository.yaml` needed no change -- it only holds
  repo-level metadata (name/url/maintainer), not a list of add-on folders;
  Supervisor discovers those itself by scanning for `config.yaml` files.

## 3.2.3

- Docs/UI text now says "app"/"App Store" instead of "add-on"/"Add-on
  Store", matching Home Assistant's own 2026.2 rename (Settings -> Apps ->
  App Store) -- the previous "Add-ons -> Add-on Store" wording in the
  install instructions no longer matches any menu in a current Home
  Assistant install. Developer/backend terminology is unchanged, matching
  Home Assistant's own choice to keep the rename UI/docs-only:
  `config.yaml`'s manifest format, the `autoterm-addon/` folder name,
  Supervisor's own API, and internal source comments still say "add-on".

- **Port autodiscovery now saves `/dev/serial/by-id/*` paths instead of
  raw `/dev/ttyUSB<N>`/`/dev/ttyACM<N>` device paths**, when the adapter
  has a USB serial number for udev to key one off of. Previously a
  successful discovery still saved a plain `/dev/ttyUSB<N>` path, which
  could shift again the next time some unrelated USB-serial device was
  plugged in or unplugged elsewhere on the same host -- exactly the
  scenario that prompted this: plugging in an unrelated adapter caused
  Supervisor's kernel-assigned numbering to reshuffle, and the
  previously-saved ports started failing to open with an I/O error. Falls
  back to the raw device path (logged as a warning) if no `by-id` symlink
  exists for that adapter, same as before.

- Renamed two sensors for a consistent "Temperature ..." naming pattern:
  **Cabin temperature** -> **Temperature at display**, **External
  temperature (polled)** -> **Temperature external sensor**. Display names
  only -- unique_id/entity_id are unchanged, so existing automations,
  dashboards, and history keep working under the same
  `sensor.autoterm_heater_cabin_temp`/`..._external_temp` entity IDs.

## 3.2.0

- Added an **External temperature sensor** option: point Auto thermostat
  and Prevent freezing at any existing Home Assistant temperature sensor's
  entity_id instead of the panel's own Cabin temperature report. No live
  entity dropdown -- Supervisor add-on config schemas can't read Home
  Assistant's entity registry, so it's a plain text entity_id field, polled
  every 15s via Home Assistant's own API (`homeassistant_api: true`, newly
  requested by this add-on). Falls back to Cabin temperature automatically
  if left unconfigured, switched off (new **Use external temperature
  sensor** switch), or the entity goes stale/unavailable for 90s -- Prevent
  freezing is a frost-protection safety net and stays working off the
  panel's own sensor rather than going blind on an external failure. New
  entities: **External temperature (polled)**, **Using external
  temperature sensor**, **Use external temperature sensor**. The climate
  entity's displayed current temperature now follows whichever source is
  actually driving control. See DOCS.md.

## 3.1.1

- Fixed **Fault** showing "unknown" at all times instead of "No faults" --
  fault code 0 (the vendor table's own "No faults" entry) was being treated
  the same as "no telemetry received yet" and blanked out instead of looked
  up. Only the display text was wrong; **Fault active** was never affected
  by this (it already used a plain truthiness check).
- Fixed **Telemetry stale** (and so **problem** showing continuously,
  looking like it tracked debug mode) firing on a normal, unrelated pattern
  in the physical panel's own polling: confirmed against a fresh capture
  that it regularly goes ~15-17s without sending a status or cabin-temp
  query at all, while it detours into other query types -- nothing to do
  with the debug handshake's own ~60s cadence (checked the timing, they
  don't correlate). `STALE_AFTER` (5s) was tighter than that native gap, so
  this was firing on ordinary panel behavior; raised to 25s.

## 3.1.0

- Added **Heater output** (%, extended telemetry only): this add-on's own
  linear estimate from Fuel pump frequency (4.2Hz = 100%), not a
  vendor-reported field -- see DOCS.md.
- **HEATER->PANEL passthrough no longer holds every frame for a full
  parse.** Every frame in that direction was being buffered until fully
  received (up to ~1 frame's worth of extra latency, ~60-100ms+ at 2400
  baud) so the extended-telemetry frame could be filtered out -- even with
  debug mode off, when that frame never appears at all. The physical
  panel's own responsiveness was paying for filtering it almost never
  needed. Now: true immediate byte-for-byte passthrough by default (same
  as the PANEL->HEATER direction always had), switching to the buffered
  filtering path only while it's actually needed -- debug mode streaming
  the extended frame, or briefly right after this add-on injects a
  command (see next item).
- **Experimental:** the heater's ack to a just-injected command (Start/
  Stop/Auto thermostat/Prevent freezing) is now withheld from reaching the
  panel, on the theory that forwarding a reply to a request the panel
  never made is the same class of problem that was confirmed to break it
  for the extended-telemetry frame (see docs/PROTOCOL.md). There's no
  field in the ack that distinguishes "reply to us" from "reply to the
  real panel" -- injected frames use the same sender identity -- so this
  is a 1.5s timing window after each injected send, not a certain match.
  Watch the capture log if this matters to you; treat as unproven the way
  the quiet-gap fix was.
- **Auto thermostat and Prevent freezing now disable themselves the
  moment the heater reports a fault**, instead of blindly re-issuing
  `start thermostat` every time they next see it idle. Previously neither
  loop looked at the fault byte at all. New entities: **Fault** (named,
  populated without debug mode), **Fault active** (binary_sensor,
  `device_class: problem` -- the natural trigger for a Home Assistant
  automation/notification), and **Last fault code/Last fault/Last fault
  time**, which keep showing the most recent fault after it clears so it
  doesn't just vanish the moment the heater recovers. See DOCS.md for a
  sample notification automation.

(No keep-alive code remains anywhere in this add-on -- it was fully
removed in 2.3.0 below, checked again while working on this release.)

## 3.0.0

- **Renamed**: `Autoterm Heater Debug` -> `Autoterm Heater`, slug
  `autoterm_heater_debug` -> `autoterm_heater`, folder
  `autoterm-debug-addon/` -> `autoterm-addon/` -- this is now the only
  add-on in the repo (the non-debug one was removed as redundant; see
  main repo README). **Not** a repeat of the 2.0.0 entity-breaking rename:
  `NODE_ID`/the device name were already `autoterm_heater`/"Autoterm
  Heater" (unchanged since 2.0.0), so every `sensor.autoterm_heater_*`
  entity ID stays exactly as it was -- only the add-on's own Supervisor
  identity (its name/slug) changes. Since Supervisor treats a slug change
  as a different add-on, you'll need to remove the old install and add
  this one fresh from the renamed repository, then re-enter your
  Configuration options (ports, heater_profile, etc. -- these don't carry
  over from a fresh add-on install, unlike the entities themselves). Also
  dropped a couple of internal-only "debug" leftovers with no
  user-visible effect (MQTT client ID suffix, Python logger name) and
  fixed a startup log line that still said "Autoterm 5D DEBUG bridge"
  from before the 2.0.0 rename.

## 2.3.0

- **Removed** the experimental Thermostat keep-alive switch added in
  2.2.0. Ruled out conclusively rather than left unresolved: a Bypass-mode
  capture (this add-on sending zero commands for over an hour, heater
  started and set to 26°C directly from the physical display, in its own
  unlimited-runtime thermostat mode) still showed the same ~30-40 minute
  self-stop-then-restart, confirming this is entirely the heater/panel's
  own native behavior -- nothing this add-on could ever have sent or
  suppressed was involved, so periodically re-affirming a marker had
  nothing to affect. See docs/PROTOCOL.md's "Open question" note (now
  resolved) and the 2.2.0 entry below for how it was tested.
- Reorganized the entity list: all temperature sensors (Cabin, Coolant,
  Flame, Liquid, Overheat, Board) are now consecutive instead of split
  across the base and extended-telemetry sections. Defined/Measured
  revolutions were already adjacent and stay that way.

## 2.2.0

- Added a **Bypass (disable all injection)** switch: while on, this add-on
  becomes a pure passive relay -- every command path (Start/Stop/Start
  pump buttons, Auto thermostat, Prevent freezing, the debug handshake) is
  suspended and logged instead of sent; real panel<->heater traffic keeps
  flowing exactly as before. Useful for capturing a clean baseline
  uninfluenced by anything this add-on injects. While on, all traffic is
  also logged to a separate `bypass_*.log` file (new "Bypass log
  file"/"Bypass log size" sensors), independent of the normal capture
  log's own on/off state, so a bypass test is captured cleanly even if the
  normal capture log is off.

## 2.1.0

- Fixed a likely cause of periodic "no communication" glitches on the
  physical panel and the heater appearing to restart on its own every
  30-40 minutes -- confirmed reported happening with debug mode
  **disabled**, so it isn't the 1.5.0 handshake-collision issue. Root
  cause: every command this add-on injects toward the heater (Start
  preheat/thermostat, Stop, Start pump -- both the manual buttons and the
  automatic stop/start-thermostat calls Auto thermostat/Prevent freezing
  make on their own) was written straight onto the bus with no check for
  whether the panel's own query/reply exchange was already mid-flight.
  1.5.0 only fixed this for the debug handshake specifically; the same
  collision risk was always present for every other injected command too,
  and Auto thermostat/Prevent freezing fire on exactly this kind of
  interval. The quiet-gap wait (250ms, 2s cap) used for the debug
  handshake is now shared by every injected command. As with 1.5.0, this
  narrows the collision window rather than formally proving it eliminated.

## 2.0.0

- **Renamed**, matching the regular add-on's 2.0.0: `Autoterm 5D Debug` ->
  `Autoterm Heater Debug`, slug `autoterm5d_debug` -> `autoterm_heater_debug`,
  repo moved to `autoterm-heater-control`. **Breaking**: every Home
  Assistant entity gets a new entity_id (device name changed from
  "Autoterm 5D Heater" to "Autoterm Heater") -- existing
  automations/dashboards/Grafana panels referencing the old
  `sensor.autoterm_5d_heater_*` entity IDs need updating, and you'll
  likely need to remove and reinstall this add-on from the renamed
  repository. `binar_5s`/`binar_5s_next` are now also marked `tested`
  (same internal codename as `autoterm_flow_5`, byte-identical data).

## 1.6.0

- Added a "Heater profile" config option: the extended-telemetry decoder
  (byte offsets, state/mode names, fault names) is now selectable across
  19 heater models, extracted from the vendor diagnostic tool's own
  per-model Profiles/*.pfl + language.res data files (plaintext, not a
  decompile) -- the same way `autoterm_flow_5`'s fields were originally
  derived. **Only `autoterm_flow_5` is confirmed against real hardware --
  every other profile is untested**: unverified byte offsets, and not
  even confirmed the extended-telemetry mechanism (PUBR0 handshake,
  dev02/type01 frame) works the same way on that model at all. A new
  "Heater profile" sensor shows the active selection and flags
  "(NOT TESTED)" for anything but Flow 5; the add-on log does the same at
  startup. Selecting a different profile only changes which formulas
  decode the extended frame -- it doesn't affect the base 18-byte
  protocol, commands, or Prevent freezing, all of which stay as
  originally confirmed regardless of this setting.

## 1.5.0

- Confirmed on real hardware (reconstructing this add-on's own "Telemetry
  stale" logic against a live capture and matching it second-for-second
  to Home Assistant's own history) that sending the debug handshake while
  the panel's own query/reply exchange is mid-flight can corrupt that
  exchange -- happened on roughly half of handshake sends, consistent
  with a timing collision. Fixed: the handshake is now held until the bus
  has been quiet for 250ms (no frame seen from either device) instead of
  fired blindly on a fixed timer, applied to both the periodic send and
  the "Send debug handshake now" button. Narrows the collision window;
  still treat debug mode as experimental.

## 1.4.0

- Added numeric mirrors for the three `enum` sensors added in 1.2.0:
  "State code", "Mode code" (`state*10+substate`), "Fault code
  (extended)". Confirmed via Grafana Explore against a real instance that
  Home Assistant's Prometheus integration tracks enum sensors'
  availability/last-updated/change-count, but never exports their actual
  text value as a metric -- the 1.2.0 fix didn't actually solve the
  Prometheus/Grafana graphing gap it was meant to. These numeric sensors
  do export normally (same as Fault code/Engine state/Relay state
  already did), and are meant to be name-mapped in Grafana itself (value
  mappings) rather than relying on HA's exporter for that. See `grafana/`
  in the main repo for updated panels using these.

## 1.3.0

- Added a "Prevent freezing" switch and target (0-10°C), mirrored from the
  Autoterm 5D add-on 1.2.0: an independent frost-protection safety net
  that starts the heater whenever cabin temperature reaches the floor,
  regardless of the auto-thermostat's own state or a prior manual Stop.

## 1.2.0

- `State`, `Mode of operation`, and `Fault (extended, named)` are now
  declared with `device_class: enum` and an explicit `options` list
  (required for MQTT enum sensors). Previously these were plain text
  sensors that Home Assistant's Prometheus exporter silently drops (it
  can't export non-numeric values) -- they're now exported the same way
  the climate entity's mode/action already were, one boolean series per
  possible value, so they show up in VictoriaMetrics/Grafana too.

## 1.1.0

- The extended telemetry frame (dev02/type01) is no longer forwarded to
  the physical panel -- confirmed on real hardware that receiving it
  visibly confuses the panel's own display. It's still decoded for HA
  sensors and still logged (marked "NOT forwarded (filtered)") if capture
  logging is on, it just never reaches the panel's wire anymore.
- The add-on's own log messages (info/warning/error) are now also written
  into the capture log, interleaved chronologically with the traffic --
  one file has everything needed to debug an incident.

## 1.0.1

- Moved the capture log from `/share/autoterm_debug/` to
  `/config/autoterm_debug/` -- it now shows up directly in the File editor
  add-on's default file tree instead of requiring extra navigation/config
  to reach `/share`.

## 1.0.0

- Initial release. Based on the Autoterm 5D add-on (1.1.0): same relay,
  commands, and MQTT discovery, plus a toggleable debug-mode extended
  telemetry probe (vendor "PUBR0" handshake), a toggleable raw traffic
  capture log under `/share`, and sensors for every known extended-frame
  field. See `docs/PROTOCOL.md` in the main repo for the field formulas.
