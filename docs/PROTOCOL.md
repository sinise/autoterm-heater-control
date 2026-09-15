# Autoterm 5D UART protocol notes

Reverse-engineered from passive capture and live testing against one real
Autoterm 5D diesel heater + comfort panel. Treat this as a strong working
model, not a vendor spec -- several parts are marked as unconfirmed below,
and other units/firmware revisions may differ.

## Physical layer

- 2400 baud, 8N1, 5V TTL logic levels (not RS-232, not 3.3V)
- Two independent full-duplex UART wires between panel and heater (not a
  shared bus): one carries panel-driven traffic, the other heater-driven
  traffic. Confirmed content-wise, not assumed. On the reference harness
  these are colored yellow (heater -> panel) and white (panel -> heater).
- A solid shared ground between any tap/proxy point and the heater/panel
  circuit is required -- a missing ground produced pure garbage on this
  setup before it was fixed.
- The harness also carries a red +12V power wire alongside the two data
  wires -- not a signal, don't tap or cut it. See the app's `DOCS.md`,
  "Wiring", for the full physical hookup and why (12V into a UART input
  built for 3.3V/5V logic can damage it).

## Frame format

```
AA | dev(1) | len(2, little-endian) | type(1) | payload(len bytes) | crc16(2, big-endian)
```

- Total frame length = 7 + len
- CRC is **CRC-16/MODBUS** (poly 0xA001 reflected, init 0xFFFF), computed
  over the entire frame **including the leading 0xAA**, transmitted
  most-significant-byte-first (the reverse of standard Modbus wire order).
  Identified by brute-forcing every common CRC-16 variant against captured
  frames -- this one was the only exact match.

## Device (sender) byte

| Byte | Role |
|---|---|
| `0x03` | **Panel/display.** Originates every start/stop handshake (it's the device with physical buttons) and reports a plain 1-byte cabin-temperature reading. |
| `0x04` | **Heater.** Reports the rich 18-byte status frame (state machine, fault code, coolant temp, timers) -- makes sense as the heater's own telemetry. |
| `0x00`, `0x02` | Secondary heater identities, used for short acks. |

**Important history:** for most of one capture session these two roles were
assumed backwards (0x03 = heater, 0x04 = panel), based on an unverified
assumption about which physical wire was tapped where. That assumption
produced a coherent-looking but wrong protocol model, and specifically
caused early command-injection attempts to send the right bytes to the
*wrong* device with the *wrong* sender byte -- two independent bugs
stacking to produce a clean "no reaction, no error" failure that looked
like a wiring fault. It was caught by noticing that the "heater's" 1-byte
report was cabin temperature (physically implausible for a device mounted
away from the cabin) while the "panel's" report carried internal telemetry
(fault codes, coolant temp -- implausible for a display to know
independently). If your own capture seems internally consistent but the
physical behavior doesn't match, re-question this assumption first.

## Message types seen

| Type | Direction | Payload | Meaning |
|---|---|---|---|
| `0x0f` | heater query (empty) -> panel reply (18 bytes) | see below | Main status poll, ~1/sec |
| `0x11` | panel report (1 byte) -> heater ack (empty) | cabin temp, °C | Cabin temperature reading |
| `0x01` | panel -> heater ack | 2 bytes | Start handshake (mode marker or duration -- see below) |
| `0x02` | panel -> heater ack | 2 bytes, big-endian u16 | Preheat duration in minutes (preheat mode only) |
| `0x03` | panel -> heater ack | empty | **Stop**, single exchange. Also reused as a *sustained, rapid-fire* (sub-second) heartbeat throughout the post-stop cooldown/fan-purge phase -- same type code, very different role by repetition rate. |
| `0x04` (dev `0x02`) | heater-side ack | empty | Generic ack following the start handshake |
| `0x04`, `0x06` (dev `0x03`/`0x04`, larger payload) | unprompted, both directions | 5 bytes each | **Unidentified.** Seen twice, ~7 minutes apart, unrelated to any button press. Payload looked plausibly date/time-like in one case (`0a 01 0d 03 01`) but this was never confirmed. Worth investigating with a longer capture. |

Every other frame type not listed here was never observed.

## Heater status frame (dev04, type0f, 18-byte payload)

Indices are 0-based into the payload (i.e. after the 5-byte header, before
the 2-byte CRC).

| Idx | Field | Notes |
|---|---|---|
| `[0]` | Main state | `0x00` idle, `0x02` running, `0x03` late-run, `0x04` cooldown/fan-purge, `0x05` final-shutdown-stage, then back to `0x00` |
| `[1]` | Sub-stage within `[0]` | Resets on every state change |
| `[2]` | **Fault code** | Plain decimal-as-integer (e.g. `0x4e` = 78 = "check fuel system"). `0` = no fault. Confirmed exact match against a real panel-displayed fault code. **Latches** until the next successful ignition -- the panel's own "clear fault" button does *not* reset this over the wire, confirmed by watching it stay non-zero for 5+ minutes after clearing on the display. |
| `[3]`, `[4]` | Coolant/water temperature | Jitter ±1 of each other; tracked the panel's own displayed water-temp readout (42→52°C during a preheat cycle) closely. Averaging the two is a reasonable smoothing. |
| `[6]` | Noisy, voltage-like | No clean state correlation found; likely raw ADC/supply voltage telemetry. |
| `[7]` | Slowly rising sensor | Climbs steadily during a burn; candidate exhaust/heat-exchanger temp. Not the setpoint (a hypothesis that was tested and falsified). |
| `[9]` | Elapsed minutes since ignition | Increments roughly once/minute, latches (does not reset) at stop. |
| `[11]` | Elapsed seconds since ignition | Increments ~1/sec, wraps at 256, freezes when combustion stops. |
| `[12]` | Burner-active flag | `0xff` while actively burning, `0x00` otherwise. |
| `[16]` | Separate elapsed/cooldown counter | Keeps incrementing into the cooldown phase after `[11]` has frozen. |

Target/setpoint temperature was **never found anywhere on the wire** in
either direction, across many capture hours and multiple modes. It appears
to live entirely in the panel and never gets transmitted -- consistent with
the panel doing its own thermostat comparison and only ever telling the
heater a plain start/stop (see "confirmed commands" below).

## Confirmed commands

All are injected impersonating the **panel** (sender byte `0x03`), sent out
whichever physical port's TX line actually reaches the real heater's RX pin
-- confirm this on your own wiring before trusting a static port assignment
(see the device-role warning above; getting it backwards produces silent
no-op, not an error).

**Start, preheat mode**, byte-for-byte reproducible:
```
type01, payload = 00 1e      (fixed marker, usually)
  ~1.5s later
type02, payload = <minutes, big-endian u16>   (the real duration)
```
Verified with 30, 70, and 120-minute preheat starts. The 120-minute case
broke the "fixed marker" pattern entirely -- `type01` carried the actual
duration directly (`00 78` = 120) with **no** `type02` at all. The encoding
isn't fully nailed down; test and watch the bus rather than assuming.

**Start, thermostat mode:**
```
type01, payload = 00 22      (fixed marker, in every case observed)
```
No `type02` ever follows in thermostat mode -- consistent with the panel
managing its own stop timing rather than giving the heater a duration.

**Resolved:** the heater has been observed self-stopping (going idle,
then restarting itself within seconds) roughly every 30-40 minutes under
this app's own Auto thermostat/Prevent freezing, which never send a
duration and never send a `stop` in that window either (confirmed from a
real overnight capture -- every restart event was `start_thermostat`,
none was `stop`). This looked like it could be either an internal timeout
independent of what started it, or something the real panel does that
this app doesn't (e.g. periodically re-affirming the `00 22` marker
while running). Settled with a Bypass-mode capture (app v2.2.0):
this app sent zero commands for over an hour while the heater ran in
thermostat mode started directly from the physical display (26°C,
"unlimited" runtime) -- the identical ~30-40 minute self-stop-then-
restart still happened. That rules out anything this app could ever
send: it's the heater/panel's own native behavior, not a protocol
detail this project is missing or getting wrong.

**Likely mechanism, found afterward:** a real capture (Bypass off, Auto
thermostat driving it) caught the `[2]` fault byte reading `25` for the
entire cooldown-to-idle span of one of these self-stops, clearing back to
`0` the moment the next `start thermostat` brought it running again. `25`
is `"Temperature growing too fast"` in the vendor string table (see the
fault-name tables below) -- unconfirmed against a real fault before this,
but it fits: thermostat mode here never sends a power-level/duration, so
if the heater has no external cabin-temp feedback to modulate against, it
may just run at a fixed output until its own rate-of-rise safety trips,
which would produce exactly this ~30-40 minute stop/restart cadence.
Auto thermostat/Prevent freezing don't look at the fault byte before
restarting -- they only check `state == idle` -- so this isn't specific
to those loops; the real panel's own thermostat mode hitting the same
safety trip is presumably why it self-restarts too.

**Stop:**
```
type03, empty payload
```
The best-confirmed command here -- derived from three independent real
manual stops in passive capture (never a timeout; each was a deliberate
button press), all showing the identical signature 1-2 seconds before the
state byte flips to cooldown. Verified live: a single injected `type03`
frame took the heater from actively running to a full clean stop-and-idle
cycle (~4 minutes) with zero fault, matching real button-press behavior
exactly.

## Byte-level corruption on the heater leg (open, unexplained)

Confirmed across two independent real captures (an 8-hour overnight
capture, and a later ~1-hour Bypass-mode capture -- app sending zero
commands) that the heater->Pi byte stream intermittently garbles for
roughly 20-260ms at a time, in short bursts happening on average every
7-8 minutes (63 such bursts counted in the 8-hour capture; a comparable
elevated rate of stray/unparsed bytes in the 1-hour Bypass capture too).
**Confirmed not caused by anything this app sends**: it happened during
the Bypass-mode window with zero commands in flight, on both the base
18-byte `type0f` status frame and the 58-byte extended frame alike, and
with no timing correlation to the quiet-gap/handshake logic above.

The corruption has a specific, repeated signature rather than looking like
scattered noise. Reconstructing the garbled bytes against the expected
next reading (same fields, incrementing counters) lines up almost
perfectly except for one thing: the frame's own `dev` byte (`0x04` or
`0x02`) turns up **before** the `0xAA` start marker instead of after it,
and the length-low byte vanishes entirely. E.g., one base-status instance:

```
expected: aa 04 12 00 0f 02 03 00 46 46 00 81 43 00 01 01 5c ff 01 00 00 ...
observed: 04 aa -- 00 0f 02 03 00 46 46 00 81 43 00 01 01 5c ff 01 00 00 ...
```

Confirmed from the app's own relay code that stray (unframeable) bytes
are forwarded to the panel byte-for-byte unmodified -- the filtering
relay path does `out += ev[1]` for a stray event, same as a real frame.
So this isn't the app's software dropping/eating bytes before they
reach the panel: whatever's happening happens upstream of the Pi (on the
wire, or in the USB-serial adapter/driver), and the panel receives the
same corrupted bytes too. This is the leading candidate explanation for
the panel's occasional "no communication" flicker, and it's independent
of both the PUBR0/quiet-gap issue above and of debug mode entirely.

Confirmed almost exclusively on the **heater** leg -- essentially never
seen on the panel leg in either capture, which points at something
specific to that one physical connection (adapter, cable, or grounding)
rather than a protocol-level bug. Root cause not yet identified (real
capture data, not guessed): could be electrical noise/marginal grounding
on that leg, or a USB-serial adapter/driver quirk (some cheap USB-UART
bridge chips are known to occasionally reorder or drop bytes at USB
packet boundaries). Deprioritized rather than actively investigated
further as of this writing -- if picking this up again, the next useful
step is probably swapping the heater-leg adapter/cable and re-capturing,
or instrumenting at a lower level than this project's own framer.

## Extended diagnostic-mode telemetry (dev `0x02`, type `0x01`, 58-byte payload)

The vendor's own Windows diagnostic tool ("Autoterm Test") talks to the
heater directly (spoofing the panel identity, same as this project's own
command injection) and gets back a much richer ~1/sec status frame than the
18-byte `type0f` poll above. Recovered by capturing real traffic between
that tool and a real heater (profile: **Flow 5** / internally `BINAR-5S`)
with a non-intrusive Windows serial sniffer -- not from decompiling the
tool itself. Confirmed against a real session: pump start -> pump stop ->
heater start -> ramp to high -> stop -> full cooldown -> idle, cross-checked
against the timestamps and durations the user reported for that exact
sequence.

**Getting the heater to send this frame** requires a literal 15-byte
handshake command first, observed once at the start of the tool's session:

```
aa 50 55 42 52 30 00 00 00 ff ff ff ff 0f b0
```

(`50 55 42 52 30` is ASCII `"PUBR0"`.) This is a fixed literal, not
`dev/len/type/payload` framing like the rest of the protocol. The CRC is
the same CRC-16/MODBUS over the whole frame, but transmitted
**least-significant-byte first** -- the reverse of every other frame in
this protocol (which are MSB-first). Caught by a byte-order mismatch when
first implementing this: `crc_bytes()` computed `b0 0f` for this body, but
the real captured frame ends `0f b0`. Immediately after this was sent, the
heater began streaming the frame below at ~1/sec, unprompted (no further
polling needed).

**Update, now tested on the live boat bus with the physical panel
connected -- two distinct failure modes found, one fixed, one narrowed:**

1. **The extended telemetry frame confuses the physical panel** if it's
   actually relayed to it (observed directly: the panel's own polling
   degraded over about a minute, then collapsed to unparseable garbage,
   while the heater side -- including the extended stream itself -- kept
   working fine throughout). The panel's firmware was clearly never built
   to receive an unsolicited 65-byte frame from `dev02` mid-poll-cycle.
   **Fixed** in the app (from v1.1.0): this specific frame is
   filtered out of the heater->panel relay direction, decoded for the
   app's own use but never forwarded to the panel's wire.
2. **Sending the `PUBR0` handshake itself can also corrupt the panel's
   query/reply exchange**, independent of (1) -- confirmed by
   reconstructing the app's own staleness logic against a real capture
   and matching it second-for-second to Home Assistant's actual
   stale/OK history, and by finding a real 18-byte heater reply missing 2
   bytes immediately after a handshake send. Happened on roughly half of
   handshake sends, not all -- a timing collision between the handshake
   write and the panel/heater's own in-flight exchange on the shared
   line, not a guaranteed failure. **Narrowed, not proven eliminated**, in
   the app from v1.5.0: the handshake is held until the bus has
   been quiet for 250ms before sending, rather than fired on a blind
   timer. Not re-validated against a fresh long capture the way (1) was --
   treat `PUBR0` as experimental, watch `Telemetry stale` after enabling
   it.
   - The same collision mechanism turned out **not to be specific to
     `PUBR0`**: every command either app injects toward the heater
     (Start preheat/thermostat, Stop, Start pump) shares the exact same
     write path and had none of this protection, including the automatic
     stop/start-thermostat calls the Auto thermostat and Prevent freezing
     loops make on their own -- which happen on their own ~30-90s+
     interval with no debug mode involved, and can visibly present as the
     panel briefly going "no communication" plus a spurious state
     transition, roughly matching Auto thermostat's own hysteresis
     interval. **Fixed** in both apps from v2.1.0: the same 250ms
     quiet-gap wait now applies to every injected command, not just
     `PUBR0`. As with the handshake fix, this narrows the window rather
     than proving it eliminated.
   - Separately, the HEATER->PANEL relay direction was found to be holding
     *every* frame (not just the extended one) for a full parse before
     forwarding it -- unconditionally, whether or not there was anything to
     filter -- adding real latency to the panel's own responsiveness even
     with debug mode off. **Fixed from v3.1.0**: immediate byte-for-byte
     forwarding by default (matching PANEL->HEATER, which always had it),
     falling back to the buffered/filtering path only while debug mode is
     streaming the extended frame or an injected command's ack is still
     pending.
   - **New, experimental in v3.1.0**: the heater's ack to an injected
     command is also withheld from the panel now, on the same "the panel
     never asked for this" reasoning as (1) above. There's no way to tell
     "ack for us" from "ack for the real panel" from the frame alone --
     injected frames use the same dev03 sender identity the real panel
     does, so the heater can't distinguish them either -- so this is a
     short timing window (1.5s) after each injected send, not a certain
     match. One real capture: after injecting `type01`/`00 22`, the next
     dev00/dev02 empty-payload frame arrived ~0.9s later. Unconfirmed
     whether withholding it actually helps the panel; watch for it
     mismatching (swallowing a frame the panel needed, or missing the real
     ack) if picking this up again.

Frame: `AA | 02 | 3a 00 | 01 | <58-byte payload> | crc16`. Indices below
are 0-based into that 58-byte payload.

**Field offsets and names are not guessed** -- the vendor tool ships a
plaintext `.pfl` "profile" per heater model (`Profiles/AUTOTERM FLOW 5.pfl`
here) that literally spells out, per field, a byte-offset formula and an
index into the tool's own string table (`language.res`). That's a
straight text-file read, not a decompile of the executable. Every formula
below is the vendor's own, cross-checked against the real capture (see the
state table further down, and the physically-sane values it produces --
flame temperature jumping from ~50 to 200+ on ignition, fuel pump
frequency landing in the real ~1.5-4 Hz range, voltage sagging to 11.0V
during glow-plug draw, etc).

| Idx | Field | Formula (0-based payload offset) | Notes |
|---|---|---|---|
| `[0]`, `[1]` | Mode of operation of the product | state=`[0]`, substate=`[1]` -> lookup table below | |
| `[2..4]` | Running time | `[2]*65536 + [3]*256 + [4]`, seconds | 3-byte big-endian counter (not 1 byte -- an earlier pass here mistook the low byte alone for a counter that "wraps at 256"; it doesn't, it's just the low byte of this). Freezes at idle, doesn't reset. |
| `[11]` | Defined Revolutions | `[11]` | Blower target, raw units. `0` in idle/pump-only, climbs through ignition or `Voltage 0` |
| `[12]` | Measured Revolutions | `[12]` | Blower actual, raw units. Tracks `[11]` closely (occasionally trails by 1) -- a real closed-loop pair. |
| `[13]` | Glow Plug | `[13] > 0` | Boolean. `True` through the ignition ramp, `False` once running/pump-only/cooldown. |
| `[15]` | Fuel pump frequency | `[15] / 10`, Hz | `0` except while actually burning; climbed 1.7 -> 3.0 -> 3.8 Hz during the ramp to High, dropped to `0` immediately on Stop (well before the fan-purge cooldown finished). |
| `[17..18]` | Flame temperature | `([17]*256 + [18]) - 273`, °C | Kelvin-to-Celsius. ~51°C baseline (no flame) -> 212-221°C once burning. |
| `[19]` | Liquid temperature | `[19]`, °C | |
| `[20]` | Overheat temperature | `[20]`, °C | |
| `[21]` | Board temperature | `[21]`, °C | |
| `[22..23]` | Voltage | `([22]*256 + [23]) / 10`, V | ~12.5-12.9V steady; dipped to 11.0V during one glow-plug-draw sample -- plausible real sag, not noise. |
| `[36]` | Fault code | `[36]` | `0` throughout this capture (no fault occurred). Presumably the same code space as the 18-byte frame's `[2]`, not independently confirmed. |
| `[51]` | Engine state | `[51]` | Always `0` in this capture -- likely only meaningful on vehicle-integrated variants. |
| `[52]` | Relay state | `[52]` | `0` at idle/pump-only/cooldown, `1` while the ignition sequence and running are active. Probably a bitmask; only bit 0 was ever exercised here. |
| `[54..55]` | Fan current | `[54]*256 + [55]`, presumably mA | Always `0` in this capture -- unconfirmed whether this model actually populates it. |

Everything else in the 58 bytes is either constant across this one capture
or too noisy to characterize yet -- treat unlisted offsets as unmapped, not
"zero"/unused. One profile field (`Stage/Mode`, formula `[0] + [1]/10`) is
just a decimal-display alternate of the same two state bytes, not a
separate piece of data.

### State/substate name table

The `.pfl` defines the "Mode of operation" display as a lookup:
`index = state*10 + substate`, indexing into a table of `language.res`
string-table indices (44 entries, `state` 0-4 x `substate` 0-9, unused
combinations point at `"unknown"`). Every value below is the vendor's own
English string, and every transition actually seen in the real capture
(the pump/heat/ramp-to-high/stop/cooldown sequence) landed on a
physically-sensible name:

| state | substate | Name | Seen in capture? |
|---|---|---|---|
| 0 | 1 | waiting for a command (idle) | yes |
| 1 | 0 | waiting for temperature reduction | no |
| 1 | 1 | locked | no |
| 2 | 0 | cooling | yes (briefly, 1s, right as ignition begins) |
| 2 | 1 | glow plug warming up | yes |
| 2 | 2 | preparation for ignition | no |
| 2 | 3 | Ignition 1 | yes |
| 2 | 4 | Ignition 2 | no |
| 2 | 5 | blowing | no |
| 2 | 6 | combustion chamber heating | yes |
| 2 | 7 | blowing | no |
| 3 | 0 | low | no (this run never dropped to Low) |
| 3 | 2 | **High** | yes |
| 3 | 4 | blowing | no |
| 3 | 5 | waiting | no |
| 3 | 6 | blowing | no |
| 3 | 7 | pump only | yes |
| 3 | 8 | **middle** | yes |
| 4 | 0 | blowing (cooldown/fan-purge) | yes |
| 4 | 3 | shutting down | no |

This directly answers what "High"/"Medium" meant in the diagnostic tool's
UI: state `3` covers all of Low/Middle/High/pump-only-vent, and it's the
*substate* byte that actually names the power level -- `0`=low, `8`=middle
("Medium" in the tool's own wording elsewhere, English base string is
"middle"), `2`=High. In this capture, `3.8` (middle) held for ~2 seconds
right after the ignition ramp finished, then `3.2` (High) held until Stop
-- consistent with the heater briefly settling at a lower level before
ramping to the commanded target.

The 18-byte frame's `[0]`/`[1]` almost certainly share this same
underlying firmware state machine (same value range, same behavior across
a real start/stop/cooldown cycle) but this hasn't been independently
confirmed against a *labeled* 18-byte capture -- the `.pfl` formulas above
are scoped to this extended frame only, not the panel's own poll.

### Other heater models

The vendor tool ships one `.pfl` profile per heater model (19 total,
`Profiles/*.pfl`) -- the extended-frame field derivation above was redone
generically across all of them (same method: read the plaintext formulas
and state/fault tables, cross-reference `language.res` for labels) and
baked into the app as a selectable "heater profile". **Only
the Flow 5 / BINAR-5S profile used throughout this document is confirmed
against real hardware** -- every other model's byte offsets, state names,
and fault names come straight from the vendor tool's own data with zero
hardware validation, and it isn't even confirmed the `PUBR0`
handshake/`dev02`/`type01` mechanism applies to those models at all. See
the app's DOCS.md, "Heater profile: other models", for the full list
and per-model status.

## New confirmed command: pump-only start

```
dev 0x03, type 0x21, payload = 00 28
```

Sent impersonating the panel, exactly like the commands below. The heater
(`dev 0x04`) echoed it back within the same second (`type 0x21`, payload
`00 28 00`), and the extended state/substate above went to `0x03`/`0x07`
("late-run"/vent) for the observed ~11-second pump-only run. The existing
`type03` empty-payload **Stop** command (below) stopped it -- confirming
`type03` is a general "stop whatever's active" command, not heat-specific.
Only one payload value (`00 28`) has been observed; whether it's
configurable (e.g. a duration or fan-speed target) or a fixed marker like
`type01`'s preheat marker is untested.

Because this uses the same spoofed-panel identity and the same live
panel<->heater bus as the already-confirmed Start/Stop commands (not the
direct-connection diagnostic mode above), it carries the same risk profile
as those and is reasonable to wire into the app the same way.

Curiosity, not yet explained: the `.pfl` profile itself labels its own
"pump start" UI button with the reference `19,30` (its own internal
command-id notation, alongside a literal `2400` baud field), which doesn't
obviously correspond to `type 0x21, payload 00 28` byte-for-byte. The real
captured wire bytes are what's documented and used here -- they're
directly observed, CRC-valid, echoed back by the heater, and produced the
correct physical state transition. The `.pfl` reference is left as an
open question in case it becomes relevant when profiling other models.

## What's still open

- The `type04`/`type06` unprompted messages (5-byte payloads, ~7 min apart,
  unrelated to button presses).
- Why the preheat duration encoding shifts between a two-frame
  (marker+duration) and single-frame (direct duration) form.
- Whether there's a maximum/minimum duration, and how out-of-range values
  are rejected (never tested -- avoid testing extremes on a live fuel
  system without supervision).
- Fine timing: request/reply gaps are consistently ~30-65ms (heater
  responding to the panel's poll), useful context if reusing this for new
  timing-sensitive analysis.
- Whether the 18-byte `type0f` frame carries any of the extended frame's
  fields (voltage, revolutions, fuel pump frequency, fan current, board/
  liquid/overheat temp) at one of its currently-undocumented offsets
  (`[5]`, `[8]`, `[10]`, `[13]`, `[14]`, `[15]`, `[17]`). The vendor `.pfl`
  profile only defines formulas for the extended frame above, not this one
  -- filling these in needs a fresh real capture of actual panel<->heater
  traffic (the app's own raw traffic capture log, see its DOCS.md),
  diffed the same way, ideally with the panel's own display readings noted
  alongside for cross-checking. It's equally possible the panel simply
  isn't sent this data at all (a simple LCD may not need voltage/fan-
  current), not that it's hiding undecoded.
