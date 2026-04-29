"""
sik_forwarder.py — RTCM pipe → SiK 915MHz radio forwarder.

Reads raw RTCM bytes from /run/mowerbase/rtcm.pipe and writes them
to the SiK radio on /dev/ttyUSB0.

If the radio is absent: logs a warning, waits 30s, retries. Does NOT crash.
If the radio disconnects mid-session: detects error, closes, retries.

Bytes/sec forwarded is written to state.json.
"""

import os
import sys
import json
import time
import signal
import logging

import serial

import state

# ── Paths ─────────────────────────────────────────────────────────────────────
RTCM_PIPE = "/run/mowerbase/rtcm.pipe"
CONFIG_FILE = "/etc/mowerbase/config.json"
LOG_FILE = "/var/log/mowerbase/sik.log"

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("/var/log/mowerbase", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] sik: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("sik")

RETRY_INTERVAL = 30  # seconds between retries when radio absent


class SikForwarder:
    def __init__(self):
        self._config = {}
        self._shutdown = False
        self._ser: "serial.Serial | None" = None
        self._pipe = None
        self._bytes_total = 0
        self._bytes_this_interval = 0
        self._interval_start = time.monotonic()
        self._bytes_per_sec = 0.0
        self._radio_connected = False

    def load_config(self) -> None:
        try:
            with open(CONFIG_FILE) as f:
                self._config = json.load(f)
        except FileNotFoundError:
            log.warning("Config not found — using defaults")
            self._config = {}
        except json.JSONDecodeError as e:
            log.error("Config parse error: %s", e)

    @property
    def sik_port(self) -> str:
        return self._config.get("sik", {}).get("port", "/dev/ttyUSB0")

    @property
    def sik_baud(self) -> int:
        return self._config.get("sik", {}).get("baud", 57600)

    def _update_state(self, connected: bool, error: str = None) -> None:
        self._radio_connected = connected
        state.update_state({
            "sik": {
                "connected": connected,
                "port": self.sik_port,
                "bytes_total": self._bytes_total,
                "bytes_per_sec": round(self._bytes_per_sec, 1),
                "error": error,
            }
        })

    def open_radio(self) -> bool:
        """
        Attempt to open the SiK radio serial port.
        Returns True on success, False if port not present.
        Does NOT raise — always returns.
        """
        if not os.path.exists(self.sik_port):
            log.warning("SiK radio not found at %s — will retry in %ds",
                        self.sik_port, RETRY_INTERVAL)
            self._update_state(False, f"Device not found: {self.sik_port}")
            return False

        try:
            self._ser = serial.Serial(
                self.sik_port,
                self.sik_baud,
                timeout=1,
                write_timeout=5,
            )
            log.info("SiK radio opened: %s @ %d baud", self.sik_port, self.sik_baud)
            self._update_state(True)
            return True
        except serial.SerialException as e:
            log.warning("Cannot open SiK radio: %s — will retry in %ds", e, RETRY_INTERVAL)
            self._update_state(False, str(e))
            self._ser = None
            return False

    def close_radio(self) -> None:
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None
        self._radio_connected = False

    def open_pipe(self) -> None:
        """Open the RTCM named pipe for reading (blocking open — blocks until writer connects)."""
        waited = 0
        while not os.path.exists(RTCM_PIPE) and not self._shutdown:
            if waited == 0:
                log.info("Waiting for RTCM pipe...")
            time.sleep(2)
            waited += 2

        if self._shutdown:
            return

        try:
            # os.open with O_RDONLY | O_NONBLOCK first to avoid blocking if no writer
            # Then wrap in Python file object with blocking reads
            fd = os.open(RTCM_PIPE, os.O_RDONLY | os.O_NONBLOCK)
            # Switch to blocking mode for reads
            import fcntl as _fcntl
            flags = _fcntl.fcntl(fd, _fcntl.F_GETFL)
            _fcntl.fcntl(fd, _fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            self._pipe = os.fdopen(fd, "rb", buffering=0)
            log.info("RTCM pipe opened for reading")
        except OSError as e:
            log.error("Cannot open RTCM pipe: %s", e)
            self._pipe = None

    def _update_rate(self, n: int) -> None:
        self._bytes_total += n
        self._bytes_this_interval += n
        now = time.monotonic()
        elapsed = now - self._interval_start
        if elapsed >= 5.0:
            self._bytes_per_sec = self._bytes_this_interval / elapsed
            self._bytes_this_interval = 0
            self._interval_start = now

    def run(self) -> None:
        log.info("SiK forwarder starting (PID %d)", os.getpid())
        signal.signal(signal.SIGUSR1, self._handle_sigusr1)

        self.load_config()
        self.open_pipe()

        while not self._shutdown:
            # Ensure radio is connected
            if not self._ser or not self._ser.is_open:
                if not self.open_radio():
                    # Radio not available — drain the pipe so gps.py never blocks,
                    # then retry. Without draining, the 64KB pipe buffer fills and
                    # gps.py's os.write() blocks the entire main loop indefinitely.
                    deadline = time.monotonic() + RETRY_INTERVAL
                    while not self._shutdown and time.monotonic() < deadline:
                        if self._pipe:
                            try:
                                self._pipe.read(4096)  # discard — no radio to send to
                            except (OSError, IOError):
                                self._pipe = None
                        else:
                            time.sleep(1)
                    continue

            # Read from pipe and forward to radio
            try:
                if not self._pipe:
                    self.open_pipe()
                    if not self._pipe:
                        time.sleep(5)
                        continue

                data = self._pipe.read(4096)
                if not data:
                    # Pipe writer (ntrip.py) closed — wait for reconnect
                    log.info("RTCM pipe closed by writer — waiting for reconnect")
                    time.sleep(5)
                    self._pipe = None
                    continue

                # Forward to SiK radio
                try:
                    self._ser.write(data)
                    self._update_rate(len(data))

                    now = time.monotonic()
                    if now - self._interval_start >= 5.0:
                        self._update_state(True)

                except serial.SerialTimeoutException:
                    log.warning("SiK write timeout — radio may be congested")
                except serial.SerialException as e:
                    log.error("SiK write error: %s — reconnecting", e)
                    self.close_radio()
                    self._update_state(False, str(e))

            except (OSError, IOError) as e:
                log.error("Pipe read error: %s", e)
                self._pipe = None
                time.sleep(2)
            except Exception as e:
                log.error("Unexpected error: %s", e)
                time.sleep(1)

        log.info("SiK forwarder stopped")

    def _handle_sigusr1(self, signum, frame) -> None:
        log.info("SIGUSR1 received — reloading config")
        self.load_config()

    def stop(self) -> None:
        self._shutdown = True
        self.close_radio()
        if self._pipe:
            try:
                self._pipe.close()
            except Exception:
                pass


def main():
    svc = SikForwarder()
    try:
        svc.run()
    except KeyboardInterrupt:
        log.info("Keyboard interrupt — stopping")
    finally:
        svc.stop()


if __name__ == "__main__":
    main()
