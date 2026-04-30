#!/usr/bin/env bash
# update.sh — Deploy updated BaseStation files to the Pi and restart services.
#
# Run from the mowerbase project directory:
#   bash update.sh
#
# Copies Python files, templates, static assets, and optionally service files.
# Restarts all three services on the Pi after copying.

set -euo pipefail

PI_USER="mowerbase"
PI_HOST="192.168.0.129"
PI="${PI_USER}@${PI_HOST}"
APP="/opt/mowerbase"

# ── Colours ───────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

step() { echo -e "${CYAN}→${NC} $*"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}!${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── 1. Copy Python files + shell scripts ─────────────────────────────────────
step "Copying Python files..."
scp *.py "${PI}:${APP}/"
ok "Python files copied"

step "Copying shell scripts..."
scp ap_manager.sh "${PI}:${APP}/"
ssh "${PI}" "chmod +x ${APP}/ap_manager.sh"
ok "Shell scripts copied"

# ── 2. Copy templates ─────────────────────────────────────────────────────────
step "Copying templates..."
scp templates/*.html "${PI}:${APP}/templates/"
ok "Templates copied"

# ── 4. Copy static assets ─────────────────────────────────────────────────────
step "Copying static assets..."
scp static/* "${PI}:${APP}/static/"
ok "Static assets copied"

# ── 5. Copy service files (needs sudo on Pi) ──────────────────────────────────
step "Copying systemd service files..."
ssh "${PI}" "mkdir -p /tmp/mb-svc"
scp systemd/*.service "${PI}:/tmp/mb-svc/"
ssh "${PI}" "
  sudo cp /tmp/mb-svc/mowerbase-gps.service /etc/systemd/system/mowerbase-gps.service &&
  sudo cp /tmp/mb-svc/mowerbase-sik.service /etc/systemd/system/mowerbase-sik.service &&
  sudo cp /tmp/mb-svc/mowerbase-ntrip.service /etc/systemd/system/mowerbase-ntrip.service &&
  sudo cp /tmp/mb-svc/mowerbase-web.service /etc/systemd/system/mowerbase-web.service &&
  sudo cp /tmp/mb-svc/mowerbase-ap.service /etc/systemd/system/mowerbase-ap.service &&
  sudo systemctl daemon-reload &&
  sudo systemctl enable mowerbase-ap
"
ok "Service files installed"

# ── 5b. Migrate web port from 8080 to 80 in existing config ──────────────────
step "Migrating web port to 80 (if needed)..."
ssh "${PI}" "python3 -c \"
import json, os
p = '/etc/mowerbase/config.json'
if os.path.exists(p):
    with open(p) as f: c = json.load(f)
    c.setdefault('web', {})['port'] = 80
    with open(p, 'w') as f: json.dump(c, f, indent=2)
    print('Port migrated to 80')
else:
    print('Config not found — skipping migration')
\"" && ok "Port migration done" || warn "Port migration failed (non-fatal)"

# ── 6. Restart services ───────────────────────────────────────────────────────
step "Restarting services..."
ssh "${PI}" "sudo systemctl restart mowerbase-gps mowerbase-sik mowerbase-ntrip mowerbase-web mowerbase-ap"
ok "Services restarted"

echo ""
echo -e "${GREEN}Deploy complete.${NC} Web UI: http://${PI_HOST}"
echo ""
