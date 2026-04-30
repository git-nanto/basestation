"""
web.py — Flask web server for BaseStation.

Routes:
  GET  /                  Dashboard
  GET  /config            Settings (survey duration, SiK port)
  GET  /position          Position management
  GET  /network           WiFi configuration
  GET  /logs              Log viewer

API endpoints:
  GET  /api/state         Current state.json as JSON
  GET  /api/logs/stream   SSE log stream
  GET  /api/wifi/scan     Scan for WiFi networks
  POST /api/config/save   Save settings + reload gps.py
  POST /api/resurvey      Trigger re-survey (SIGUSR2 to gps.py)
  POST /api/resurvey/accept   Accept pending re-survey result
  POST /api/resurvey/reject   Reject pending re-survey result
  POST /api/wifi/save     Save WiFi credentials + reconnect

Runs on port 80 (via authbind — see install.sh).
Updates OLED every 2 seconds via background thread.
Controls LED via led.py.
Handles GPIO 27 button via interrupt (1s hold).
"""

import os
import sys
import json
import time
import signal
import logging
import sqlite3
import subprocess
import threading
import queue as _queue
import collections
from pathlib import Path
from datetime import datetime, timezone, timedelta

from flask import (
    Flask, render_template, jsonify, request,
    Response, stream_with_context, redirect, url_for
)

import state
import led
import oled

# ── Paths ─────────────────────────────────────────────────────────────────────
CONFIG_FILE = "/etc/mowerbase/config.json"
POSITION_FILE = "/etc/mowerbase/position.json"
HISTORY_DB = "/var/lib/mowerbase/history.db"
GPS_PID_FILE = "/run/mowerbase/gps.pid"
NTRIP_PID_FILE = "/run/mowerbase/ntrip.pid"
LOG_FILES = {
    "gps":   "/var/log/mowerbase/gps.log",
    "sik":   "/var/log/mowerbase/sik.log",
    "web":   "/var/log/mowerbase/web.log",
    "ntrip": "/var/log/mowerbase/ntrip.log",
}
LOG_FILE = "/var/log/mowerbase/web.log"

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("/var/log/mowerbase", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] web: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("web")

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.urandom(24)

# Graceful GPIO fallback
try:
    import RPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except ImportError:
    _GPIO_AVAILABLE = False
    log.warning("RPi.GPIO not available — GPIO button disabled")

# ── Default config ────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "correction_source": "gps",
    "survey": {"min_duration_seconds": 60, "accuracy_limit_m": 2.0},
    "drift": {"alert_threshold_m": 0.5, "check_interval_minutes": 5},
    "sik": {"port": "/dev/ttyUSB0", "baud": 57600},
    "gps": {"port": "/dev/ttyAMA0", "baud": 115200},
    "web": {"port": 80, "hostname": "mowerbase"},
    "oled": {"i2c_address": "0x3C"},
    "led": {"gpio_pin": 17},
    "button": {"gpio_pin": 27},
    "ntrip_client": {
        "host": "",
        "port": 2101,
        "mountpoint": "",
        "username": "",
        "password": "",
    },
    "ntrip_server": {
        "enabled": True,
        "port": 2101,
        "mountpoint": "MOWERBASE",
    },
}


# ── Config helpers ────────────────────────────────────────────────────────────
def load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return dict(DEFAULT_CONFIG)
    except Exception as e:
        log.error("Config load error: %s", e)
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_FILE)


def _read_pid_file(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _send_signal(pid_file: str, sig: int) -> bool:
    pid = _read_pid_file(pid_file)
    if pid:
        try:
            os.kill(pid, sig)
            return True
        except ProcessLookupError:
            return False
    return False


# ── LED state mapping ─────────────────────────────────────────────────────────
def _compute_led_state(sys_state: dict) -> str:
    sm = sys_state.get("gps", {}).get("sm_state", "BOOT")
    drift_alert = sys_state.get("gps", {}).get("drift_alert", False)

    if sm == "FIXED":
        return "drift_alert" if drift_alert else "fixed_ok"
    elif sm == "SURVEYING":
        return "surveying"
    elif sm == "BOOT":
        return "boot"
    elif sm == "NO_WIFI":
        return "captive_portal"
    else:
        return "no_fix"


# ── Background thread: OLED update + LED control ──────────────────────────────
_oled_thread_running = False


_wifi_check_counter = 0

def _oled_updater() -> None:
    global _oled_thread_running, _wifi_check_counter
    while _oled_thread_running:
        try:
            sys_state = state.read_state()
            # Update wifi.home_connected every ~5s (every 2-3 cycles)
            _wifi_check_counter += 1
            if _wifi_check_counter >= 3:
                _wifi_check_counter = 0
                home_ssid = _get_current_wifi_ssid()
                state.update_state({"wifi": {"home_connected": bool(home_ssid), "ssid": home_ssid}})
                sys_state = state.read_state()
            oled.update(sys_state)
            led_state = _compute_led_state(sys_state)
            led.set_state(led_state)
        except Exception as e:
            log.error("OLED/LED update error: %s", e)
        time.sleep(2)


# ── GPIO Button ───────────────────────────────────────────────────────────────
_button_press_time: float | None = None
BUTTON_HOLD_SECONDS = 1.0


def _button_pressed(channel):
    global _button_press_time
    _button_press_time = time.monotonic()


def _button_released(channel):
    global _button_press_time
    if _button_press_time is not None:
        held = time.monotonic() - _button_press_time
        _button_press_time = None
        if held >= BUTTON_HOLD_SECONDS:
            log.info("GPIO button held %.1fs (no action configured)", held)


def _setup_gpio_button(pin: int) -> None:
    if not _GPIO_AVAILABLE:
        return
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.add_event_detect(
            pin, GPIO.FALLING, callback=_button_pressed, bouncetime=200
        )
        GPIO.add_event_detect(
            pin, GPIO.RISING, callback=_button_released, bouncetime=200
        )
        log.info("GPIO button configured on pin %d", pin)
    except Exception as e:
        log.warning("GPIO button setup failed: %s", e)


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
def dashboard():
    sys_state = state.read_state()
    cfg = load_config()
    return render_template("index.html", state=sys_state, config=cfg)


@app.route("/config")
def config_page():
    cfg = load_config()
    return render_template("config.html", config=cfg)


@app.route("/position")
def position_page():
    sys_state = state.read_state()
    cfg = load_config()
    position = None
    try:
        with open(POSITION_FILE) as f:
            position = json.load(f)
    except FileNotFoundError:
        pass
    return render_template("position.html", state=sys_state, config=cfg, position=position)


@app.route("/network")
def network_page():
    cfg = load_config()
    wifi_ssid = _get_current_wifi_ssid()
    ap_status = _get_ap_status()
    return render_template("network.html", config=cfg, wifi_ssid=wifi_ssid, ap_status=ap_status)


@app.route("/logs")
def logs_page():
    return render_template("logs.html", log_files=list(LOG_FILES.keys()))


@app.route("/rtcm")
def rtcm_page():
    return render_template("rtcm.html")


@app.route("/swagger")
def swagger_page():
    return render_template("swagger.html")


# ── Captive portal detection ──────────────────────────────────────────────────
# When a phone/laptop connects to the BaseStation AP, the OS probes these URLs.
# Returning the OS-expected response triggers the captive portal browser popup.

@app.route("/generate_204")          # Android
def captive_android():
    return Response("", status=204)

@app.route("/hotspot-detect.html")   # Apple / macOS
def captive_apple():
    return Response("<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>", status=200, mimetype="text/html")

@app.route("/ncsi.txt")              # Windows
def captive_windows():
    return Response("Microsoft NCSI", status=200, mimetype="text/plain")

@app.route("/connecttest.txt")       # Windows 10+
def captive_windows10():
    return Response("Microsoft Connect Test", status=200, mimetype="text/plain")


# ── API: State ────────────────────────────────────────────────────────────────
@app.route("/api/state")
def api_state():
    sys_state = state.read_state()
    sys_state["_timestamp"] = datetime.utcnow().isoformat() + "Z"
    return jsonify(sys_state)


# ── API: Survey status ────────────────────────────────────────────────────────
@app.route("/api/survey/status")
def api_survey_status():
    """Clean survey-in status endpoint — easier to poll than /api/state."""
    s = state.read_state()
    g = s.get("gps", {})
    sm = g.get("sm_state", "UNKNOWN")
    elapsed = g.get("svin_elapsed_s", 0)
    min_dur = g.get("svin_min_duration_s", 60)
    progress = min(100, round(elapsed / max(min_dur, 1) * 100)) if sm == "SURVEYING" else (100 if sm in ("FIXED", "RESURVEY_PENDING") else 0)
    return jsonify({
        "sm_state": sm,
        "is_surveying": sm == "SURVEYING",
        "is_fixed": sm in ("FIXED", "RESURVEY_PENDING"),
        "elapsed_s": elapsed,
        "min_duration_s": min_dur,
        "accuracy_limit_m": g.get("svin_accuracy_limit_m"),
        "accuracy_m": g.get("accuracy_m"),
        "num_sats": g.get("num_sats", 0),
        "progress_pct": progress,
        "stored_position": g.get("stored_position"),
        "_timestamp": datetime.utcnow().isoformat() + "Z",
    })


# ── API: OpenAPI spec ─────────────────────────────────────────────────────────
@app.route("/api/openapi.json")
def api_openapi():
    """OpenAPI 3.0 spec — consumed by /swagger Swagger UI page."""
    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "BaseStation API",
            "version": "1.0.0",
            "description": (
                "RTK Base Station control and monitoring API.\n\n"
                "All POST endpoints accept and return `application/json`. "
                "All timestamps are ISO-8601 UTC strings unless noted as Unix floats.\n\n"

                "---\n\n"
                "## Survey-In Background\n\n"
                "Survey-In is GPS-only (no NTRIP corrections). The LC29H(BS) averages its "
                "position over time until its internal accuracy estimate reaches the configured "
                "`accuracy_limit_m`. It then self-declares Fixed mode and begins emitting RTCM3 "
                "corrections.\n\n"

                "**GPS-only absolute accuracy is ~1–3 m**, but this does not matter for mowing — "
                "waypoints are collected against the same base, so absolute error cancels out. "
                "Only relative accuracy (base → mower) matters, and RTK delivers that regardless.\n\n"

                "**Recommended `accuracy_limit_m`:** 3.0 m. Values below 2.0 m may never be "
                "reached with GPS-only in typical suburban sky conditions.\n\n"

                "---\n\n"
                "## $PQTMCFGSVIN,R*26 — Read Command\n\n"
                "Queried every 10 seconds during Survey-In. "
                "Response format:\n\n"
                "```\n"
                "$PQTMCFGSVIN,OK,<Mode>,<MinDur>,<3D_AccLimit>,<ECEFx>,<ECEFy>,<ECEFz>*<chk>\n"
                "```\n\n"
                "| Field | Position | Meaning |\n"
                "|---|---|---|\n"
                "| Mode | parts[2] | 1 = Survey-In active, 2 = Fixed |\n"
                "| MinDur | parts[3] | Configured minimum survey duration (s) |\n"
                "| 3D_AccLimit | parts[4] | **Configured accuracy limit** — NOT live accuracy |\n"
                "| ECEFx | parts[5] | Running-average ECEF X (decimal metres) — updates each query |\n"
                "| ECEFy | parts[6] | Running-average ECEF Y (decimal metres) — updates each query |\n"
                "| ECEFz | parts[7] | Running-average ECEF Z (decimal metres) — updates each query |\n\n"
                "The module provides **no live accuracy field** via PAIR commands. "
                "BaseStation derives a convergence indicator by computing the 3D delta between "
                "consecutive ECEF samples (parts[5..7]). As the running average stabilises, "
                "this delta approaches zero. Returned in `svin_convergence` as `[{elapsed_s, delta_m}]`.\n\n"

                "---\n\n"
                "## RTCM 1005 — Survey Completion Signal\n\n"
                "RTCM3 message **1005** (Stationary RTK Reference Station ARP) is **only emitted "
                "after the module self-declares Fixed mode**. It never appears during active Survey-In.\n\n"
                "Its first appearance is the survey completion trigger in `gps.py`. "
                "The module contains the current averaged ECEF base position, parsed from "
                "38-bit signed integers scaled 0.0001 m at bit offsets:\n\n"
                "- **ECEF-X:** bit 34\n"
                "- **ECEF-Y:** bit 74\n"
                "- **ECEF-Z:** bit 114** (NOT 113 — bits 112–113 are QuarterCycleIndicator DF364)\n\n"
                "If RTCM 1005 never appears, the configured `accuracy_limit_m` is too tight for "
                "current sky conditions. Increase it via `POST /api/config/save` and trigger a "
                "re-survey via `POST /api/resurvey`.\n\n"

                "---\n\n"
                "## RTCM MSM4 Messages\n\n"
                "Messages 1074 (GPS), 1084 (GLONASS), 1094 (Galileo), 1124 (BeiDou) are "
                "Multi-Signal Message type 4 — satellite observations used by the rover for RTK. "
                "The 64-bit satellite mask is at bit offset **73** (NOT 36). Count set bits for "
                "satellite count per constellation."
            ),
        },
        "servers": [{"url": "", "description": "This device"}],
        "tags": [
            {"name": "state",   "description": "Live system state"},
            {"name": "survey",  "description": "Survey-In control"},
            {"name": "config",  "description": "Configuration"},
            {"name": "network", "description": "WiFi management"},
            {"name": "system",  "description": "Service management and logs"},
        ],
        "paths": {
            "/api/state": {
                "get": {
                    "tags": ["state"],
                    "summary": "Full system state",
                    "description": "Returns the complete state.json including GPS, RTCM stats, SiK status, and system uptime.",
                    "responses": {
                        "200": {
                            "description": "System state",
                            "content": {"application/json": {"schema": {
                                "type": "object",
                                "properties": {
                                    "gps": {
                                        "type": "object",
                                        "description": "GPS / survey state",
                                        "properties": {
                                            "sm_state":    {"type": "string", "enum": ["BOOT", "SURVEYING", "FIXED", "RESURVEY_PENDING", "ERROR", "GPS_NOT_FOUND"]},
                                            "serial_ok":   {"type": "boolean", "description": "True once /dev/ttyAMA0 opens; False (and sm_state=GPS_NOT_FOUND) after 5 failed retries"},
                                            "lat":         {"type": "number", "nullable": True},
                                            "lon":         {"type": "number", "nullable": True},
                                            "alt_m":       {"type": "number", "nullable": True},
                                            "num_sats":    {"type": "integer"},
                                            "accuracy_m":  {"type": "number", "nullable": True},
                                            "drift_current_m": {"type": "number"},
                                            "drift_alert": {"type": "boolean"},
                                        },
                                    },
                                    "wifi": {
                                        "type": "object",
                                        "description": "Home WiFi connection state (updated every ~6 s)",
                                        "properties": {
                                            "home_connected": {"type": "boolean", "description": "True when connected to a home WiFi network (not the BaseStation AP)"},
                                            "ssid":           {"type": "string",  "description": "SSID of connected home network, or empty string"},
                                        },
                                    },
                                    "rtcm": {"type": "object", "description": "RTCM message stats (counts_10s, bytes_per_sec, frame_rate, last_1005_ts, recent_frames)"},
                                    "sik":  {"type": "object", "description": "SiK radio status (connected, bytes_per_sec, error)"},
                                    "_timestamp": {"type": "string"},
                                },
                            }}},
                        }
                    },
                }
            },
            "/api/survey/status": {
                "get": {
                    "tags": ["survey"],
                    "summary": "Survey-In status",
                    "description": (
                        "Clean survey status endpoint. Poll this during Survey-In instead of parsing /api/state.\n\n"
                        "**`sm_state` values:**\n"
                        "- `SURVEYING` — GPS averaging in progress. Module queried every 10 s via `$PQTMCFGSVIN,R*26`. "
                        "ECEF delta between consecutive samples returned in parent `/api/state` as `gps.svin_convergence`.\n"
                        "- `FIXED` — Survey complete. Module emitted RTCM 1005 (first time after min_duration). "
                        "Stored position written to `/etc/mowerbase/position.json`.\n"
                        "- `RESURVEY_PENDING` — Re-survey completed but new position differs ≥ 50 cm from stored. "
                        "Manual accept/reject required.\n"
                        "- `ERROR` — No RTCM 1005 received within 5 minutes of survey start. "
                        "Check antenna sky view or increase `accuracy_limit_m`."
                    ),
                    "responses": {
                        "200": {
                            "description": "Survey status",
                            "content": {"application/json": {"schema": {
                                "type": "object",
                                "properties": {
                                    "sm_state":         {"type": "string", "enum": ["BOOT", "SURVEYING", "FIXED", "RESURVEY_PENDING", "ERROR", "GPS_NOT_FOUND"]},
                                    "is_surveying":     {"type": "boolean"},
                                    "is_fixed":         {"type": "boolean"},
                                    "elapsed_s":        {"type": "integer", "description": "Seconds since survey started"},
                                    "min_duration_s":   {"type": "integer"},
                                    "accuracy_limit_m": {"type": "number", "description": "Configured accuracy threshold (metres)"},
                                    "accuracy_m":       {"type": "number", "nullable": True},
                                    "num_sats":         {"type": "integer"},
                                    "progress_pct":     {"type": "integer", "description": "0-100 time progress (not accuracy)"},
                                    "stored_position":  {"type": "object", "nullable": True},
                                    "_timestamp":       {"type": "string"},
                                },
                            }}},
                        }
                    },
                }
            },
            "/api/drift/history": {
                "get": {
                    "tags": ["state"],
                    "summary": "Drift history (24 h)",
                    "description": "Returns drift readings from the last 24 hours from history.db.",
                    "responses": {
                        "200": {
                            "description": "Drift readings",
                            "content": {"application/json": {"schema": {
                                "type": "object",
                                "properties": {
                                    "ok": {"type": "boolean"},
                                    "readings": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "ts":       {"type": "string"},
                                                "total_3d": {"type": "number", "description": "3D displacement in metres"},
                                            },
                                        },
                                    },
                                },
                            }}},
                        }
                    },
                }
            },
            "/api/resurvey": {
                "post": {
                    "tags": ["survey"],
                    "summary": "Trigger re-survey",
                    "description": (
                        "Sends SIGUSR2 to gps.py to start a new Survey-In. "
                        "Device must be stationary with clear sky view. RTCM output is interrupted until the new survey completes.\n\n"
                        "Survey completes when the module emits RTCM 1005 (after `min_duration_seconds` and internal accuracy reaches `accuracy_limit_m`). "
                        "If the new surveyed position differs < 50 cm from stored, it is auto-accepted. "
                        "If ≥ 50 cm, `sm_state` becomes `RESURVEY_PENDING` and manual accept/reject is required.\n\n"
                        "If survey never completes, increase `accuracy_limit_m` via `POST /api/config/save` "
                        "(recommended: 3.0 m for GPS-only)."
                    ),
                    "responses": {
                        "200": {"description": "Re-survey triggered", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OkResponse"}}}},
                        "500": {"description": "GPS service not running"},
                    },
                }
            },
            "/api/resurvey/accept": {
                "post": {
                    "tags": ["survey"],
                    "summary": "Accept pending re-survey result",
                    "description": "When sm_state is RESURVEY_PENDING (delta >= 50 cm), call this to adopt the new position.",
                    "responses": {
                        "200": {"description": "Accepted", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OkResponse"}}}},
                    },
                }
            },
            "/api/resurvey/reject": {
                "post": {
                    "tags": ["survey"],
                    "summary": "Reject pending re-survey result",
                    "description": "When sm_state is RESURVEY_PENDING, call this to keep the existing stored position.",
                    "responses": {
                        "200": {"description": "Rejected", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OkResponse"}}}},
                    },
                }
            },
            "/api/config/save": {
                "post": {
                    "tags": ["config"],
                    "summary": "Save configuration",
                    "description": "Saves survey and SiK settings to /etc/mowerbase/config.json and signals gps.py to reload (SIGUSR1).",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "properties": {
                                "survey": {
                                    "type": "object",
                                    "properties": {
                                        "min_duration_seconds": {"type": "integer", "example": 60},
                                        "accuracy_limit_m":     {"type": "number",  "example": 3.0},
                                    },
                                },
                                "sik": {
                                    "type": "object",
                                    "properties": {
                                        "port": {"type": "string", "example": "/dev/ttyUSB0"},
                                        "baud": {"type": "integer", "example": 57600},
                                    },
                                },
                            },
                        }}},
                    },
                    "responses": {
                        "200": {"description": "Saved", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OkResponse"}}}},
                        "400": {"description": "Bad request"},
                        "500": {"description": "Save error"},
                    },
                }
            },
            "/api/restart": {
                "post": {
                    "tags": ["system"],
                    "summary": "Restart all services",
                    "description": "Restarts mowerbase-gps, mowerbase-sik, and mowerbase-web via systemctl. Response arrives before the restart — expect ~2 s delay then reconnect.",
                    "responses": {
                        "200": {"description": "Restart triggered", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OkResponse"}}}},
                        "500": {"description": "Restart failed"},
                    },
                }
            },
            "/api/wifi/scan": {
                "get": {
                    "tags": ["network"],
                    "summary": "Scan WiFi networks",
                    "description": "Returns visible WiFi SSIDs and signal strengths via nmcli.",
                    "responses": {
                        "200": {
                            "description": "Network list",
                            "content": {"application/json": {"schema": {
                                "type": "object",
                                "properties": {
                                    "ok": {"type": "boolean"},
                                    "networks": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "ssid":   {"type": "string"},
                                                "signal": {"type": "string"},
                                            },
                                        },
                                    },
                                },
                            }}},
                        }
                    },
                }
            },
            "/api/wifi/save": {
                "post": {
                    "tags": ["network"],
                    "summary": "Connect to WiFi network",
                    "description": "Connects to the specified WiFi network via nmcli.",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {
                            "type": "object",
                            "required": ["ssid"],
                            "properties": {
                                "ssid":     {"type": "string", "example": "MyNetwork"},
                                "password": {"type": "string", "example": "secret"},
                            },
                        }}},
                    },
                    "responses": {
                        "200": {"description": "Connected", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OkResponse"}}}},
                        "400": {"description": "SSID required"},
                        "500": {"description": "Connection failed"},
                    },
                }
            },
            "/api/logs/download": {
                "get": {
                    "tags": ["system"],
                    "summary": "Download log file",
                    "parameters": [
                        {
                            "name": "service",
                            "in": "query",
                            "schema": {"type": "string", "enum": ["gps", "sik", "ntrip", "web", "all"], "default": "gps"},
                            "description": "Which service log to download",
                        }
                    ],
                    "responses": {
                        "200": {"description": "Log file (text/plain)"},
                        "404": {"description": "Log file not found"},
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "OkResponse": {
                    "type": "object",
                    "properties": {
                        "ok":      {"type": "boolean"},
                        "message": {"type": "string"},
                        "error":   {"type": "string"},
                    },
                }
            }
        },
    }
    return jsonify(spec)


# ── Log helpers ───────────────────────────────────────────────────────────────
def _tail_lines(path: str, n: int = 50) -> list:
    """Return the last n lines of a file without reading the whole thing into memory."""
    try:
        with open(path, "r") as f:
            return list(collections.deque(f, maxlen=n))
    except FileNotFoundError:
        return []


# ── API: Log streaming (SSE) ──────────────────────────────────────────────────
@app.route("/api/logs/stream")
def api_logs_stream():
    service = request.args.get("service", "gps")
    level_filter = request.args.get("level", "").upper()

    if service == "all":
        def generate_all():
            q = _queue.Queue(maxsize=500)
            stop = threading.Event()

            # Emit recent history from all logs immediately
            for svc, path in LOG_FILES.items():
                for line in _tail_lines(path, 20):
                    line = line.rstrip()
                    if line and (not level_filter or level_filter in line):
                        yield f"data: [history][{svc}] {line}\n\n"
            yield "data: [history end]\n\n"

            def tail_file(svc, path):
                try:
                    with open(path, "r") as f:
                        f.seek(0, 2)
                        while not stop.is_set():
                            line = f.readline()
                            if line:
                                if not level_filter or level_filter in line:
                                    try:
                                        q.put_nowait(f"[{svc}] {line.rstrip()}")
                                    except _queue.Full:
                                        pass
                            else:
                                time.sleep(0.1)
                except FileNotFoundError:
                    pass
                except Exception:
                    pass

            for svc, path in LOG_FILES.items():
                t = threading.Thread(target=tail_file, args=(svc, path), daemon=True)
                t.start()

            try:
                while True:
                    try:
                        line = q.get(timeout=1.0)
                        yield f"data: {line}\n\n"
                    except _queue.Empty:
                        yield ": keepalive\n\n"
            except GeneratorExit:
                stop.set()

        return Response(
            stream_with_context(generate_all()),
            content_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    log_file = LOG_FILES.get(service, LOG_FILES["gps"])

    def generate():
        # Emit recent history immediately so the user sees output right away
        for line in _tail_lines(log_file, 50):
            line = line.rstrip()
            if line and (not level_filter or level_filter in line):
                yield f"data: [history] {line}\n\n"
        yield "data: [history end]\n\n"

        try:
            with open(log_file, "r") as f:
                f.seek(0, 2)  # seek to end for live tail
                while True:
                    line = f.readline()
                    if line:
                        if not level_filter or level_filter in line:
                            yield f"data: {line.rstrip()}\n\n"
                    else:
                        time.sleep(0.1)
        except FileNotFoundError:
            yield f"data: Log file not found: {log_file}\n\n"
        except GeneratorExit:
            pass

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── API: Config save ──────────────────────────────────────────────────────────
@app.route("/api/config/save", methods=["POST"])
def api_config_save():
    data = request.get_json(force=True)
    if not data:
        return jsonify({"ok": False, "error": "No JSON body"}), 400

    cfg = load_config()

    if "survey" in data:
        cfg.setdefault("survey", {}).update(data["survey"])
    if "sik" in data:
        cfg.setdefault("sik", {}).update(data["sik"])
    if "correction_source" in data:
        cfg["correction_source"] = data["correction_source"]
    if "ntrip_client" in data:
        cfg.setdefault("ntrip_client", {}).update(data["ntrip_client"])
    if "ntrip_server" in data:
        cfg.setdefault("ntrip_server", {}).update(data["ntrip_server"])

    try:
        save_config(cfg)
        _send_signal(GPS_PID_FILE, signal.SIGUSR1)
        _send_signal(NTRIP_PID_FILE, signal.SIGUSR1)
        log.info("Config saved, services signalled to reload")
        return jsonify({"ok": True})
    except Exception as e:
        log.error("Config save error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ── API: Re-survey ────────────────────────────────────────────────────────────
@app.route("/api/resurvey", methods=["POST"])
def api_resurvey():
    sent = _send_signal(GPS_PID_FILE, signal.SIGUSR2)
    if sent:
        return jsonify({"ok": True, "message": "Re-survey triggered"})
    return jsonify({"ok": False, "error": "GPS service not running"}), 500


@app.route("/api/resurvey/accept", methods=["POST"])
def api_resurvey_accept():
    # Write a flag to state for gps.py to pick up
    state.update_state({"resurvey_action": "accept"})
    return jsonify({"ok": True})


@app.route("/api/resurvey/reject", methods=["POST"])
def api_resurvey_reject():
    state.update_state({"resurvey_action": "reject"})
    return jsonify({"ok": True})


# ── API: WiFi ─────────────────────────────────────────────────────────────────
@app.route("/api/wifi/scan")
def api_wifi_scan():
    try:
        result = subprocess.run(
            ["nmcli", "-t", "--escape", "no", "-f", "SSID,SIGNAL",
             "dev", "wifi", "list", "--rescan", "yes"],
            capture_output=True, text=True, timeout=15
        )
        networks = []
        seen = set()
        for line in result.stdout.splitlines():
            # SSID can contain ':' but signal is always the last field
            parts = line.rsplit(":", 1)
            if len(parts) == 2:
                ssid = parts[0].strip()
                signal = parts[1].strip()
                if ssid and ssid not in seen and ssid != "BaseStation":
                    seen.add(ssid)
                    networks.append({"ssid": ssid, "signal": signal})
        return jsonify({"ok": True, "networks": networks})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/wifi/save", methods=["POST"])
def api_wifi_save():
    data = request.get_json(force=True)
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "")
    if not ssid:
        return jsonify({"ok": False, "error": "SSID required"}), 400

    try:
        cmd = ["nmcli", "dev", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            # Set 30s autoconnect retry so NM keeps trying if home WiFi drops
            subprocess.run(
                ["nmcli", "con", "modify", ssid,
                 "connection.autoconnect-retry-interval", "30"],
                capture_output=True, timeout=5
            )
            log.info("WiFi connected to %s", ssid)
            return jsonify({"ok": True, "message": f"Connected to {ssid}"})
        else:
            err = result.stderr.strip() or result.stdout.strip()
            log.warning("WiFi connect failed: %s", err)
            return jsonify({"ok": False, "error": err})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/logs/download")
def api_logs_download():
    service = request.args.get("service", "gps")

    if service == "all":
        parts = []
        for svc, path in LOG_FILES.items():
            parts.append(f"{'='*60}\n=== {svc} ===\n{'='*60}\n")
            try:
                with open(path) as f:
                    parts.append(f.read())
            except FileNotFoundError:
                parts.append(f"(log file not found: {path})\n")
            parts.append("\n")
        return Response(
            "".join(parts),
            mimetype="text/plain",
            headers={"Content-Disposition": "attachment; filename=mowerbase-all.log"},
        )

    log_file = LOG_FILES.get(service, LOG_FILES["gps"])
    try:
        with open(log_file) as f:
            content = f.read()
        return Response(
            content,
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={service}.log"},
        )
    except FileNotFoundError:
        return f"Log file not found: {log_file}", 404


# ── API: Restart all services ─────────────────────────────────────────────────
MANAGED_SERVICES = [
    "mowerbase-gps",
    "mowerbase-sik",
    "mowerbase-ntrip",
    "mowerbase-web",
]

@app.route("/api/drift/history")
def api_drift_history():
    """Return last 24 hours of drift readings from history.db."""
    try:
        db = sqlite3.connect(HISTORY_DB)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        rows = db.execute(
            "SELECT timestamp, total_3d FROM drift_history WHERE timestamp >= ? ORDER BY timestamp ASC",
            (cutoff,)
        ).fetchall()
        db.close()
        return jsonify({"ok": True, "readings": [{"ts": r[0], "total_3d": r[1]} for r in rows]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "readings": []})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    try:
        # Small delay so the JSON response reaches the browser before web.py dies
        subprocess.Popen([
            "bash", "-c",
            "sleep 1 && sudo systemctl restart " + " ".join(MANAGED_SERVICES)
        ])
        log.info("Restart triggered for all services")
        return jsonify({"ok": True, "message": "Restarting all services..."})
    except Exception as e:
        log.error("Restart failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Helpers ───────────────────────────────────────────────────────────────────
def _get_current_wifi_ssid() -> str:
    """Return the SSID of the active home WiFi connection (not the BaseStation AP)."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "--escape", "no", "-f", "ACTIVE,SSID", "dev", "wifi"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if line.startswith("yes:"):
                ssid = line.split(":", 1)[1].strip()
                if ssid and ssid != "BaseStation":
                    return ssid
    except Exception:
        pass
    return ""


def _get_ap_status() -> dict:
    """Return status of the always-on BaseStation AP."""
    active = False
    clients = 0
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,STATE", "con", "show", "--active"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if "mowerbase-ap" in line.lower() and "activated" in line.lower():
                active = True
                break
        # Count DHCP leases on the AP subnet
        import glob
        for lease_file in glob.glob("/var/lib/NetworkManager/dnsmasq-*.leases"):
            try:
                with open(lease_file) as f:
                    clients = sum(1 for ln in f if ln.strip())
            except Exception:
                pass
    except Exception:
        pass
    return {"active": active, "ssid": "BaseStation", "ip": "10.42.0.1", "clients": clients}


# ── App startup ───────────────────────────────────────────────────────────────
def start_background_services(cfg: dict) -> None:
    global _oled_thread_running

    # Init LED
    led_pin = cfg.get("led", {}).get("gpio_pin", 17)
    led.init(led_pin)
    led.set_state("boot")

    # Init OLED
    oled_addr_str = cfg.get("oled", {}).get("i2c_address", "0x3C")
    oled_addr = int(oled_addr_str, 16) if isinstance(oled_addr_str, str) else oled_addr_str
    oled.init(oled_addr)

    # Start OLED updater thread
    _oled_thread_running = True
    t = threading.Thread(target=_oled_updater, daemon=True, name="oled-updater")
    t.start()

    # Setup GPIO button
    button_pin = cfg.get("button", {}).get("gpio_pin", 27)
    _setup_gpio_button(button_pin)

    log.info("Background services started (LED=%d, OLED=0x%02X, Button=%d)",
             led_pin, oled_addr, button_pin)


def main():
    cfg = load_config()
    port = cfg.get("web", {}).get("port", 8080)

    log.info("BaseStation web server starting on port %d", port)

    start_background_services(cfg)

    try:
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        log.info("Keyboard interrupt — stopping")
    finally:
        global _oled_thread_running
        _oled_thread_running = False
        oled.cleanup()
        led.cleanup()
        if _GPIO_AVAILABLE:
            try:
                GPIO.cleanup()
            except Exception:
                pass


if __name__ == "__main__":
    main()
