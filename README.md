# MowerBase — RTK Base Station

A self-contained RTK base station built on a **Raspberry Pi Zero W (v1)** with a Waveshare LC29H(BS) GPS/RTK HAT. Provides centimetre-accurate RTCM3 correction data to an autonomous lawn mower via a 915 MHz SiK radio link.

Part of the [Autonomous Lawn Mower](../README.md) project — Block B06.

---

## Quick Start

### What you need

| Item | Notes |
|---|---|
| Raspberry Pi Zero W v1 | Single-core ARMv6, 512 MB RAM |
| Waveshare LC29H(BS) GPS/RTK HAT | Stacks directly on Pi 40-pin header |
| SSD1306 OLED 128×64 | I2C (address 0x3C or 0x3D) — optional |
| Holybro SiK V3 915 MHz radio | USB to Pi via micro-USB OTG adapter |
| Milwaukee 18 V battery → LM2596 step-down → 5.1 V | Power supply |
| Active GNSS antenna with clear sky view | Included with LC29H(BS) HAT |

---

### Step 1 — Prepare the Pi

Flash **Raspberry Pi OS Lite 32-bit Bookworm** to an SD card.

Boot and configure:

```bash
sudo raspi-config
```

- **Interface Options → Serial** → disable login shell, **enable hardware serial**
- **Interface Options → I2C** → enable

Add to `/boot/firmware/config.txt`:

```
dtoverlay=disable-bt
```

Reboot:

```bash
sudo reboot
```

---

### Step 2 — Connect hardware

1. Seat the LC29H(BS) HAT on the Pi's 40-pin GPIO header
2. Set the UART jumper to **Position B** (routes LC29H serial to Pi GPIO UART)
3. Connect the GNSS antenna to the HAT
4. Connect the SiK radio via micro-USB OTG adapter to the Pi data port (closest to SD card)

---

### Step 3 — Install

Copy the project files to the Pi and run the installer:

```bash
scp -r . pi@<pi-ip>:~/mowerbase
ssh pi@<pi-ip> "sudo bash ~/mowerbase/install.sh"
```

The installer (~10–15 min on first run):
- Creates the `mowerbase` system user
- Installs Python packages into a venv
- Writes default config to `/etc/mowerbase/config.json`
- Installs and enables 4 systemd services
- Configures `authbind` for port 80
- Creates the always-on **MowerBase** WiFi AP
- Sets hostname to `mowerbase`

---

### Step 4 — Connect to WiFi

1. On your phone or laptop, connect to the **MowerBase** WiFi network (open, no password)
2. Your browser will open automatically — if not, go to `http://10.42.0.1`
3. Go to **Network → Change WiFi** and connect to your home network

Once connected to home WiFi, the device is also reachable at `http://mowerbase.local`.

---

### Step 5 — Wait for Survey-In

On first boot (no stored position), the device starts a **GPS Survey-In** automatically:

1. Open `http://mowerbase.local` (or `http://10.42.0.1` via MowerBase AP)
2. Dashboard shows **SURVEYING** — a progress bar counts to 60 seconds
3. Wait for status to change to **FIXED** (typically 1–3 minutes)
4. The surveyed position is saved — future boots skip the survey and enter FIXED immediately

The status LED shows:
- **Fast amber blink** = Survey-In in progress
- **Green solid** = FIXED, corrections transmitting

---

### Step 6 — Check RTCM output

Go to **RTCM** tab to verify corrections are flowing:
- Messages 1005, 1074, 1084, 1094, 1124 should all show counts
- Bytes/sec should be ~1–2 KB/s

The SiK radio (connected to Pi USB) forwards corrections to the mower automatically once the survey completes.

---

## Ongoing use

**Deploying code updates:**

```bash
bash update.sh
```

Copies updated Python files, templates, static assets, and service files to the Pi then restarts all services.

**Web UI pages:**

| Page | Purpose |
|---|---|
| `/` Dashboard | Fix status, survey progress, drift, SiK radio status |
| `/config` Settings | Survey-In parameters, SiK port/baud, NTRIP source selection |
| `/position` | Stored position, drift history chart, re-survey |
| `/network` | MowerBase AP status, home WiFi connection |
| `/rtcm` | Live RTCM frame counts and rate |
| `/logs` | Live log tail for all services |
| `/swagger` | API documentation |

---

## NTRIP relay mode (optional)

If you have access to an external NTRIP caster (e.g. AUSCORS, RTK2Go), MowerBase can relay its corrections instead of using its own GPS Survey-In:

1. Go to **Settings → Correction Source → NTRIP Relay**
2. Enter your caster host, port, mountpoint, username, and password
3. Click Save — corrections flow from the external caster to both the SiK radio and the local NTRIP server simultaneously

NTRIP relay requires a home WiFi connection (the device needs internet access to reach the caster).

**Connecting MAVProxy to the local NTRIP server:**

```
--ntrip=mowerbase.local:2101/MOWERBASE
```

---

## Troubleshooting

### No GPS HAT detected

**Symptom:** Dashboard shows **NO GPS** banner; Settings page shows "GPS HAT not detected" warning.

**Fix:**
1. Power off the Pi
2. Seat the LC29H(BS) HAT firmly on the GPIO 40-pin header
3. Ensure the **UART jumper is in Position B** (not A or C)
4. Power on — survey starts automatically

### Survey-In never completes

**Symptom:** Dashboard stuck on SURVEYING for more than 5 minutes; transitions to ERROR.

**Fix:**
- Move the antenna to a location with a clear, unobstructed view of the sky
- Go to **Settings → Survey-In** and increase the Accuracy Limit to 3.0 m or 5.0 m
- Click the **Re-Survey** button on the Position page

### NTRIP relay not working

**Symptom:** Settings page shows "Home WiFi required for NTRIP relay" warning; no RTCM from external caster.

**Fix:**
1. Go to **Network** and connect to your home WiFi
2. Return to **Settings** — the warning should disappear
3. Verify caster credentials (host, mountpoint, username, password)
4. Check the **Logs** page for connection errors from the `ntrip` service

### No GPS + No home WiFi

**Symptom:** Dashboard shows red banner "No GPS + No home WiFi".

**Steps:**
1. Connect to the **MowerBase** WiFi network (open, no password) from your phone
2. The browser opens automatically — go to **Network → Change WiFi**
3. Connect to your home network, then check the GPS HAT hardware connection (see above)

### Can't reach `mowerbase.local`

- Try the IP address directly: `http://192.168.0.x` (check your router's DHCP table)
- Or connect to the **MowerBase** AP and go to `http://10.42.0.1`
- mDNS (`mowerbase.local`) requires `avahi-daemon` — should be running after install

### SiK radio status meanings

| Dashboard status | Meaning |
|---|---|
| **Not on USB** | USB device not found at configured port (`/dev/ttyUSB0`). Check cable and OTG adapter. |
| **Found, idle (0.0 KB/s)** | Radio is connected and the port is open, but no RTCM data is flowing. GPS may not be in FIXED state yet, or correction source is NTRIP relay and not connected. |
| **Active (X KB/s)** | Radio is transmitting RTCM corrections. Whether the remote end is receiving depends on RF conditions. |

**If status is "Not on USB":**
- Check the micro-USB OTG adapter and USB cable between Pi and SiK radio
- Verify the port in **Settings → SiK Radio** matches your device (default `/dev/ttyUSB0`)
- Check the **Logs → sik** tab for connection errors

**If status is "Found, idle":**
- Wait for GPS Survey-In to complete (status must reach FIXED before RTCM flows)
- If using NTRIP relay mode, check the NTRIP client is connected to the caster

### Position tab with no GPS

The Position tab remains accessible when GPS is not connected because it can still show a **stored position** from a previous survey session, plus the drift history chart. A warning banner is shown at the top and the Re-Survey button is disabled until the GPS HAT is reconnected.

---

## Architecture

```
┌────────────────────────────────────────────────────┐
│  Raspberry Pi Zero W v1                            │
│                                                    │
│  mowerbase-gps.service                             │
│    LC29H(BS) UART → parse RTCM3 + NMEA             │
│    → rtcm.pipe + rtcm_ntrip.pipe (tmpfs)           │
│                                                    │
│  mowerbase-sik.service                             │
│    rtcm.pipe → SiK radio USB → RF → Pixhawk        │
│                                                    │
│  mowerbase-ntrip.service                           │
│    GPS mode:   rtcm_ntrip.pipe → TCP server :2101  │
│    NTRIP mode: external caster → rtcm.pipe + TCP   │
│                                                    │
│  mowerbase-web.service                             │
│    Flask :80 — dashboard, config, WiFi, logs       │
│    OLED updater thread + LED control               │
└────────────────────────────────────────────────────┘
```

All services communicate via `/run/mowerbase/state.json` (tmpfs, written by `gps.py` every 2 s).

The Pi broadcasts its own open WiFi AP (**MowerBase**, no password) at all times via NetworkManager concurrent AP+STA. It also connects to your home WiFi when credentials are saved. Both run simultaneously on the BCM43430 chip.

---

## File layout

```
install.sh          One-shot Pi installer
update.sh           Deploy code updates via SCP
gps.py              GPS serial reader + Survey-In state machine
sik_forwarder.py    RTCM pipe → SiK radio
ntrip.py            NTRIP server/client + correction relay
web.py              Flask web server + OLED + LED
oled.py             SSD1306 driver wrapper
led.py              GPIO LED controller
state.py            state.json read/write helpers
templates/          Flask Jinja2 HTML templates
static/             CSS + JavaScript
systemd/            systemd service unit files
```
