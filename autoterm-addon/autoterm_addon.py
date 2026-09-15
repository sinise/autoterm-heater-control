#!/usr/bin/env python3
"""
Autoterm heater <-> Home Assistant bridge (Supervisor add-on).

Owns both UART ports directly: a transparent passthrough relay in each
direction, command injection impersonating the panel (Start preheat/
thermostat, Stop, Start pump), software Auto thermostat and Prevent
freezing, and MQTT + Home Assistant MQTT discovery for all of it.

Plus:

  - Optional periodic "PUBR0" handshake toward the heater (the vendor
    diagnostic tool's own literal command), which unlocks a much richer
    58-byte extended telemetry frame (dev02, type01). See docs/PROTOCOL.md,
    "Extended diagnostic-mode telemetry" -- field formulas below are the
    vendor's own (read from its plaintext .pfl profile), cross-checked
    against a real capture, NOT guessed. Off by default -- see DOCS.md
    before enabling it.
  - A toggleable raw traffic capture: every parsed frame and every stray
    (unparsed) byte, tagged with who sent it (display, heater, or this
    add-on itself), written to a human-readable log under /config so it's
    reachable from outside the add-on (Samba / File editor / SSH) without
    needing a dashboard of its own.
  - A Bypass switch that suspends all command injection (passive relay
    only), for capturing a clean baseline uninfluenced by anything this
    add-on sends.
  - Sensors for every known extended-frame field, plus the base sensors.
  - A selectable heater profile: extended-telemetry field formulas across
    19 vendor models (only the Autoterm 5D/Flow 5 family is confirmed
    against real hardware -- see docs/PROTOCOL.md and DOCS.md).

Device roles, frame layout, and confirmed commands: dev03 = panel
(originates start/stop, reports cabin temp), dev04 = heater (rich 18-byte
status frame). Commands are injected as dev03 (panel) out the port wired to
the heater. See docs/PROTOCOL.md in the main repo.
"""

import glob
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import paho.mqtt.client as mqtt
import serial

from autoterm_protocol import Framer, KNOWN_DEV, crc_bytes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("autoterm")

NODE_ID = "autoterm_heater"
DISCOVERY_PREFIX = "homeassistant"
STATE_TOPIC = f"autoterm/{NODE_ID}/state"
AVAILABILITY_TOPIC = f"autoterm/{NODE_ID}/availability"
CMD_PREFIX = f"autoterm/{NODE_ID}/cmd"
STATE_FILE = "/data/autoterm_state.json"
# The physical panel's own type=0f (status)/type=11 (cabin temp) query cadence
# isn't a steady ~2s -- confirmed against a real capture, it regularly takes a
# ~15-17s detour into other query types (seen as type=06/type=04 exchanges)
# during which it sends neither, unrelated to this add-on's own debug
# handshake timing. 5.0 was tighter than that native gap, so "Telemetry
# stale" was flipping on every such detour even with nothing actually wrong.
STALE_AFTER = 25.0
EXT_STALE_AFTER = 5.0
EXTERNAL_TEMP_POLL_INTERVAL = 15.0
# Tolerates a few missed/slow polls of Home Assistant's own API before
# falling back to the panel's own cabin sensor -- deliberately looser than
# STALE_AFTER since this is a whole extra hop (HA core, not just the UART
# bus) with its own transient-failure modes.
EXTERNAL_TEMP_STALE_AFTER = 90.0

STATE_NAMES = {
    0x00: "idle",
    0x02: "running",
    0x03: "late-run",
    0x04: "cooldown",
    0x05: "final-shutdown",
}
# For the MQTT discovery "enum" device class, which requires every possible
# value spelled out up front -- an undocumented state byte would just show
# up as "unknown" in HA (dropped from Prometheus export too), not a crash.
STATE_NAME_OPTIONS = list(STATE_NAMES.values())

DEVICE_INFO = {
    "identifiers": [NODE_ID],
    "name": "Autoterm Heater",
    "manufacturer": "Autoterm",
    "model": "Diesel heater",
}

# --------------------------------------------------------------------------
# Extended telemetry (dev02, type01, N-byte payload) -- vendor-defined field
# formulas, one set per heater model ("profile"), read from the vendor's
# own Profiles/*.pfl files + language.res (plaintext data files, not a
# decompile of the tool itself). Only "autoterm_flow_5" has been checked
# against a real capture -- see docs/PROTOCOL.md, "Extended diagnostic-mode
# telemetry". Every other profile here is taken straight from the vendor
# tool's own data and has NEVER been validated against real hardware: byte
# offsets, field names, and even whether the frame is the same size, the
# PUBR0 handshake works the same way, or the extended mode exists at all
# for that model are all unconfirmed. HEATER_PROFILES["<slug>"]["tested"]
# reflects this -- check it (and log a warning) before trusting one.
# --------------------------------------------------------------------------

# Auto-generated from the vendor diagnostic tool's own Profiles/*.pfl files
# (plaintext, not a decompile) + language.res, the same way AUTOTERM_FLOW_5's
# fields were originally derived and cross-checked against a real capture.
# ONLY 'autoterm_flow_5' has been checked against real hardware -- every other
# entry is read straight from the vendor tool's own data and UNTESTED. See
# docs/PROTOCOL.md and this add-on's DOCS.md before trusting one of these.
HEATER_PROFILES = {
    "14tc_10_molex": {
        "label": "14TC-10 MOLEX",
        "internal": "4TC-10 MOLEX",
        "tested": False,
        "state_mult": 10,
        "state_names": ["unknown", "waiting for a command", "unknown", "cooling the flame sensor", "unknown", "shutting down", "unknown", "unknown", "unknown", "unknown", "preparation for ignition", "unknown", "waiting for temperature reduction", "cooling", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "Ignition 1", "blowing", "Ignition 2", "combustion chamber heating", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "blowing", "unknown", "blowing", "blowing", "shutting down", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "blowing", "unknown", "unknown", "low", "middle", "High", "blowing", "waiting", "pump only"],
        "slots": {
            "flame_temp": "a19*256+a20",
            "liquid_temp": "a21",
            "overheat_temp": "a22",
            "board_temp": "a23",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a9",
            "measured_rev": "a10",
            "glow_plug": "a18",
            "fuel_pump_hz": "a13*256+a14/100",
            "voltage": "(a24*256+a25)/10",
            "fault_code": "a44",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            3: "Overheat",
            4: "Liquid temperature sensor",
            5: "Flame temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            14: "Faulty water pump",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            22: "Faulty fuel pump",
            24: "Temperature sensor off-scale",
            25: "Temperature growing too fast",
            26: "Fan overloaded",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame breaks too often",
            30: "No connection",
            37: "Overheat locking",
            78: "Flame break during running",
        },
    },
    "autoterm_air_2d": {
        "label": "AUTOTERM AIR 2D",
        "internal": "PLANAR-2MK",
        "tested": False,
        "state_mult": 13,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "working", "unknown", "unknown", "unknown", "blowdown before ventilation mode", "ventilation", "cooling the flame sensor", "glow plug warming up", "glow plug warming up", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a19*256+a20)-273",
            "liquid_temp": "a25",
            "board_temp": "a26",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a22>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a27*256+a28)/10",
            "fault_code": "a53",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            5: "Faulty temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame breaks too often",
            30: "No connection",
            78: "Flame break",
        },
    },
    "autoterm_air_4d": {
        "label": "AUTOTERM AIR 4D",
        "internal": "PLANAR-44MK",
        "tested": False,
        "state_mult": 12,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "cooling the flame sensor", "unknown", "unknown", "unknown", "unknown", "unknown", "working", "blowdown before ventilation mode", "ventilation", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a21*256+a22)-273",
            "liquid_temp": "a25",
            "board_temp": "a26",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a22>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a27*256+a28)/10",
            "fault_code": "a53",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            5: "Faulty temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame breaks too often",
            30: "No connection",
            78: "Flame break",
        },
    },
    "autoterm_air_8d": {
        "label": "AUTOTERM AIR 8D",
        "internal": "PLANAR-8D",
        "tested": False,
        "state_mult": 12,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unlocking;Ðóññêèé", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "cooling the flame sensor", "combustion chamber heating", "unknown", "unknown", "unknown", "unknown", "working", "blowdown before ventilation mode", "ventilation", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "blowing", "shutting down", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a23*256+a24)-273",
            "liquid_temp": "a29",
            "overheat_temp": "(a31*256+a32)-273",
            "board_temp": "a30",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a20>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a33*256+a34)/10",
            "fault_code": "a64",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            4: "Board temperature sensor",
            5: "Flame temperature sensor",
            8: "Flame break during running",
            9: "Malfunction of a glow plug",
            10: "Faulty fan",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            30: "Flame break during running",
            31: "Overheat",
            32: "Faulty temperature sensor",
            33: "Overheat locking",
        },
    },
    "autoterm_air_9d": {
        "label": "AUTOTERM AIR 9D",
        "internal": "PLANAR-9D",
        "tested": False,
        "state_mult": 12,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "cooling the flame sensor", "unknown", "unknown", "unknown", "unknown", "unknown", "working", "blowdown before ventilation mode", "ventilation", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a21*256+a22)-273",
            "liquid_temp": "a23",
            "overheat_temp": "(a26*256+a27)-273",
            "board_temp": "a24",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a18>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a28*256+a29)/10",
            "fault_code": "a58",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            4: "Board temperature sensor",
            5: "Flame temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame break",
            30: "No connection",
            37: "Overheat locking",
            78: "Flame break",
        },
    },
    "autoterm_flow_5": {
        "label": "AUTOTERM FLOW 5",
        "internal": "BINAR-5S",
        "tested": True,
        "state_mult": 10,
        "state_names": ["unknown", "waiting for a command", "cooling the flame sensor", "air blowing", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "waiting for temperature reduction", "locked", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "blowing", "combustion chamber heating", "blowing", "unknown", "unknown", "low", "unknown", "High", "unknown", "blowing", "waiting", "blowing", "pump only", "middle", "unknown", "blowing", "blowing", "blowing", "shutting down"],
        "slots": {
            "flame_temp": "a18*256+a19-273",
            "liquid_temp": "a20",
            "overheat_temp": "a21",
            "board_temp": "a22",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a14>0",
            "fuel_pump_hz": "a16/10",
            "voltage": "(a23*256+a24)/10",
            "fault_code": "a37",
            "engine_state": "a52",
            "relay_state": "a53",
            "fan_current": "a55*256+a56",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            3: "Overheat",
            4: "Liquid temperature sensor",
            5: "Flame temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            14: "Faulty water pump",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            22: "Faulty fuel pump",
            24: "Temperature sensor off-scale",
            25: "Temperature growing too fast",
            26: "Fan overloaded",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame breaks too often",
            30: "No connection",
            37: "Overheat locking",
            78: "Flame break during running",
        },
    },
    "binar_5s_next": {
        "label": "BINAR-5S-NEXT",
        "internal": "BINAR-5S",
        # Same internal codename as autoterm_flow_5 (confirmed from the
        # vendor's own .pfl files, not a guess) -- byte-for-byte identical
        # slots/state_names/faults to the profile actually tested against
        # real hardware. Marked tested on that basis.
        "tested": True,
        "state_mult": 10,
        "state_names": ["unknown", "waiting for a command", "cooling the flame sensor", "air blowing", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "waiting for temperature reduction", "locked", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "blowing", "combustion chamber heating", "blowing", "unknown", "unknown", "low", "unknown", "High", "unknown", "blowing", "waiting", "blowing", "pump only", "middle", "unknown", "blowing", "blowing", "blowing", "shutting down"],
        "slots": {
            "flame_temp": "a18*256+a19-273",
            "liquid_temp": "a20",
            "overheat_temp": "a21",
            "board_temp": "a22",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a14>0",
            "fuel_pump_hz": "a16/10",
            "voltage": "(a23*256+a24)/10",
            "fault_code": "a37",
            "engine_state": "a52",
            "relay_state": "a53",
            "fan_current": "a55*256+a56",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            3: "Overheat",
            4: "Liquid temperature sensor",
            5: "Flame temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            14: "Faulty water pump",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            22: "Faulty fuel pump",
            24: "Temperature sensor off-scale",
            25: "Temperature growing too fast",
            26: "Fan overloaded",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame breaks too often",
            30: "No connection",
            37: "Overheat locking",
            78: "Flame break during running",
        },
    },
    "binar_5s": {
        "label": "BINAR-5S",
        "internal": "BINAR-5S",
        # Same note as binar_5s_next above.
        "tested": True,
        "state_mult": 10,
        "state_names": ["unknown", "waiting for a command", "cooling the flame sensor", "air blowing", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "waiting for temperature reduction", "locked", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "blowing", "combustion chamber heating", "blowing", "unknown", "unknown", "low", "unknown", "High", "unknown", "blowing", "waiting", "blowing", "pump only", "middle", "unknown", "blowing", "blowing", "blowing", "shutting down"],
        "slots": {
            "flame_temp": "a18*256+a19-273",
            "liquid_temp": "a20",
            "overheat_temp": "a21",
            "board_temp": "a22",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a14>0",
            "fuel_pump_hz": "a16/10",
            "voltage": "(a23*256+a24)/10",
            "fault_code": "a37",
            "engine_state": "a52",
            "relay_state": "a53",
            "fan_current": "a55*256+a56",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            3: "Overheat",
            4: "Liquid temperature sensor",
            5: "Flame temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            14: "Faulty water pump",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            22: "Faulty fuel pump",
            24: "Temperature sensor off-scale",
            25: "Temperature growing too fast",
            26: "Fan overloaded",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame breaks too often",
            30: "No connection",
            37: "Overheat locking",
            78: "Flame break during running",
        },
    },
    "planar_2_with_flame_sensor": {
        "label": "PLANAR-2 with flame sensor",
        "internal": "PLANAR-2 with flame sensor",
        "tested": False,
        "state_mult": 12,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "cooling the flame sensor", "unknown", "unknown", "unknown", "unknown", "unknown", "working", "blowdown before ventilation mode", "ventilation", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "a23*256+a24",
            "liquid_temp": "a26",
            "board_temp": "a27",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a20>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a28*256+a29)/10",
            "fault_code": "a52",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            4: "Board temperature sensor",
            5: "Flame temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame break",
            30: "No connection",
            37: "Overheat locking",
            78: "Flame break",
        },
    },
    "planar_2d": {
        "label": "PLANAR-2D",
        "internal": "PLANAR-2D",
        "tested": False,
        "state_mult": 13,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "working", "unknown", "unknown", "unknown", "blowdown before ventilation mode", "ventilation", "cooling the flame sensor", "glow plug warming up", "glow plug warming up", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a23*256+a24)-273",
            "liquid_temp": "a26",
            "board_temp": "a27",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a20>0",
            "fuel_pump_hz": "15625/(65536-(a14*256+a15))",
            "voltage": "(a28*256+a29)/10",
            "fault_code": "a50",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            5: "Faulty temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame breaks too often",
            30: "No connection",
            78: "Flame break",
        },
    },
    "planar_2mk": {
        "label": "PLANAR-2MK",
        "internal": "PLANAR-2MK",
        "tested": False,
        "state_mult": 13,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "working", "unknown", "unknown", "unknown", "blowdown before ventilation mode", "ventilation", "cooling the flame sensor", "glow plug warming up", "glow plug warming up", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a19*256+a20)-273",
            "liquid_temp": "a25",
            "board_temp": "a26",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a22>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a27*256+a28)/10",
            "fault_code": "a53",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            5: "Faulty temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame breaks too often",
            30: "No connection",
            78: "Flame break",
        },
    },
    "planar_44d_s_p": {
        "label": "PLANAR-44D-S-P",
        "internal": "PLANAR-44D-SP",
        "tested": False,
        "state_mult": 12,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "cooling the flame sensor", "unknown", "unknown", "unknown", "unknown", "unknown", "working", "blowdown before ventilation mode", "ventilation", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a23*256+a24)-273",
            "liquid_temp": "a26",
            "board_temp": "a27",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a20>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a28*256+a29)/10",
            "fault_code": "a52",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            4: "Board temperature sensor",
            5: "Flame temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame break",
            30: "No connection",
            37: "Overheat locking",
            78: "Flame break",
        },
    },
    "planar_44mk": {
        "label": "PLANAR-44MK",
        "internal": "PLANAR-44MK",
        "tested": False,
        "state_mult": 12,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "cooling the flame sensor", "unknown", "unknown", "unknown", "unknown", "unknown", "working", "blowdown before ventilation mode", "ventilation", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a21*256+a22)-273",
            "liquid_temp": "a25",
            "board_temp": "a26",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a22>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a27*256+a28)/10",
            "fault_code": "a53",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            5: "Faulty temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame breaks too often",
            30: "No connection",
            78: "Flame break",
        },
    },
    "planar_4d_s_p": {
        "label": "PLANAR-4D-S-P",
        "internal": "PLANAR-4D",
        "tested": False,
        "state_mult": 10,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "cooling the flame sensor", "blowing", "unknown", "unknown", "working", "blowdown before ventilation mode", "ventilation", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a23*256+a24)-273",
            "liquid_temp": "a26",
            "board_temp": "a27",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a20>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a28*256+a29)/10",
            "fault_code": "a52",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            4: "Overheat",
            5: "Flame temperature sensor",
            8: "Flame break during running",
            9: "Malfunction of a glow plug",
            10: "Faulty fan",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
        },
    },
    "planar_4d": {
        "label": "PLANAR-4D",
        "internal": "PLANAR-4D",
        "tested": False,
        "state_mult": 10,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "cooling the flame sensor", "blowing", "unknown", "unknown", "working", "blowdown before ventilation mode", "ventilation", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a23*256+a24)-273",
            "liquid_temp": "a26",
            "board_temp": "a27",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a20>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a28*256+a29)/10",
            "fault_code": "a52",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            4: "Overheat",
            5: "Flame temperature sensor",
            8: "Flame break during running",
            9: "Malfunction of a glow plug",
            10: "Faulty fan",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
        },
    },
    "planar_8d_s_p": {
        "label": "PLANAR-8D-S-P",
        "internal": "PLANAR-8D",
        "tested": False,
        "state_mult": 12,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unlocking;Ðóññêèé", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "cooling the flame sensor", "combustion chamber heating", "unknown", "unknown", "unknown", "unknown", "working", "blowdown before ventilation mode", "ventilation", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "blowing", "shutting down", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a23*256+a24)-273",
            "liquid_temp": "a29",
            "overheat_temp": "(a31*256+a32)-273",
            "board_temp": "a30",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a20>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a33*256+a34)/10",
            "fault_code": "a64",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            4: "Board temperature sensor",
            5: "Flame temperature sensor",
            8: "Flame break during running",
            9: "Malfunction of a glow plug",
            10: "Faulty fan",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            30: "Flame break during running",
            31: "Overheat",
            32: "Faulty temperature sensor",
            33: "Overheat locking",
        },
    },
    "planar_9d": {
        "label": "PLANAR-9D",
        "internal": "PLANAR-9D",
        "tested": False,
        "state_mult": 12,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "cooling the flame sensor", "unknown", "unknown", "unknown", "unknown", "unknown", "working", "blowdown before ventilation mode", "ventilation", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a21*256+a22)-273",
            "liquid_temp": "a23",
            "overheat_temp": "(a26*256+a27)-273",
            "board_temp": "a24",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a18>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a28*256+a29)/10",
            "fault_code": "a58",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            4: "Board temperature sensor",
            5: "Flame temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame break",
            30: "No connection",
            37: "Overheat locking",
            78: "Flame break",
        },
    },
    "sputnik_2": {
        "label": "SPUTNIK-2",
        "internal": "SPUTNIK-2",
        "tested": False,
        "state_mult": 13,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "working", "unknown", "unknown", "unknown", "blowdown before ventilation mode", "ventilation", "cooling the flame sensor", "glow plug warming up", "glow plug warming up", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "(a19*256+a20)-273",
            "liquid_temp": "a25",
            "board_temp": "a26",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a22>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a27*256+a28)/10",
            "fault_code": "a53",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            5: "Faulty temperature sensor",
            6: "Board temperature sensor",
            9: "Malfunction of a glow plug",
            10: "Turnover mismatch",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
            29: "Flame breaks too often",
            30: "No connection",
            78: "Flame break",
        },
    },
    "sputnik_3": {
        "label": "SPUTNIK-3",
        "internal": "Sputnik-3",
        "tested": False,
        "state_mult": 10,
        "state_names": ["unknown", "waiting for a command", "air blowing", "cooling the flame sensor", "fuel pumping", "unknown", "unknown", "unknown", "unknown", "unknown", "cooling the flame sensor", "ventilation", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "glow plug warming up", "preparation for ignition", "Ignition 1", "Ignition 2", "combustion chamber heating", "blowing", "cooling the flame sensor", "blowing", "unknown", "unknown", "working", "blowdown before ventilation mode", "ventilation", "blowing", "unknown", "unknown", "unknown", "unknown", "unknown", "unknown", "shutting down", "blowing", "blowing", "blowing"],
        "slots": {
            "flame_temp": "a23*256+a24",
            "liquid_temp": "a26",
            "board_temp": "a27",
            "running_time_s": "a3*65536+a4*256+a5",
            "defined_rev": "a12",
            "measured_rev": "a13",
            "glow_plug": "a20>0",
            "fuel_pump_hz": "(a14*256+a15)/100",
            "voltage": "(a28*256+a29)/10",
            "fault_code": "a52",
        },
        "faults": {
            0: "No faults",
            1: "Overheat",
            2: "Possible overheat",
            4: "Overheat",
            5: "Flame temperature sensor",
            8: "Flame break during running",
            9: "Malfunction of a glow plug",
            10: "Faulty fan",
            12: "Increased supply voltage",
            13: "No ignition",
            15: "Low voltage",
            16: "Blowing time exceeded",
            17: "Faulty fuel pump",
            20: "No connection",
            27: "Fan. No rotation",
            28: "Fan. Autorotation",
        },
    },
}


import re as _re

_A_REF = _re.compile(r"a(\d+)")


def _compile_formula(formula):
    """Vendor formula string (e.g. "a18*256+a19-273") -> compiled Python
    expression indexing a 0-based payload list (aN -> p[N-1]). Formulas
    come only from HEATER_PROFILES above (our own extraction), never from
    live/network input, so compiling and eval()'ing them is safe -- this
    isn't arbitrary user input."""
    py_expr = _A_REF.sub(lambda m: f"p[{int(m.group(1)) - 1}]", formula)
    return compile(py_expr, "<heater-profile-formula>", "eval")


# slot key (as extracted/mapped) -> the stable JSON/entity field name this
# add-on has always used for that concept (kept fixed across all profiles
# so existing entities/dashboards don't change when switching profiles --
# a profile that lacks a given slot just leaves that key absent).
SLOT_TO_KEY = {
    "flame_temp": "ext_flame_temp_c",
    "liquid_temp": "ext_liquid_temp_c",
    "overheat_temp": "ext_overheat_temp_c",
    "board_temp": "ext_board_temp_c",
    "running_time_s": "ext_running_time_s",
    "defined_rev": "ext_defined_rev",
    "measured_rev": "ext_measured_rev",
    "glow_plug": "ext_glow_plug",
    "fuel_pump_hz": "ext_fuel_pump_hz",
    "voltage": "ext_voltage",
    "fault_code": "ext_fault_code",
    "engine_state": "ext_engine_state",
    "relay_state": "ext_relay_state",
    "fan_current": "ext_fan_current_ma",
}

DEFAULT_HEATER_PROFILE = "autoterm_flow_5"


def prepare_profile(slug):
    """Look up a profile by slug and precompile its formulas once, so
    decode_extended_payload() doesn't re-parse formula strings per frame."""
    profile = dict(HEATER_PROFILES[slug])
    profile["slug"] = slug
    profile["_compiled_slots"] = {k: _compile_formula(v) for k, v in profile["slots"].items()}
    return profile


def profile_mode_name(profile, state, substate):
    mult = profile["state_mult"]
    names = profile["state_names"]
    if mult is None:
        return f"unknown({state}.{substate})"
    idx = state * mult + substate
    if 0 <= idx < len(names):
        return names[idx]
    return f"unknown({state}.{substate})"


def profile_mode_name_options(profile):
    if not profile["state_names"]:
        return ["unknown"]
    return sorted(set(profile["state_names"]))


def profile_fault_name_options(profile):
    if not profile["faults"]:
        return ["unknown"]
    return sorted(set(profile["faults"].values()))


def decode_extended_payload(payload, profile):
    """dev02, type01 payload -> named fields, using the given (precompiled,
    see prepare_profile()) heater profile's field formulas. A profile that
    doesn't define a given slot, or whose formula indexes past the end of
    this particular payload, just omits that key rather than failing the
    whole decode -- untested profiles may have a different real frame
    length than what their formulas assume."""
    if len(payload) < 2:
        return {}
    p = payload
    state, substate = p[0], p[1]
    result = {
        "ext_state_raw": state,
        "ext_substate_raw": substate,
        "ext_mode_code": (state * profile["state_mult"] + substate) if profile["state_mult"] else None,
        "ext_mode_name": profile_mode_name(profile, state, substate),
    }
    for slotkey, code in profile["_compiled_slots"].items():
        key = SLOT_TO_KEY[slotkey]
        try:
            result[key] = eval(code, {"__builtins__": {}}, {"p": p})
        except Exception:
            continue
    if "ext_glow_plug" in result:
        result["ext_glow_plug"] = bool(result["ext_glow_plug"])
    for key in ("ext_fuel_pump_hz", "ext_voltage"):
        if key in result:
            result[key] = round(result[key], 1)
    if "ext_fuel_pump_hz" in result:
        # Not vendor data like the slots above -- our own assumption
        # (requested as such, not derived from any confirmed spec): output
        # scales linearly with fuel pump frequency, 4.2Hz = 100%. Clamped
        # to 0-100 since the raw frequency can read slightly outside that
        # band during ignition/ramp transients.
        pct = result["ext_fuel_pump_hz"] / 4.2 * 100
        result["ext_output_pct"] = round(max(0.0, min(100.0, pct)), 0)
    if "ext_fault_code" in result:
        fault = int(result["ext_fault_code"])
        result["ext_fault_code"] = fault
        result["ext_fault_name"] = profile["faults"].get(fault, f"unknown({fault})")
    return result


def build_frame(dev, type_, payload=b""):
    raw = bytes([0xAA, dev]) + len(payload).to_bytes(2, "little") + bytes([type_]) + payload
    return raw + crc_bytes(raw)


def build_pubr0_frame():
    """The vendor diagnostic tool's literal handshake that unlocks the
    extended telemetry frame. Not dev/len/type/payload framing like the
    rest of the protocol -- a fixed 13-byte body ("PUBR0" + padding) plus
    the same CRC-16/MODBUS used everywhere else, but transmitted
    **least-significant-byte first** -- the reverse of every other frame in
    this protocol (crc_bytes() returns MSB-first). Confirmed against the
    real captured handshake, which ended `0f b0`, not `b0 0f`. See
    docs/PROTOCOL.md."""
    body = bytes([0xAA]) + b"PUBR0" + bytes([0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF, 0xFF])
    crc = crc_bytes(body)
    return body + bytes([crc[1], crc[0]])


def is_extended_telemetry_frame(raw):
    """dev02, type01, 58-byte payload -- the frame unlocked by PUBR0. The
    physical panel was never designed to receive this and gets visibly
    confused by it (observed directly on real hardware -- see
    docs/PROTOCOL.md), so it's filtered out of the HEATER->PANEL relay
    direction rather than forwarded like everything else."""
    return len(raw) > 4 and raw[1] == 0x02 and raw[4] == 0x01 and len(raw) - 7 == 58


def is_injection_ack_candidate(raw):
    """A short ack-class frame from the heater (dev00 or dev02, empty
    payload) -- the shape seen both for the heater's ack to the display's
    own cabin-temp report and, going by timing alone, its ack to a command
    this add-on just injected. Injected frames use the same dev03 sender
    identity as the real panel, so the heater can't tell the two apart
    either -- there's no field to match on, only timing (see
    StatusModel.arm_reply_suppression()). Only treated as "ours" within a
    short window right after Commander sends something, not
    unconditionally, to limit the chance of swallowing a genuine
    display<->heater exchange that happens to land in that window."""
    return len(raw) > 4 and raw[1] in (0x00, 0x02) and len(raw) - 7 == 0


def heater_to_panel_should_filter(model):
    """Whether the HEATER->PANEL relay needs to parse-before-forward right
    now: either debug mode is on (the extended telemetry frame can appear
    at any moment and must never reach the panel), or an injected command's
    ack is still pending (see StatusModel.arm_reply_suppression()). Off
    otherwise, so ordinary traffic gets true immediate passthrough."""
    debug_enabled, _ = model.get_debug_settings()
    return debug_enabled or model.should_suppress_reply()


def make_heater_to_panel_filter(model):
    """Per-frame drop decision for the HEATER->PANEL relay's filtering
    path: always strip the extended telemetry frame, and additionally
    swallow the one pending injection ack, if any (clearing the gate as
    soon as it's used so nothing past that first match is affected)."""
    def filter_fn(raw):
        if is_extended_telemetry_frame(raw):
            return True
        if model.should_suppress_reply() and is_injection_ack_candidate(raw):
            model.consume_reply_suppression()
            return True
        return False
    return filter_fn


def decode_status_payload(payload):
    """Heater's (dev04) 18-byte type0f payload -> named fields."""
    if len(payload) < 18:
        return {}
    return {
        "state_raw": payload[0],
        "state": STATE_NAMES.get(payload[0], f"unknown(0x{payload[0]:02x})"),
        "substate": payload[1],
        "fault": payload[2],
        "coolant_temp": (payload[3] + payload[4]) / 2,
        "elapsed_min": payload[9],
        "elapsed_sec": payload[11],
        "burner_active": payload[12] == 0xFF,
    }


def _iso(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def _iso_dt(ts):
    """Full ISO-8601 with UTC offset -- what HA's `timestamp` device_class
    requires, unlike the HH:MM:SS.mmm _iso() above (log display only)."""
    return datetime.fromtimestamp(ts).astimezone().isoformat()


# --------------------------------------------------------------------------
# Port autodiscovery (identical to the base add-on -- see its comments)
# --------------------------------------------------------------------------

HEATER_PROBE_ATTEMPTS = 5
HEATER_PROBE_TIMEOUT = 1.0


def list_candidate_ports():
    return sorted(set(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")))


def resolve_by_id(devpath):
    """Best-effort: map a /dev/ttyUSB*//dev/ttyACM* path to its stable
    /dev/serial/by-id/* symlink, tied to the adapter's USB vendor/product/
    serial number rather than plug-order enumeration -- so a saved
    panel_port/heater_port keeps pointing at the right physical adapter
    even after some OTHER USB-serial device is plugged in/unplugged and
    shifts what /dev/ttyUSB<N> everything else gets renumbered to.

    Returns devpath unchanged if no matching by-id symlink is found (a
    cheap adapter chipset with no USB serial number won't get one -- this
    is a real gap, not just a missing feature, since without a serial
    number udev has nothing stable to key on either)."""
    try:
        target = os.path.realpath(devpath)
        for link in glob.glob("/dev/serial/by-id/*"):
            if os.path.realpath(link) == target:
                return link
    except OSError:
        pass
    return devpath


def _discover_panel_port(candidates, baud, timeout):
    listeners = {}
    framers = {}
    for path in candidates:
        try:
            listeners[path] = serial.Serial(path, baud, timeout=0.1)
            framers[path] = Framer()
        except serial.SerialException as e:
            log.warning("discovery: could not open %s: %r", path, e)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for path, ser in listeners.items():
                try:
                    data = ser.read(ser.in_waiting or 1)
                except serial.SerialException:
                    continue
                if not data:
                    continue
                for ev in framers[path].feed(data):
                    if ev[0] == "frame" and ev[2] and ev[1][1] == 0x03:
                        return path
        return None
    finally:
        for ser in listeners.values():
            ser.close()


def _discover_heater_port(candidates, baud):
    poll = build_frame(0x03, 0x0F, b"")
    for path in candidates:
        try:
            ser = serial.Serial(path, baud, timeout=0.1)
        except serial.SerialException as e:
            log.warning("discovery: could not open %s: %r", path, e)
            continue
        try:
            framer = Framer()
            for _ in range(HEATER_PROBE_ATTEMPTS):
                ser.write(poll)
                deadline = time.time() + HEATER_PROBE_TIMEOUT
                while time.time() < deadline:
                    data = ser.read(ser.in_waiting or 1)
                    if not data:
                        continue
                    for ev in framer.feed(data):
                        if ev[0] == "frame" and ev[2] and ev[1][1] == 0x04:
                            return path
        finally:
            ser.close()
    return None


def discover_ports(baud, panel_timeout=8.0):
    """Returns (panel_port, heater_port) or None. Never sends a start/stop
    command -- only the empty type0f status query, which is non-actuating."""
    candidates = list_candidate_ports()
    if len(candidates) < 2:
        log.error("discovery: need at least 2 serial candidates, found %s", candidates)
        return None

    log.info("discovery: listening for the panel on %s (up to %.0fs)", candidates, panel_timeout)
    panel_port = _discover_panel_port(candidates, baud, panel_timeout)
    if panel_port is None:
        log.error(
            "discovery: no dev03 (panel) frames seen on any candidate within %.0fs "
            "-- is the panel powered and actually wired to one of these ports?",
            panel_timeout,
        )
        return None
    log.info("discovery: panel found on %s -- probing remaining ports for the heater", panel_port)

    remaining = [p for p in candidates if p != panel_port]
    heater_port = _discover_heater_port(remaining, baud)
    if heater_port is None:
        log.error("discovery: no dev04 (heater) reply seen on any of %s", remaining)
        return None

    # Resolved to a stable /dev/serial/by-id/* path where possible (falls
    # back to the raw device path if the adapter has no USB serial number
    # for udev to key on) -- what actually gets saved back to config.yaml
    # below, so a future unrelated USB-serial device won't shift these
    # again the way plain /dev/ttyUSB<N> numbering can.
    by_id_panel = resolve_by_id(panel_port)
    by_id_heater = resolve_by_id(heater_port)
    if by_id_panel == panel_port or by_id_heater == heater_port:
        log.warning(
            "discovery: no stable /dev/serial/by-id symlink found for panel=%s and/or "
            "heater=%s (common for adapters with no USB serial number) -- saving the "
            "raw device path instead, which can still shift if another USB-serial "
            "device is plugged in later",
            panel_port, heater_port,
        )
    panel_port, heater_port = by_id_panel, by_id_heater

    log.info("discovery: panel=%s heater=%s", panel_port, heater_port)
    return panel_port, heater_port


def load_persisted():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_persisted(data):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except OSError as e:
        log.warning("could not persist state: %r", e)


# --------------------------------------------------------------------------
# Raw traffic capture -- toggleable, downloadable log of every message and
# who sent it. Written under /config (mapped config:rw in config.yaml) so
# it's reachable via Samba / File editor / SSH without this add-on needing
# a web server of its own.
# --------------------------------------------------------------------------

class CaptureLog:
    def __init__(self, directory, max_mb, name_prefix="capture"):
        self.directory = directory
        self.max_bytes = max_mb * 1024 * 1024
        self.name_prefix = name_prefix
        # Reentrant: note() is called from a logging.Handler, and a couple
        # of code paths (e.g. the size-cap warning in _write()) log from
        # inside an already-held lock -- a plain Lock would deadlock there.
        self.lock = threading.RLock()
        self.fh = None
        self.path = None
        self.bytes_written = 0
        self.enabled = False
        self.capped = False

    def start(self):
        with self.lock:
            if self.fh is not None:
                return
            os.makedirs(self.directory, exist_ok=True)
            name = f"{self.name_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            self.path = os.path.join(self.directory, name)
            self.fh = open(self.path, "a", buffering=1)
            self.bytes_written = 0
            self.capped = False
            self.enabled = True
            self.fh.write(f"# === autoterm debug capture start {datetime.now().isoformat()} ===\n")
            self.fh.write("# columns: time  sender  status  dev  type  len  full-frame-hex\n")
        log.info("capture log started: %s", self.path)

    def stop(self):
        with self.lock:
            self.enabled = False
            if self.fh is not None:
                self.fh.write(f"# === capture end {datetime.now().isoformat()} ===\n")
                self.fh.close()
                self.fh = None
        log.info("capture log stopped")

    def frame(self, ts, sender, raw, crc_ok, note=""):
        with self.lock:
            if not self.enabled or self.fh is None or self.capped:
                return
            dev = raw[1] if len(raw) > 1 else 0
            type_ = raw[4] if len(raw) > 4 else 0
            length = max(0, len(raw) - 7)
            status = "OK " if crc_ok else "BAD"
            suffix = f"  # {note}" if note else ""
            line = (f"{_iso(ts)}  {sender:<11s}  {status}  "
                    f"dev={dev:02x} type={type_:02x} len={length:<3d}  {raw.hex(' ')}{suffix}\n")
            self._write(line)

    def stray(self, ts, sender, data):
        with self.lock:
            if not self.enabled or self.fh is None or self.capped:
                return
            self._write(f"{_iso(ts)}  {sender:<11s}  STRAY {len(data)}B  {data.hex(' ')}\n")

    def raw_send(self, ts, sender, raw, note=""):
        with self.lock:
            if not self.enabled or self.fh is None or self.capped:
                return
            suffix = f"  # {note}" if note else ""
            self._write(f"{_iso(ts)}  {sender:<11s}  SENT      {raw.hex(' ')}{suffix}\n")

    def note(self, ts, level, msg):
        """Mirrors an add-on log message into the capture, interleaved
        chronologically with the traffic -- so one file has both what was
        on the wire and what the add-on itself was doing/seeing."""
        with self.lock:
            if not self.enabled or self.fh is None or self.capped:
                return
            self._write(f"{_iso(ts)}  {'log':<11s}  {level:<9s}{msg}\n")

    def _write(self, line):
        # caller holds self.lock
        self.fh.write(line)
        self.bytes_written += len(line)
        if self.bytes_written > self.max_bytes and not self.capped:
            self.capped = True
            self.fh.write(f"# === capture stopped: reached the {self.max_bytes // (1024*1024)}MB cap ===\n")
            log.warning("capture log %s reached its size cap -- no longer writing "
                        "(toggle it off and back on to start a fresh file)", self.path)

    def status(self, prefix="capture_log"):
        with self.lock:
            return {
                f"{prefix}_enabled": self.enabled,
                f"{prefix}_file": os.path.basename(self.path) if self.path else None,
                f"{prefix}_bytes": self.bytes_written,
            }


class CaptureLogFanout:
    """Looks like a single CaptureLog to Relay/Commander/DebugSender, but
    dispatches every call to a list of real ones -- each independently
    decides whether it's started and actually writes. Used so the same
    traffic can land in both the normal capture log and the separate
    bypass-mode log without either caller needing to know there are two."""

    def __init__(self, logs):
        self.logs = logs

    def frame(self, *args, **kwargs):
        for lg in self.logs:
            lg.frame(*args, **kwargs)

    def stray(self, *args, **kwargs):
        for lg in self.logs:
            lg.stray(*args, **kwargs)

    def raw_send(self, *args, **kwargs):
        for lg in self.logs:
            lg.raw_send(*args, **kwargs)

    def note(self, *args, **kwargs):
        for lg in self.logs:
            lg.note(*args, **kwargs)


class CaptureLogHandler(logging.Handler):
    """Attached to this add-on's own logger so its info/warning/error
    messages land in the same capture file as the wire traffic, in the
    same timeline -- e.g. a serial exception or "capture log reached its
    size cap" shows up right next to the frames around it, instead of
    needing the Supervisor log pulled separately to explain an anomaly."""

    def __init__(self, capture_log):
        super().__init__()
        self.capture_log = capture_log

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        self.capture_log.note(record.created, record.levelname, msg)


class StatusModel:
    def __init__(self, preheat_minutes_default, debug_mode_default, debug_interval_default, capture_log_default,
                 profile, external_temp_entity=None):
        self.lock = threading.Lock()
        self.profile = profile
        self.status = {}
        self.status_ts = None
        self.extended = {}
        self.extended_ts = None
        self.cabin_temp = None
        self.cabin_temp_ts = None
        self.last_frame_ts = None
        self.last_command = None
        self.last_fault = None
        self._suppress_reply_until = None
        self.external_temp_entity = external_temp_entity or None
        self.external_temp = None
        self.external_temp_ts = None
        self.external_temp_raw_state = None
        persisted = load_persisted()
        self.preheat_minutes = persisted.get("preheat_minutes", preheat_minutes_default)
        self.debug_mode = persisted.get("debug_mode", debug_mode_default)
        self.debug_interval = persisted.get("debug_interval", debug_interval_default)
        self.capture_log_wanted = persisted.get("capture_log_enabled", capture_log_default)
        self.bypass_mode = persisted.get("bypass_mode", False)
        self.external_temp_enabled = persisted.get("external_temp_enabled", True)

    def note_frame(self, ts, direction, raw):
        dev = raw[1]
        type_ = raw[4] if len(raw) > 4 else None
        payload = raw[5:-2]
        with self.lock:
            self.last_frame_ts = ts
            if dev == 0x04 and type_ == 0x0F and len(payload) == 18:
                self.status = decode_status_payload(payload)
                self.status_ts = ts
                fault_code = self.status.get("fault")
                if fault_code and (self.last_fault is None or self.last_fault["code"] != fault_code):
                    self.last_fault = {
                        "code": fault_code,
                        "name": self.profile["faults"].get(fault_code, f"unknown({fault_code})"),
                        "ts": ts,
                    }
            elif dev == 0x03 and type_ == 0x11 and len(payload) == 1:
                self.cabin_temp = payload[0]
                self.cabin_temp_ts = ts
            elif dev == 0x02 and type_ == 0x01 and len(payload) >= 2:
                # Not gated on ==58: that's autoterm_flow_5's confirmed
                # length, but an untested profile's real frame length is
                # unknown -- decode_extended_payload() itself skips any
                # field whose formula indexes past the end of payload.
                self.extended = decode_extended_payload(payload, self.profile)
                self.extended_ts = ts
        devname = KNOWN_DEV.get(dev, f"0x{dev:02x}")
        log.debug("%s dev=%s type=%s %s", direction, devname, type_, payload.hex(" "))

    def note_command(self, text):
        with self.lock:
            self.last_command = {"text": text, "ts": time.time()}

    def set_bypass_mode(self, enabled):
        with self.lock:
            self.bypass_mode = bool(enabled)
        persisted = load_persisted()
        persisted["bypass_mode"] = bool(enabled)
        save_persisted(persisted)

    def get_bypass_mode(self):
        with self.lock:
            return self.bypass_mode

    def set_external_temp_enabled(self, enabled):
        with self.lock:
            self.external_temp_enabled = bool(enabled)
        persisted = load_persisted()
        persisted["external_temp_enabled"] = bool(enabled)
        save_persisted(persisted)

    def set_external_temp(self, value, raw_state):
        """Called by ExternalTempPoller after each poll. `value` is a float
        reading or None; `raw_state` is HA's own state string, used to tell
        a definite "unavailable"/"unknown" answer (fall back immediately)
        apart from a failed poll (leave the existing reading in place and
        let it age out via EXTERNAL_TEMP_STALE_AFTER instead -- a single
        blip to Home Assistant's own API shouldn't flip the control source)."""
        with self.lock:
            self.external_temp_raw_state = raw_state
            if value is not None:
                self.external_temp = value
                self.external_temp_ts = time.time()
            elif raw_state in ("unknown", "unavailable"):
                self.external_temp = None
                self.external_temp_ts = None

    def set_preheat_minutes(self, minutes):
        with self.lock:
            self.preheat_minutes = minutes
        persisted = load_persisted()
        persisted["preheat_minutes"] = minutes
        save_persisted(persisted)

    def get_preheat_minutes(self):
        with self.lock:
            return self.preheat_minutes

    def set_debug_mode(self, enabled):
        with self.lock:
            self.debug_mode = bool(enabled)
        persisted = load_persisted()
        persisted["debug_mode"] = bool(enabled)
        save_persisted(persisted)

    def set_debug_interval(self, seconds):
        seconds = max(5, min(int(seconds), 3600))
        with self.lock:
            self.debug_interval = seconds
        persisted = load_persisted()
        persisted["debug_interval"] = seconds
        save_persisted(persisted)

    def get_debug_settings(self):
        with self.lock:
            return self.debug_mode, self.debug_interval

    def set_capture_log_wanted(self, enabled):
        with self.lock:
            self.capture_log_wanted = bool(enabled)
        persisted = load_persisted()
        persisted["capture_log_enabled"] = bool(enabled)
        save_persisted(persisted)

    def get_capture_log_wanted(self):
        with self.lock:
            return self.capture_log_wanted

    def get_last_frame_ts(self):
        with self.lock:
            return self.last_frame_ts

    def arm_reply_suppression(self):
        """Called right after Commander writes an injected frame: the next
        short ack-shaped frame the heater sends (see
        is_injection_ack_candidate) is withheld from the HEATER->PANEL relay
        for SUPPRESS_WINDOW seconds, on the theory that it's the heater's
        ack to what we just sent rather than something the real panel
        needs to see."""
        with self.lock:
            self._suppress_reply_until = time.time() + SUPPRESS_WINDOW

    def should_suppress_reply(self):
        with self.lock:
            if self._suppress_reply_until is None:
                return False
            if time.time() >= self._suppress_reply_until:
                self._suppress_reply_until = None
                return False
            return True

    def consume_reply_suppression(self):
        """Called once a candidate ack frame has actually been withheld --
        clears the window immediately rather than waiting it out, so a
        second, unrelated ack-shaped frame arriving moments later (e.g. the
        display's own type11 report) isn't also swallowed."""
        with self.lock:
            self._suppress_reply_until = None

    def snapshot(self, auto_snapshot=None, pf_snapshot=None, capture_status=None,
                 bypass_log_status=None):
        auto_snapshot = auto_snapshot or {}
        pf_snapshot = pf_snapshot or {}
        capture_status = capture_status or {}
        bypass_log_status = bypass_log_status or {}
        with self.lock:
            now = time.time()
            status_age = None if self.status_ts is None else now - self.status_ts
            cabin_age = None if self.cabin_temp_ts is None else now - self.cabin_temp_ts
            ext_age = None if self.extended_ts is None else now - self.extended_ts
            stale = (
                status_age is None or cabin_age is None
                or status_age > STALE_AFTER or cabin_age > STALE_AFTER
            )
            external_age = None if self.external_temp_ts is None else now - self.external_temp_ts
            using_external = (
                bool(self.external_temp_entity) and self.external_temp_enabled
                and self.external_temp is not None and external_age is not None
                and external_age <= EXTERNAL_TEMP_STALE_AFTER
            )
            effective_temp = self.external_temp if using_external else self.cabin_temp
            effective_age = external_age if using_external else cabin_age
            snap = {
                "cabin_temp": self.cabin_temp,
                "cabin_temp_age": cabin_age,
                "external_temp_entity": self.external_temp_entity,
                "external_temp_enabled": self.external_temp_enabled,
                "external_temp": self.external_temp,
                "external_temp_age": external_age,
                "external_temp_raw_state": self.external_temp_raw_state,
                "external_temp_active": using_external,
                "effective_temp": effective_temp,
                "effective_temp_age": effective_age,
                "status_age": status_age,
                "stale": stale,
                "last_command": self.last_command,
                "preheat_minutes": self.preheat_minutes,
                "debug_mode": self.debug_mode,
                "debug_interval": self.debug_interval,
                "extended_active": ext_age is not None and ext_age <= EXT_STALE_AFTER,
                "extended_age": ext_age,
                "heater_profile": (
                    self.profile["label"] if self.profile["tested"]
                    else f"{self.profile['label']} (NOT TESTED)"
                ),
                "bypass_mode": self.bypass_mode,
            }
            snap.update(self.status)
            snap.update(self.extended)
            fault_code = self.status.get("fault")
            snap["fault_active"] = bool(fault_code)
            snap["fault_name"] = (
                self.profile["faults"].get(fault_code, f"unknown({fault_code})")
                if fault_code is not None else None
            )
            if self.last_fault is not None:
                snap["last_fault_code"] = self.last_fault["code"]
                snap["last_fault_name"] = self.last_fault["name"]
                snap["last_fault_time"] = _iso_dt(self.last_fault["ts"])
            else:
                snap["last_fault_code"] = None
                snap["last_fault_name"] = None
                snap["last_fault_time"] = None
            snap["auto_enabled"] = auto_snapshot.get("enabled")
            snap["auto_target"] = auto_snapshot.get("target")
            snap["auto_last_note"] = auto_snapshot.get("last_note")
            snap["prevent_freezing_enabled"] = pf_snapshot.get("enabled")
            snap["prevent_freezing_target"] = pf_snapshot.get("target")
            snap["prevent_freezing_last_note"] = pf_snapshot.get("last_note")
            snap.update(capture_status)
            snap.update(bypass_log_status)
            return snap


class ExternalTempPoller(threading.Thread):
    """Optional: polls one Home Assistant entity's own state via the
    Supervisor's Home Assistant API proxy (requires `homeassistant_api:
    true` in config.yaml, which grants SUPERVISOR_TOKEN access to
    http://supervisor/core/api) so Auto thermostat/Prevent freezing can use
    an external temperature sensor instead of the panel's own cabin-temp
    report. A no-op thread if no entity is configured -- always started
    the same way DebugSender is, rather than conditionally, to keep
    Bridge.run() uniform."""

    def __init__(self, model, entity_id, stop_evt):
        super().__init__(daemon=True, name="external-temp-poller")
        self.model = model
        self.entity_id = entity_id
        self.stop_evt = stop_evt
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def run(self):
        if not self.entity_id:
            return
        url = f"http://supervisor/core/api/states/{self.entity_id}"
        while not self.stop_evt.is_set():
            try:
                req = urllib.request.Request(url, headers=self.headers)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                state = data.get("state")
                try:
                    self.model.set_external_temp(float(state), state)
                except (TypeError, ValueError):
                    # "unknown"/"unavailable"/anything else non-numeric --
                    # a definite answer from HA, not a failed poll.
                    self.model.set_external_temp(None, state)
            except (urllib.error.URLError, OSError, ValueError) as e:
                # Couldn't reach HA at all this round -- leave the existing
                # reading in place; set_external_temp(None, ...) here would
                # discard a still-good value over one transient hiccup.
                log.warning("External temp sensor (%s) poll failed: %r", self.entity_id, e)
            self.stop_evt.wait(EXTERNAL_TEMP_POLL_INTERVAL)


class Relay(threading.Thread):
    """Transparent byte-for-byte passthrough in one direction, plus decoding
    and (if enabled) raw capture logging.

    Bytes are forwarded immediately, before parsing, whenever there's
    nothing that might need withholding -- passthrough latency is then
    whatever the OS/serial layer itself costs, not an extra frame's worth
    of buffering. `should_filter_fn` (if given) is polled once per read
    cycle; only while it returns True does this relay switch to parsing
    each chunk into full frames *before* writing anything (holding up to
    ~1 frame's worth of extra latency), so `frame_filter_fn` can withhold a
    matched frame from dst entirely instead of forwarding it.

    Used for HEATER->PANEL, and deliberately scoped to only the moments
    filtering is actually needed (debug mode streaming the extended frame,
    or a just-injected command's ack still pending) rather than
    unconditionally -- holding every single heater frame for a full parse,
    all the time, was adding real latency to the panel's own display even
    when there was nothing to filter (see docs/PROTOCOL.md)."""

    def __init__(self, name, sender_label, src, dst, dst_lock, model, capture_log, stop_evt,
                 should_filter_fn=None, frame_filter_fn=None):
        super().__init__(daemon=True, name=name)
        self.label = name
        self.sender_label = sender_label
        self.src = src
        self.dst = dst
        self.dst_lock = dst_lock
        self.model = model
        self.capture_log = capture_log
        self.stop_evt = stop_evt
        self.should_filter_fn = should_filter_fn
        self.frame_filter_fn = frame_filter_fn
        self.framer = Framer()
        self.error = None

    def run(self):
        while not self.stop_evt.is_set():
            try:
                data = self.src.read(1)
                if not data:
                    continue
                data += self.src.read(self.src.in_waiting)
            except serial.SerialException as e:
                self.error = str(e)
                log.error("%s read failed: %r", self.label, e)
                self.stop_evt.set()
                return

            ts = time.time()
            filtering = self.should_filter_fn is not None and self.should_filter_fn()

            if not filtering:
                try:
                    with self.dst_lock:
                        self.dst.write(data)
                except serial.SerialException as e:
                    self.error = str(e)
                    log.error("%s write failed: %r", self.label, e)
                    self.stop_evt.set()
                    return

                for ev in self.framer.feed(data):
                    if ev[0] == "stray":
                        self.capture_log.stray(ts, self.sender_label, ev[1])
                        continue
                    raw, crc_ok = ev[1], ev[2]
                    self.capture_log.frame(ts, self.sender_label, raw, crc_ok)
                    if crc_ok:
                        self.model.note_frame(ts, self.label, raw)
                continue

            # Filtering path: must know what a chunk contains before
            # forwarding it, so parse first and rebuild the output from
            # the parsed events (byte-identical to the input except for
            # whatever a matched frame is withheld).
            out = bytearray()
            for ev in self.framer.feed(data):
                if ev[0] == "stray":
                    out += ev[1]
                    self.capture_log.stray(ts, self.sender_label, ev[1])
                    continue
                raw, crc_ok = ev[1], ev[2]
                if crc_ok:
                    self.model.note_frame(ts, self.label, raw)
                drop = crc_ok and self.frame_filter_fn is not None and self.frame_filter_fn(raw)
                if drop:
                    self.capture_log.frame(ts, self.sender_label, raw, crc_ok, note="NOT forwarded (filtered)")
                else:
                    self.capture_log.frame(ts, self.sender_label, raw, crc_ok)
                    out += raw

            if out:
                try:
                    with self.dst_lock:
                        self.dst.write(bytes(out))
                except serial.SerialException as e:
                    self.error = str(e)
                    log.error("%s write failed: %r", self.label, e)
                    self.stop_evt.set()
                    return


# Shared by every code path that injects a frame toward the heater
# (Commander, DebugSender): confirmed on real hardware that a frame landing
# while the panel's own query/reply exchange is mid-flight can corrupt that
# exchange (see DebugSender's docstring -- originally diagnosed for the
# debug handshake specifically, but the mechanism is generic to anything
# written to heater_ser, including AutoThermostat/PreventFreezing/manual
# start-stop commands via Commander, which is why this lives at module
# level instead of duplicated per class).
QUIET_GAP = 0.25
MAX_EXTRA_WAIT = 2.0

# How long the HEATER->PANEL relay withholds the next ack-shaped frame after
# Commander injects something (see StatusModel.arm_reply_suppression() and
# is_injection_ack_candidate()) -- generous relative to the ~0.9s ack delay
# seen in a real capture, without leaving it armed indefinitely.
SUPPRESS_WINDOW = 1.5


def wait_for_quiet_bus(model, stop_evt):
    deadline = time.time() + MAX_EXTRA_WAIT
    while time.time() < deadline and not stop_evt.is_set():
        last = model.get_last_frame_ts()
        if last is None or time.time() - last >= QUIET_GAP:
            return
        time.sleep(0.05)
    # Gave up waiting for a quiet gap -- send anyway rather than delaying
    # indefinitely if the bus is unusually busy.


class Commander:
    """Builds and injects command frames toward the heater, impersonating the panel."""

    def __init__(self, heater_ser, heater_lock, model, capture_log, stop_evt):
        self.heater_ser = heater_ser
        self.heater_lock = heater_lock
        self.model = model
        self.capture_log = capture_log
        self.stop_evt = stop_evt

    def _send(self, dev, type_, payload=b""):
        """Returns True if a frame actually went out, False if bypass mode
        suppressed it -- callers use this to decide whether to record a
        start-mode change or send a follow-up frame."""
        if self.model.get_bypass_mode():
            log.warning(
                "BYPASS: not sending dev=%02x type=%02x payload=%s (bypass mode "
                "enabled -- injection suspended, passthrough only)",
                dev, type_, payload.hex(" "),
            )
            return False
        wait_for_quiet_bus(self.model, self.stop_evt)
        frame = build_frame(dev, type_, payload)
        try:
            with self.heater_lock:
                self.heater_ser.write(frame)
            ts = time.time()
            log.info("INJECT sent %s", frame.hex(" "))
            self.capture_log.raw_send(ts, "rpi", frame)
            self.model.note_command(f"sent {frame.hex(' ')}")
            self.model.arm_reply_suppression()
            return True
        except Exception as e:
            log.error("INJECT failed %s: %r", frame.hex(" "), e)
            self.model.note_command(f"FAILED to send {frame.hex(' ')}: {e!r}")
            raise

    # Sender byte 0x03 = panel/display -- confirmed to be the device that
    # originates every start/stop handshake. We impersonate it here.

    def start_preheat(self, minutes):
        minutes = max(0, min(int(minutes), 600))
        log.info("requested: start preheat %dmin", minutes)
        if not self._send(0x03, 0x01, bytes([0x00, 0x1E])):
            return
        time.sleep(1.5)
        self._send(0x03, 0x02, minutes.to_bytes(2, "big"))

    def start_thermostat(self):
        log.info("requested: start thermostat")
        self._send(0x03, 0x01, bytes([0x00, 0x22]))

    def stop(self):
        log.info("requested: stop")
        self._send(0x03, 0x03)

    def start_pump(self):
        # type 0x21, payload 00 28 -- confirmed against a real capture
        # (see docs/PROTOCOL.md, "New confirmed command: pump-only start").
        # Only this exact payload has been observed; it's sent verbatim
        # rather than parameterized since nothing else is confirmed safe.
        log.info("requested: start pump (ventilation only)")
        self._send(0x03, 0x21, bytes([0x00, 0x28]))


class DebugSender(threading.Thread):
    """Periodically re-sends the vendor diagnostic tool's PUBR0 handshake
    toward the heater, when debug mode is enabled -- unlocks the extended
    telemetry frame decoded by decode_extended_payload() above.

    Confirmed on real hardware (a live capture, reconstructing this add-on's
    own staleness logic against it and matching it second-for-second to
    Home Assistant's own "Telemetry stale" history): sending the handshake
    while the panel's own query/reply exchange is mid-flight on the shared
    heater_port line can corrupt that exchange -- a real 18-byte heater
    reply was seen missing 2 bytes immediately after a handshake send. It
    happened on roughly half of the handshake sends, not all -- consistent
    with a timing collision, not a deterministic effect. To reduce this,
    the handshake is held until the bus has been quiet for QUIET_GAP
    seconds (no frame seen from either device) rather than fired blindly on
    a fixed timer -- see wait_for_quiet_bus() above (shared with Commander,
    which turned out to need the same protection for its own injected
    commands). This narrows the collision window but doesn't formally prove
    it's eliminated; still treat this as experimental (see DOCS.md)."""

    def __init__(self, heater_ser, heater_lock, model, capture_log, stop_evt):
        super().__init__(daemon=True, name="debug-sender")
        self.heater_ser = heater_ser
        self.heater_lock = heater_lock
        self.model = model
        self.capture_log = capture_log
        self.stop_evt = stop_evt

    def send_once(self):
        if self.model.get_bypass_mode():
            log.warning("BYPASS: not sending PUBR0 handshake (bypass mode enabled)")
            return
        frame = build_pubr0_frame()
        try:
            with self.heater_lock:
                self.heater_ser.write(frame)
            ts = time.time()
            log.info("DEBUG sent PUBR0 handshake %s", frame.hex(" "))
            self.capture_log.raw_send(ts, "rpi", frame, note="PUBR0 handshake")
            self.model.note_command(f"sent PUBR0 handshake {frame.hex(' ')}")
        except Exception as e:
            log.error("DEBUG PUBR0 send failed: %r", e)

    def send_when_quiet(self):
        wait_for_quiet_bus(self.model, self.stop_evt)
        self.send_once()

    def run(self):
        while not self.stop_evt.is_set():
            enabled, interval = self.model.get_debug_settings()
            if not enabled:
                if self.stop_evt.wait(1.0):
                    return
                continue
            self.send_when_quiet()
            if self.stop_evt.wait(max(5, interval)):
                return


class AutoThermostat(threading.Thread):
    """Software hysteresis loop: stop at target+1, start (thermostat mode) at
    target-1. Runs independently of manual commands -- only acts when
    enabled, only on live/fresh cabin-temp readings. Rate-limited between its
    own actions to avoid thrashing near a boundary."""

    HYSTERESIS = 1.0
    MIN_ACTION_INTERVAL = 90.0
    MAX_READING_AGE = 10.0
    POLL_INTERVAL = 3.0

    def __init__(self, model, commander, stop_evt, target_default):
        super().__init__(daemon=True, name="auto-thermostat")
        self.model = model
        self.commander = commander
        self.stop_evt = stop_evt
        self.lock = threading.Lock()
        persisted = load_persisted()
        self.enabled = persisted.get("auto_enabled", False)
        self.target = persisted.get("auto_target", target_default)
        self.last_action_ts = 0.0
        self.last_note = None
        # Cooldown after a real stop runs for minutes with state != "idle" the
        # whole time -- without this latch we'd re-send stop every
        # MIN_ACTION_INTERVAL for the entire cooldown. Cleared once idle.
        self.stop_pending = False

    def configure(self, enabled=None, target=None):
        with self.lock:
            if enabled is not None:
                self.enabled = bool(enabled)
            if target is not None:
                self.target = float(target)
            persisted = load_persisted()
            persisted["auto_enabled"] = self.enabled
            persisted["auto_target"] = self.target
        save_persisted(persisted)

    def snapshot(self):
        with self.lock:
            return {"enabled": self.enabled, "target": self.target, "last_note": self.last_note}

    def run(self):
        while not self.stop_evt.wait(self.POLL_INTERVAL):
            with self.lock:
                enabled, target = self.enabled, self.target
            if not enabled:
                continue

            # Checked before the action-rate-limit below, and independent of
            # a fresh cabin-temp reading, so a fault gets a response within
            # one POLL_INTERVAL rather than waiting out MIN_ACTION_INTERVAL
            # or MAX_READING_AGE first.
            snap = self.model.snapshot()
            fault = snap.get("fault")
            if fault:
                note = f"fault {fault} ({snap.get('fault_name')}) -> disabling Auto thermostat"
                log.error("AUTO %s", note)
                self.configure(enabled=False)
                with self.lock:
                    self.last_note = note
                continue

            now = time.time()
            if now - self.last_action_ts < self.MIN_ACTION_INTERVAL:
                continue

            cabin, age = snap["effective_temp"], snap["effective_temp_age"]
            state = snap.get("state")
            if cabin is None or age is None or age > self.MAX_READING_AGE or state is None:
                continue

            if state == "idle":
                self.stop_pending = False

            try:
                if cabin >= target + self.HYSTERESIS and state != "idle" and not self.stop_pending:
                    note = f"cabin {cabin} >= {target + self.HYSTERESIS} -> stop"
                    log.info("AUTO %s", note)
                    self.commander.stop()
                    self.last_action_ts = now
                    self.stop_pending = True
                    with self.lock:
                        self.last_note = note
                elif cabin <= target - self.HYSTERESIS and state == "idle":
                    note = f"cabin {cabin} <= {target - self.HYSTERESIS} -> start thermostat"
                    log.info("AUTO %s", note)
                    self.commander.start_thermostat()
                    self.last_action_ts = now
                    with self.lock:
                        self.last_note = note
            except Exception as e:
                log.error("AUTO action failed: %r", e)


class PreventFreezing(threading.Thread):
    """Independent frost-protection safety net: starts the heater
    (thermostat mode) whenever cabin temperature reaches the configured
    floor, REGARDLESS of the auto-thermostat's own enabled state or a prior
    manual Stop -- the whole point is that it can't be silently defeated by
    turning the normal comfort thermostat off or pressing Stop once.
    Disabling this feature itself is the only way to turn it off.

    Never stops a heater run it didn't start itself (tracked via
    started_by_me), so it doesn't fight the auto-thermostat or a manual
    preheat session that's running for an unrelated reason. Also never
    aborts a run it did start just because it's disabled mid-run --
    disabling relinquishes responsibility for that run rather than cutting
    heat abruptly; something else (manual Stop, auto-thermostat) ends it."""

    HYSTERESIS = 1.0
    MIN_ACTION_INTERVAL = 90.0
    MAX_READING_AGE = 10.0
    POLL_INTERVAL = 3.0

    def __init__(self, model, commander, stop_evt, target_default):
        super().__init__(daemon=True, name="prevent-freezing")
        self.model = model
        self.commander = commander
        self.stop_evt = stop_evt
        self.lock = threading.Lock()
        persisted = load_persisted()
        self.enabled = persisted.get("prevent_freezing_enabled", False)
        self.target = persisted.get("prevent_freezing_target", target_default)
        self.last_action_ts = 0.0
        self.last_note = None
        self.started_by_me = False

    def configure(self, enabled=None, target=None):
        with self.lock:
            if enabled is not None:
                self.enabled = bool(enabled)
            if target is not None:
                self.target = max(0.0, min(float(target), 10.0))
            persisted = load_persisted()
            persisted["prevent_freezing_enabled"] = self.enabled
            persisted["prevent_freezing_target"] = self.target
        save_persisted(persisted)

    def snapshot(self):
        with self.lock:
            return {"enabled": self.enabled, "target": self.target, "last_note": self.last_note}

    def run(self):
        while not self.stop_evt.wait(self.POLL_INTERVAL):
            with self.lock:
                enabled, target = self.enabled, self.target
            if not enabled:
                self.started_by_me = False
                continue

            # See AutoThermostat.run() for why this is checked before the
            # rate-limit/staleness gates below. Disabling frost protection
            # itself on a fault is a deliberate choice, not an oversight:
            # blindly re-igniting into whatever just faulted seemed worse
            # than surfacing it and leaving the heater alone -- flag via
            # "Last fault"/an automation if this needs to escalate instead.
            snap = self.model.snapshot()
            fault = snap.get("fault")
            if fault:
                note = f"fault {fault} ({snap.get('fault_name')}) -> disabling Prevent freezing"
                log.error("PREVENT-FREEZING %s", note)
                self.configure(enabled=False)
                self.started_by_me = False
                with self.lock:
                    self.last_note = note
                continue

            now = time.time()
            if now - self.last_action_ts < self.MIN_ACTION_INTERVAL:
                continue

            cabin, age = snap["effective_temp"], snap["effective_temp_age"]
            state = snap.get("state")
            if cabin is None or age is None or age > self.MAX_READING_AGE or state is None:
                continue

            if state == "idle":
                self.started_by_me = False

            try:
                if cabin <= target and state == "idle":
                    note = f"cabin {cabin} <= {target} (frost floor) -> start thermostat"
                    log.info("PREVENT-FREEZING %s", note)
                    self.commander.start_thermostat()
                    self.last_action_ts = now
                    self.started_by_me = True
                    with self.lock:
                        self.last_note = note
                elif self.started_by_me and cabin >= target + self.HYSTERESIS and state != "idle":
                    note = f"cabin {cabin} >= {target + self.HYSTERESIS} -> stop (frost-protection run ending)"
                    log.info("PREVENT-FREEZING %s", note)
                    self.commander.stop()
                    self.last_action_ts = now
                    self.started_by_me = False
                    with self.lock:
                        self.last_note = note
            except Exception as e:
                log.error("PREVENT-FREEZING action failed: %r", e)


def discovery_configs(profile):
    """(topic, payload) pairs for every entity, published retained on connect."""
    base = {"availability_topic": AVAILABILITY_TOPIC, "device": DEVICE_INFO}

    def blank_to_none(field):
        return f"{{{{ value_json.{field} if value_json.{field} is not none else '' }}}}"

    entries = []

    # -- base sensors/controls ---------------------------------------------

    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/state/config", {
        **base, "name": "State", "unique_id": f"{NODE_ID}_state",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("state"),
        "device_class": "enum", "options": STATE_NAME_OPTIONS,
        "icon": "mdi:radiator",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/state_code/config", {
        # Numeric mirror of "State" -- HA's Prometheus exporter doesn't
        # export enum sensors' text value as a metric at all (confirmed:
        # it exports availability/last-updated/change-count metadata for
        # them, but never the value itself), so this is what Grafana
        # actually graphs, with the name applied there via value mappings.
        **base, "name": "State code", "unique_id": f"{NODE_ID}_state_code",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("state_raw"),
        "icon": "mdi:radiator", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/fault/config", {
        **base, "name": "Fault code", "unique_id": f"{NODE_ID}_fault",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("fault"),
        "icon": "mdi:alert-circle-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/fault_name/config", {
        # Named mirror of "Fault code" using the same table as "Fault
        # (extended, named)" below, but populated from the base 18-byte
        # frame -- available without debug/extended telemetry turned on.
        **base, "name": "Fault", "unique_id": f"{NODE_ID}_fault_name",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("fault_name"),
        "device_class": "enum", "options": profile_fault_name_options(profile),
        "icon": "mdi:alert-circle-outline",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/fault_active/config", {
        **base, "name": "Fault active", "unique_id": f"{NODE_ID}_fault_active",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.fault_active else 'OFF' }}",
        "device_class": "problem",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/last_fault_code/config", {
        # These three persist the most recent fault (code/name/when it
        # started) even after it clears back to 0, so it stays visible
        # instead of vanishing the moment the heater recovers -- see
        # StatusModel.note_frame()/last_fault.
        **base, "name": "Last fault code", "unique_id": f"{NODE_ID}_last_fault_code",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("last_fault_code"),
        "icon": "mdi:alert-circle-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/last_fault_name/config", {
        **base, "name": "Last fault", "unique_id": f"{NODE_ID}_last_fault_name",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("last_fault_name"),
        "device_class": "enum", "options": profile_fault_name_options(profile),
        "icon": "mdi:alert-circle-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/last_fault_time/config", {
        **base, "name": "Last fault time", "unique_id": f"{NODE_ID}_last_fault_time",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("last_fault_time"),
        "device_class": "timestamp", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/cabin_temp/config", {
        **base, "name": "Temperature at display", "unique_id": f"{NODE_ID}_cabin_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("cabin_temp"),
        "device_class": "temperature", "unit_of_measurement": "°C",
        "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/coolant_temp/config", {
        **base, "name": "Coolant temperature", "unique_id": f"{NODE_ID}_coolant_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("coolant_temp"),
        "device_class": "temperature", "unit_of_measurement": "°C",
        "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/external_temp/config", {
        # Whatever this add-on last polled from external_temp_sensor_entity
        # (config.yaml) via Home Assistant's own API -- populated only if
        # that option is set. See "Using external temperature" below for
        # whether it's actually the one driving Auto thermostat/Prevent
        # freezing right now.
        **base, "name": "Temperature external sensor", "unique_id": f"{NODE_ID}_external_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("external_temp"),
        "device_class": "temperature", "unit_of_measurement": "°C",
        "state_class": "measurement", "entity_category": "diagnostic",
    }))
    # -- extended-frame temperatures grouped here with the base ones above,
    #    rather than down with the rest of the extended sensors, so every
    #    temperature reading sits together in the entity list. Populated
    #    only while Extended telemetry active is on (see below).
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_flame_temp/config", {
        **base, "name": "Flame temperature", "unique_id": f"{NODE_ID}_ext_flame_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_flame_temp_c"),
        "device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_liquid_temp/config", {
        **base, "name": "Liquid temperature (extended)", "unique_id": f"{NODE_ID}_ext_liquid_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_liquid_temp_c"),
        "device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_overheat_temp/config", {
        **base, "name": "Overheat sensor temperature", "unique_id": f"{NODE_ID}_ext_overheat_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_overheat_temp_c"),
        "device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_board_temp/config", {
        **base, "name": "Board temperature", "unique_id": f"{NODE_ID}_ext_board_temp",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_board_temp_c"),
        "device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/elapsed_min/config", {
        **base, "name": "Elapsed run time", "unique_id": f"{NODE_ID}_elapsed_min",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("elapsed_min"),
        "unit_of_measurement": "min", "icon": "mdi:timer-outline",
        "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/burner/config", {
        **base, "name": "Burner active", "unique_id": f"{NODE_ID}_burner",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.burner_active else 'OFF' }}",
        "device_class": "heat",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/stale/config", {
        **base, "name": "Telemetry stale", "unique_id": f"{NODE_ID}_stale",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.stale else 'OFF' }}",
        "device_class": "problem", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/external_temp_active/config", {
        # ON only while the polled external sensor is actually the one
        # feeding Auto thermostat/Prevent freezing -- OFF (falling back to
        # the panel's own Cabin temperature) whenever it's unconfigured,
        # switched off, or stale/unavailable for EXTERNAL_TEMP_STALE_AFTER.
        **base, "name": "Using external temperature sensor", "unique_id": f"{NODE_ID}_external_temp_active",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.external_temp_active else 'OFF' }}",
        "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/switch/{NODE_ID}/external_temp_enabled/config", {
        **base, "name": "Use external temperature sensor", "unique_id": f"{NODE_ID}_external_temp_enabled",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.external_temp_enabled else 'OFF' }}",
        "command_topic": f"{CMD_PREFIX}/external_temp_enabled/set", "icon": "mdi:thermometer-lines",
    }))

    entries.append((f"{DISCOVERY_PREFIX}/climate/{NODE_ID}/thermostat/config", {
        **base, "name": "Autoterm thermostat", "unique_id": f"{NODE_ID}_climate",
        "modes": ["off", "heat"],
        "mode_state_topic": STATE_TOPIC,
        "mode_state_template": "{{ 'heat' if value_json.auto_enabled else 'off' }}",
        "mode_command_topic": f"{CMD_PREFIX}/auto_mode/set",
        "temperature_state_topic": STATE_TOPIC,
        "temperature_state_template": blank_to_none("auto_target"),
        "temperature_command_topic": f"{CMD_PREFIX}/auto_target/set",
        "current_temperature_topic": STATE_TOPIC,
        "current_temperature_template": blank_to_none("effective_temp"),
        "action_topic": STATE_TOPIC,
        "action_template": (
            "{{ 'heating' if value_json.burner_active "
            "else ('idle' if value_json.auto_enabled else 'off') }}"
        ),
        "min_temp": 5, "max_temp": 35, "temp_step": 0.5, "temperature_unit": "C",
    }))

    entries.append((f"{DISCOVERY_PREFIX}/number/{NODE_ID}/preheat_minutes/config", {
        **base, "name": "Preheat duration", "unique_id": f"{NODE_ID}_preheat_minutes",
        "command_topic": f"{CMD_PREFIX}/preheat_minutes/set",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("preheat_minutes"),
        "min": 1, "max": 600, "step": 1, "unit_of_measurement": "min", "mode": "box",
        "icon": "mdi:timer-cog-outline",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/button/{NODE_ID}/start_preheat/config", {
        **base, "name": "Start preheat", "unique_id": f"{NODE_ID}_start_preheat",
        "command_topic": f"{CMD_PREFIX}/start_preheat", "icon": "mdi:fire",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/button/{NODE_ID}/start_thermostat/config", {
        **base, "name": "Start thermostat (manual)", "unique_id": f"{NODE_ID}_start_thermostat_manual",
        "command_topic": f"{CMD_PREFIX}/start_thermostat", "icon": "mdi:thermostat",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/button/{NODE_ID}/stop/config", {
        **base, "name": "Stop", "unique_id": f"{NODE_ID}_stop",
        "command_topic": f"{CMD_PREFIX}/stop",
        "icon": "mdi:stop-circle-outline",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/button/{NODE_ID}/start_pump/config", {
        **base, "name": "Start pump (ventilation only)", "unique_id": f"{NODE_ID}_start_pump",
        "command_topic": f"{CMD_PREFIX}/start_pump", "icon": "mdi:fan",
    }))

    entries.append((f"{DISCOVERY_PREFIX}/switch/{NODE_ID}/prevent_freezing/config", {
        **base, "name": "Prevent freezing", "unique_id": f"{NODE_ID}_prevent_freezing",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.prevent_freezing_enabled else 'OFF' }}",
        "command_topic": f"{CMD_PREFIX}/prevent_freezing/set", "icon": "mdi:snowflake-alert",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/number/{NODE_ID}/prevent_freezing_target/config", {
        **base, "name": "Prevent freezing target", "unique_id": f"{NODE_ID}_prevent_freezing_target",
        "command_topic": f"{CMD_PREFIX}/prevent_freezing_target/set",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("prevent_freezing_target"),
        "min": 0, "max": 10, "step": 0.5, "unit_of_measurement": "°C", "mode": "box",
        "icon": "mdi:thermometer-low",
    }))
    # -- debug controls -----------------------------------------------------

    entries.append((f"{DISCOVERY_PREFIX}/switch/{NODE_ID}/debug_mode/config", {
        **base, "name": "Debug mode (extended telemetry probing)", "unique_id": f"{NODE_ID}_debug_mode",
        "state_topic": STATE_TOPIC, "value_template": "{{ 'ON' if value_json.debug_mode else 'OFF' }}",
        "command_topic": f"{CMD_PREFIX}/debug_mode/set", "icon": "mdi:bug-outline",
        "entity_category": "config",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/number/{NODE_ID}/debug_interval/config", {
        **base, "name": "Debug probe interval", "unique_id": f"{NODE_ID}_debug_interval",
        "command_topic": f"{CMD_PREFIX}/debug_interval/set",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("debug_interval"),
        "min": 5, "max": 3600, "step": 1, "unit_of_measurement": "s", "mode": "box",
        "icon": "mdi:timer-sand", "entity_category": "config",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/button/{NODE_ID}/send_debug_handshake/config", {
        **base, "name": "Send debug handshake now", "unique_id": f"{NODE_ID}_send_debug_handshake",
        "command_topic": f"{CMD_PREFIX}/send_debug_handshake", "icon": "mdi:handshake-outline",
        "entity_category": "config",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/extended_active/config", {
        **base, "name": "Extended telemetry active", "unique_id": f"{NODE_ID}_extended_active",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.extended_active else 'OFF' }}",
        "icon": "mdi:radar", "entity_category": "diagnostic",
    }))

    entries.append((f"{DISCOVERY_PREFIX}/switch/{NODE_ID}/capture_log/config", {
        **base, "name": "Capture raw traffic log", "unique_id": f"{NODE_ID}_capture_log",
        "state_topic": STATE_TOPIC, "value_template": "{{ 'ON' if value_json.capture_log_enabled else 'OFF' }}",
        "command_topic": f"{CMD_PREFIX}/capture_log/set", "icon": "mdi:record-rec",
        "entity_category": "config",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/capture_log_file/config", {
        **base, "name": "Capture log file", "unique_id": f"{NODE_ID}_capture_log_file",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("capture_log_file"),
        "icon": "mdi:file-document-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/capture_log_size/config", {
        **base, "name": "Capture log size", "unique_id": f"{NODE_ID}_capture_log_size",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ (value_json.capture_log_bytes / 1024) | round(1) if value_json.capture_log_bytes is not none else '' }}",
        "unit_of_measurement": "KB", "icon": "mdi:file-chart-outline", "entity_category": "diagnostic",
    }))

    entries.append((f"{DISCOVERY_PREFIX}/switch/{NODE_ID}/bypass_mode/config", {
        **base, "name": "Bypass (disable all injection)", "unique_id": f"{NODE_ID}_bypass_mode",
        "state_topic": STATE_TOPIC, "value_template": "{{ 'ON' if value_json.bypass_mode else 'OFF' }}",
        "command_topic": f"{CMD_PREFIX}/bypass_mode/set", "icon": "mdi:transit-connection-variant",
        "entity_category": "config",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/bypass_log_file/config", {
        **base, "name": "Bypass log file", "unique_id": f"{NODE_ID}_bypass_log_file",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("bypass_log_file"),
        "icon": "mdi:file-document-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/bypass_log_size/config", {
        **base, "name": "Bypass log size", "unique_id": f"{NODE_ID}_bypass_log_size",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ (value_json.bypass_log_bytes / 1024) | round(1) if value_json.bypass_log_bytes is not none else '' }}",
        "unit_of_measurement": "KB", "icon": "mdi:file-chart-outline", "entity_category": "diagnostic",
    }))

    # -- extended telemetry sensors (populated only while debug mode is on
    #    and the heater is actually replying -- see "Extended telemetry
    #    active" above) --------------------------------------------------

    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/heater_profile/config", {
        **base, "name": "Heater profile", "unique_id": f"{NODE_ID}_heater_profile",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("heater_profile"),
        "icon": "mdi:file-cog-outline", "entity_category": "diagnostic",
    }))

    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_mode_name/config", {
        **base, "name": "Mode of operation", "unique_id": f"{NODE_ID}_ext_mode_name",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_mode_name"),
        "device_class": "enum", "options": profile_mode_name_options(profile),
        "icon": "mdi:state-machine",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_mode_code/config", {
        # Numeric mirror of "Mode of operation" (state*10+substate) -- see
        # the comment on "State code" above for why this exists.
        **base, "name": "Mode code", "unique_id": f"{NODE_ID}_ext_mode_code",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_mode_code"),
        "icon": "mdi:state-machine", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_running_time/config", {
        **base, "name": "Running time (extended)", "unique_id": f"{NODE_ID}_ext_running_time",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ (value_json.ext_running_time_s / 60) | round(1) if value_json.ext_running_time_s is not none else '' }}",
        "unit_of_measurement": "min", "icon": "mdi:timer-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_defined_rev/config", {
        **base, "name": "Defined revolutions", "unique_id": f"{NODE_ID}_ext_defined_rev",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_defined_rev"),
        "icon": "mdi:fan", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_measured_rev/config", {
        **base, "name": "Measured revolutions", "unique_id": f"{NODE_ID}_ext_measured_rev",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_measured_rev"),
        "icon": "mdi:fan", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/binary_sensor/{NODE_ID}/ext_glow_plug/config", {
        **base, "name": "Glow plug", "unique_id": f"{NODE_ID}_ext_glow_plug",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.ext_glow_plug else 'OFF' }}",
        "device_class": "heat",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_fuel_pump_hz/config", {
        **base, "name": "Fuel pump frequency", "unique_id": f"{NODE_ID}_ext_fuel_pump_hz",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_fuel_pump_hz"),
        "unit_of_measurement": "Hz", "icon": "mdi:gas-station-outline", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_output_pct/config", {
        # Not a vendor-reported field -- derived here assuming output scales
        # linearly with fuel pump frequency, 4.2Hz = 100% (see
        # decode_extended_payload()). Unconfirmed against a real spec.
        **base, "name": "Heater output", "unique_id": f"{NODE_ID}_ext_output_pct",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_output_pct"),
        "unit_of_measurement": "%", "icon": "mdi:gauge", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_voltage/config", {
        **base, "name": "Supply voltage", "unique_id": f"{NODE_ID}_ext_voltage",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_voltage"),
        "device_class": "voltage", "unit_of_measurement": "V", "state_class": "measurement",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_fault_name/config", {
        **base, "name": "Fault (extended, named)", "unique_id": f"{NODE_ID}_ext_fault_name",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_fault_name"),
        "device_class": "enum", "options": profile_fault_name_options(profile),
        "icon": "mdi:alert-circle-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_fault_code/config", {
        # Numeric mirror of "Fault (extended, named)" -- see the comment on
        # "State code" above for why this exists. Distinct from the base
        # "Fault code" sensor (same presumed code space, different frame).
        **base, "name": "Fault code (extended)", "unique_id": f"{NODE_ID}_ext_fault_code",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_fault_code"),
        "icon": "mdi:alert-circle-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_engine_state/config", {
        **base, "name": "Engine state", "unique_id": f"{NODE_ID}_ext_engine_state",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_engine_state"),
        "icon": "mdi:engine-outline", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_relay_state/config", {
        **base, "name": "Relay state", "unique_id": f"{NODE_ID}_ext_relay_state",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_relay_state"),
        "icon": "mdi:electric-switch", "entity_category": "diagnostic",
    }))
    entries.append((f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/ext_fan_current/config", {
        **base, "name": "Fan current", "unique_id": f"{NODE_ID}_ext_fan_current",
        "state_topic": STATE_TOPIC, "value_template": blank_to_none("ext_fan_current_ma"),
        "device_class": "current", "unit_of_measurement": "mA", "state_class": "measurement",
        "entity_category": "diagnostic",
    }))

    return entries


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stop_evt = threading.Event()
        self._shutdown_done = False
        self.heater_lock = threading.Lock()
        self.panel_lock = threading.Lock()

        profile_slug = cfg["heater_profile"] if cfg["heater_profile"] in HEATER_PROFILES else DEFAULT_HEATER_PROFILE
        if profile_slug != cfg["heater_profile"]:
            log.error("Unknown heater_profile %r, falling back to %r", cfg["heater_profile"], DEFAULT_HEATER_PROFILE)
        self.profile = prepare_profile(profile_slug)
        if self.profile["tested"]:
            log.info("Heater profile: %s (confirmed against real hardware)", self.profile["label"])
        else:
            log.warning(
                "Heater profile: %s -- NOT TESTED against real hardware. Byte offsets, field "
                "names, state/fault tables (and whether the extended-telemetry mechanism even "
                "applies to this model at all) are read straight from the vendor tool's own "
                "data, unverified. See docs/PROTOCOL.md and this add-on's DOCS.md.",
                self.profile["label"],
            )

        self.model = StatusModel(
            cfg["preheat_default"], cfg["debug_mode_default"],
            cfg["debug_interval_default"], cfg["capture_log_default"],
            self.profile, cfg["external_temp_sensor_entity"],
        )
        self.external_temp_poller = ExternalTempPoller(
            self.model, cfg["external_temp_sensor_entity"], self.stop_evt)
        self.cmd_queue = queue.Queue()

        self.capture_log = CaptureLog(cfg["capture_log_dir"], cfg["capture_log_max_mb"])
        if self.model.get_capture_log_wanted():
            self.capture_log.start()
        # Separate log, separate file, started/stopped only by the Bypass
        # switch -- so a bypass test run's traffic is captured cleanly even
        # if the normal capture log is off (or vice versa).
        self.bypass_log = CaptureLog(cfg["capture_log_dir"], cfg["capture_log_max_mb"], name_prefix="bypass")
        if self.model.get_bypass_mode():
            self.bypass_log.start()
        self._wire_log = CaptureLogFanout([self.capture_log, self.bypass_log])
        self._capture_log_handler = CaptureLogHandler(self._wire_log)
        self._capture_log_handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(self._capture_log_handler)

        self.panel_ser = serial.Serial(cfg["panel_port"], cfg["baud"], timeout=0.05)
        self.heater_ser = serial.Serial(cfg["heater_port"], cfg["baud"], timeout=0.05)

        self.commander = Commander(self.heater_ser, self.heater_lock, self.model, self._wire_log, self.stop_evt)
        self.auto = AutoThermostat(self.model, self.commander, self.stop_evt, cfg["auto_target_default"])
        self.prevent_freezing = PreventFreezing(
            self.model, self.commander, self.stop_evt, cfg["prevent_freezing_target_default"])
        self.debug_sender = DebugSender(self.heater_ser, self.heater_lock, self.model, self._wire_log, self.stop_evt)

        self.panel_to_heater = Relay(
            "PANEL->HEATER", "display", self.panel_ser, self.heater_ser, self.heater_lock,
            self.model, self._wire_log, self.stop_evt)
        self.heater_to_panel = Relay(
            "HEATER->PANEL", "heater", self.heater_ser, self.panel_ser, self.panel_lock,
            self.model, self._wire_log, self.stop_evt,
            should_filter_fn=lambda: heater_to_panel_should_filter(self.model),
            frame_filter_fn=make_heater_to_panel_filter(self.model))

        self.mqtt = mqtt.Client(client_id=f"{NODE_ID}-bridge", clean_session=True)
        if cfg.get("mqtt_username"):
            self.mqtt.username_pw_set(cfg["mqtt_username"], cfg.get("mqtt_password") or None)
        self.mqtt.will_set(AVAILABILITY_TOPIC, payload="offline", retain=True)
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message

    # -- MQTT ---------------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed, rc=%s", rc)
            return
        log.info("MQTT connected")
        for topic, payload in discovery_configs(self.profile):
            client.publish(topic, json.dumps(payload), retain=True)
        client.publish(AVAILABILITY_TOPIC, "online", retain=True)
        for suffix in (
            "auto_mode/set", "auto_target/set", "preheat_minutes/set",
            "start_preheat", "start_thermostat", "stop", "start_pump",
            "prevent_freezing/set", "prevent_freezing_target/set",
            "debug_mode/set", "debug_interval/set", "send_debug_handshake",
            "capture_log/set", "bypass_mode/set", "external_temp_enabled/set",
        ):
            client.subscribe(f"{CMD_PREFIX}/{suffix}")
        self._publish_state()

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="replace").strip()
        self.cmd_queue.put((msg.topic, payload))

    def _command_worker(self):
        while not self.stop_evt.is_set():
            try:
                topic, payload = self.cmd_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle_command(topic, payload)
            except Exception as e:
                log.error("command %s(%r) failed: %r", topic, payload, e)
            self._publish_state()

    def _handle_command(self, topic, payload):
        suffix = topic[len(CMD_PREFIX) + 1:]
        if suffix == "start_preheat":
            self.commander.start_preheat(self.model.get_preheat_minutes())
        elif suffix == "start_thermostat":
            self.commander.start_thermostat()
        elif suffix == "stop":
            self.commander.stop()
        elif suffix == "start_pump":
            self.commander.start_pump()
        elif suffix == "preheat_minutes/set":
            self.model.set_preheat_minutes(max(1, min(int(float(payload)), 600)))
        elif suffix == "auto_mode/set":
            self.auto.configure(enabled=(payload.lower() == "heat"))
        elif suffix == "auto_target/set":
            self.auto.configure(target=float(payload))
        elif suffix == "prevent_freezing/set":
            self.prevent_freezing.configure(enabled=(payload.upper() == "ON"))
        elif suffix == "prevent_freezing_target/set":
            self.prevent_freezing.configure(target=float(payload))
        elif suffix == "debug_mode/set":
            self.model.set_debug_mode(payload.upper() == "ON")
        elif suffix == "debug_interval/set":
            self.model.set_debug_interval(float(payload))
        elif suffix == "send_debug_handshake":
            self.debug_sender.send_when_quiet()
        elif suffix == "capture_log/set":
            wanted = payload.upper() == "ON"
            self.model.set_capture_log_wanted(wanted)
            if wanted:
                self.capture_log.start()
            else:
                self.capture_log.stop()
        elif suffix == "bypass_mode/set":
            wanted = payload.upper() == "ON"
            self.model.set_bypass_mode(wanted)
            if wanted:
                self.bypass_log.start()
                log.warning(
                    "BYPASS MODE ENABLED -- all command injection suspended (Start/Stop "
                    "buttons, Auto thermostat, Prevent freezing, debug handshake); pure "
                    "passive relay only from here on. Logging separately to %s.",
                    self.bypass_log.path,
                )
            else:
                log.info("Bypass mode disabled -- command injection resumed")
                self.bypass_log.stop()
        elif suffix == "external_temp_enabled/set":
            self.model.set_external_temp_enabled(payload.upper() == "ON")
        else:
            log.warning("unhandled command topic %s", topic)

    def _publish_state(self):
        snap = self.model.snapshot(
            self.auto.snapshot(), self.prevent_freezing.snapshot(), self.capture_log.status(),
            self.bypass_log.status(prefix="bypass_log"),
        )
        self.mqtt.publish(STATE_TOPIC, json.dumps(snap), retain=True)

    def _heartbeat(self):
        while not self.stop_evt.wait(2.0):
            self._publish_state()

    # -- lifecycle ------------------------------------------------------

    def run(self):
        # Relay + command handling start regardless of MQTT status -- the
        # physical panel must keep working even if Home Assistant/MQTT is
        # completely unreachable, same as the base add-on's passthrough.
        self.panel_to_heater.start()
        self.heater_to_panel.start()
        self.auto.start()
        self.prevent_freezing.start()
        self.debug_sender.start()
        self.external_temp_poller.start()
        threading.Thread(target=self._command_worker, daemon=True).start()
        threading.Thread(target=self._heartbeat, daemon=True).start()
        threading.Thread(target=self._start_mqtt, daemon=True).start()

        log.info(
            "Autoterm DEBUG bridge running: panel=%s heater=%s baud=%s "
            "(MQTT connecting in background: %s:%s, debug_mode default=%s, capture default=%s)",
            self.cfg["panel_port"], self.cfg["heater_port"], self.cfg["baud"],
            self.cfg["mqtt_host"], self.cfg["mqtt_port"],
            self.cfg["debug_mode_default"], self.cfg["capture_log_default"],
        )

        self.stop_evt.wait()

    def _start_mqtt(self):
        host, port = self.cfg["mqtt_host"], self.cfg["mqtt_port"]
        if not host:
            log.error(
                "No MQTT broker configured -- relay/passthrough keeps running, "
                "but Home Assistant entities need MQTT. Install the Mosquitto "
                "broker app, or set mqtt_host in this app's Configuration "
                "tab, then restart it."
            )
            return
        try:
            self.mqtt.connect_async(host, port, keepalive=30)
            self.mqtt.loop_start()
        except Exception as e:
            log.error("Could not start MQTT connection to %s:%s: %r", host, port, e)

    def shutdown(self):
        if self.stop_evt.is_set() and self._shutdown_done:
            return
        log.info("shutting down")
        self.stop_evt.set()
        self._shutdown_done = True
        try:
            self.mqtt.publish(AVAILABILITY_TOPIC, "offline", retain=True).wait_for_publish(timeout=2)
        except Exception:
            pass
        self.mqtt.loop_stop()
        try:
            self.mqtt.disconnect()
        except Exception:
            pass
        self.panel_to_heater.join(timeout=1.0)
        self.heater_to_panel.join(timeout=1.0)
        self.auto.join(timeout=1.0)
        self.prevent_freezing.join(timeout=1.0)
        self.debug_sender.join(timeout=1.0)
        self.capture_log.stop()
        self.bypass_log.stop()
        log.removeHandler(self._capture_log_handler)
        self.panel_ser.close()
        self.heater_ser.close()


def cfg_from_env():
    def env_float(name, default):
        v = os.environ.get(name)
        return float(v) if v else default

    def env_int(name, default):
        v = os.environ.get(name)
        return int(v) if v else default

    def env_bool(name, default):
        v = os.environ.get(name)
        if v is None or v == "":
            return default
        return v.strip().lower() in ("1", "true", "yes", "on")

    return {
        "panel_port": os.environ.get("AUTOTERM_PANEL_PORT", "/dev/ttyUSB1"),
        "heater_port": os.environ.get("AUTOTERM_HEATER_PORT", "/dev/ttyUSB3"),
        "baud": env_int("AUTOTERM_BAUD", 2400),
        "preheat_default": env_int("AUTOTERM_PREHEAT_DEFAULT", 30),
        "auto_target_default": env_float("AUTOTERM_AUTO_TARGET_DEFAULT", 20.0),
        "prevent_freezing_target_default": env_float("AUTOTERM_PREVENT_FREEZING_TARGET_DEFAULT", 5.0),
        # run.sh always exports this var (possibly to an empty string when no
        # MQTT service/option is set), so a plain .get(..., default) default
        # never actually applies -- fall back explicitly on emptiness too.
        "mqtt_host": os.environ.get("AUTOTERM_MQTT_HOST") or "core-mosquitto",
        "mqtt_port": env_int("AUTOTERM_MQTT_PORT", 1883),
        "mqtt_username": os.environ.get("AUTOTERM_MQTT_USERNAME") or None,
        "mqtt_password": os.environ.get("AUTOTERM_MQTT_PASSWORD") or None,
        "debug_mode_default": env_bool("AUTOTERM_DEBUG_MODE_DEFAULT", False),
        "debug_interval_default": env_int("AUTOTERM_DEBUG_INTERVAL_DEFAULT", 60),
        "capture_log_default": env_bool("AUTOTERM_CAPTURE_LOG_DEFAULT", False),
        "capture_log_max_mb": env_int("AUTOTERM_CAPTURE_LOG_MAX_MB", 20),
        "capture_log_dir": os.environ.get("AUTOTERM_CAPTURE_LOG_DIR", "/config/autoterm_debug"),
        "heater_profile": os.environ.get("AUTOTERM_HEATER_PROFILE") or DEFAULT_HEATER_PROFILE,
        "external_temp_sensor_entity": os.environ.get("AUTOTERM_EXTERNAL_TEMP_SENSOR_ENTITY") or None,
    }


def main():
    if "--discover-ports" in sys.argv:
        baud = int(os.environ.get("AUTOTERM_BAUD", "2400"))
        found = discover_ports(baud)
        if found is None:
            return 1
        panel_port, heater_port = found
        # Machine-parseable lines for run.sh -- logging goes to stderr, so
        # stdout stays clean for these two.
        print(f"PANEL_PORT={panel_port}")
        print(f"HEATER_PORT={heater_port}")
        return 0

    cfg = cfg_from_env()
    try:
        bridge = Bridge(cfg)
    except serial.SerialException as e:
        log.error("could not open serial ports: %r", e)
        return 1

    def handle_signal(signum, frame):
        bridge.shutdown()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        bridge.run()
    finally:
        bridge.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
