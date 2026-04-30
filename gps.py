"""
gps.py — GPS serial reader + LC29H(BS) controller + Survey-In state machine.

Responsibilities:
- Open /dev/ttyAMA0 at 115200 baud (pyserial)
- Parse RTCM3 frames from LC29H(BS) output (base station mode — no NMEA)
- Parse occasional PQTM/PAIR response lines
- Send PAIR commands to control Survey-In / Fixed mode
- Run Survey-In state machine (no NTRIP required — GPS-only averaging)
- Drift monitoring thread in FIXED state
- Write state.json every 2 seconds
- SIGUSR1 = reload config, SIGUSR2 = trigger re-survey
- Create /run/mowerbase/ dir and rtcm.pipe on startup

State machine:
  BOOT → SURVEYING → FIXED
  BOOT → SURVEYING → ERROR (timeout — no RTCM 1005 after SVIN_TIMEOUT_S)
  FIXED → (re-survey trigger) → SURVEYING

Position accuracy:
  Survey-In without NTRIP corrections achieves ~1–3m absolute accuracy.
  This is intentional — relative accuracy (base station to mower) is
  centimetre-level as long as the base station does not move between sessions.
  Waypoints collected against this base will mow correctly regardless of
  absolute GPS accuracy.

Survey-In completion:
  - Send $PQTMCFGSVIN,W,1,<min_duration>,<accuracy_limit>,0.0,0.0,0.0
  - Module outputs RTCM3 message 1005 with current averaged position
  - After min_duration seconds, first RTCM 1005 received triggers completion
  - Completion: save position.json, send Fixed mode command
  - Subsequent boots: load position.json → immediate Fixed mode (no survey wait)
"""

import os
import sys
import json
import math
import time
import signal
import logging
import threading
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import collections
import serial
import pynmea2

import state

# ── Paths ────────────────────────────────────────────────────────────────────
STATE_DIR = "/run/mowerbase"
RTCM_PIPE = "/run/mowerbase/rtcm.pipe"
RTCM_NTRIP_PIPE = "/run/mowerbase/rtcm_ntrip.pipe"
CONFIG_FILE = "/etc/mowerbase/config.json"
POSITION_FILE = "/etc/mowerbase/position.json"
HISTORY_DB = "/var/lib/mowerbase/history.db"
LOG_FILE = "/var/log/mowerbase/gps.log"
PID_FILE = "/run/mowerbase/gps.pid"

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("/var/log/mowerbase", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] gps: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("gps")

# ── State machine states ──────────────────────────────────────────────────────
SM_BOOT = "BOOT"
SM_SURVEYING = "SURVEYING"
SM_FIXED = "FIXED"
SM_RESURVEY_PENDING = "RESURVEY_PENDING"
SM_ERROR = "ERROR"
SM_GPS_NOT_FOUND = "GPS_NOT_FOUND"

# ── Survey-In timeout ─────────────────────────────────────────────────────────
# If RTCM 1005 is not received within this many seconds of starting Survey-In,
# transition to ERROR state. Operator must fix the antenna/GPS issue and restart
# the service. Default: 5 minutes. Covers bad sky view, rejected PAIR command,
# or hardware failure. We do NOT silently fall back to a worse position.
SVIN_TIMEOUT_S = 300

# ── PAIR command templates ────────────────────────────────────────────────────
# Start Survey-In: mode=1, min_duration_s (int), accuracy_limit_m (float),
# ECEF X/Y/Z = 0.0 (ignored in survey-in mode but required by parser)
PAIR_SVIN_START = "$PQTMCFGSVIN,W,1,{duration},{accuracy},0.0,0.0,0.0*"

# Set Fixed mode with ECEF coordinates in decimal metres (4 decimal places)
# Example: $PQTMCFGSVIN,W,2,0,0.0,-3935857.5283,3440073.8749,-3642935.1065*XX
PAIR_FIXED_MODE = "$PQTMCFGSVIN,W,2,0,0.0,{ecef_x:.4f},{ecef_y:.4f},{ecef_z:.4f}*"

# Query current Survey-In / Fixed mode configuration
PAIR_SVIN_QUERY = "$PQTMCFGSVIN,R*"


def _build_pair_cmd(template: str, **kwargs) -> bytes:
    """Build a PAIR command with correct NMEA checksum appended."""
    body = template.format(**kwargs)
    # body ends with * — compute checksum over content between $ and *
    cs = 0
    for ch in body[1:]:
        if ch == "*":
            break
        cs ^= ord(ch)
    full = f"{body}{cs:02X}\r\n"
    return full.encode()


def _ecef_to_llh(x: float, y: float, z: float) -> tuple:
    """Convert ECEF (metres) to lat, lon, height (degrees, metres)."""
    a = 6378137.0
    f = 1 / 298.257223563
    b = a * (1 - f)
    e2 = 1 - (b / a) ** 2
    ep2 = (a / b) ** 2 - 1

    lon = math.degrees(math.atan2(y, x))
    p = math.sqrt(x**2 + y**2)
    th = math.atan2(a * z, b * p)
    lat = math.atan2(
        z + ep2 * b * math.sin(th) ** 3,
        p - e2 * a * math.cos(th) ** 3,
    )
    N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - N if abs(math.cos(lat)) > 1e-10 else abs(z) / math.sin(lat) - N * (1 - e2)
    return math.degrees(lat), lon, alt


def _llh_to_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> tuple:
    """Convert lat/lon/height (degrees, metres) to ECEF (metres)."""
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = 2 * f - f**2
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    x = (N + alt_m) * math.cos(lat) * math.cos(lon)
    y = (N + alt_m) * math.cos(lat) * math.sin(lon)
    z = (N * (1 - e2) + alt_m) * math.sin(lat)
    return x, y, z


class GpsService:
    def __init__(self):
        self._config = {}
        self._ser: Optional[serial.Serial] = None
        self._state_machine = SM_BOOT
        self._shutdown = False

        # Current GPS data (populated from RTCM3 message 1005)
        self._lat: Optional[float] = None
        self._lon: Optional[float] = None
        self._alt: Optional[float] = None
        self._num_sats: int = 0
        self._accuracy_m: Optional[float] = None

        # Survey-In progress
        self._svin_start_time: Optional[float] = None
        self._svin_min_duration: int = 60          # seconds — short, GPS-only accuracy
        self._svin_accuracy_limit: float = 2.0     # metres — achievable without corrections

        # Stored fixed position
        self._stored_position: Optional[dict] = None

        # Drift monitoring
        self._drift_current_m: float = 0.0
        self._drift_alert: bool = False
        self._position_samples: list = []

        # Re-survey
        self._resurvey_requested = False
        self._resurvey_pending_new: Optional[dict] = None

        # RTCM statistics (sliding 10s window for web UI RTCM tab)
        self._rtcm_frame_buf: collections.deque = collections.deque()
        self._rtcm_recent_frames: collections.deque = collections.deque(maxlen=20)
        self._rtcm_last_1005_ts: Optional[float] = None

        # Survey convergence tracking — delta between consecutive ECEF samples
        self._svin_convergence: list = []   # [{elapsed_s, delta_m}]
        self._svin_last_ecef: Optional[tuple] = None  # (x, y, z) from previous query
        self._last_svin_query: float = 0.0
        self._svin_rtcm_frames: int = 0     # MSM4 frames received since survey start

        # Satellite count history during survey (sampled every 10s)
        self._svin_sat_history: list = []   # [{elapsed_s, sats}]
        # Preserved after survey completes — shown on position page post-survey
        self._last_svin_sat_history: list = []
        self._last_svin_convergence: list = []

        # Threads
        self._state_thread: Optional[threading.Thread] = None
        self._drift_thread: Optional[threading.Thread] = None

        # DB connection
        self._db: Optional[sqlite3.Connection] = None

        # RTCM pipe write ends (opened in background threads once readers connect)
        self._pipe_fd: Optional[int] = None         # → sik_forwarder
        self._pipe_ntrip_fd: Optional[int] = None   # → ntrip.py server

        # Correction source: "gps" (own Survey-In) or "ntrip" (relay from external caster)
        self._correction_source: str = "gps"

        # GPS hardware presence
        self._serial_ok: bool = False

    def load_config(self) -> None:
        try:
            with open(CONFIG_FILE) as f:
                self._config = json.load(f)
            self._svin_min_duration = (
                self._config.get("survey", {}).get("min_duration_seconds", 60)
            )
            self._svin_accuracy_limit = (
                self._config.get("survey", {}).get("accuracy_limit_m", 2.0)
            )
            self._correction_source = self._config.get("correction_source", "gps")
            log.info("Config loaded: survey duration=%ds, accuracy=%.1fm, source=%s",
                     self._svin_min_duration, self._svin_accuracy_limit, self._correction_source)
        except FileNotFoundError:
            log.warning("Config file not found — using defaults")
        except json.JSONDecodeError as e:
            log.error("Config parse error: %s — using defaults", e)

    def setup_ipc(self) -> None:
        """Create IPC directory, RTCM named pipes, and start pipe writer threads."""
        os.makedirs(STATE_DIR, exist_ok=True)
        if not os.path.exists(RTCM_PIPE):
            os.mkfifo(RTCM_PIPE)
            log.info("Created RTCM pipe: %s", RTCM_PIPE)
        if not os.path.exists(RTCM_NTRIP_PIPE):
            os.mkfifo(RTCM_NTRIP_PIPE)
            log.info("Created NTRIP pipe: %s", RTCM_NTRIP_PIPE)

        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))

        t = threading.Thread(target=self._open_pipe_writer_thread, daemon=True, name="pipe-writer")
        t.start()
        t2 = threading.Thread(target=self._open_ntrip_pipe_writer_thread, daemon=True, name="ntrip-pipe-writer")
        t2.start()

    def init_db(self) -> None:
        """Initialise SQLite history database."""
        os.makedirs(os.path.dirname(HISTORY_DB), exist_ok=True)
        self._db = sqlite3.connect(HISTORY_DB, check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS drift_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                delta_x REAL,
                delta_y REAL,
                delta_z REAL,
                total_3d REAL
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS survey_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT,
                completed_at TEXT,
                accuracy_m REAL,
                duration_s INTEGER,
                ecef_x REAL,
                ecef_y REAL,
                ecef_z REAL,
                lat REAL,
                lon REAL,
                alt_m REAL
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT,
                message TEXT
            )
        """)
        self._db.commit()

    def open_serial(self) -> None:
        """Open the GPS serial port. After 5 failures, marks GPS_NOT_FOUND in state."""
        gps_port = self._config.get("gps", {}).get("port", "/dev/ttyAMA0")
        gps_baud = self._config.get("gps", {}).get("baud", 115200)
        _retries = 0
        while not self._shutdown:
            try:
                self._ser = serial.Serial(
                    gps_port, gps_baud, timeout=2,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                )
                self._serial_ok = True
                if self._state_machine == SM_GPS_NOT_FOUND:
                    self._state_machine = SM_BOOT
                log.info("Opened GPS serial: %s @ %d baud", gps_port, gps_baud)
                return
            except serial.SerialException as e:
                _retries += 1
                log.error("Cannot open %s: %s — retrying in 5s", gps_port, e)
                if _retries >= 5 and self._state_machine not in (SM_GPS_NOT_FOUND,):
                    log.warning("GPS HAT not detected after %d retries — reporting GPS_NOT_FOUND", _retries)
                    self._serial_ok = False
                    self._state_machine = SM_GPS_NOT_FOUND
                time.sleep(5)

    def _send_pair(self, cmd_template: str, **kwargs) -> None:
        """Build and send a PAIR command to the LC29H(BS)."""
        if not self._ser:
            return
        cmd = _build_pair_cmd(cmd_template, **kwargs)
        try:
            self._ser.write(cmd)
            log.debug("PAIR >> %s", cmd.decode().strip())
        except serial.SerialException as e:
            log.error("Serial write error: %s", e)

    def cmd_start_surveyin(self) -> None:
        """Send command to start Survey-In mode (GPS-only averaging)."""
        self._send_pair(
            PAIR_SVIN_START,
            duration=self._svin_min_duration,
            accuracy=self._svin_accuracy_limit,
        )
        log.info(
            "Survey-In started: min %ds, accuracy %.1fm (GPS-only, no NTRIP)",
            self._svin_min_duration, self._svin_accuracy_limit
        )

    def cmd_set_fixed(self, ecef_x: float, ecef_y: float, ecef_z: float) -> None:
        """Send command to enter Fixed Base mode with given ECEF coordinates (decimal metres)."""
        self._send_pair(
            PAIR_FIXED_MODE,
            ecef_x=ecef_x,
            ecef_y=ecef_y,
            ecef_z=ecef_z,
        )
        log.info(
            "Fixed mode sent: ECEF (%.4f, %.4f, %.4f)",
            ecef_x, ecef_y, ecef_z
        )

    # ── RTCM3 pipe ───────────────────────────────────────────────────────────────

    def _open_pipe_writer_thread(self) -> None:
        """Open the RTCM pipe write end in a background thread.
        open() blocks until sik_forwarder opens the read end — that is fine.
        The fd is set to non-blocking after open so that writes never stall
        the main GPS loop if sik_forwarder falls behind or stops reading."""
        try:
            import fcntl
            log.info("Waiting for RTCM pipe reader to connect...")
            fd = os.open(RTCM_PIPE, os.O_WRONLY)
            # Set non-blocking so os.write raises BlockingIOError (EAGAIN)
            # rather than blocking forever if sik_forwarder stops draining.
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            self._pipe_fd = fd
            log.info("RTCM pipe open for writing (non-blocking)")
        except OSError as e:
            log.error("Failed to open RTCM pipe for writing: %s", e)

    def _write_rtcm_to_pipe(self, data: bytes) -> None:
        """Write an RTCM3 frame to the SiK pipe (non-blocking, drops on full/error)."""
        if self._pipe_fd is None:
            return
        try:
            os.write(self._pipe_fd, data)
        except BlockingIOError:
            pass
        except OSError:
            try:
                os.close(self._pipe_fd)
            except OSError:
                pass
            self._pipe_fd = None
            t = threading.Thread(target=self._open_pipe_writer_thread, daemon=True)
            t.start()

    def _open_ntrip_pipe_writer_thread(self) -> None:
        """Open the NTRIP pipe write end in a background thread (blocks until ntrip.py connects)."""
        try:
            import fcntl
            log.info("Waiting for NTRIP pipe reader to connect...")
            fd = os.open(RTCM_NTRIP_PIPE, os.O_WRONLY)
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            self._pipe_ntrip_fd = fd
            log.info("NTRIP pipe open for writing (non-blocking)")
        except OSError as e:
            log.error("Failed to open NTRIP pipe for writing: %s", e)

    def _write_rtcm_to_ntrip_pipe(self, data: bytes) -> None:
        """Write an RTCM3 frame to the NTRIP pipe (non-blocking, drops on full/error)."""
        if self._pipe_ntrip_fd is None:
            return
        try:
            os.write(self._pipe_ntrip_fd, data)
        except BlockingIOError:
            pass
        except OSError:
            try:
                os.close(self._pipe_ntrip_fd)
            except OSError:
                pass
            self._pipe_ntrip_fd = None
            t = threading.Thread(target=self._open_ntrip_pipe_writer_thread, daemon=True)
            t.start()

    # ── RTCM3 frame handling ──────────────────────────────────────────────────

    def _read_rtcm3_frame(self) -> Optional[bytes]:
        """Read a complete RTCM3 frame from serial (caller already consumed 0xD3 byte)."""
        try:
            hdr = self._ser.read(2)
            if len(hdr) < 2:
                return None
            length = ((hdr[0] & 0x03) << 8) | hdr[1]
            body = self._ser.read(length + 3)  # payload + 3-byte CRC
            if len(body) < length + 3:
                return None
            return bytes([0xD3]) + hdr + body
        except serial.SerialException:
            return None

    def _handle_rtcm_frame(self, frame: bytes) -> None:
        """Dispatch an RTCM3 frame: extract position/sats, forward to pipe."""
        if len(frame) < 6:
            return

        payload = frame[3:-3]  # strip preamble+length header and CRC
        if len(payload) < 2:
            return

        msg_type = (payload[0] << 4) | (payload[1] >> 4)

        # Track RTCM stats for web UI RTCM tab
        _now = time.time()
        self._rtcm_frame_buf.append((_now, msg_type, len(frame)))
        self._rtcm_recent_frames.append({"ts": _now, "type": msg_type, "len": len(frame)})
        if msg_type == 1005:
            self._rtcm_last_1005_ts = _now

        # Message 1005: Stationary RTK Reference Station ARP
        # Module outputs this during Survey-In (with live averaged position)
        # and in Fixed mode (with locked position).
        # After min_duration has elapsed, the next 1005 triggers survey completion.
        if msg_type == 1005:
            self._parse_rtcm1005(payload)
            if self._state_machine == SM_SURVEYING and self._svin_start_time is not None:
                elapsed = int(time.monotonic() - self._svin_start_time)
                if elapsed >= self._svin_min_duration:
                    log.info("RTCM 1005 received after %ds — Survey-In complete", elapsed)
                    self._complete_surveyin()
                else:
                    log.debug("RTCM 1005 at %ds / %ds — still surveying",
                              elapsed, self._svin_min_duration)

        # MSM4 messages: extract satellite count
        # 1074=GPS, 1084=GLONASS, 1094=Galileo, 1114=QZSS, 1124=BDS
        elif msg_type in (1074, 1084, 1094, 1114, 1124):
            sats = self._count_msm4_sats(payload)
            if sats > 0:
                self._num_sats = max(self._num_sats, sats)
            if self._state_machine == SM_SURVEYING:
                self._svin_rtcm_frames += 1

        # Forward to pipes only when gps.py is the correction source.
        # In NTRIP relay mode, ntrip.py owns both pipes.
        if self._correction_source == "gps":
            self._write_rtcm_to_pipe(frame)
            self._write_rtcm_to_ntrip_pipe(frame)

    def _parse_rtcm1005(self, payload: bytes) -> None:
        """Parse RTCM3 message 1005 to extract ECEF base station position.
        RTCM 10403.3 bit layout:
          msg_type(0:12), station_id(12:24), ITRFYear(24:30), GPS/GLO/GAL/RefStn flags(30:34)
          DF025 ECEF-X: int38, bits 34-71
          DF142 (SingleRcvOscillator): 1 bit, bit 72
          DF001 (Reserved): 1 bit, bit 73
          DF026 ECEF-Y: int38, bits 74-111
          DF364 (QuarterCycleIndicator): 2 bits, bits 112-113
          DF027 ECEF-Z: int38, bits 114-151"""
        if len(payload) < 19:
            return
        try:
            bits = int.from_bytes(payload, "big")
            n = len(payload) * 8

            def s38(start: int) -> float:
                v = (bits >> (n - start - 38)) & 0x3FFFFFFFFF
                if v & (1 << 37):
                    v -= (1 << 38)
                return v * 0.0001  # 0.0001 m units → metres

            ecef_x = s38(34)
            ecef_y = s38(74)
            ecef_z = s38(114)   # bit 114, NOT 113 — bits 112-113 are QuarterCycleIndicator

            lat, lon, alt = _ecef_to_llh(ecef_x, ecef_y, ecef_z)
            self._lat = lat
            self._lon = lon
            self._alt = alt
            log.info("RTCM1005: ECEF(%.2f, %.2f, %.2f) → lat=%.7f lon=%.7f alt=%.1fm",
                     ecef_x, ecef_y, ecef_z, lat, lon, alt)
        except Exception as e:
            log.debug("RTCM1005 parse error: %s", e)

    def _count_msm4_sats(self, payload: bytes) -> int:
        """Count satellites from an MSM4 satellite mask (64 bits at bit offset 73).
        MSM header: msg_type(12) + station_id(12) + epoch_time(30) + multi_msg(1)
          + iods(3) + reserved(7) + clk_steering(2) + ext_clk(2) + smoothing(1)
          + smooth_interval(3) = 73 bits before the 64-bit satellite mask."""
        if len(payload) < 18:
            return 0
        try:
            bits = int.from_bytes(payload, "big")
            n = len(payload) * 8
            sat_mask = (bits >> (n - 73 - 64)) & 0xFFFFFFFFFFFFFFFF
            return bin(sat_mask).count("1")
        except Exception:
            return 0

    def load_stored_position(self) -> bool:
        """Load position.json if it exists. Returns True if found."""
        try:
            with open(POSITION_FILE) as f:
                self._stored_position = json.load(f)
            log.info(
                "Stored position loaded: lat=%.7f lon=%.7f acc=%s",
                self._stored_position.get("lat", 0),
                self._stored_position.get("lon", 0),
                f"{self._stored_position.get('accuracy_m', 0):.1f}m"
                if self._stored_position.get("accuracy_m") else "GPS",
            )
            return True
        except FileNotFoundError:
            return False
        except Exception as e:
            log.error("Error reading position file: %s", e)
            return False

    def save_position(self, pos: dict) -> None:
        """Write a surveyed position to position.json."""
        os.makedirs(os.path.dirname(POSITION_FILE), exist_ok=True)
        tmp = POSITION_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(pos, f, indent=2)
        os.replace(tmp, POSITION_FILE)
        self._stored_position = pos
        log.info("Position saved to %s", POSITION_FILE)

    def _parse_pair_response(self, line: str) -> None:
        """Handle PQTM/PAIR response lines from LC29H(BS)."""
        if '*' in line:
            line = line[:line.rfind('*')]
        parts = line.split(',')
        if not parts:
            return
        cmd = parts[0]

        if cmd == '$PQTMCFGSVIN' and len(parts) >= 2:
            # Responses: $PQTMCFGSVIN,OK  or  $PQTMCFGSVIN,OK,<Mode>,...  or  $PQTMCFGSVIN,ERROR,...
            result = parts[1] if len(parts) > 1 else ''
            if result == 'OK' and len(parts) >= 8:
                # Get response: $PQTMCFGSVIN,OK,<Mode>,<MinDur>,<3D_AccLimit>,<ECEFx>,<ECEFy>,<ECEFz>
                # parts[4] = configured accuracy limit (not live accuracy).
                # parts[5..7] = current running-average ECEF position — these DO update each query.
                # Convergence metric: 3D delta between consecutive ECEF samples.
                # As the survey stabilises the average stops moving → delta → 0.
                try:
                    mode = int(parts[2])
                    ecef_x = float(parts[5])
                    ecef_y = float(parts[6])
                    ecef_z = float(parts[7])
                    log.info("PQTMCFGSVIN query: mode=%d ECEF=(%.2f, %.2f, %.2f)",
                             mode, ecef_x, ecef_y, ecef_z)
                    if self._state_machine == SM_SURVEYING and mode == 1 and self._svin_start_time:
                        # Filter out zero ECEF — module returns 0,0,0 before it has a position
                        if abs(ecef_x) < 1.0 and abs(ecef_y) < 1.0 and abs(ecef_z) < 1.0:
                            log.debug("PQTMCFGSVIN R: ECEF zero — position not yet computed during survey")
                        else:
                            elapsed = int(time.monotonic() - self._svin_start_time)
                            if self._svin_last_ecef is not None:
                                px, py, pz = self._svin_last_ecef
                                delta = math.sqrt(
                                    (ecef_x - px) ** 2 +
                                    (ecef_y - py) ** 2 +
                                    (ecef_z - pz) ** 2
                                )
                                self._svin_convergence.append({
                                    "elapsed_s": elapsed,
                                    "delta_m": round(delta, 4),
                                })
                                log.info("Survey convergence delta: %.4fm at %ds", delta, elapsed)
                            self._svin_last_ecef = (ecef_x, ecef_y, ecef_z)
                except (ValueError, IndexError):
                    pass
            elif result == 'OK':
                log.debug("PQTMCFGSVIN command acknowledged")
            elif result == 'ERROR':
                err_code = parts[2] if len(parts) > 2 else '?'
                log.warning("PQTMCFGSVIN error %s — check command format", err_code)

        elif cmd == '$PQTMVERNO' and len(parts) >= 2 and parts[1] not in ('ERROR',):
            log.info("LC29H(BS) firmware: %s", ','.join(parts[1:]))

        elif cmd == '$PAIR001':
            # ACK for PAIR commands
            cmd_id = parts[1] if len(parts) > 1 else '?'
            result = parts[2] if len(parts) > 2 else '?'
            if result != '0':
                log.warning("PAIR%s result=%s (0=OK, 2=fail, 3=unsupported, 4=param error)",
                            cmd_id, result)
            else:
                log.debug("PAIR%s acknowledged OK", cmd_id)

        else:
            log.debug("PAIR/PQTM response: %r", line[:80])

    def _parse_nmea(self, line: str) -> None:
        """Parse a PQTM/PAIR response line (LC29H(BS) in base station mode outputs no standard NMEA)."""
        if line.startswith("$PQTM") or line.startswith("$PAIR"):
            self._parse_pair_response(line)

    def _complete_surveyin(self) -> None:
        """Finalize Survey-In — save position, switch to Fixed mode."""
        if self._lat is None or self._lon is None or self._alt is None:
            log.error("Survey-In complete but no valid position — waiting for RTCM 1005")
            return

        ecef_x, ecef_y, ecef_z = _llh_to_ecef(self._lat, self._lon, self._alt)
        elapsed = int(time.monotonic() - self._svin_start_time) if self._svin_start_time else 0

        new_pos = {
            "ecef_x": round(ecef_x, 4),
            "ecef_y": round(ecef_y, 4),
            "ecef_z": round(ecef_z, 4),
            "lat": round(self._lat, 7),
            "lon": round(self._lon, 7),
            "alt_m": round(self._alt, 3),
            "accuracy_m": round(self._accuracy_m, 4) if self._accuracy_m else None,
            "survey_duration_s": elapsed,
            "sample_count": elapsed,
            "surveyed_at": datetime.now(timezone.utc).isoformat(),
        }

        if self._state_machine == SM_SURVEYING and self._resurvey_requested:
            self._resurvey_requested = False
            if self._stored_position:
                dx = new_pos["ecef_x"] - self._stored_position["ecef_x"]
                dy = new_pos["ecef_y"] - self._stored_position["ecef_y"]
                dz = new_pos["ecef_z"] - self._stored_position["ecef_z"]
                delta = math.sqrt(dx**2 + dy**2 + dz**2)
                if delta < 0.5:
                    # Within 50cm: auto-accept (coarser threshold since GPS-only)
                    log.info("Re-survey delta %.2fm < 50cm — auto-accepting", delta)
                    self.save_position(new_pos)
                    self._log_event("resurvey_accepted", f"Delta: {delta:.3f}m (auto)")
                else:
                    log.warning(
                        "Re-survey delta %.2fm >= 50cm — requires manual acceptance", delta
                    )
                    self._resurvey_pending_new = new_pos
                    self._state_machine = SM_RESURVEY_PENDING
                    state.update_state({
                        "sm_state": SM_RESURVEY_PENDING,
                        "resurvey_delta_m": round(delta, 3),
                        "resurvey_new_position": new_pos,
                        "resurvey_old_position": self._stored_position,
                    })
                    return
            else:
                self.save_position(new_pos)
        else:
            self.save_position(new_pos)

        # Record survey in DB
        if self._db:
            try:
                self._db.execute("""
                    INSERT INTO survey_history
                    (started_at, completed_at, accuracy_m, duration_s, ecef_x, ecef_y, ecef_z, lat, lon, alt_m)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    datetime.fromtimestamp(
                        time.time() - elapsed, timezone.utc
                    ).isoformat(),
                    new_pos["surveyed_at"],
                    new_pos["accuracy_m"],
                    elapsed,
                    new_pos["ecef_x"], new_pos["ecef_y"], new_pos["ecef_z"],
                    new_pos["lat"], new_pos["lon"], new_pos["alt_m"],
                ))
                self._db.commit()
            except Exception as e:
                log.error("DB survey insert error: %s", e)

        # Preserve survey history for post-survey display on position page
        self._last_svin_sat_history = list(self._svin_sat_history)
        self._last_svin_convergence = list(self._svin_convergence)

        # Enter Fixed mode — send stored ECEF to module
        self.cmd_set_fixed(
            new_pos["ecef_x"], new_pos["ecef_y"], new_pos["ecef_z"]
        )
        self._state_machine = SM_FIXED
        self._accuracy_m = new_pos.get("accuracy_m")
        self._start_drift_monitor()
        log.info("Entered FIXED state. Base position: lat=%.7f lon=%.7f alt=%.1fm",
                 new_pos["lat"], new_pos["lon"], new_pos["alt_m"])

    def _log_event(self, event_type: str, message: str) -> None:
        if self._db:
            try:
                self._db.execute(
                    "INSERT INTO events (timestamp, event_type, message) VALUES (?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), event_type, message)
                )
                self._db.commit()
            except Exception:
                pass

    def _state_writer_thread(self) -> None:
        """Background thread: write state.json every 2 seconds."""
        while not self._shutdown:
            try:
                self._write_state()
            except Exception as e:
                log.error("State writer error: %s", e)
            time.sleep(2)

    def _write_state(self) -> None:
        """Assemble and write the full GPS state to state.json."""
        s = {
            "sm_state": self._state_machine,
            "serial_ok": self._serial_ok,
            "lat": self._lat,
            "lon": self._lon,
            "alt_m": self._alt,
            "num_sats": self._num_sats,
            "accuracy_m": self._accuracy_m,
            "drift_current_m": self._drift_current_m,
            "drift_alert": self._drift_alert,
            "stored_position": self._stored_position,
        }
        if self._state_machine == SM_SURVEYING:
            elapsed = int(time.monotonic() - self._svin_start_time) if self._svin_start_time else 0
            s["svin_elapsed_s"] = elapsed
            s["svin_min_duration_s"] = self._svin_min_duration
            s["svin_accuracy_limit_m"] = self._svin_accuracy_limit
            s["svin_convergence"] = list(self._svin_convergence)
            s["svin_rtcm_frames"] = self._svin_rtcm_frames
            s["svin_sat_history"] = list(self._svin_sat_history)

        # Always include last survey satellite history for post-survey display
        if self._last_svin_sat_history:
            s["last_svin_sat_history"] = self._last_svin_sat_history

        # Compute RTCM stats from sliding 10s window
        _ts_now = time.time()
        _cutoff = _ts_now - 10.0
        while self._rtcm_frame_buf and self._rtcm_frame_buf[0][0] < _cutoff:
            self._rtcm_frame_buf.popleft()

        _counts: dict = {"1005": 0, "1074": 0, "1084": 0, "1094": 0, "1114": 0, "1124": 0}
        _total_bytes = 0
        for _ts, _mtype, _length in self._rtcm_frame_buf:
            _key = str(_mtype)
            _counts[_key] = _counts.get(_key, 0) + 1
            _total_bytes += _length

        rtcm_s = {
            "counts_10s": _counts,
            "bytes_per_sec": round(_total_bytes / 10.0, 1),
            "last_1005_ts": self._rtcm_last_1005_ts,
            "frame_rate": round(len(self._rtcm_frame_buf) / 10.0, 1),
            "recent_frames": list(self._rtcm_recent_frames),
        }
        state.update_state({"gps": s, "rtcm": rtcm_s, "correction_source": self._correction_source})

    def _drift_monitor_thread(self) -> None:
        """Background thread: monitor position drift every 5 minutes."""
        check_interval = (
            self._config.get("drift", {}).get("check_interval_minutes", 5) * 60
        )
        alert_threshold = (
            self._config.get("drift", {}).get("alert_threshold_m", 0.5)
        )
        log.info("Drift monitor started (interval=%ds, threshold=%.2fm)",
                 check_interval, alert_threshold)

        while not self._shutdown and self._state_machine == SM_FIXED:
            time.sleep(10)

            if self._lat is None:
                continue

            self._position_samples.append((self._lat, self._lon, self._alt or 0))
            if len(self._position_samples) > 30:
                self._position_samples.pop(0)

            if len(self._position_samples) >= 30:
                avg_lat = sum(s[0] for s in self._position_samples) / len(self._position_samples)
                avg_lon = sum(s[1] for s in self._position_samples) / len(self._position_samples)
                avg_alt = sum(s[2] for s in self._position_samples) / len(self._position_samples)

                cur_x, cur_y, cur_z = _llh_to_ecef(avg_lat, avg_lon, avg_alt)

                if self._stored_position:
                    dx = cur_x - self._stored_position["ecef_x"]
                    dy = cur_y - self._stored_position["ecef_y"]
                    dz = cur_z - self._stored_position["ecef_z"]
                    total_3d = math.sqrt(dx**2 + dy**2 + dz**2)
                    self._drift_current_m = total_3d
                    self._drift_alert = total_3d > alert_threshold

                    if self._db:
                        try:
                            self._db.execute("""
                                INSERT INTO drift_history
                                (timestamp, delta_x, delta_y, delta_z, total_3d)
                                VALUES (?,?,?,?,?)
                            """, (
                                datetime.now(timezone.utc).isoformat(),
                                round(dx, 3), round(dy, 3), round(dz, 3),
                                round(total_3d, 3),
                            ))
                            self._db.commit()
                        except Exception as e:
                            log.error("DB drift insert error: %s", e)

                    if self._drift_alert:
                        log.warning("Drift alert: %.2fm (threshold %.2fm)", total_3d, alert_threshold)

                self._position_samples.clear()
                remaining = check_interval - 300
                for _ in range(max(0, remaining // 10)):
                    if self._shutdown or self._state_machine != SM_FIXED:
                        return
                    time.sleep(10)

    def run(self) -> None:
        """Main service loop."""
        log.info("GPS service starting (PID %d)", os.getpid())

        self.load_config()
        self.setup_ipc()
        self.init_db()

        # Start state writer before open_serial so GPS_NOT_FOUND is visible if HAT is absent
        self._state_thread = threading.Thread(
            target=self._state_writer_thread, daemon=True, name="state-writer"
        )
        self._state_thread.start()

        self.open_serial()

        # Boot: check for stored position from previous survey
        if self.load_stored_position():
            log.info("Stored position found — entering FIXED mode (no survey needed)")
            pos = self._stored_position
            self.cmd_set_fixed(pos["ecef_x"], pos["ecef_y"], pos["ecef_z"])
            self._state_machine = SM_FIXED
            self._accuracy_m = pos.get("accuracy_m")
            # Seed lat/lon/alt from stored position for drift monitor
            self._lat = pos.get("lat")
            self._lon = pos.get("lon")
            self._alt = pos.get("alt_m")
            self._start_drift_monitor()
        else:
            log.info("No stored position — starting Survey-In immediately")
            self._svin_start_time = time.monotonic()
            self._accuracy_m = None
            self._svin_convergence = []
            self._svin_last_ecef = None
            self._last_svin_query = 0.0
            self._svin_rtcm_frames = 0
            self._svin_sat_history = []
            self.cmd_start_surveyin()
            self._state_machine = SM_SURVEYING

        signal.signal(signal.SIGUSR1, self._handle_sigusr1)
        signal.signal(signal.SIGUSR2, self._handle_sigusr2)

        log.info("GPS state machine: %s", self._state_machine)

        # Main read loop — byte-stream based to handle mixed RTCM3 + PAIR text output
        _last_periodic = 0.0
        _last_sat_log = 0.0
        while not self._shutdown:
            try:
                if not self._ser or not self._ser.is_open:
                    self.open_serial()
                    continue

                byte = self._ser.read(1)

                if not byte:
                    # Serial timeout — periodic tasks
                    now = time.monotonic()
                    if now - _last_periodic >= 2.0:
                        _last_periodic = now
                        self._check_periodic()
                    continue

                b = byte[0]

                if b == 0xD3:
                    # RTCM3 binary frame
                    frame = self._read_rtcm3_frame()
                    if frame:
                        self._handle_rtcm_frame(frame)

                elif b == 0x24:  # '$'
                    # PQTM/PAIR text line
                    try:
                        rest = self._ser.read_until(b"\n", size=512)
                        line = ("$" + rest.decode("ascii", errors="replace")).strip()
                        self._parse_nmea(line)
                    except serial.SerialException:
                        pass

                # Periodic tasks
                now = time.monotonic()
                if now - _last_periodic >= 2.0:
                    _last_periodic = now
                    self._check_periodic()

                if now - _last_sat_log >= 30:
                    _last_sat_log = now
                    log.info(
                        "GPS status: state=%s sats=%d lat=%s lon=%s acc=%s",
                        self._state_machine, self._num_sats,
                        f"{self._lat:.6f}" if self._lat else "None",
                        f"{self._lon:.6f}" if self._lon else "None",
                        f"{self._accuracy_m:.1f}m" if self._accuracy_m else "None",
                    )

            except serial.SerialException as e:
                log.error("Serial error: %s — reconnecting in 5s", e)
                self._ser = None
                time.sleep(5)
            except Exception as e:
                log.error("Unexpected error in read loop: %s", e)
                time.sleep(1)

        log.info("GPS service stopped")

    def _check_periodic(self) -> None:
        """Periodic checks called every ~2s from read loop."""
        # Survey-In timeout — surface the problem rather than silently degrade
        if self._state_machine == SM_SURVEYING and self._svin_start_time is not None:
            elapsed = time.monotonic() - self._svin_start_time
            if elapsed > SVIN_TIMEOUT_S:
                log.error(
                    "Survey-In timeout after %.0fs — no RTCM 1005 received. "
                    "Check antenna has clear sky view and GPS has a fix. "
                    "Restart service to retry.",
                    elapsed,
                )
                self._state_machine = SM_ERROR
                self._log_event("svin_timeout", f"No RTCM 1005 after {elapsed:.0f}s")
                self._write_state()
                return

        # Periodic 10s sample during Survey-In: record sat count + query ECEF
        if self._state_machine == SM_SURVEYING:
            _now_mono = time.monotonic()
            if _now_mono - self._last_svin_query >= 10.0:
                self._last_svin_query = _now_mono
                _elapsed = int(_now_mono - self._svin_start_time) if self._svin_start_time else 0
                self._svin_sat_history.append({"elapsed_s": _elapsed, "sats": self._num_sats})
                self._send_pair(PAIR_SVIN_QUERY)

        # Handle re-survey request
        if self._resurvey_requested and self._state_machine == SM_FIXED:
            log.info("Re-survey requested — switching to SURVEYING")
            self._state_machine = SM_SURVEYING
            self._svin_start_time = time.monotonic()
            self._num_sats = 0
            self._position_samples.clear()
            self._svin_convergence = []
            self._svin_last_ecef = None
            self._last_svin_query = 0.0
            self._svin_rtcm_frames = 0
            self._svin_sat_history = []
            self.cmd_start_surveyin()
            self._resurvey_requested = False

    def _start_drift_monitor(self) -> None:
        self._drift_thread = threading.Thread(
            target=self._drift_monitor_thread, daemon=True, name="drift-monitor"
        )
        self._drift_thread.start()

    def _handle_sigusr1(self, signum, frame) -> None:
        """SIGUSR1: reload config."""
        log.info("SIGUSR1 received — reloading config")
        self.load_config()

    def _handle_sigusr2(self, signum, frame) -> None:
        """SIGUSR2: trigger re-survey."""
        log.info("SIGUSR2 received — re-survey requested")
        self._resurvey_requested = True

    def accept_resurvey(self) -> None:
        """Accept a pending re-survey result (called from web UI)."""
        if self._resurvey_pending_new and self._state_machine == SM_RESURVEY_PENDING:
            self.save_position(self._resurvey_pending_new)
            self._resurvey_pending_new = None
            self.cmd_set_fixed(
                self._stored_position["ecef_x"],
                self._stored_position["ecef_y"],
                self._stored_position["ecef_z"],
            )
            self._state_machine = SM_FIXED
            state.update_state({
                "sm_state": SM_FIXED,
                "resurvey_pending": False,
            })
            log.info("Re-survey result accepted")

    def reject_resurvey(self) -> None:
        """Keep existing position, discard re-survey result."""
        if self._state_machine == SM_RESURVEY_PENDING:
            self._resurvey_pending_new = None
            self._state_machine = SM_FIXED
            state.update_state({
                "sm_state": SM_FIXED,
                "resurvey_pending": False,
            })
            log.info("Re-survey result rejected — keeping existing position")

    def stop(self) -> None:
        self._shutdown = True
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass


def main():
    svc = GpsService()
    try:
        svc.run()
    except KeyboardInterrupt:
        log.info("Keyboard interrupt — stopping")
    finally:
        svc.stop()


if __name__ == "__main__":
    main()
