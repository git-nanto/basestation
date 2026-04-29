"""
ntrip.py — NTRIP server (caster) + optional NTRIP client for MowerBase.

Two modes, controlled by config.json "correction_source":

  "gps"   — reads RTCM from /run/mowerbase/rtcm_ntrip.pipe (written by gps.py)
             and serves it to connected NTRIP clients over TCP.

  "ntrip" — connects to an external NTRIP caster (AUSCORS etc.), receives RTCM,
             serves it to local NTRIP clients AND writes it to /run/mowerbase/rtcm.pipe
             so sik_forwarder continues to work unchanged.

NTRIP v1 server protocol (ICY 200 OK — supported by MAVProxy, Mission Planner):
  Client sends:  GET /MOUNTPOINT HTTP/1.0\\r\\n...\\r\\n
  Server sends:  ICY 200 OK\\r\\n\\r\\n  then raw RTCM bytes indefinitely.

SIGUSR1 = reload config (switches source mode if changed).
"""

import os
import sys
import json
import time
import signal
import socket
import base64
import logging
import fcntl
import threading
import queue
from typing import Optional

import state

# ── Paths ─────────────────────────────────────────────────────────────────────
RTCM_PIPE = "/run/mowerbase/rtcm.pipe"           # write here in NTRIP relay mode
RTCM_NTRIP_PIPE = "/run/mowerbase/rtcm_ntrip.pipe"  # read here in GPS mode
CONFIG_FILE = "/etc/mowerbase/config.json"
PID_FILE = "/run/mowerbase/ntrip.pid"
LOG_FILE = "/var/log/mowerbase/ntrip.log"

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("/var/log/mowerbase", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] ntrip: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("ntrip")

STATE_UPDATE_INTERVAL = 5.0   # seconds between state.json writes
NTRIP_CONNECT_RETRY = 15      # seconds between NTRIP client reconnect attempts


class NtripService:
    def __init__(self):
        self._config: dict = {}
        self._shutdown = False
        self._reload_config = False

        # Broadcast: list of per-client queues (max 50 frames each)
        self._client_queues: list[queue.Queue] = []
        self._client_lock = threading.Lock()

        # State tracking
        self._server_clients = 0
        self._server_bytes_sent = 0
        self._client_connected = False
        self._client_bytes_received = 0
        self._correction_source = "gps"

        # SiK pipe write fd (used in NTRIP relay mode)
        self._sik_pipe_fd: Optional[int] = None

        # Source thread handle
        self._source_thread: Optional[threading.Thread] = None

    # ── Config ────────────────────────────────────────────────────────────────

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
    def _server_enabled(self) -> bool:
        return self._config.get("ntrip_server", {}).get("enabled", True)

    @property
    def _server_port(self) -> int:
        return int(self._config.get("ntrip_server", {}).get("port", 2101))

    @property
    def _server_mountpoint(self) -> str:
        return self._config.get("ntrip_server", {}).get("mountpoint", "MOWERBASE")

    @property
    def _client_host(self) -> str:
        return self._config.get("ntrip_client", {}).get("host", "")

    @property
    def _client_port(self) -> int:
        return int(self._config.get("ntrip_client", {}).get("port", 2101))

    @property
    def _client_mountpoint(self) -> str:
        return self._config.get("ntrip_client", {}).get("mountpoint", "")

    @property
    def _client_username(self) -> str:
        return self._config.get("ntrip_client", {}).get("username", "")

    @property
    def _client_password(self) -> str:
        return self._config.get("ntrip_client", {}).get("password", "")

    # ── Broadcast ─────────────────────────────────────────────────────────────

    def _broadcast(self, data: bytes) -> None:
        """Deliver RTCM data to all connected server clients."""
        with self._client_lock:
            dead = []
            for q in self._client_queues:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._client_queues.remove(q)

    def _add_client_queue(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=50)
        with self._client_lock:
            self._client_queues.append(q)
        return q

    def _remove_client_queue(self, q: queue.Queue) -> None:
        with self._client_lock:
            try:
                self._client_queues.remove(q)
            except ValueError:
                pass

    # ── SiK pipe (relay mode write) ───────────────────────────────────────────

    def _open_sik_pipe(self) -> None:
        """Open rtcm.pipe for writing (blocks until sik_forwarder connects)."""
        while not self._shutdown:
            if os.path.exists(RTCM_PIPE):
                try:
                    fd = os.open(RTCM_PIPE, os.O_WRONLY)
                    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                    self._sik_pipe_fd = fd
                    log.info("SiK pipe open for writing (relay mode)")
                    return
                except OSError as e:
                    log.warning("Cannot open SiK pipe: %s — retrying", e)
            time.sleep(5)

    def _write_sik_pipe(self, data: bytes) -> None:
        if self._sik_pipe_fd is None:
            return
        try:
            os.write(self._sik_pipe_fd, data)
        except BlockingIOError:
            pass
        except OSError:
            try:
                os.close(self._sik_pipe_fd)
            except OSError:
                pass
            self._sik_pipe_fd = None
            t = threading.Thread(target=self._open_sik_pipe, daemon=True)
            t.start()

    # ── GPS pipe source thread ────────────────────────────────────────────────

    def _gps_pipe_reader(self) -> None:
        """Read RTCM from rtcm_ntrip.pipe (written by gps.py) and broadcast."""
        log.info("GPS pipe reader starting")
        while not self._shutdown and self._correction_source == "gps":
            # Wait for pipe to exist
            waited = 0
            while not os.path.exists(RTCM_NTRIP_PIPE) and not self._shutdown:
                if waited == 0:
                    log.info("Waiting for NTRIP pipe...")
                time.sleep(2)
                waited += 2

            if self._shutdown:
                return

            pipe = None
            try:
                fd = os.open(RTCM_NTRIP_PIPE, os.O_RDONLY | os.O_NONBLOCK)
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
                pipe = os.fdopen(fd, "rb", buffering=0)
                log.info("NTRIP pipe opened for reading")

                while not self._shutdown and self._correction_source == "gps":
                    data = pipe.read(4096)
                    if not data:
                        log.info("NTRIP pipe closed by writer — waiting")
                        break
                    self._broadcast(data)

            except OSError as e:
                log.error("GPS pipe error: %s", e)
                time.sleep(5)
            finally:
                if pipe:
                    try:
                        pipe.close()
                    except Exception:
                        pass

        log.info("GPS pipe reader stopped")

    # ── NTRIP client source thread ────────────────────────────────────────────

    def _ntrip_client(self) -> None:
        """Connect to external NTRIP caster, relay RTCM to broadcast + SiK pipe."""
        log.info("NTRIP client starting")

        # Ensure we can write to the SiK pipe
        t = threading.Thread(target=self._open_sik_pipe, daemon=True)
        t.start()

        while not self._shutdown and self._correction_source == "ntrip":
            host = self._client_host
            port = self._client_port
            mountpoint = self._client_mountpoint

            if not host or not mountpoint:
                log.warning("NTRIP client: host/mountpoint not configured — waiting")
                time.sleep(NTRIP_CONNECT_RETRY)
                continue

            log.info("NTRIP client connecting to %s:%d/%s", host, port, mountpoint)
            self._client_connected = False

            try:
                sock = socket.create_connection((host, port), timeout=10)
            except OSError as e:
                log.warning("NTRIP connect failed: %s — retry in %ds", e, NTRIP_CONNECT_RETRY)
                time.sleep(NTRIP_CONNECT_RETRY)
                continue

            try:
                # Build NTRIP v1 request
                creds = ""
                if self._client_username:
                    raw = f"{self._client_username}:{self._client_password}"
                    creds = f"Authorization: Basic {base64.b64encode(raw.encode()).decode()}\r\n"

                request = (
                    f"GET /{mountpoint} HTTP/1.0\r\n"
                    f"Host: {host}:{port}\r\n"
                    f"Ntrip-Version: Ntrip/1.0\r\n"
                    f"User-Agent: NTRIP MowerBase/1.0\r\n"
                    f"{creds}"
                    f"\r\n"
                )
                sock.sendall(request.encode())

                # Read response header (look for ICY 200 OK or HTTP 200)
                header = b""
                while b"\r\n\r\n" not in header:
                    chunk = sock.recv(256)
                    if not chunk:
                        raise OSError("Connection closed before headers complete")
                    header += chunk
                    if len(header) > 2048:
                        raise OSError("Response header too large")

                first_line = header.split(b"\r\n")[0].decode(errors="replace")
                if "200" not in first_line:
                    raise OSError(f"NTRIP caster rejected request: {first_line}")

                log.info("NTRIP client connected: %s", first_line.strip())
                self._client_connected = True

                # Any bytes after the header separator are already RTCM data
                sep = header.find(b"\r\n\r\n")
                tail = header[sep + 4:]
                if tail:
                    self._client_bytes_received += len(tail)
                    self._broadcast(tail)
                    self._write_sik_pipe(tail)

                sock.settimeout(30)

                while not self._shutdown and self._correction_source == "ntrip":
                    data = sock.recv(4096)
                    if not data:
                        raise OSError("NTRIP caster closed connection")
                    self._client_bytes_received += len(data)
                    self._broadcast(data)
                    self._write_sik_pipe(data)

            except OSError as e:
                log.warning("NTRIP client error: %s — reconnecting in %ds", e, NTRIP_CONNECT_RETRY)
            finally:
                self._client_connected = False
                try:
                    sock.close()
                except Exception:
                    pass

            if not self._shutdown and self._correction_source == "ntrip":
                time.sleep(NTRIP_CONNECT_RETRY)

        log.info("NTRIP client stopped")

    # ── NTRIP server ──────────────────────────────────────────────────────────

    def _handle_server_client(self, conn: socket.socket, addr: tuple, q: queue.Queue) -> None:
        """Handle a single connected NTRIP client."""
        log.info("NTRIP client connected from %s:%d", addr[0], addr[1])
        with self._client_lock:
            self._server_clients += 1
        try:
            # Read the GET request
            conn.settimeout(10)
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = conn.recv(512)
                if not chunk:
                    return
                raw += chunk
                if len(raw) > 2048:
                    return

            first_line = raw.split(b"\r\n")[0].decode(errors="replace")
            log.debug("NTRIP server: %s from %s", first_line.strip(), addr[0])

            # Send ICY 200 OK (NTRIP v1 — no Content-Type header needed)
            conn.sendall(b"ICY 200 OK\r\n\r\n")
            conn.settimeout(None)

            # Stream RTCM
            while not self._shutdown:
                try:
                    data = q.get(timeout=5)
                except queue.Empty:
                    continue
                try:
                    conn.sendall(data)
                    self._server_bytes_sent += len(data)
                except OSError:
                    break

        except OSError as e:
            log.debug("NTRIP server client %s disconnected: %s", addr[0], e)
        finally:
            self._remove_client_queue(q)
            with self._client_lock:
                self._server_clients = max(0, self._server_clients - 1)
            try:
                conn.close()
            except Exception:
                pass
            log.info("NTRIP client %s:%d disconnected", addr[0], addr[1])

    def _ntrip_server(self) -> None:
        """TCP server that accepts NTRIP client connections."""
        port = self._server_port
        log.info("NTRIP server listening on :%d", port)

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("", port))
            srv.listen(5)
            srv.settimeout(2)
        except OSError as e:
            log.error("NTRIP server bind failed on port %d: %s", port, e)
            return

        try:
            while not self._shutdown:
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                q = self._add_client_queue()
                t = threading.Thread(
                    target=self._handle_server_client,
                    args=(conn, addr, q),
                    daemon=True,
                )
                t.start()
        finally:
            srv.close()
            log.info("NTRIP server stopped")

    # ── State updates ─────────────────────────────────────────────────────────

    def _state_writer(self) -> None:
        while not self._shutdown:
            state.update_state({
                "ntrip": {
                    "correction_source": self._correction_source,
                    "server_clients": self._server_clients,
                    "server_bytes_sent": self._server_bytes_sent,
                    "client_connected": self._client_connected,
                    "client_bytes_received": self._client_bytes_received,
                }
            })
            time.sleep(STATE_UPDATE_INTERVAL)

    # ── Source thread management ──────────────────────────────────────────────

    def _start_source_thread(self) -> None:
        if self._correction_source == "ntrip":
            target = self._ntrip_client
            name = "ntrip-client"
        else:
            target = self._gps_pipe_reader
            name = "gps-pipe-reader"
        self._source_thread = threading.Thread(target=target, daemon=True, name=name)
        self._source_thread.start()
        log.info("Source thread started: %s", name)

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _handle_sigusr1(self, signum, frame) -> None:
        self._reload_config = True

    def _handle_sigterm(self, signum, frame) -> None:
        log.info("SIGTERM received — shutting down")
        self._shutdown = True

    # ── Main ──────────────────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("MowerBase NTRIP service starting (PID %d)", os.getpid())
        signal.signal(signal.SIGUSR1, self._handle_sigusr1)
        signal.signal(signal.SIGTERM, self._handle_sigterm)

        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))

        self.load_config()
        self._correction_source = self._config.get("correction_source", "gps")

        # Start state writer
        threading.Thread(target=self._state_writer, daemon=True, name="state-writer").start()

        # Start NTRIP server if enabled
        if self._server_enabled:
            threading.Thread(target=self._ntrip_server, daemon=True, name="ntrip-server").start()
        else:
            log.info("NTRIP server disabled in config")

        # Start source thread
        self._start_source_thread()

        # Main loop — handle config reloads
        prev_source = self._correction_source
        while not self._shutdown:
            time.sleep(1)
            if self._reload_config:
                self._reload_config = False
                old_source = self._correction_source
                self.load_config()
                self._correction_source = self._config.get("correction_source", "gps")
                if self._correction_source != old_source:
                    log.info("Correction source changed: %s → %s", old_source, self._correction_source)
                    # Old source thread will exit on its own (_correction_source check in loop)
                    # Start new source thread
                    self._start_source_thread()
                    # Close SiK pipe if switching away from relay mode
                    if self._correction_source == "gps" and self._sik_pipe_fd is not None:
                        try:
                            os.close(self._sik_pipe_fd)
                        except OSError:
                            pass
                        self._sik_pipe_fd = None

        log.info("NTRIP service stopped")


def main():
    svc = NtripService()
    try:
        svc.run()
    except KeyboardInterrupt:
        log.info("Keyboard interrupt — stopping")


if __name__ == "__main__":
    main()
