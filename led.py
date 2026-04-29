"""
led.py — Single-colour GPIO LED controller for MowerBase.

LED on GPIO 17 (configurable). States encoded via blink patterns.
All blinking is non-blocking via a background thread.

State → pattern mapping:
  fixed_ok        → solid on
  float           → slow blink 1Hz
  surveying       → fast blink 4Hz
  awaiting_ntrip  → slow blink 1Hz (same as float)
  drift_alert     → alternating 2Hz
  no_fix          → solid on (always-on = error indicator)
  captive_portal  → slow blink 0.5Hz
  boot            → medium blink 2Hz
  off             → solid off
"""

import threading
import time
import logging

log = logging.getLogger(__name__)

# Graceful fallback if RPi.GPIO is not available (dev machine, no hardware)
try:
    import RPi.GPIO as GPIO
    _GPIO_AVAILABLE = True
except ImportError:
    log.warning("RPi.GPIO not available — LED control disabled (running on non-Pi hardware?)")
    _GPIO_AVAILABLE = False

# Blink pattern format: list of (on_seconds, off_seconds) tuples
# Repeated indefinitely.
_PATTERNS = {
    "fixed_ok":       [(9999, 0)],           # solid on (long "on", never off)
    "float":          [(0.5, 0.5)],          # 1Hz slow blink
    "surveying":      [(0.125, 0.125)],      # 4Hz fast blink
    "awaiting_ntrip": [(0.5, 0.5)],          # 1Hz (same as float)
    "drift_alert":    [(0.25, 0.25)],        # 2Hz alternating
    "no_fix":         [(9999, 0)],           # solid on
    "captive_portal": [(1.0, 1.0)],          # 0.5Hz very slow blink
    "boot":           [(0.25, 0.25)],        # 2Hz medium blink (same as drift_alert visually)
    "off":            [(0, 9999)],           # solid off
}


class Led:
    def __init__(self, gpio_pin: int = 17):
        self._pin = gpio_pin
        self._state = "off"
        self._shutdown = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

        if _GPIO_AVAILABLE:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(self._pin, GPIO.OUT)
            GPIO.output(self._pin, GPIO.LOW)
            log.info("LED initialised on GPIO %d", self._pin)
        else:
            log.info("LED stub: GPIO %d (hardware not available)", self._pin)

        self._thread = threading.Thread(
            target=self._blink_loop, daemon=True, name="led-blinker"
        )
        self._thread.start()

    def set_state(self, state: str) -> None:
        """Set the LED display state by name. Thread-safe."""
        if state not in _PATTERNS:
            log.warning("Unknown LED state '%s' — using 'off'", state)
            state = "off"
        with self._lock:
            if self._state != state:
                log.debug("LED state: %s → %s", self._state, state)
                self._state = state

    def _set_gpio(self, on: bool) -> None:
        if not _GPIO_AVAILABLE:
            return
        try:
            GPIO.output(self._pin, GPIO.HIGH if on else GPIO.LOW)
        except Exception as e:
            log.debug("LED GPIO error: %s", e)

    def _blink_loop(self) -> None:
        """Background thread: executes the current blink pattern."""
        while not self._shutdown:
            with self._lock:
                current_state = self._state

            pattern = _PATTERNS.get(current_state, _PATTERNS["off"])

            for on_s, off_s in pattern:
                if self._shutdown:
                    return

                # Check if state changed mid-pattern
                with self._lock:
                    if self._state != current_state:
                        break

                if on_s > 0:
                    self._set_gpio(True)
                    self._sleep_interruptible(on_s, current_state)

                with self._lock:
                    if self._state != current_state:
                        break

                if off_s > 0:
                    self._set_gpio(False)
                    self._sleep_interruptible(off_s, current_state)

    def _sleep_interruptible(self, duration: float, expected_state: str) -> None:
        """Sleep duration in small increments, waking early if state changes."""
        end = time.monotonic() + duration
        while time.monotonic() < end:
            with self._lock:
                if self._state != expected_state:
                    return
            time.sleep(min(0.05, end - time.monotonic()))

    def cleanup(self) -> None:
        """Stop blinking and release GPIO."""
        self._shutdown = True
        self._set_gpio(False)
        if _GPIO_AVAILABLE:
            try:
                GPIO.cleanup(self._pin)
            except Exception:
                pass
        log.info("LED cleaned up")


# Module-level singleton for easy import
_led_instance: Led | None = None


def init(gpio_pin: int = 17) -> Led:
    """Initialise and return the module-level LED instance."""
    global _led_instance
    _led_instance = Led(gpio_pin)
    return _led_instance


def set_state(led_state: str) -> None:
    """Set the module-level LED state. Call init() first."""
    if _led_instance:
        _led_instance.set_state(led_state)
    else:
        log.debug("LED not initialised — ignoring set_state('%s')", led_state)


def cleanup() -> None:
    if _led_instance:
        _led_instance.cleanup()
