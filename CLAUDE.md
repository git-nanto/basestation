# MOWERBASE.md — RTK Base Station Project

> This file is the primary context document for AI agents working on the MowerBase sub-project.
> MowerBase is the RTK base station component of the Autonomous Lawn Mower project.
> It is designed to be a standalone project that can be developed and deployed independently.
> Read this fully before taking any action.

---

## What This Project Is

A self-contained RTK base station built on a **Raspberry Pi Zero W (v1)** with a Waveshare LC29H(BS) GPS/RTK HAT. It provides RTCM correction data to the autonomous lawn mower via a SiK 915MHz radio link.

The device:
- Acquires its own position via **GPS-only Survey-In** (no external corrections required)
- Locks to Fixed mode after survey completes (~60 seconds) and stores the position permanently
- Continuously outputs RTCM correction data to the mower via SiK radio
- Monitors for position drift and alerts the user if base station has moved
- Is fully configurable via a web UI accessible at **mowerbase.local** once on WiFi
- Uses a captive portal for first-boot WiFi setup
- Shows live status on an SSD1306 OLED display and a status LED

---

## Hardware

| Component | Detail | Notes |
|---|---|---|
| Computer | Raspberry Pi Zero W (v1) | ~$10. Single-core ARMv6 (BCM2835), 512MB RAM. Full Linux. Python ecosystem. mDNS via avahi. |
| GPS HAT | Waveshare LC29H(BS) GPS/RTK HAT | Stacks directly on Pi 40-pin GPIO header. Purpose-built RTK base station module. |
| GPS chip | Quectel LC29H(BS) | L1+L5 dual-band, multi-GNSS (GPS, BDS, GLONASS, Galileo, QZSS). -165dBm sensitivity. |
| Display | SSD1306 128x64 OLED | I2C. Address 0x3C (confirm on actual hardware — some modules use 0x3D). |
| Status LED | Single LED + resistor | GPIO 17. Indicates fix state. Colours/states defined below. |
| Radio | Holybro SiK V3 915MHz 100mW | Ground unit of the pair. Connected to Pi Zero W via USB. Forwards RTCM to mower. |
| Power | Milwaukee 18V battery → LM2596 step-down → 5.1V → Pi micro-USB power port | LM2596 has onboard LED voltmeter — set to exactly 5.1V to compensate for cable drop. |
| Antenna | LC29H(BS) included active GNSS antenna | Place with clear sky view. Fixed location between sessions. |

---

## Physical Setup

- LC29H(BS) HAT stacks directly on Pi Zero W 40-pin GPIO header — no wiring between them
- OLED and LED are additional components wired to remaining GPIO pins
- SiK radio connects via Pi USB (micro-USB OTG adapter required on Pi Zero W)
- Power input: micro-USB power port on Pi Zero W from LM2596 output (port furthest from SD card)
- All housed in a weatherproof enclosure (design TBD)
- Antenna placed with unobstructed sky view — cable routed to enclosure

---

## LC29H(BS) HAT — Key Hardware Facts

- Standard 40-pin GPIO header — direct Pi stack, no external wiring for GPS comms
- **UART jumper must be set to Position B** — this routes LC29H serial through Pi GPIO 14 (TX) / GPIO 15 (RX)
- Position A = USB-to-UART chip (for PC connection via micro-USB)
- Position C = access Pi via USB-to-UART (not needed here)
- HAT has 4 onboard LEDs: PWR, RXD, TXD, PPS — these are always present
- HAT has onboard ML1220 battery holder — install battery to preserve ephemeris for fast hot starts
- Communicates via PAIR commands (Quectel proprietary NMEA extension) for configuration
- Outputs standard NMEA + RTCM3 messages
- Survey-In configured via `$PQTMCFGSVIN` PAIR command
- Fixed mode configured via `$PQTMCFGSVIN` with mode=2 and ECEF coordinates
- Supported RTCM3 output messages: 1005, 1074, 1084, 1094, 1124 (MSM4), plus ephemeris

---

## Pi Zero W (v1) Setup Notes

- **OS:** Raspberry Pi OS Lite 32-bit Bookworm (supports ARMv6). Do NOT use 64-bit — Pi Zero W v1 is ARMv6 only.
- Disable serial console on `/dev/ttyAMA0` (raspi-config → Interface Options → Serial → disable console, enable hardware)
- On Pi Zero W, the PL011 UART (`/dev/ttyAMA0`) is shared with Bluetooth by default. **Must add `dtoverlay=disable-bt`** to `/boot/firmware/config.txt` to free it for GPS.
- After disabling BT, `/dev/ttyAMA0` maps to GPIO 14 (TX) / GPIO 15 (RX) — this is what the LC29H(BS) HAT uses.
- USB OTG: Pi Zero W has a single micro-USB port for data (closest to SD card). Use a micro-USB OTG adapter → USB-A → SiK radio USB cable.
- Enable I2C in raspi-config for OLED
- Install avahi-daemon for mDNS (`mowerbase.local` hostname resolution)
- **Performance:** Single-core 1GHz — pip installs are slow, first boot takes longer. All services run fine once installed.

---

## Software Architecture

Three systemd services, all under `/opt/mowerbase/`. They communicate via shared state file on tmpfs and a named pipe for RTCM data.

```
systemd boot order:
├── mowerbase-gps.service        ← GPS serial reader + LC29H(BS) controller + state machine
├── mowerbase-sik.service        ← RTCM → SiK radio forwarder (After: gps)
└── mowerbase-web.service        ← Flask web server + OLED driver + LED control (After: gps)
```

### IPC Between Services

| Resource | Path | Purpose |
|---|---|---|
| State file | `/run/mowerbase/state.json` | Live system state. Written by gps.py and sik_forwarder. Read by web + OLED. tmpfs — cleared on reboot. |
| RTCM pipe | `/run/mowerbase/rtcm.pipe` | Named pipe. **gps.py writes** the LC29H(BS)'s RTCM3 output. sik_forwarder reads. |
| Config file | `/etc/mowerbase/config.json` | User configuration. Written by web UI. Read by services on start + SIGUSR1 reload. |
| Position file | `/etc/mowerbase/position.json` | Surveyed base position. Written by gps.py after survey. Read on boot for Fixed mode. |
| History DB | `/var/lib/mowerbase/history.db` | SQLite. Drift history, survey history, event log. |

---

## Service Details

### mowerbase-gps.py
Responsibilities:
- Open serial port to LC29H(BS) at `/dev/ttyAMA0`, 115200 baud
- **Byte-stream reader** — handles mixed RTCM3 binary (0xD3 preamble) and NMEA text ('$' start) on the same serial stream
- Parse RTCM3 frames: message 1005 (base station ECEF position), MSM4 1074/1084/1094/1124 (satellite counts)
- Parse NMEA sentences (GGA, GSA) when present
- **Survey-In completion detected by first appearance of RTCM3 message 1005** after min_duration elapsed — the module only outputs 1005 after it self-declares Fixed mode
- Send PAIR commands to control Survey-In / Fixed mode:
  - Survey-In: `$PQTMCFGSVIN,W,1,{duration},{accuracy_m},0.0,0.0,0.0*`
  - Fixed mode: `$PQTMCFGSVIN,W,2,0,0.0,{ecef_x:.4f},{ecef_y:.4f},{ecef_z:.4f}*`
- Forward all RTCM3 frames to `/run/mowerbase/rtcm.pipe` for sik_forwarder
- Run drift monitoring thread once in Fixed state
- Write to `/run/mowerbase/state.json` every 2 seconds
- Accept reload signal (SIGUSR1), re-survey trigger (SIGUSR2)

### mowerbase-sik.py
Responsibilities:
- Read from `/run/mowerbase/rtcm.pipe`
- Write to SiK radio at `/dev/ttyUSB0`, 57600 baud (confirm baud not changed)
- Track bytes/sec transmitted
- Report SiK connection status to state.json
- Handle SiK USB disconnect/reconnect gracefully

### mowerbase-web.py
Responsibilities:
- Flask server on port 8080
- Serve all web UI pages (see Web UI section)
- Read state.json for live status (served via REST endpoint or SSE)
- Read/write config.json for configuration changes
- Update SSD1306 OLED every 2 seconds
- Control status LED GPIO pin
- Trigger re-survey by sending SIGUSR2 to gps.py
- Serve captive portal redirect if WiFi not configured

---

## Survey-In State Machine

States and transitions — owned by mowerbase-gps.py:

```
BOOT
 │
 ├── position.json exists?
 │     YES → load ECEF coords → seed lat/lon/alt for drift monitor
 │           → send Fixed mode PAIR command → FIXED
 │     NO  → send Survey-In PAIR command → SURVEYING
 │           ($PQTMCFGSVIN,W,1,60,2.0,0.0,0.0,0.0)   ← 60s min, 2.0m accuracy limit

SURVEYING
 │   Continuously:
 │   - Parse RTCM3 MSM4 frames to track satellite count
 │   - Parse any NMEA GGA/GSA that appears
 │   - Update state.json with progress (elapsed_s, satellites)
 │   - Update OLED with progress bar
 │
 └── RTCM3 message 1005 received AND elapsed >= min_duration → SURVEY_COMPLETE
       - Parse ECEF position from RTCM 1005
         (38-bit signed fields: X at bit 34, Y at bit 74, Z at bit 114, scaled 0.0001m)
       - Write position.json
       - Send Fixed mode PAIR command:
         ($PQTMCFGSVIN,W,2,0,0.0,{ecef_x:.4f},{ecef_y:.4f},{ecef_z:.4f})
       → FIXED
       (Note: message 1005 only appears once LC29H(BS) self-declares Fixed mode —
        it is both the survey-complete signal and the position source)

FIXED
 │   Drift monitoring thread runs every 5 minutes (see Drift Monitoring)
 │   RTCM stream active
 │
 └── Re-survey triggered (web UI) → RESURVEY_CONFIRM → SURVEYING
```

Re-survey compare logic (GPS-only appropriate thresholds):
- After new survey completes, compare new ECEF to stored ECEF
- Calculate 3D displacement (sqrt of sum of squared differences)
- If delta < 0.50m (50cm): auto-accept, update position.json, log event
- If delta >= 0.50m: set `resurvey_pending` flag in state.json, hold both positions
- Web UI shows both positions and delta, requires manual "Accept New" or "Keep Existing" button

---

## Drift Monitoring

Background thread in mowerbase-gps.py, active only in FIXED state:

- Every 5 minutes: collect 30-sample rolling average of current position (from RTCM 1005 / NMEA GGA)
- Convert averaged position to ECEF
- Calculate delta from stored position.json ECEF (3D displacement in metres)
- Write to SQLite `drift_history` table: `(timestamp, delta_x, delta_y, delta_z, total_3d)`
- Update `drift_current_m` and `drift_trend` fields in state.json
- If total_3d > config `drift.alert_threshold_m` (default 0.5m — appropriate for GPS-only accuracy):
  - Set `drift_alert: true` in state.json
  - OLED shows drift warning
  - Web UI shows alert banner

---

## Position Accuracy Philosophy

MowerBase uses **GPS-only Survey-In** — no external NTRIP corrections. This is a deliberate design choice:

**Why GPS-only is sufficient:**
- The mower's position is computed *relative to the base station*. If the base has a 2m absolute GPS error and the mower waypoints were collected using that same base, all positions share the same error — they cancel out.
- Waypoints collected in one session (with this base locked to its GPS-only position) will be followed accurately in subsequent sessions, as long as the base doesn't physically move.
- GPS-only Survey-In over 60 seconds achieves ~1-3m absolute accuracy. This is irrelevant to mowing quality — the mower needs centimetre *relative* accuracy, which RTK provides regardless of the base's absolute position.

**When absolute accuracy matters (future):**
- If the base station is physically relocated, the old waypoints will be offset by the base displacement.
- Planned future feature: **"Move / Reposition"** — two-sided operation spanning both devices:
  - **MowerBase side:** User triggers a new Survey-In from the new physical location. After completion, MowerBase computes the 3D ECEF delta between the old `position.json` and the new surveyed position, and exposes it via the API.
  - **Mower Pi side:** User initiates a "shift waypoints" operation in the mower's web app. It fetches the delta vector from MowerBase and applies it to all stored waypoints in the mission database, offsetting each waypoint by the same displacement.
  - Result: missions collected against the old base position remain valid after the base moves, without requiring the user to re-fly/re-collect all waypoints.
  - This is NOT needed when the base station stays fixed between sessions — which is the normal operating mode.

**What was removed:**
- NTRIP client (`ntrip.py`) — required internet access, credentials, 5+ minutes per start-up, connection management, plugin architecture
- `ntrip_plugins/` directory — AUSCORS, RTK2Go, custom caster plugins
- `mowerbase-ntrip.service` — the 4th systemd service

---

## Configuration File

`/etc/mowerbase/config.json` — written by web UI, read by all services:

```json
{
  "survey": {
    "min_duration_seconds": 60,
    "accuracy_limit_m": 2.0
  },
  "drift": {
    "alert_threshold_m": 0.5,
    "check_interval_minutes": 5
  },
  "sik": {
    "port": "/dev/ttyUSB0",
    "baud": 57600
  },
  "gps": {
    "port": "/dev/ttyAMA0",
    "baud": 115200
  },
  "web": {
    "port": 8080,
    "hostname": "mowerbase"
  }
}
```

Position file — written only by gps.py, never edited manually:

`/etc/mowerbase/position.json`

```json
{
  "ecef_x": -3935857.5283,
  "ecef_y": 3440073.8749,
  "ecef_z": -3642935.1065,
  "lat": -35.0534,
  "lon": 138.8454,
  "alt_m": 312.4,
  "accuracy_m": 0.014,
  "survey_duration_s": 487,
  "sample_count": 4870,
  "surveyed_at": "2026-03-02T09:14:22+10:30"
}
```

---

## OLED Display Layout (SSD1306 128x64)

Normal operation (FIXED):
```
┌────────────────────────────┐
│ MowerBase     [FIXED]      │  ← hostname + fix state
│ Sats: 22   Alt: 312.4m     │  ← satellite count + altitude
│ Drift: +120mm              │  ← current drift from stored position
│ RTCM: 1.2kb/s  SiK: OK    │  ← correction rate + radio status
└────────────────────────────┘
```

During Survey-In:
```
┌────────────────────────────┐
│ MowerBase   [SURVEYING]    │
│ ████████░░░░░░  0:48 / 1:00│  ← progress bar + elapsed/target time
│ Accuracy: 1.8m             │  ← current GPS accuracy (target <2.0m)
│ Sats: 22                   │  ← satellite count
└────────────────────────────┘
```

Captive portal / no WiFi:
```
┌────────────────────────────┐
│ MowerBase   [NO WIFI]      │
│ Connect to WiFi:           │
│ SSID: MowerBase-Setup      │
│ Then: 192.168.4.1          │
└────────────────────────────┘
```

---

## Status LED States

| State | LED Behaviour | Meaning |
|---|---|---|
| FIXED, RTCM flowing | Green solid | All good, corrections transmitting |
| FLOAT | Green slow blink (1Hz) | Corrections running, not fully fixed |
| SURVEYING | Amber fast blink (4Hz) | GPS-only survey in progress |
| Drift alert | Green + Red alternate | Fixed but position drift detected |
| No fix / error | Red solid | GPS error — check OLED |
| Captive portal active | Blue blink | Waiting for WiFi setup |
| Boot / initialising | White blink | Services starting |

Note: A single-colour LED (e.g. green) can encode states via blink patterns. If RGB LED is used, full colour set applies. Decide on LED type when building enclosure.

---

## Web UI — Pages and Functions

All pages accessible at `http://mowerbase.local` once device is on the network.

### `/` — Dashboard
- Fix status badge (colour-coded: SURVEYING / FIXED / ERROR)
- Survey-In progress bar and ETA (if surveying)
- Surveyed position: lat, lon, alt, accuracy, survey date
- Current drift: 3D displacement from stored position
- RTCM transmission rate (bytes/sec)
- SiK radio connection status
- System uptime

### `/config` — Settings
- **Position Mode** explanation — GPS-only survey, relative accuracy rationale
- **Survey-In Settings** — minimum duration (seconds), GPS accuracy limit (metres)
- **SiK Radio** — USB port and baud rate

### `/position` — Position Management
- Stored position display: ECEF (X, Y, Z), lat/lon/alt, accuracy, survey date
- Drift history chart: 24-hour time series, Y-axis = 3D displacement in mm
- "Re-Survey" button → confirmation dialog ("Mower must be stationary. This will interrupt corrections for ~60 seconds. Continue?")
- If resurvey_pending: show both old and new position, delta, Accept / Keep buttons

### `/network` — WiFi Configuration
- Current WiFi SSID and connection status
- "Change WiFi" button → triggers captive portal mode (or shows WiFi scan + password form inline)

### `/logs` — System Logs
- Live log tail via Server-Sent Events (SSE)
- Log level filter (DEBUG / INFO / WARNING / ERROR)
- Download log file button

---

## Captive Portal — First Boot WiFi Setup

Triggered when: no WiFi credentials saved, or user presses network reset (web UI or GPIO button TBD).

Behaviour:
1. Pi boots in AP mode — SSID: `MowerBase-Setup`, no password
2. OLED shows setup instructions
3. User connects phone/laptop to `MowerBase-Setup`
4. Any HTTP request redirects to `192.168.4.1` (captive portal page)
5. Portal shows: WiFi network scan list, password field, Save button
6. On save: write `wpa_supplicant.conf`, restart networking
7. Pi connects to home WiFi, drops AP mode
8. Advertise `mowerbase.local` via avahi mDNS
9. OLED updates to show "Connected — go to mowerbase.local"

Implementation: `hostapd` + `dnsmasq` for AP. Python subprocess for wpa_supplicant config write. Standard Pi captive portal pattern.

Re-trigger: via `/network` page "Change WiFi" button. Physical GPIO button TBD (see Open Questions).

---

## File Layout

```
[project root — dev machine]
├── install.sh               ← One-shot install (run once on fresh Pi)
├── update.sh                ← Deploy updates via SCP + restart (ongoing)
├── SPEC.md / SPEC.html      ← Full functional + technical specification
├── README.md                ← User-facing documentation
└── claude.md                ← AI agent context (this file)

/opt/mowerbase/              ← Application (mowerbase user owns this dir)
├── gps.py                   ← GPS serial reader + state machine + RTCM pipe writer
├── sik_forwarder.py         ← RTCM pipe → SiK USB serial
├── web.py                   ← Flask web server + OLED + LED
├── oled.py                  ← SSD1306 display driver wrapper
├── led.py                   ← GPIO LED controller
├── state.py                 ← state.json read/write helpers (shared)
├── templates/               ← Flask Jinja2 HTML templates
│   ├── base.html            ← Layout, nav
│   ├── index.html           ← Dashboard (includes Restart All button)
│   ├── config.html          ← Survey-In + SiK settings
│   ├── position.html        ← Position management
│   ├── network.html         ← WiFi config
│   └── logs.html            ← Log viewer (All Services + per-service)
└── static/
    ├── style.css
    └── dashboard.js         ← Polling loop for live updates

/etc/mowerbase/
├── config.json              ← User configuration (managed by web UI)
└── position.json            ← Surveyed position (managed by gps.py only)

/etc/sudoers.d/mowerbase     ← Allows mowerbase user to restart services + deploy

/var/lib/mowerbase/
└── history.db               ← SQLite: drift_history, survey_history, events

/run/mowerbase/              ← tmpfs, recreated each boot
├── state.json               ← Live system state
└── rtcm.pipe                ← Named pipe: gps.py → sik_forwarder

/etc/systemd/system/
├── mowerbase-gps.service
├── mowerbase-sik.service
└── mowerbase-web.service
```

---

## Systemd Service Dependencies

```
mowerbase-gps.service
    ├── mowerbase-sik.service    (After: gps, reads rtcm.pipe created by gps.py)
    └── mowerbase-web.service    (After: gps, independent of sik)
```

All services: `Restart=on-failure`, `RestartSec=5`. GPS service creates `/run/mowerbase/` directory and `rtcm.pipe` on start.

---

## Open Questions

| # | Question | Impact |
|---|---|---|
| 1 | Physical GPIO button for captive portal reset — include in build or web-UI-only? | Affects GPIO pin assignment and button wiring |
| 2 | SSD1306 I2C address — 0x3C or 0x3D? | Confirm on actual hardware before writing OLED code |
| 3 | SiK radio baud rate — Holybro V3 default is 57600. Has it been changed? | sik_forwarder.py serial config |
| 4 | mDNS hostname — confirmed `mowerbase.local`? | avahi config, web UI references |
| 5 | Milwaukee battery + LM2596 — is the step-down already built and set to 5.1V? | Affects whether power testing is a build step |
| 6 | RGB LED or single-colour LED for status? | LED driver code (single GPIO vs PWM/I2C for RGB) |

**Resolved:** Web server port → **8080** (confirmed running on 8080, accessible at `http://mowerbase.local:8080`)

---

## Implementation Status

All core files written and deployed. Services running on Pi Zero W v1 at `192.168.0.171`.

| File | Status | Notes |
|---|---|---|
| `state.py` | ✅ Done | Atomic JSON read/write with merge |
| `gps.py` | ✅ Done | Byte-stream RTCM3+NMEA reader, RTCM1005 survey detection, GPS-only survey, RTCM stats + convergence tracking |
| `sik_forwarder.py` | ✅ Done | RTCM pipe → SiK USB |
| `led.py` | ✅ Done | GPIO LED blink patterns |
| `oled.py` | ✅ Done | SSD1306 wrapper |
| `web.py` | ✅ Done | Flask 8080, SSE log streaming, `/api/state`, `/api/drift/history`, `/api/survey/status`, `/api/openapi.json`, `/rtcm` route, `/swagger` route, restart API |
| `templates/base.html` | ✅ Done | Nav (Dashboard, Config, Position, Network, RTCM, Logs, API), uptime |
| `templates/index.html` | ✅ Done | Dashboard with Restart All button |
| `templates/config.html` | ✅ Done | Survey-In settings + SiK radio settings |
| `templates/position.html` | ✅ Done | GPS Status (always-live), Survey-In progress, satellite convergence chart (Chart.js, post-survey persistent), Leaflet map, drift chart, human-readable surveyed-at date |
| `templates/rtcm.html` | ✅ Done | RTCM verification tab: per-type counts (10s window, incl. QZSS 1114), bytes/sec, health indicator, last 1005 timestamp, recent 20 frames |
| `templates/swagger.html` | ✅ Done | Standalone Swagger UI (no base.html extend — avoids CSS conflict) |
| `templates/network.html` | ✅ Done | WiFi config |
| `templates/logs.html` | ✅ Done | SSE log tail, per-service selector |
| `static/style.css` | ✅ Done | |
| Systemd unit files | ✅ Done | 4 services (gps, sik, ntrip, web), LogsDirectory, Restart=on-failure |
| `install.sh` | ✅ Done | One-shot install with `--system-site-packages` venv, authbind, AP profile |
| `update.sh` | ✅ Done | SCP deploy + restart + port 80 migration |
| `ntrip.py` | ✅ Done | NTRIP server + client relay, dual-output (SiK + TCP) |
| `templates/config.html` | ✅ Done | Correction source toggle, GPS gate, WiFi gate, NTRIP dual-output note |
| `templates/network.html` | ✅ Done | MowerBase AP status + home WiFi config |
| `templates/index.html` | ✅ Done | Hardware state banners (no GPS / no WiFi / both) |
| `static/dashboard.js` | ✅ Done | Banner logic reading `gps.serial_ok` + `wifi.home_connected` |

**Concurrent AP+STA:** Always-on open "MowerBase" WiFi AP via NetworkManager profile (`wifi.mode ap`, `ipv4.method shared`). Captive portal detection routes in Flask. Port **80** (authbind). GPS hardware gating via `gps.serial_ok`. WiFi gating via `wifi.home_connected`.

---

## Python Dependencies

```
# GPS / GNSS
pyserial          ← serial comms to LC29H(BS)
pynmea2           ← NMEA sentence parsing

# Display
luma.oled         ← SSD1306 OLED driver (wraps smbus2 + PIL)
Pillow            ← Image/font rendering for OLED

# Web
Flask             ← Web server

# System
RPi.GPIO          ← LED GPIO control
sqlite3           ← History DB (built-in Python)
```

---

## Relationship to Main Lawn Mower Project

This is **Block B06** in the main autonomous lawn mower project (see CLAUDE.md / BUILD_LOG.md / PROJECT.md).

The MowerBase device connects to the mower system via:
- **SiK 915MHz radio** — MowerBase Pi Zero W USB → SiK ground unit → RF → SiK air unit → Pixhawk TELEM1
- RTCM corrections flow: LC29H(BS) outputs RTCM3 stream → gps.py reads + forwards to pipe → sik_forwarder reads pipe → SiK radio TX → RF → SiK radio RX → Pixhawk TELEM1

The MowerBase is **independent of the mower Pi**. It does not speak MAVLink. It does not know about ArduRover. It only produces RTCM and sends it over the radio. Keep this separation clean.

---

## Key Reference Links

- Waveshare LC29H(BS) Wiki: https://www.waveshare.com/wiki/LC29H(XX)_GPS/RTK_HAT
- Quectel LC29H(BS) Protocol Spec: https://files.waveshare.com/wiki/LC29H(XX)-GPS-RTK-HAT/Quectel_LC29H(BS)_GNSS_Protocol_Specification_V1.0.pdf
- RTCM3 message 1005 spec: RTCM 10403.3 standard (Table 3.5-91)
- luma.oled docs: https://luma-oled.readthedocs.io
- pynmea2: https://github.com/Knio/pynmea2
- Flask docs: https://flask.palletsprojects.com
- Raspberry Pi serial setup: https://www.raspberrypi.com/documentation/computers/configuration.html#configure-uarts

---

## Build Status

**Last updated: 2026-03-06**

**Hardware:** Pi Zero W v1, Pi OS Lite 32-bit Bookworm. IP: `192.168.0.171`. Web UI: `http://mowerbase.local:8080`.

| Component | Status | Notes |
|---|---|---|
| 3 systemd services | ✅ Running | `gps`, `sik`, `web` |
| GPS serial stream | ✅ Working | Byte-stream reader handles LC29H(BS) RTCM3+NMEA output |
| Survey-In → Fixed | ✅ Confirmed | GPS-only, 60s, completes on first RTCM 1005 after min_duration |
| Re-survey | ✅ Fixed | Was blocked by pipe deadlock — fixed v1.2 |
| RTCM → SiK radio | ⏳ Untested | Pipe logic confirmed; SiK radio not yet connected |
| Web UI — Dashboard | ✅ Working | Live state via polling |
| Web UI — Config | ✅ Working | Survey-In settings + SiK radio settings |
| Web UI — Position | ✅ Working | GPS Status, survey progress, sat convergence chart, Leaflet map, drift chart, human-readable dates |
| Web UI — RTCM | ✅ Working | Per-type frame counts, bytes/sec, last 1005 ts, recent frames |
| Web UI — Logs | ✅ Working | Per-service SSE log tail |
| Web UI — API/Swagger | ✅ Working | OpenAPI 3.0 spec + Swagger UI at /swagger |
| OLED display | ⏳ Not tested | Hardware not yet connected |
| Status LED | ⏳ Not tested | Hardware not yet connected |
| Captive portal | ❌ Not built | WiFi configured manually |

---

## Learned Context & Nuances

### Hardware
- The LC29H(BS) is **not** the same as the LC29B mentioned in earlier project discussions. The LC29H(BS) is purpose-built for base station use with raw observation output. Do not confuse the two.
- The ECEF coordinate string provided by AUSCORS for 5MTB had spaces stripped, making Y appear as 4400073.8749. The correct value is **Y=3440073.8749**. Always verify ECEF coordinates by converting to lat/lon.
- The Pi Zero W has one micro-USB port for data (closest to SD card). A micro-USB OTG adapter is required to connect the SiK radio USB. Power port is furthest from the SD card.
- **Pi Zero W v1 is single-core ARMv6 (BCM2835, 1GHz, 512MB RAM).** pip installs are slow (no pre-built ARMv6 wheels — `luma.oled` builds from source). Allow 10-15 minutes for `install.sh` on first run.

### LC29H(BS) Serial Output — Critical Discovery
- **In Survey-In and Fixed mode, the LC29H(BS) outputs RTCM3 binary frames as its primary output, not NMEA.** The RTCM3 preamble is `0xD3`. NMEA lines (`$` = `0x24`) appear only occasionally.
- The ML1220 backup battery on the HAT retains the module's operating mode across power cycles. Once put into Survey-In mode, it stays there until explicitly commanded otherwise. A power cycle does NOT reset it to NMEA-only mode.
- **gps.py uses a byte-stream reader** — reads one byte at a time, branches on `0xD3` (RTCM3 frame) vs `0x24` (NMEA line). This handles the mixed stream correctly.
- **RTCM3 message 1005** (Stationary RTK Reference Station ARP) is only emitted after the module self-declares Fixed mode. Its first appearance signals survey completion AND provides the ECEF base position. Parse ECEF from 38-bit signed fields at bit offsets 34 (X), 74 (Y), **114 (Z)** (not 113 — bits 112-113 are QuarterCycleIndicator DF364), scaled 0.0001m.
- **RTCM3 MSM4 messages** (1074/1084/1094/1124) contain a 64-bit satellite mask at bit offset **73** (not 36 — bits 37-72 are DF3/DF023/DF024/reserved fields). Count set bits for satellite count.
- Survey-In on the LC29H(BS) uses `$PQTMCFGSVIN` not the u-blox `UBX-CFG-TMODE3`. Do not copy ZED-F9P Survey-In commands.
- **Correct PQTMCFGSVIN format (from spec, not guessed):**
  - Survey-In: `$PQTMCFGSVIN,W,1,{duration},{accuracy_m},0.0,0.0,0.0*{checksum}` — all 3 ECEF fields required even in mode 1
  - Fixed mode: `$PQTMCFGSVIN,W,2,0,0.0,{x:.4f},{y:.4f},{z:.4f}*{checksum}` — decimal metres, not millimetre integers
  - The module responds with `$PQTMCFGSVIN,OK` on success or `$PAIR001,PQTMCFGSVIN,1` (ERROR,1) on malformed command
  - `$PQTMSVINSTATUS` does NOT exist in this module — do not poll it
- **GPS-only Survey-In is sufficient.** ~1-3m absolute accuracy is fine because the mower's waypoints are collected relative to the same base. Absolute error cancels out. See Position Accuracy Philosophy section.

### Pi Software
- **Must use Pi OS Lite, not Desktop.** Desktop runs Wayfire compositor — consumes most RAM/CPU, barely usable over SSH.
- **Venv must use `--system-site-packages`.** Without this flag, apt-installed packages are invisible to the venv and all services fail at import.
- **`LogsDirectory` in systemd units** ensures `/var/log/mowerbase` is created with correct ownership. Without it, the `mowerbase` user cannot write logs → crash-restart loops.
- **`flask threaded=True`** is mandatory. Without threading, SSE log streams block all other HTTP requests.
- **Log streaming** pre-loads last 50 lines on connect, then tails at 0.1s intervals.
- **`update.sh`** deploys via SCP and restarts services. Use `ssh-keygen -t ed25519` + `ssh-copy-id` on the dev machine to eliminate repeated password prompts.
- mDNS (`mowerbase.local`) requires `avahi-daemon` — not installed by default on Pi OS Lite.
- **RTCM named pipe deadlock (fixed in v1.2):** `gps.py` writes RTCM frames to `/run/mowerbase/rtcm.pipe` via `os.write()`. If `sik_forwarder.py` stops reading the pipe (e.g. SiK radio absent, sleeping 30s between retries), the Linux pipe buffer (64KB) fills in seconds at RTCM data rates. Once full, `os.write()` blocks indefinitely, freezing the entire `gps.py` main loop — including SIGUSR2 handling. Fix: (1) `sik_forwarder` now drains the pipe (read+discard) during the retry wait period; (2) `gps.py` sets the pipe write fd to `O_NONBLOCK` via `fcntl.F_SETFL` — raises `BlockingIOError` instead of blocking (frames are silently dropped if no reader, which is acceptable).
- **Diagnosing a blocked gps.py:** `cat /proc/<pid>/wchan` shows `pipe_write` if blocked on the named pipe. `ls -l /proc/<pid>/fd/` confirms which fd points to `rtcm.pipe`.

---

### $PQTMCFGSVIN,R — Read Command and Convergence (confirmed 2026-03-06)

**Command:** `$PQTMCFGSVIN,R*26`
**Response:** `$PQTMCFGSVIN,OK,<Mode>,<MinDur>,<3D_AccLimit>,<ECEFx>,<ECEFy>,<ECEFz>*<chk>`

| Field | Index | Value |
|---|---|---|
| Mode | parts[2] | 1 = Survey-In active, 2 = Fixed |
| MinDuration | parts[3] | Configured minimum survey duration (seconds) |
| 3D_AccLimit | parts[4] | **Configured accuracy LIMIT** (what was written in the W command) — **NOT** live accuracy |
| ECEFx | parts[5] | **Returns 0.0000 during active Survey-In** — NOT a running average. Only populated after Fixed mode is internally declared. |
| ECEFy | parts[6] | Same — 0.0000 during Survey-In |
| ECEFz | parts[7] | Same — 0.0000 during Survey-In |

**Critical:** `parts[4]` is the configured limit, not live accuracy. The module provides no live accuracy field via PAIR commands. The module provides no live ECEF average during Survey-In — all ECEF fields are 0.0000 until Fixed mode is declared internally.

**Convergence chart (our implementation):** Since ECEF-delta cannot be computed during Survey-In, the Position page "Survey Convergence" chart instead plots **satellite count over time** — sampled every 10 seconds from MSM4 frame satellite masks. Stored as `svin_sat_history: [{elapsed_s, sat_count}]` in `state.json`. The sat history from the most recently completed survey is preserved in `last_svin_sat_history` so the convergence section stays visible after the survey completes.

### RTCM 1005 — Survey Completion Signal

- **Message 1005** = Stationary RTK Reference Station ARP (base station ECEF position)
- **Only emitted after the module self-declares Fixed mode** — never during active Survey-In
- **First appearance = survey completion trigger** in gps.py — detected by `_handle_rtcm_frame` after `min_duration` has elapsed
- The module declares Fixed mode when its internal accuracy estimate reaches `3D_AccLimit` — this is completely internal; the module never tells us the current accuracy explicitly
- Contains ECEF position as 38-bit signed integers scaled 0.0001m. Bit offsets: X=34, Y=74, **Z=114** (NOT 113 — bits 112-113 are QuarterCycleIndicator DF364)
- If 1005 never appears: either the sky view is too poor to reach the accuracy target, or the target is too tight. Fix: increase `accuracy_limit_m` in Config (try 3.0m or 5.0m) then Re-Survey.

### RTCM Stats, Web UI, API (2026-03-05 / corrected 2026-03-06)
- **RTCM stats** in `state.json` under top-level `"rtcm"` key: `counts_10s` (per-type, 10s window), `bytes_per_sec`, `frame_rate`, `last_1005_ts` (Unix float), `recent_frames` (last 20 as `{ts, type, len}`)
- **`counts_10s` pre-initialised to 0** for all 5 expected types before each write — ensures `_deep_merge` correctly resets stale counts
- **Chart.js 4.4.0** loaded from CDN by user's browser — no Pi performance impact
- **`/rtcm` tab:** health indicator, per-type counts, bytes/sec, last 1005 timestamp, recent 20 frames
- **`/swagger` tab:** standalone Swagger UI page; OpenAPI 3.0 spec served at `/api/openapi.json`
- **`/api/survey/status`:** dedicated clean survey status endpoint (sm_state, elapsed, progress_pct, stored_position)

*MOWERBASE.md — Standalone context for the MowerBase RTK base station project.*
*Last updated: 2026-03-06 (NTRIP removed; GPS-only Survey-In; pipe deadlock fix; ECEF-zero-during-survey confirmed; satellite count chart; Swagger/API tab)*
*Created by: Claude (Anthropic)*
