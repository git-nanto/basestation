"""
oled.py — SSD1306 128x64 OLED display driver wrapper for MowerBase.

Uses luma.oled to render display layouts based on current system state.
Handles ImportError gracefully for development without OLED hardware.

Screens:
  FIXED      — normal operation
  SURVEYING  — survey-in progress bar
  NO_WIFI    — captive portal / no WiFi

I2C address defaults to 0x3C (configurable in config.json).
"""

import logging
import math

log = logging.getLogger(__name__)

# Graceful fallback if luma.oled not available
try:
    from luma.core.interface.serial import i2c as luma_i2c
    from luma.oled.device import ssd1306
    from luma.core.render import canvas
    from PIL import ImageFont
    _LUMA_AVAILABLE = True
except ImportError:
    log.warning("luma.oled not available — OLED display disabled")
    _LUMA_AVAILABLE = False


def _get_font(size: int = 10):
    """Load a small bitmap font, fallback to default."""
    if not _LUMA_AVAILABLE:
        return None
    try:
        # luma.oled default font is adequate for small text
        return ImageFont.load_default()
    except Exception:
        return None


class OledDisplay:
    def __init__(self, i2c_address: int = 0x3C, i2c_port: int = 1):
        self._device = None
        self._available = False
        self._last_state_hash = None

        if not _LUMA_AVAILABLE:
            log.info("OLED: luma.oled not available — running in stub mode")
            return

        try:
            serial_iface = luma_i2c(port=i2c_port, address=i2c_address)
            self._device = ssd1306(serial_iface, width=128, height=64)
            self._available = True
            log.info("OLED initialised at I2C 0x%02X", i2c_address)
        except Exception as e:
            log.warning("OLED init failed (address 0x%02X): %s — display disabled", i2c_address, e)

    def update(self, state: dict) -> None:
        """Render the appropriate screen based on current state."""
        if not self._available or not self._device:
            return

        sm_state = state.get("gps", {}).get("sm_state", "BOOT")

        try:
            if sm_state == "FIXED":
                self._render_fixed(state)
            elif sm_state == "SURVEYING":
                self._render_surveying(state)
            elif sm_state in ("AWAITING_NTRIP", "BOOT"):
                self._render_awaiting(state)
            elif sm_state == "NO_WIFI":
                self._render_no_wifi()
            else:
                self._render_status(sm_state)
        except Exception as e:
            log.error("OLED render error: %s", e)

    def _render_fixed(self, state: dict) -> None:
        """Normal operation display."""
        gps = state.get("gps", {})
        ntrip = state.get("ntrip", {})
        sik = state.get("sik", {})

        drift_m = gps.get("drift_current_m", 0.0) or 0.0
        drift_mm = drift_m * 1000
        rtcm_bps = ntrip.get("bytes_per_sec", 0.0)
        rtcm_kbps = rtcm_bps / 1024
        sik_ok = "OK" if sik.get("connected") else "NC"
        plugin = ntrip.get("plugin", "?").upper()[:6]
        mountpoint = ""
        # Try to get mountpoint from config state if available
        mp_raw = state.get("config_mountpoint", "")
        if mp_raw:
            mountpoint = mp_raw[:6]

        with canvas(self._device) as draw:
            font = _get_font()
            draw.text((0, 0),  "MowerBase     [FIXED]", font=font, fill="white")
            draw.text((0, 16), f"{plugin}  {mountpoint}", font=font, fill="white")
            draw.text((0, 32), f"Drift: {drift_mm:+.0f}mm", font=font, fill="white")
            draw.text((0, 48), f"RTCM:{rtcm_kbps:.1f}k/s SiK:{sik_ok}", font=font, fill="white")

    def _render_surveying(self, state: dict) -> None:
        """Survey-In progress display with progress bar."""
        gps = state.get("gps", {})
        elapsed = gps.get("svin_elapsed_s", 0) or 0
        target = gps.get("svin_min_duration_s", 300) or 300
        accuracy = gps.get("accuracy_m")
        num_sats = gps.get("num_sats", 0) or 0
        pdop = gps.get("pdop", 99.9) or 99.9

        # Progress bar: 0..1
        progress = min(1.0, elapsed / max(target, 1))
        bar_width = 100
        filled = int(bar_width * progress)

        elapsed_min = elapsed // 60
        elapsed_sec = elapsed % 60
        target_min = target // 60
        target_sec = target % 60

        acc_str = f"{accuracy:.3f}m" if accuracy else "---"

        with canvas(self._device) as draw:
            font = _get_font()
            draw.text((0, 0),  "MowerBase  [SURVEYING]", font=font, fill="white")
            # Progress bar (y=16..24)
            draw.rectangle((0, 16, bar_width, 24), outline="white", fill="black")
            if filled > 0:
                draw.rectangle((0, 16, filled, 24), outline="white", fill="white")
            # Time
            draw.text((0, 28), f"{elapsed_min}:{elapsed_sec:02d} / {target_min}:{target_sec:02d}", font=font, fill="white")
            draw.text((0, 40), f"Accuracy: {acc_str}", font=font, fill="white")
            draw.text((0, 52), f"Sats:{num_sats}  PDOP:{pdop:.1f}", font=font, fill="white")

    def _render_awaiting(self, state: dict) -> None:
        """Waiting for NTRIP display."""
        ntrip = state.get("ntrip", {})
        plugin = ntrip.get("plugin", "?").upper()[:8]
        connected = ntrip.get("connected", False)
        ntrip_str = "Connecting..." if not connected else "Connected"

        with canvas(self._device) as draw:
            font = _get_font()
            draw.text((0, 0),  "MowerBase", font=font, fill="white")
            draw.text((0, 16), "[AWAITING NTRIP]", font=font, fill="white")
            draw.text((0, 32), f"NTRIP: {ntrip_str}", font=font, fill="white")
            draw.text((0, 48), f"Plugin: {plugin}", font=font, fill="white")

    def _render_no_wifi(self) -> None:
        """Captive portal / no WiFi display."""
        with canvas(self._device) as draw:
            font = _get_font()
            draw.text((0, 0),  "MowerBase   [NO WIFI]", font=font, fill="white")
            draw.text((0, 16), "Connect to WiFi:", font=font, fill="white")
            draw.text((0, 32), "SSID: MowerBase-Setup", font=font, fill="white")
            draw.text((0, 48), "Then: 192.168.4.1", font=font, fill="white")

    def _render_status(self, status: str) -> None:
        """Generic status display."""
        with canvas(self._device) as draw:
            font = _get_font()
            draw.text((0, 0),  "MowerBase", font=font, fill="white")
            draw.text((0, 24), status, font=font, fill="white")

    def clear(self) -> None:
        """Clear the display."""
        if self._available and self._device:
            try:
                self._device.clear()
            except Exception:
                pass

    def cleanup(self) -> None:
        """Clean up display resources."""
        self.clear()
        if self._device:
            try:
                self._device.cleanup()
            except Exception:
                pass
        log.info("OLED cleaned up")


# Module-level singleton
_oled_instance: OledDisplay | None = None


def init(i2c_address: int = 0x3C, i2c_port: int = 1) -> OledDisplay:
    global _oled_instance
    _oled_instance = OledDisplay(i2c_address, i2c_port)
    return _oled_instance


def update(state_dict: dict) -> None:
    if _oled_instance:
        _oled_instance.update(state_dict)


def cleanup() -> None:
    if _oled_instance:
        _oled_instance.cleanup()
