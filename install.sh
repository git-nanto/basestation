#!/usr/bin/env bash
# install.sh — MowerBase one-shot install script
#
# Designed for Raspberry Pi Zero W v1 running Raspberry Pi OS Lite 32-bit Bookworm.
#
# Run as root: sudo bash install.sh
#
# What this does:
#   1. Creates mowerbase system user
#   2. Installs system packages via apt
#   3. Creates directories
#   4. Creates Python venv and installs pip packages
#   5. Copies application files
#   6. Writes default config.json (if not already present)
#   7. Installs and enables systemd services
#   8. Adds /run/mowerbase tmpfs to /etc/fstab
#   9. Starts GPS and web services

set -euo pipefail

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Must be root ───────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  error "This script must be run as root. Try: sudo bash install.sh"
fi

# ── Script location (source files are alongside this script) ──────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Paths ──────────────────────────────────────────────────────────────────────
APP_DIR="/opt/mowerbase"
VENV_DIR="$APP_DIR/venv"
CONFIG_DIR="/etc/mowerbase"
DATA_DIR="/var/lib/mowerbase"
LOG_DIR="/var/log/mowerbase"
RUN_DIR="/run/mowerbase"
SYSTEMD_DIR="/etc/systemd/system"

# ── 1. Create mowerbase system user ───────────────────────────────────────────
info "Step 1/12: Creating mowerbase system user..."
if id "mowerbase" &>/dev/null; then
  warn "User 'mowerbase' already exists — skipping"
else
  useradd --system --no-create-home --shell /bin/false mowerbase
  success "User 'mowerbase' created"
fi

# Add to required groups
usermod -aG dialout mowerbase || warn "Could not add mowerbase to dialout group"
usermod -aG gpio    mowerbase || warn "Could not add mowerbase to gpio group (may not exist yet)"
usermod -aG plugdev mowerbase || warn "Could not add mowerbase to plugdev group"
usermod -aG i2c     mowerbase || warn "Could not add mowerbase to i2c group (may not exist)"

# ── 2. Install system packages ─────────────────────────────────────────────────
info "Step 2/12: Installing system packages (this may take a few minutes)..."
apt-get update -qq

APT_PACKAGES=(
  python3
  python3-venv
  python3-serial
  python3-pil
  python3-flask
  python3-requests
  python3-smbus2
  avahi-daemon
  authbind
  i2c-tools
  sqlite3
  network-manager
  git
)

apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"
success "System packages installed"

# Ensure avahi is enabled
systemctl enable avahi-daemon || true
systemctl start  avahi-daemon || true

# ── 3. Create directories ──────────────────────────────────────────────────────
info "Step 3/12: Creating directories..."
mkdir -p "$APP_DIR" "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR"

chown -R mowerbase:mowerbase "$APP_DIR" "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR"
chmod 750 "$CONFIG_DIR"  # config may contain credentials
success "Directories created"

# ── 4. Create Python venv ──────────────────────────────────────────────────────
info "Step 4/12: Creating Python venv at $VENV_DIR..."
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv --system-site-packages "$VENV_DIR"
  success "Venv created (with system-site-packages)"
else
  warn "Venv already exists at $VENV_DIR — skipping creation"
fi

# Upgrade pip inside venv
"$VENV_DIR/bin/pip" install --quiet --upgrade pip

# ── 5. Install pip packages into venv ─────────────────────────────────────────
info "Step 5/12: Installing Python packages into venv..."

# Note: on Pi Zero W v1 (ARMv6), some packages may take a while to build.
# Most of these are also installed via apt (system-site-packages) but we list
# them here as a safety net in case the apt versions are missing or outdated.
PIP_PACKAGES=(
  pyserial
  pynmea2
  flask
  requests
  "luma.oled"
  smbus2
  Pillow
  RPi.GPIO
)

"$VENV_DIR/bin/pip" install --quiet "${PIP_PACKAGES[@]}"
success "Python packages installed"

# ── 6. Copy application files ──────────────────────────────────────────────────
info "Step 6/12: Copying application files to $APP_DIR..."

PY_FILES=(
  state.py
  gps.py
  sik_forwarder.py
  ntrip.py
  web.py
  oled.py
  led.py
)

for f in "${PY_FILES[@]}"; do
  if [[ -f "$SCRIPT_DIR/$f" ]]; then
    cp "$SCRIPT_DIR/$f" "$APP_DIR/"
  else
    warn "Source file not found: $SCRIPT_DIR/$f"
  fi
done

# Copy directories
for d in templates static; do
  if [[ -d "$SCRIPT_DIR/$d" ]]; then
    cp -r "$SCRIPT_DIR/$d" "$APP_DIR/"
  else
    warn "Source directory not found: $SCRIPT_DIR/$d"
  fi
done

chown -R mowerbase:mowerbase "$APP_DIR"
success "Application files copied"

# ── 7. Write default config.json ──────────────────────────────────────────────
info "Step 7/12: Writing default config.json..."
if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  cat > "$CONFIG_DIR/config.json" << 'EOF'
{
  "correction_source": "gps",
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
  },
  "oled": {
    "i2c_address": "0x3C"
  },
  "led": {
    "gpio_pin": 17
  },
  "button": {
    "gpio_pin": 27
  },
  "ntrip_client": {
    "host": "",
    "port": 2101,
    "mountpoint": "",
    "username": "",
    "password": ""
  },
  "ntrip_server": {
    "enabled": true,
    "port": 2101,
    "mountpoint": "MOWERBASE"
  }
}
EOF
  chown mowerbase:mowerbase "$CONFIG_DIR/config.json"
  chmod 640 "$CONFIG_DIR/config.json"
  success "Default config.json written"
else
  warn "Config file already exists at $CONFIG_DIR/config.json — not overwriting"
fi

# ── 8. Install systemd services ───────────────────────────────────────────────
info "Step 8/12: Installing systemd service files..."

SERVICES=(
  mowerbase-gps.service
  mowerbase-sik.service
  mowerbase-ntrip.service
  mowerbase-web.service
  mowerbase-ap.service
)

for svc in "${SERVICES[@]}"; do
  if [[ -f "$SCRIPT_DIR/systemd/$svc" ]]; then
    cp "$SCRIPT_DIR/systemd/$svc" "$SYSTEMD_DIR/"
    success "Installed $svc"
  else
    warn "Service file not found: $SCRIPT_DIR/systemd/$svc"
  fi
done

systemctl daemon-reload
success "systemd reloaded"

# ── 9. Sudoers rule — allow mowerbase to restart its own services ──────────────
info "Step 9/12: Adding sudoers rule for service restart..."
SUDOERS_FILE="/etc/sudoers.d/mowerbase"
cat > "$SUDOERS_FILE" << 'EOF'
# Allow mowerbase user to restart/reload mowerbase services (web UI + update.sh)
mowerbase ALL=(ALL) NOPASSWD: /bin/systemctl restart mowerbase-gps mowerbase-sik mowerbase-ntrip mowerbase-web
mowerbase ALL=(ALL) NOPASSWD: /bin/systemctl daemon-reload
mowerbase ALL=(ALL) NOPASSWD: /bin/cp /tmp/mb-svc/mowerbase-gps.service /etc/systemd/system/mowerbase-gps.service
mowerbase ALL=(ALL) NOPASSWD: /bin/cp /tmp/mb-svc/mowerbase-sik.service /etc/systemd/system/mowerbase-sik.service
mowerbase ALL=(ALL) NOPASSWD: /bin/cp /tmp/mb-svc/mowerbase-ntrip.service /etc/systemd/system/mowerbase-ntrip.service
mowerbase ALL=(ALL) NOPASSWD: /bin/cp /tmp/mb-svc/mowerbase-web.service /etc/systemd/system/mowerbase-web.service
EOF
chmod 440 "$SUDOERS_FILE"
success "Sudoers rule written to $SUDOERS_FILE"

# ── 10. authbind — allow mowerbase to bind port 80 ───────────────────────────
info "Step 10/13: Configuring authbind for port 80..."
touch /etc/authbind/byport/80
chmod 500 /etc/authbind/byport/80
chown mowerbase /etc/authbind/byport/80
success "authbind configured for port 80"

# ── 10b. Persistent concurrent AP — NetworkManager profile ───────────────────
info "Setting up always-on MowerBase WiFi AP (concurrent AP+STA)..."

# Disable system dnsmasq so NM can run its own for AP DHCP
systemctl disable dnsmasq 2>/dev/null || true
systemctl stop    dnsmasq 2>/dev/null || true

if nmcli con show mowerbase-ap &>/dev/null; then
  warn "AP profile 'mowerbase-ap' already exists — skipping"
else
  nmcli con add type wifi ifname wlan0 con-name "mowerbase-ap" \
    ssid "MowerBase" \
    wifi.mode ap \
    ipv4.method shared \
    ipv4.addresses "10.42.0.1/24" \
    connection.autoconnect no
  success "AP profile 'mowerbase-ap' created (open, 10.42.0.1, managed by mowerbase-ap.service)"
fi

# Copy and install the AP manager script
cp "$SCRIPT_DIR/ap_manager.sh" "$APP_DIR/"
chmod +x "$APP_DIR/ap_manager.sh"
chown root:root "$APP_DIR/ap_manager.sh"

# ── 11. Enable services ────────────────────────────────────────────────────────
info "Step 11/13: Enabling services..."
for svc in "${SERVICES[@]}"; do
  systemctl enable "$svc" 2>/dev/null && success "Enabled $svc" || warn "Could not enable $svc"
done

# ── 12. Add tmpfs for /run/mowerbase ──────────────────────────────────────────
info "Step 12/13: Configuring tmpfs for $RUN_DIR..."
FSTAB_ENTRY="tmpfs  $RUN_DIR  tmpfs  defaults,noatime,mode=755,uid=mowerbase,gid=mowerbase  0  0"

if grep -q "$RUN_DIR" /etc/fstab; then
  warn "tmpfs entry for $RUN_DIR already in /etc/fstab — skipping"
else
  echo "$FSTAB_ENTRY" >> /etc/fstab
  success "tmpfs entry added to /etc/fstab"
fi

# Mount it now without reboot
mkdir -p "$RUN_DIR"
mount "$RUN_DIR" 2>/dev/null || warn "Could not mount $RUN_DIR now (will mount on next boot)"
chown mowerbase:mowerbase "$RUN_DIR" 2>/dev/null || true

# ── 13. Set hostname ──────────────────────────────────────────────────────────
info "Step 13/13: Setting hostname to 'mowerbase'..."
CURRENT_HOSTNAME=$(hostname)
if [[ "$CURRENT_HOSTNAME" != "mowerbase" ]]; then
  echo "mowerbase" > /etc/hostname
  sed -i "s/$CURRENT_HOSTNAME/mowerbase/g" /etc/hosts 2>/dev/null || true
  hostname mowerbase
  success "Hostname set to mowerbase"
else
  warn "Hostname already 'mowerbase' — skipping"
fi

# ── Start services ─────────────────────────────────────────────────────────────
info "Starting services..."
systemctl start mowerbase-gps.service  && success "mowerbase-gps started"  || warn "mowerbase-gps failed to start"
systemctl start mowerbase-web.service  && success "mowerbase-web started"  || warn "mowerbase-web failed to start"

# ntrip and sik will start automatically once gps is ready
# (or user can start them: systemctl start mowerbase-ntrip mowerbase-sik)

# ── Done ───────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║         MowerBase Install Complete!            ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Dashboard:  http://mowerbase.local:8080"
echo "  (or use IP if mDNS not working)"
echo ""
echo "  Next steps:"
echo "  1. Open http://mowerbase.local:8080"
echo "  2. Wait ~60s for Survey-In to complete (first boot only)"
echo "  3. Status will show FIXED — base station is ready"
echo ""
echo "  Service status:"
for svc in "${SERVICES[@]}"; do
  STATUS=$(systemctl is-active "$svc" 2>/dev/null || echo "unknown")
  if [[ "$STATUS" == "active" ]]; then
    echo -e "    ${GREEN}●${NC} $svc ($STATUS)"
  else
    echo -e "    ${YELLOW}○${NC} $svc ($STATUS)"
  fi
done
echo ""

# ── Manual Pi setup reminder ───────────────────────────────────────────────────
echo -e "${YELLOW}IMPORTANT — Manual Pi setup required before first use:${NC}"
echo "  1. sudo raspi-config"
echo "     → Interface Options → Serial → disable console, enable hardware"
echo "     → Interface Options → I2C → enable"
echo "  2. Add to /boot/firmware/config.txt:  dtoverlay=disable-bt"
echo "  3. Reboot: sudo reboot"
echo ""
