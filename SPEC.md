# MowerBase — Functional and Technical Specification

**Version:** 1.2
**Date:** 2026-03-06
**Status:** Implemented — running on Pi Zero W v1 at 192.168.0.215 (mowerbase.local:8080)

---

## Contents

1. [Purpose and Scope](#1-purpose-and-scope)
2. [System Context](#2-system-context)
3. [Functional Requirements](#3-functional-requirements)
4. [Hardware Specification](#4-hardware-specification)
5. [Software Architecture](#5-software-architecture)
6. [Service Specifications](#6-service-specifications)
7. [State Machine](#7-state-machine)
8. [IPC and Data Contracts](#8-ipc-and-data-contracts)
9. [GPS Command Protocol](#9-gps-command-protocol)
10. [Web API](#10-web-api)
11. [Configuration Schema](#11-configuration-schema)
12. [Persisted Data Schemas](#12-persisted-data-schemas)
13. [LED and OLED Specification](#13-led-and-oled-specification)
14. [Captive Portal Specification](#14-captive-portal-specification)
15. [Error Handling and Recovery](#15-error-handling-and-recovery)
16. [Security Considerations](#16-security-considerations)
17. [Constraints and Assumptions](#17-constraints-and-assumptions)
18. [Known Issues](#18-known-issues)

---

## 1. Purpose and Scope

MowerBase is a self-contained RTK (Real-Time Kinematic) GNSS base station. Its sole purpose is to produce a continuous stream of RTCM3 correction data from a surveyed, fixed position and forward those corrections via a 915 MHz radio link to a Pixhawk-based autonomous lawn mower.

**In scope:**
- GPS receiver management and GPS-only Survey-In process
- RTCM forwarding to SiK radio
- Position drift monitoring
- Web UI for configuration and monitoring
- OLED display and GPIO LED status
- WiFi provisioning via captive portal

**Out of scope:**
- MAVLink communication
- Any knowledge of the mower's position or navigation state
- Path planning or mowing logic
- The mower-side radio or Pixhawk configuration

---

## 2. System Context

```
┌─────────────────────────────────────────────────────────────┐
│                        MowerBase                             │
│                                                              │
│  ┌─────────────┐   NMEA/PAIR   ┌────────────────────────┐   │
│  │ LC29H(BS)   │◄─────────────►│  gps.py                │   │
│  │ GPS/RTK HAT │               │  Survey-In state mach. │   │
│  └─────────────┘               └──────────┬─────────────┘   │
│         │ RTCM3 output                     │ rtcm.pipe       │
│         │ (serial)                         │ state.json      │
│         │                                  ▼                 │
│         └───────────────────►┌────────────────────────┐     │
│                               │  sik_forwarder.py      │     │
│                               │  RTCM → SiK USB serial │     │
│                               └────────────┬───────────┘     │
│                                             │ /dev/ttyUSB0   │
│  ┌──────────┐  ┌──────────┐                 ▼               │
│  │  OLED    │  │  LED     │   ┌────────────────────────┐   │
│  │ SSD1306  │  │ GPIO 17  │◄──│  web.py                │   │
│  └──────────┘  └──────────┘   │  Flask + OLED + LED    │   │
│                                └────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
                             │ 915 MHz RF
                             ▼
                    ┌─────────────────┐
                    │  Mower (SiK RX) │
                    │  Pixhawk TELEM1 │
                    └─────────────────┘
```

The MowerBase device has no awareness of the mower's navigation state. Corrections flow one way: base → radio → mower. There is no return data path.

---

## 3. Functional Requirements

### FR-01: GNSS Position Acquisition

| ID | Requirement |
|---|---|
| FR-01.1 | The system shall open a serial connection to the LC29H(BS) GPS module at startup and parse incoming NMEA sentences continuously. |
| FR-01.2 | The system shall parse GGA sentences to extract: latitude, longitude, altitude, fix quality, number of satellites, HDOP. |
| FR-01.3 | The system shall parse GSA sentences to extract: PDOP. |
| FR-01.4 | The system shall detect fix quality transitions (no fix / float / fixed). |
| FR-01.5 | The system shall tolerate temporary loss of GPS signal and resume when signal returns, without restarting. |

### FR-02: Survey-In

| ID | Requirement |
|---|---|
| FR-02.1 | On first boot (no stored position), the system shall immediately start GPS-only Survey-In. No internet connection or NTRIP client is required. |
| FR-02.2 | Survey-In shall be initiated by sending a `$PQTMCFGSVIN` PAIR command to the LC29H(BS) (mode 1, default 60s minimum, 2.0m accuracy limit). |
| FR-02.3 | Survey-In completion is detected by the first appearance of RTCM3 message 1005 after the minimum duration has elapsed. The LC29H(BS) only outputs message 1005 after it internally declares Fixed mode — this serves as both the completion signal and position source. |
| FR-02.4 | On survey completion, the ECEF position extracted from RTCM 1005 shall be written to `/etc/mowerbase/position.json`. |
| FR-02.5 | The system shall then send a `$PQTMCFGSVIN` Fixed mode command with the stored ECEF coordinates. |
| FR-02.6 | Survey-In progress (elapsed time, satellite count) shall be written to state.json every 2 seconds. Satellite count history (sampled every 10s) is stored as `svin_sat_history` for the convergence chart. |

### FR-03: Fixed Mode Operation

| ID | Requirement |
|---|---|
| FR-03.1 | On boot, if `/etc/mowerbase/position.json` exists, the system shall load the stored ECEF position and send Fixed mode commands immediately, skipping Survey-In. |
| FR-03.2 | In Fixed mode, the LC29H(BS) outputs RTCM3 correction messages. The system shall not interfere with this output stream. |
| FR-03.3 | Fixed mode shall persist across power cycles without re-survey. |

### FR-05: RTCM Forwarding to SiK Radio

| ID | Requirement |
|---|---|
| FR-05.1 | The system shall read RTCM bytes from `/run/mowerbase/rtcm.pipe` (written by gps.py) and write them to the SiK radio at `/dev/ttyUSB0`. |
| FR-05.2 | If the SiK radio is absent at startup, the service shall drain the pipe (read and discard bytes) and retry every 30 seconds. Draining is mandatory to prevent the pipe buffer from filling and blocking gps.py's main loop. |
| FR-05.3 | If the SiK radio disconnects mid-operation, the service shall detect the error, close the port, and retry reconnection. |
| FR-05.4 | Bytes/sec forwarded shall be written to state.json. |

### FR-06: Drift Monitoring

| ID | Requirement |
|---|---|
| FR-06.1 | In Fixed mode, the system shall monitor the GPS module's reported position against the stored survey position. |
| FR-06.2 | Monitoring shall use a 30-sample rolling average of GGA position reports, evaluated every 5 minutes. |
| FR-06.3 | The current position average shall be converted to ECEF and compared to stored ECEF via 3D Euclidean distance. |
| FR-06.4 | Each drift measurement shall be written to the SQLite history database. |
| FR-06.5 | If the 3D displacement exceeds `drift.alert_threshold_m` (default 0.03m), `drift_alert` shall be set to `true` in state.json and an alert shown in the web UI and OLED. |

### FR-07: Re-Survey

| ID | Requirement |
|---|---|
| FR-07.1 | A re-survey may be triggered via the web UI `/position` page or via SIGUSR2 sent to gps.py. |
| FR-07.2 | After re-survey, if the new position is within 50 cm of the stored position, the new position shall be accepted automatically and position.json updated. |
| FR-07.3 | If the displacement is ≥ 50 cm, both positions and the delta shall be displayed in the web UI, requiring manual Accept or Keep decision before position.json is updated. |

### FR-08: Web UI

| ID | Requirement |
|---|---|
| FR-08.1 | The system shall serve a web UI on port 8080, accessible at `http://mowerbase.local:8080`. |
| FR-08.2 | The dashboard page shall show: fix state, survey progress (if surveying), stored position, current drift, RTCM rate, SiK status, system uptime. |
| FR-08.3 | Dashboard data shall refresh without page reload via polling of `/api/state` every 3 seconds. |
| FR-08.4 | The config page shall allow configuring Survey-In settings (minimum duration, accuracy limit) and SiK radio settings (port, baud). |
| FR-08.5 | The position page shall allow viewing stored position, viewing drift history, and triggering a re-survey. |
| FR-08.6 | The network page shall allow viewing WiFi status, changing WiFi credentials, and triggering captive portal mode. |
| FR-08.7 | The logs page shall stream live log output via Server-Sent Events with level filtering and log file download. |
| FR-08.8 | The web UI shall be usable on a smartphone screen without zooming. |
| FR-08.9 | The web UI shall not depend on any external CDN. It must work without internet access. |

### FR-09: Status Display

| ID | Requirement |
|---|---|
| FR-09.1 | The SSD1306 OLED display shall be updated every 2 seconds with a screen appropriate to the current state. |
| FR-09.2 | The status LED shall continuously reflect the current system state via blink patterns. |
| FR-09.3 | Display and LED code shall fail gracefully if hardware is absent (ImportError, GPIO unavailable). |

### FR-10: WiFi Provisioning

| ID | Requirement |
|---|---|
| FR-10.1 | On first boot or after WiFi reset, the device shall broadcast an AP named `MowerBase-Setup`. |
| FR-10.2 | Any HTTP request to the AP's IP (192.168.4.1) shall be redirected to a WiFi setup page. |
| FR-10.3 | The setup page shall scan for available networks and allow entering credentials. |
| FR-10.4 | On saving credentials, the device shall connect to the specified network and stop broadcasting the AP. |
| FR-10.5 | WiFi reset shall be triggerable via: (a) GPIO 27 held for 1 second, (b) web UI `/network` page. |

---

## 4. Hardware Specification

### 4.1 Compute

| Item | Value |
|---|---|
| Board | Raspberry Pi Zero W v1 |
| Architecture | ARMv6 32-bit |
| OS | Raspberry Pi OS Lite 32-bit Bookworm |
| Primary UART | `/dev/ttyAMA0` (PL011), assigned to GPIO 14/15 via `dtoverlay=disable-bt` |
| I2C bus | I2C1, GPIO 2 (SDA) / GPIO 3 (SCL), enabled in raspi-config |
| USB | Single micro-USB data port (USB OTG) — used for SiK radio |

### 4.2 GPS Module

| Item | Value |
|---|---|
| Module | Waveshare LC29H(BS) GPS/RTK HAT |
| Chip | Quectel LC29H(BS) |
| Interface | UART via 40-pin GPIO header (HAT Position B jumper) |
| Baud rate | 115200 |
| Serial device | `/dev/ttyAMA0` |
| GNSS bands | L1 + L5 dual-band |
| GNSS systems | GPS, BDS, GLONASS, Galileo, QZSS |
| Sensitivity | −165 dBm |
| RTCM3 output messages | 1005, 1074, 1084, 1094, 1124 (MSM4), ephemeris |
| Configuration protocol | PAIR commands (`$PQTM...` prefix) — not u-blox UBX |
| Battery | ML1220 backup battery — install to preserve ephemeris |

### 4.3 OLED Display

| Item | Value |
|---|---|
| Controller | SSD1306 |
| Interface | I2C |
| Resolution | 128×64 pixels |
| Default I2C address | 0x3C (configurable — some modules use 0x3D) |
| I2C bus | I2C1 |
| Driver library | luma.oled |

### 4.4 Status LED

| Item | Value |
|---|---|
| Type | Single-colour |
| GPIO pin | 17 (BCM) |
| Current limiting | 330Ω series resistor to GPIO, cathode to GND |
| Control | RPi.GPIO direct output + background blink thread |

### 4.5 GPIO Button

| Item | Value |
|---|---|
| GPIO pin | 27 (BCM) |
| Pull-up | Internal pull-up (PUD_UP) |
| Active level | LOW (falling edge = press) |
| Trigger action | Hold ≥ 1 second = captive portal reset |
| Debounce | 200ms hardware debounce via RPi.GPIO |

### 4.6 SiK Radio

| Item | Value |
|---|---|
| Model | Holybro SiK V3 915 MHz 100 mW |
| Connection | USB (micro-USB OTG adapter on Pi Zero W) |
| Device | `/dev/ttyUSB0` |
| Default baud | 57600 |
| Role | Ground unit — forwards RTCM bytes to air unit on mower |
| RTCM direction | One-way: base → mower |

### 4.7 Power

| Item | Value |
|---|---|
| Input | Milwaukee 18V battery |
| Regulation | LM2596 step-down module |
| Output voltage | 5.1V (set higher than 5.0V to compensate for cable drop) |
| Pi input | Micro-USB power port (port furthest from SD card) |

---

## 5. Software Architecture

### 5.1 Process Model

Three independent systemd services run concurrently. They do not share a process or address space. All IPC is via files on the filesystem.

```
mowerbase-gps.service       gps.py
mowerbase-sik.service       sik_forwarder.py
mowerbase-web.service       web.py
```

### 5.2 Startup Order

```
network.target
  └─► mowerbase-gps.service        (creates /run/mowerbase/, rtcm.pipe, PID file)
        ├─► mowerbase-sik.service   (waits for pipe to exist, optional radio)
        └─► mowerbase-web.service   (independent of sik)
```

All services have `Restart=on-failure, RestartSec=5`. A crashed service restarts after 5 seconds without affecting the others.

All services use `LogsDirectory=mowerbase` and `RuntimeDirectory=mowerbase` (where applicable). This causes systemd to create `/var/log/mowerbase` and `/run/mowerbase` with correct ownership for the `mowerbase` user before the process starts, preventing permission errors on log file creation. **Do not remove these directives** — without them, the log directory is owned by root and all services crash on startup.

### 5.3 IPC Resources

| Resource | Path | Type | Owner (writer) | Readers |
|---|---|---|---|---|
| Live state | `/run/mowerbase/state.json` | File (tmpfs) | All services | web.py |
| RTCM stream | `/run/mowerbase/rtcm.pipe` | Named pipe | gps.py | sik_forwarder.py |
| GPS PID | `/run/mowerbase/gps.pid` | File (tmpfs) | gps.py | web.py |
| Configuration | `/etc/mowerbase/config.json` | File | web.py | All services |
| Surveyed position | `/etc/mowerbase/position.json` | File | gps.py | gps.py (boot) |
| History | `/var/lib/mowerbase/history.db` | SQLite | gps.py | web.py |

### 5.4 Python Environment

- Python 3.11 (system Python3 on Bookworm)
- Virtual environment at `/opt/mowerbase/venv`, created with **`--system-site-packages`**
- The `--system-site-packages` flag is mandatory — it makes apt-installed Python packages (Flask, pyserial, Pillow, RPi.GPIO) visible inside the venv. Without it all services fail at import.
- All services use `/opt/mowerbase/venv/bin/python`
- Additional packages installed via pip into the venv (see 5.5)

### 5.5 Dependency Summary

| Package | Source | Purpose |
|---|---|---|
| `pyserial` | pip + apt | GPS serial port |
| `pynmea2` | pip | NMEA sentence parsing |
| `luma.oled` | pip | SSD1306 OLED driver (may build from source on ARMv6) |
| `smbus2` | pip + apt | I2C bus for luma.oled |
| `Pillow` | pip + apt | Font/image rendering for OLED |
| `Flask` | pip + apt | Web server (threaded mode required for SSE) |
| `RPi.GPIO` | pip + apt | GPIO LED and button |
| `sqlite3` | stdlib | History database |
| `fcntl` | stdlib | File locking for state.json + O_NONBLOCK pipe write |
| `collections` | stdlib | Deque for log tail history |
| `avahi-daemon` | apt (system) | mDNS hostname (`mowerbase.local`) |
| `hostapd` | apt (system) | AP mode for captive portal |
| `dnsmasq` | apt (system) | DHCP/DNS for captive portal |
| `network-manager` | apt (system) | WiFi management (nmcli) |

---

## 6. Service Specifications

### 6.1 gps.py

**Entry point:** `main()` → `GpsService.run()`

**Startup sequence:**
1. Load `/etc/mowerbase/config.json`
2. Create `/run/mowerbase/` directory
3. Create `/run/mowerbase/rtcm.pipe` named pipe (if not exists)
4. Write PID to `/run/mowerbase/gps.pid`
5. Open SQLite history database
6. Open serial port `/dev/ttyAMA0` at 115200 baud (O_NONBLOCK on RTCM pipe write fd set after open)
7. Start state-writer background thread (writes state.json every 2s)
8. Register SIGUSR1 (reload config) and SIGUSR2 (trigger re-survey) handlers
9. Check for `/etc/mowerbase/position.json` → branch to FIXED or SURVEYING
10. Enter byte-stream read loop

**Main loop (byte-stream reader):**
- Read one byte at a time from serial port
- On `0xD3` (RTCM3 preamble): accumulate full RTCM3 frame, write to pipe, dispatch to `_handle_rtcm_frame`
- On `0x24` (NMEA '$'): accumulate to line end, parse with pynmea2, dispatch to `_handle_nmea`
- Other bytes: discard (LC29H(BS) output may include inter-frame garbage)
- State machine checks and re-survey flag handled in the frame/NMEA dispatch paths

**Threads:**
- Main thread: byte-stream reader + state machine
- `state-writer`: writes state.json every 2 seconds
- `drift-monitor`: active only in FIXED state, runs on 5-minute cycle

**Signal handlers:**
- `SIGUSR1` → reload config (non-blocking flag check in main loop)
- `SIGUSR2` → set `_resurvey_requested = True` (acted on in main loop)

**Supplementary groups required:** `dialout` (serial access)

### 6.2 sik_forwarder.py

**Entry point:** `main()` → `SikForwarder.run()`

**Startup sequence:**
1. Load config
2. Wait for RTCM pipe to exist
3. Open RTCM pipe for reading (blocking open, then set to blocking read)
4. Attempt to open SiK radio serial port

**Main loop:**
- If radio not open: attempt open → if fails, sleep 30s, retry
- Read from RTCM pipe (blocking, up to 4096 bytes)
- Write to SiK serial
- Track bytes/sec
- Update state.json
- Handle serial exceptions → close, flag for reconnect

**Radio absence handling:** If `/dev/ttyUSB0` does not exist, log a warning and **drain the RTCM pipe** (read+discard) for 30 seconds before retrying. Draining is critical — without it the 64KB pipe buffer fills within seconds, causing `gps.py`'s `os.write()` to block indefinitely (freezing the GPS main loop). The gps.py pipe write fd is also set to `O_NONBLOCK` as a second line of defence.

**Supplementary groups required:** `dialout`, `plugdev`

### 6.3 web.py

**Entry point:** `main()` → `start_background_services()` + `app.run()`

**Startup sequence:**
1. Load config
2. Initialise LED on configured GPIO pin
3. Initialise OLED at configured I2C address
4. Start OLED/LED updater background thread (2-second cycle)
5. Set up GPIO button interrupt
6. Start Flask on port 8080

**OLED/LED updater thread:**
- Reads state.json every 2 seconds
- Calls `oled.update(state)` to render current screen
- Computes LED state from current state → calls `led.set_state()`

**GPIO button:**
- FALLING edge interrupt records press timestamp
- RISING edge interrupt checks hold duration
- Duration ≥ 1.0 second → trigger captive portal

**Flask routes:** See Section 11.

**Supplementary groups required:** `gpio`

---

## 7. State Machine

Owned entirely by `gps.py`. The current state is written to `state.json["gps"]["sm_state"]` and read by web.py and oled.py.

### 7.1 States

| State | Meaning |
|---|---|
| `BOOT` | Service has started, checking for stored position |
| `SURVEYING` | GPS-only Survey-In in progress |
| `FIXED` | Fixed base mode, RTCM corrections active |
| `RESURVEY_PENDING` | Re-survey completed with ≥50 cm displacement — awaiting manual decision |
| `ERROR` | Unrecoverable GPS error |

### 7.2 Transitions

```
BOOT
  ├── position.json exists ──────────────────────────────────► FIXED
  │     (send Fixed mode $PQTMCFGSVIN command immediately)
  └── position.json absent ──────────────────────────────────► SURVEYING
        (send Survey-In $PQTMCFGSVIN command)

SURVEYING
  └── first RTCM 1005 received AND elapsed ≥ min_duration ──► FIXED
        (parse ECEF from 1005, write position.json,
         send Fixed mode command)
      — OR (if re-survey) —
        └── delta ≥ 0.50m ──────────────────────────────────► RESURVEY_PENDING

FIXED
  └── SIGUSR2 / web UI re-survey ────────────────────────────► SURVEYING
        (send Survey-In command)

RESURVEY_PENDING
  ├── web UI "Accept New" (via resurvey_action = "accept") ──► FIXED
  │     (update position.json with new survey)
  └── web UI "Keep Existing" (via resurvey_action = "reject")► FIXED
        (discard new survey result)
```

### 7.3 Re-Survey Decision Logic

After a re-survey completes:
```
delta = sqrt((new_x - stored_x)² + (new_y - stored_y)² + (new_z - stored_z)²)

if delta < 0.50:
    auto-accept → update position.json → FIXED
else:
    set state RESURVEY_PENDING
    write both positions + delta to state.json
    wait for user decision via web UI
```

---

## 8. IPC and Data Contracts

### 8.1 state.json Structure

Written to `/run/mowerbase/state.json` on tmpfs. All values are optional — a missing key means "not yet known". All services use `state.update_state(patch)` for thread-safe partial updates.

```jsonc
{
  // GPS service fields
  "gps": {
    "sm_state": "FIXED",        // current state machine state
    "fix_quality": 4,           // 0=none, 1=GPS, 2=DGPS, 4=RTK fixed, 5=RTK float
    "lat": -35.0534123,         // decimal degrees, null if no fix
    "lon": 138.8454456,
    "alt_m": 312.4,
    "num_sats": 22,
    "hdop": 0.8,
    "pdop": 1.1,
    "accuracy_m": 0.014,        // current 3D accuracy estimate

    // Survey-In fields (present when sm_state == "SURVEYING")
    "svin_elapsed_s": 47,
    "svin_min_duration_s": 60,
    "svin_accuracy_limit_m": 2.0,
    "svin_rtcm_frames": 0,           // MSM4 frames received (0 until Fixed mode declared)
    "svin_sat_history": [            // sat count sampled every 10s during survey
      {"elapsed_s": 10, "sat_count": 18},
      {"elapsed_s": 20, "sat_count": 20}
    ],
    "last_svin_sat_history": [...],  // preserved after survey completes (for chart)

    // Drift fields (only when sm_state == "FIXED")
    "drift_current_m": 0.002,
    "drift_alert": false,

    // Stored position (loaded from position.json)
    "stored_position": {
      "ecef_x": -3935857.5283,
      "ecef_y": 3440073.8749,
      "ecef_z": -3642935.1065,
      "lat": -35.0534000,
      "lon": 138.8454000,
      "alt_m": 312.4,
      "accuracy_m": 0.014,
      "surveyed_at": "2026-03-02T09:14:22+00:00"
    }
  },

  // SiK forwarder fields
  "sik": {
    "connected": true,
    "port": "/dev/ttyUSB0",
    "bytes_total": 1234567,
    "bytes_per_sec": 1228.8,
    "error": null
  },

  // Re-survey pending fields (only when sm_state == "RESURVEY_PENDING")
  "resurvey_delta_m": 0.082,
  "resurvey_new_position": { ... },
  "resurvey_old_position": { ... },
  "resurvey_action": null       // set to "accept" or "reject" by web UI

}
```

### 8.2 RTCM Pipe Protocol

The RTCM pipe at `/run/mowerbase/rtcm.pipe` carries raw binary RTCM3 data with no additional framing. The byte stream is verbatim RTCM3 output from the LC29H(BS) serial port.

RTCM3 messages are self-delimited by their internal header (0xD3 preamble, 10-bit length field). The receiving device (Pixhawk) parses this framing.

- Writer: `gps.py` (pipe write fd opened with `O_NONBLOCK` via `fcntl.F_SETFL`)
- Reader: `sik_forwarder.py`
- Buffer: kernel pipe buffer (64KB default on Linux)
- Backpressure handling: `gps.py` raises `BlockingIOError` instead of blocking when pipe is full (O_NONBLOCK); frames are silently dropped. `sik_forwarder.py` drains the pipe while radio is absent to prevent buffer fill.

### 8.3 Inter-Service Signalling

| Signal | Sender | Recipient | Effect |
|---|---|---|---|
| `SIGUSR1` | web.py | gps.py (via gps.pid) | Reload config |
| `SIGUSR2` | web.py | gps.py (via gps.pid) | Trigger re-survey |

PID file: `/run/mowerbase/gps.pid`

### 8.4 state.json Concurrency

`state.py` uses `fcntl.LOCK_EX` (exclusive lock) for writes and `fcntl.LOCK_SH` (shared lock) for reads. Writes are atomic: data is written to a `.tmp` file then renamed to the final path. This prevents readers from seeing a partial JSON document.

Lock file path: `{state_path}.lock` (separate from the data file to allow read locking without write-locking the data file).

---

## 9. GPS Command Protocol

The LC29H(BS) uses Quectel's PAIR/PQTM proprietary command set — **not** u-blox UBX. Commands are sent as ASCII NMEA-formatted strings over the same serial port that outputs NMEA + RTCM.

### 9.1 NMEA Checksum

All PAIR commands use standard NMEA XOR checksum:
```
checksum = XOR of all bytes between '$' and '*' (exclusive)
formatted as two uppercase hex digits appended after '*'
```

### 9.2 Survey-In Command

```
$PQTMCFGSVIN,W,1,{min_duration},{accuracy_m},0.0,0.0,0.0*{checksum}\r\n
```

| Parameter | Type | Description |
|---|---|---|
| `W` | literal | Write mode |
| `1` | literal | Mode = Survey-In |
| `min_duration` | integer (seconds) | Minimum survey duration (default 60) |
| `accuracy_m` | float | 3D accuracy limit in metres (default 2.0) |
| `0.0,0.0,0.0` | required | Three ECEF fields — must be present even in mode 1 |

Example: `$PQTMCFGSVIN,W,1,60,2.0,0.0,0.0,0.0*XX\r\n`

Module responds: `$PQTMCFGSVIN,OK` on success, `$PAIR001,PQTMCFGSVIN,1` on error.

### 9.3 Fixed Mode Command

```
$PQTMCFGSVIN,W,2,0,0.0,{ecef_x:.4f},{ecef_y:.4f},{ecef_z:.4f}*{checksum}\r\n
```

| Parameter | Type | Description |
|---|---|---|
| `W` | literal | Write mode |
| `2` | literal | Mode = Fixed Base |
| `0,0.0` | reserved | Fixed zeros |
| `ecef_x:.4f` | float | ECEF X in decimal metres (4 decimal places) |
| `ecef_y:.4f` | float | ECEF Y in decimal metres |
| `ecef_z:.4f` | float | ECEF Z in decimal metres |

Example: `$PQTMCFGSVIN,W,2,0,0.0,-3935857.5283,3440073.8749,-3642935.1065*XX\r\n`

**Note:** Coordinates are in decimal metres, **not** millimetre integers. Using integer mm values causes command rejection (`$PAIR001,PQTMCFGSVIN,1`).

### 9.4 ECEF Coordinate Extraction from RTCM 1005

Survey completion is signalled by the first RTCM3 message 1005 after `min_duration`. The ECEF coordinates are extracted from this message (not from a PAIR query):

- Each coordinate: 38-bit signed integer, scaled 0.0001 m
- Bit offsets: X at bit 34, Y at bit 74, **Z at bit 114** (NOT 113 — bits 112-113 are QuarterCycleIndicator DF364)
- Value in metres: `coord_m = int38 * 0.0001`

### 9.5 ECEF Coordinate Conversion (geodetic fallback)

Survey-In completion triggers a conversion from geodetic (lat/lon/alt) to ECEF using the WGS-84 ellipsoid:

```
a = 6378137.0          (semi-major axis, metres)
f = 1/298.257223563    (flattening)
e² = 2f - f²           (eccentricity squared)

N(φ) = a / sqrt(1 - e² sin²φ)

X = (N + h) cos(φ) cos(λ)
Y = (N + h) cos(φ) sin(λ)
Z = (N(1 - e²) + h) sin(φ)
```

---

## 10. Web API

Base URL: `http://mowerbase.local:8080`

All API endpoints return `Content-Type: application/json`.

### 10.1 Page Routes

| Method | Path | Description |
|---|---|---|
| GET | `/` | Dashboard page |
| GET | `/config` | Survey-In and SiK radio configuration page |
| GET | `/position` | Position management page |
| GET | `/network` | WiFi configuration page |
| GET | `/rtcm` | RTCM statistics page (per-type counts, bytes/sec, frame log) |
| GET | `/logs` | Log viewer page |
| GET | `/swagger` | Interactive API documentation (Swagger UI) |

### 10.2 State API

**`GET /api/state`**

Returns the current state.json contents plus a server timestamp.

Response:
```json
{
  "gps": { ... },
  "sik": { ... },
  "rtcm": { ... },
  "_timestamp": "2026-03-02T09:14:22Z"
}
```

**`GET /api/survey/status`**

Dedicated clean endpoint for Survey-In status (used by position page polling).

Response:
```json
{
  "sm_state": "SURVEYING",
  "elapsed_s": 47,
  "min_duration_s": 60,
  "progress_pct": 78,
  "stored_position": { ... }
}
```

**`GET /api/drift/history`**

Returns last 24 hours of drift measurements from SQLite.

**`GET /api/openapi.json`**

Returns OpenAPI 3.0 specification for all REST endpoints. Used by Swagger UI.

### 10.3 Log Streaming

**`GET /api/logs/stream?service={service}&level={level}`**

Server-Sent Events stream. On connect, immediately emits the last 50 lines of the selected log as `[history]`-prefixed events, then switches to live tail mode (0.1s poll).

Query parameters:
- `service`: `gps` | `sik` | `web` | `all` (default: `gps`)
- `level`: `DEBUG` | `INFO` | `WARNING` | `ERROR` | `` (empty = all)

Flask must be started with `threaded=True`. Without threading, a live SSE connection blocks all other HTTP requests.

**`GET /api/logs/download?service={service}`**

Returns log content as `text/plain` download. When `service=all`, concatenates all log files.

### 10.4 Restart API

**`POST /api/restart`**

Restarts all three MowerBase services. Uses a 1-second delay (subprocess Popen) so the JSON response reaches the browser before web.py is killed. The web UI shows a 15-second countdown then reloads automatically.

Requires `/etc/sudoers.d/mowerbase` to grant the mowerbase user passwordless `systemctl restart` for the three services.

Response: `{"ok": true, "message": "Restarting all services..."}` or `{"ok": false, "error": "..."}`

### 10.5 Configuration API

**`POST /api/config`**

Body:
```json
{
  "survey": {
    "min_duration_seconds": 60,
    "accuracy_limit_m": 2.0
  },
  "sik": {
    "port": "/dev/ttyUSB0",
    "baud": 57600
  }
}
```

On success: writes config.json, sends SIGUSR1 to gps.py.

Response: `{"ok": true}` or `{"ok": false, "error": "..."}`

### 10.6 Position API

**`POST /api/resurvey`**

Sends SIGUSR2 to gps.py to trigger a re-survey.

Response: `{"ok": true, "message": "Re-survey triggered"}`

**`POST /api/resurvey/accept`**

Sets `state.json["resurvey_action"] = "accept"` for gps.py to pick up.

**`POST /api/resurvey/reject`**

Sets `state.json["resurvey_action"] = "reject"` for gps.py to pick up.

### 10.7 WiFi API

**`GET /api/wifi/scan`**

Runs `nmcli dev wifi list` and returns:
```json
{
  "ok": true,
  "networks": [
    {"ssid": "MyNetwork", "signal": "75"},
    ...
  ]
}
```

**`POST /api/wifi/save`**

Body: `{"ssid": "MyNetwork", "password": "secret"}`

Runs `nmcli dev wifi connect {ssid} password {password}`.

Response: `{"ok": true, "message": "Connected to MyNetwork"}` or error.

**`POST /api/captive-portal`**

Triggers captive portal mode (AP broadcast + DNS redirect).

---

## 11. Configuration Schema

File: `/etc/mowerbase/config.json`
Permissions: `640`, owner `mowerbase:mowerbase`
Written by: web.py
Read by: all services on start and on SIGUSR1

```jsonc
{
  "survey": {
    "min_duration_seconds": 60,          // integer, minimum 60
    "accuracy_limit_m": 2.0              // float, metres
  },
  "drift": {
    "alert_threshold_m": 0.5,            // float, metres (GPS-only: 0.5m appropriate threshold)
    "check_interval_minutes": 5          // integer
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
  },
  "oled": {
    "i2c_address": "0x3C"               // string: "0x3C" or "0x3D"
  },
  "led": {
    "gpio_pin": 17                       // BCM pin number
  },
  "button": {
    "gpio_pin": 27                       // BCM pin number
  }
}
```

---

## 12. Persisted Data Schemas

### 12.1 position.json

File: `/etc/mowerbase/position.json`
Written by: `gps.py` only (never by other services or manually)
Read by: `gps.py` at boot

```json
{
  "ecef_x": -3935857.5283,
  "ecef_y": 3440073.8749,
  "ecef_z": -3642935.1065,
  "lat": -35.0534123,
  "lon": 138.8454456,
  "alt_m": 312.4,
  "accuracy_m": 0.014,
  "survey_duration_s": 487,
  "sample_count": 487,
  "surveyed_at": "2026-03-02T09:14:22+00:00"
}
```

All ECEF coordinates in metres (4 decimal places = 0.1mm precision). All geodetic coordinates in decimal degrees (7 decimal places = ~1cm precision).

### 12.2 SQLite Database

File: `/var/lib/mowerbase/history.db`

**Table: `drift_history`**

| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Autoincrement |
| timestamp | TEXT | ISO 8601 UTC |
| delta_x | REAL | ECEF X drift (metres) |
| delta_y | REAL | ECEF Y drift (metres) |
| delta_z | REAL | ECEF Z drift (metres) |
| total_3d | REAL | 3D Euclidean displacement (metres) |

Written every 5 minutes while in FIXED state.

**Table: `survey_history`**

| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Autoincrement |
| started_at | TEXT | ISO 8601 UTC |
| completed_at | TEXT | ISO 8601 UTC |
| accuracy_m | REAL | Final 3D accuracy |
| duration_s | INTEGER | Survey duration |
| ecef_x, ecef_y, ecef_z | REAL | Final ECEF position |
| lat, lon | REAL | Final geodetic position |
| alt_m | REAL | Final altitude |

Written once per completed survey.

**Table: `events`**

| Column | Type | Description |
|---|---|---|
| id | INTEGER PK | Autoincrement |
| timestamp | TEXT | ISO 8601 UTC |
| event_type | TEXT | e.g. `resurvey_accepted`, `drift_alert` |
| message | TEXT | Human-readable detail |

---

## 13. LED and OLED Specification

### 13.1 LED State Mapping

`web.py` computes the LED state from `state.json` every 2 seconds:

```
sm_state == "FIXED" AND drift_alert == true  →  "drift_alert"
sm_state == "FIXED"                           →  "fixed_ok"
sm_state == "SURVEYING"                       →  "surveying"
sm_state == "BOOT"                            →  "boot"
sm_state == "NO_WIFI"                         →  "captive_portal"
otherwise                                     →  "no_fix"
```

### 13.2 LED Blink Patterns

| State name | On (s) | Off (s) | Frequency |
|---|---|---|---|
| `fixed_ok` | 9999 (solid) | 0 | — |
| `surveying` | 0.125 | 0.125 | 4 Hz |
| `drift_alert` | 0.25 | 0.25 | 2 Hz |
| `no_fix` | 9999 (solid) | 0 | — |
| `captive_portal` | 1.0 | 1.0 | 0.5 Hz |
| `boot` | 0.25 | 0.25 | 2 Hz |
| `off` | 0 | 9999 | — |

The LED blink thread wakes every 50ms to check for state changes. State transitions take effect within one 50ms tick.

### 13.3 OLED Screens

The OLED is updated by `oled.update(state)` called from the web.py background thread every 2 seconds. The screen is chosen based on `state["gps"]["sm_state"]`:

**FIXED screen:**
```
Row 0:  "MowerBase     [FIXED]"
Row 1:  "Sats: {num_sats}   Alt: {alt_m:.1f}m"
Row 2:  "Drift: {drift_mm:+.0f}mm"
Row 3:  "RTCM:{rtcm_kbps:.1f}k/s SiK:{OK|NC}"
```

**SURVEYING screen:**
```
Row 0:  "MowerBase  [SURVEYING]"
Rows 1–2: progress bar (filled proportion = elapsed / target)
Row 2b: "{elapsed_min}:{elapsed_sec:02d} / {target_min}:{target_sec:02d}"
Row 3:  "Accuracy: {accuracy_m:.1f}m  (target)"
Row 4:  "Sats:{num_sats}"
```

**NO_WIFI screen:**
```
Row 0:  "MowerBase   [NO WIFI]"
Row 1:  "Connect to WiFi:"
Row 2:  "SSID: MowerBase-Setup"
Row 3:  "Then: 192.168.4.1"
```

All text rendered using the luma.oled default bitmap font. No anti-aliasing.

---

## 14. Captive Portal Specification

### 14.1 Trigger Conditions

Captive portal mode is triggered by:
1. GPIO 27 held low for ≥ 1.0 second
2. `POST /api/captive-portal` from web UI

### 14.2 AP Configuration

| Parameter | Value |
|---|---|
| SSID | `MowerBase-Setup` |
| Security | Open (no password) |
| Pi AP IP | `192.168.4.1` |
| DHCP range | `192.168.4.2` – `192.168.4.20` |
| DNS redirect | All queries → `192.168.4.1` |
| Implementation | `hostapd` (AP) + `dnsmasq` (DHCP/DNS) |

### 14.3 Captive Portal Flow

```
1. Pi starts AP via hostapd
2. dnsmasq provides DHCP and resolves all hostnames to 192.168.4.1
3. Flask serves captive portal HTML at 192.168.4.1:8080
4. User submits SSID + password form
5. web.py calls: nmcli dev wifi connect {ssid} password {password}
6. On success:
   a. Stop hostapd and dnsmasq
   b. Pi switches to station mode and joins the network
   c. avahi-daemon broadcasts mowerbase.local
   d. OLED shows "Connected — mowerbase.local:8080"
```

### 14.4 OLED During Captive Portal

While captive portal is active, `sm_state` is set to `NO_WIFI` and the OLED shows the setup instructions screen (see Section 14.3).

---

## 15. Error Handling and Recovery

### 15.1 Service-Level Recovery

All three services have `Restart=on-failure, RestartSec=5` in their systemd units. A service that crashes will be restarted after 5 seconds.

`StartLimitBurst=5` over `StartLimitIntervalSec=60` means: if a service crashes 5 times in 60 seconds, systemd stops restarting it. This prevents crash loops consuming CPU. Manual intervention (`systemctl start`) is then required.

### 15.2 GPS Serial Recovery

If the GPS serial port raises a `SerialException`, `gps.py` closes the port, logs the error, and sleeps 5 seconds before attempting to reopen. The byte-stream reader resumes automatically on reconnect.

### 15.3 SiK Radio Recovery

`sik_forwarder.py` never crashes due to radio absence:
- No radio at startup: log warning, **drain pipe** (read+discard) for 30 seconds then retry
- Radio disconnects during operation: `SerialException` caught, port closed, retry loop entered
- Pipe drain during retry is mandatory — without it, the 64KB kernel pipe buffer fills and `gps.py`'s O_NONBLOCK write starts dropping RTCM frames

### 15.4 RTCM Pipe Deadlock Prevention

Two mechanisms prevent `gps.py` from blocking on the RTCM named pipe:
1. **O_NONBLOCK on write fd**: `gps.py` sets `O_NONBLOCK` on the pipe write fd via `fcntl.F_SETFL`. A full pipe raises `BlockingIOError` instead of blocking — the frame is silently dropped.
2. **Pipe drain in sik_forwarder**: During the 30-second radio retry wait, `sik_forwarder.py` continuously reads and discards pipe data to keep the buffer empty.

Without both mechanisms, a missing SiK radio fills the pipe within seconds (~64KB at typical RTCM data rates), then `gps.py`'s main loop freezes indefinitely — blocking SIGUSR2 handling and making the re-survey button non-functional.

### 15.5 Hardware Absence

`led.py` and `oled.py` catch `ImportError` for `RPi.GPIO` and `luma.oled` respectively. If the library is missing (dev machine, no hardware) the modules initialise in stub mode: all function calls are no-ops. The rest of the software runs normally.

OLED and LED init failures (I2C error, GPIO busy) are logged as warnings and the module continues in stub mode. They do not propagate exceptions to the calling service.

---

## 16. Security Considerations

### 16.1 Configuration Storage

`/etc/mowerbase/config.json` has permissions `640` (owner `mowerbase`, group `mowerbase`). The file is not world-readable.

### 16.2 Network Exposure

The web server binds to `0.0.0.0:8080` (all interfaces). There is no authentication on the web UI. This is acceptable for a device expected to be on a private home network. If exposure to an untrusted network is a concern, a reverse proxy with HTTP Basic Auth can be added in front of Flask.

No outbound connections are made by MowerBase (NTRIP removed). The only inbound port is 8080.

### 16.3 Captive Portal

The captive portal AP is open (no WPA). It is intended as a short-duration setup mechanism, not a permanent access point. The hostapd service is stopped as soon as WiFi credentials are saved successfully.

### 16.4 Command Injection

Web API endpoints that accept SSID and password strings pass them to `nmcli` via `subprocess.run()` with arguments as a list (not a shell string). This prevents shell injection. No other user-supplied data is passed to shell commands.

### 16.5 Path Traversal

Log download (`/api/logs/download`) selects log files from a fixed dict (`LOG_FILES`) keyed by service name. The `service` query parameter is validated against this dict before use. Arbitrary paths cannot be requested.

---

## 17. Constraints and Assumptions

### 17.1 Hardware Constraints

- Pi Zero W v1 is ARMv6 32-bit, single-core BCM2835 at 1GHz with 512MB RAM. **Confirmed working** with all three services running concurrently. Install takes ~10–15 minutes due to pip source builds on ARMv6 (no pre-built wheels for luma.oled on ARMv6).
- **Must use Raspberry Pi OS Lite**, not Desktop. The Desktop edition runs a Wayfire compositor that consumes most RAM and CPU, making the device unusable for this workload.
- The Pi Zero W has a single micro-USB data port (closest to SD card slot). Only one USB device (the SiK radio) can be connected without a USB hub. If a hub is added, `/dev/ttyUSB0` assignment may change. Power port is the other micro-USB connector (furthest from SD card).
- `/dev/ttyAMA0` is the PL011 hardware UART. On Bookworm, it is assigned to Bluetooth by default. `dtoverlay=disable-bt` in `/boot/firmware/config.txt` reassigns it to GPIO 14/15. This must be done before install.

### 17.2 GPS Assumptions

- **Survey-In completion** is detected by the first RTCM3 message 1005 received after `min_duration` elapsed. The LC29H(BS) only emits 1005 after it internally reaches Fixed mode — this is the authoritative completion signal AND the source of ECEF coordinates.
- **ECEF during Survey-In**: `$PQTMCFGSVIN,R` returns 0.0000 for ECEF while Survey-In is active. Do not attempt to use it for a convergence metric.
- The module outputs RTCM3 binary as its primary output during Survey-In and Fixed mode. NMEA appears occasionally. The serial stream is mixed; use byte-stream reader branching on `0xD3` / `0x24`.
- GPS-only Survey-In achieves ~1–3m absolute accuracy. This is sufficient because waypoints are collected relative to the same base — absolute error cancels out.

### 17.3 Operational Assumptions

- The base station antenna is placed in a fixed outdoor location with a clear sky view before Survey-In.
- The antenna is not moved between sessions. Moving the antenna after a survey requires a new survey.
- The mower operates within ≈200m of the base station, within the effective range of the Holybro 100mW SiK radio.
- The mower's Pixhawk TELEM1 port is configured to receive RTCM corrections. This is outside MowerBase's scope.

### 17.4 Software Constraints

- Python 3 only. The codebase uses type hints with `|` union syntax (Python 3.10+). Bookworm ships Python 3.11.
- All services run under the `mowerbase` system user (no root). GPIO access requires the `gpio` group; serial access requires `dialout`.
- Flask's built-in development server is used (`app.run()`). For production use, a WSGI server such as gunicorn should be considered, but is not required at this scale.

---

## 18. Known Issues

| Issue | Impact | Status |
|---|---|---|
| OLED display not yet tested on hardware | Display may not work until tested | Not started |
| LED not yet tested on hardware | LED blink states unverified | Not started |
| Captive portal not implemented | WiFi must be configured manually via raspi-config or nmcli | Planned |
| Re-survey button blocked when SiK radio absent | Fixed in v1.2: sik_forwarder now drains pipe + gps.py uses O_NONBLOCK writes | Fixed |

---

*MowerBase Functional and Technical Specification*
*Version 1.2 — 2026-03-06*
*Changes from v1.1: NTRIP removed (GPS-only Survey-In); 3 services; RTCM pipe deadlock fix; updated Survey-In detection (RTCM 1005); corrected PAIR command formats; corrected ECEF-zero-during-survey behaviour; re-survey threshold 5cm → 50cm; added /rtcm, /swagger, /api/survey/status endpoints; satellite count convergence chart.*
