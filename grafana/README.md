# Grafana dashboard

`autoterm-heater-dashboard.json` -- a dashboard for the Autoterm Heater
metrics, built against the specific metric names Home Assistant's built-in
Prometheus integration produces for these entities (confirmed against a
real instance via Grafana Explore, not guessed).

## Requires

- Home Assistant's [Prometheus integration](https://www.home-assistant.io/integrations/prometheus/)
  enabled, scraped into VictoriaMetrics (e.g. via vmagent or Prometheus
  remote_write).
- The [Autoterm Heater](../autoterm-addon/) app installed and its
  entities present in Home Assistant. Several panels (Defined/Measured
  revolutions, Fuel pump frequency, Flame/Liquid/Overheat/Board
  temperature, Fan current) only populate while its Debug mode is on and
  Extended telemetry active.

## Datasource is hardcoded, not a prompted import input

Every panel's `datasource` is hardcoded to
`{"type": "victoriametrics-metrics-datasource", "uid": "cfxh0obaj30g0f"}` --
the dedicated VictoriaMetrics Grafana plugin (not the generic "Prometheus"
datasource type), at the UID it has on the instance this was built for.
An earlier version of this file used Grafana's `__inputs`/`${DS_...}`
mechanism to prompt for a datasource on import instead, but that assumed
the generic Prometheus plugin -- this instance uses the VictoriaMetrics
plugin specifically, and there was no matching datasource type to select,
so import failed outright.

**If you're importing this into a different Grafana instance** (or this
one after re-adding the datasource with a new UID), find/replace
`cfxh0obaj30g0f` throughout the file with your own datasource UID first
(Grafana -> Connections -> Data sources -> click your VictoriaMetrics
source -> the UID is in the URL). If your instance uses the generic
Prometheus datasource plugin instead of the VictoriaMetrics one, also
change `"type": "victoriametrics-metrics-datasource"` to `"type":
"prometheus"` throughout.

## Importing

Grafana -> Dashboards -> New -> Import -> upload
`autoterm-heater-dashboard.json`. No prompts -- it should just work against the
datasource baked in above.

## Panels

- At-a-glance stats: cabin/coolant temperature, supply voltage, fault
  code, elapsed run time, burner active.
- **Temperature, revolutions and fuel pump frequency** -- the combined
  panel. Temperature on the left axis; revolutions and fuel pump frequency
  share the right axis (frequency is scaled x50 in the query purely so a
  ~0-5Hz line is visible next to ~0-250 revolutions -- see the panel's own
  description for the real-units caveat, or read it off the dedicated Fuel
  pump frequency panel below instead).
- All temperatures (flame, liquid, coolant, cabin, board, overheat).
- Blower speed: defined vs. measured revolutions.
- Fuel pump frequency (unscaled).
- Electrical: supply voltage + fan current.
- Running time: base (always available) vs. extended (debug mode only).
- Engine / relay state: raw diagnostic codes, not yet individually decoded
  -- useful for spotting *when* they change, not yet for reading a specific
  meaning off the value.
- Status flags: burner active / glow plug / telemetry stale / extended
  telemetry active, as a timeline.
- **State**, **Mode of operation (named)**, **Fault (extended, named)** --
  state-timeline panels, each a single numeric series (`State code` / `Mode
  code` / `Fault code (extended)`) name- and color-mapped entirely in
  Grafana (value mappings) -- see the section below for why.
- **Cabin temperature vs. heater output (%)** -- cabin temperature against
  two independent derived "power output" estimates: measured revolutions
  and fuel pump frequency, each normalized against the highest value seen
  within the panel's own displayed time range (`$__range` -- self-adjusting
  as you change the dashboard's time window, since there's no known
  manufacturer spec to normalize against instead). Kept as two series, not
  collapsed to one -- checked against real capture data and they diverge
  sharply outside steady combustion: revolutions-based reads ~26% during
  the pre-ignition glow-plug phase and ~69% during the post-shutdown
  cooldown purge (fuel is 0 in both, no heat is actually being produced),
  while fuel-based correctly reads 0% in both cases. They only agree within
  ~5-10 points during steady mid-to-high combustion. Only extended-frame
  fields, so only populated while Extended telemetry active is on.

## `State`/`Mode of operation`/`Fault (extended, named)` -- why these are numeric-mapped, not `enum`-exported

These three are text-valued (e.g. "idle", "High", "glow plug warming up").
The first attempt (app v1.2.0) declared them as MQTT
`enum` sensors (`device_class: enum` + an `options` list), on the theory
that HA's Prometheus integration would export them the same way it already
exports the climate entity's `mode`/`action` (a separate boolean series per
possible value). **That theory was wrong** -- confirmed via Explore against
a real instance:

```
homeassistant_entity_available{entity="sensor.autoterm_heater_state", ...}
homeassistant_last_updated_time_seconds{entity="sensor.autoterm_heater_state", ...}
homeassistant_state_change_total{entity="sensor.autoterm_heater_state", ...}
```

HA tracks these entities (availability, last-updated, state-change count)
but never exports their actual text *value* as a metric at all -- unlike
climate's `mode`/`action`, which do get a value-carrying metric. Generic
`sensor`-domain enums just aren't given that treatment.

**The actual fix** (app v1.4.0): three new *numeric*
sensors -- `State code`, `Mode code` (`state*10 + substate`), and `Fault
code (extended)` -- that export exactly like the already-working `Fault
code`/`Engine state`/`Relay state` sensors always did (plain numbers have
never been the problem, only text was). The three panels above query these
numeric sensors and do the number-to-name-and-color translation entirely in
Grafana, via each panel's own `fieldConfig.defaults.mappings` (built from
the same `STATE_NAMES`/`EXT_MODE_TABLE`/`EXT_FAULT_NAMES` tables the
app itself uses) -- not dependent on HA's Prometheus exporter for that
translation at all.
