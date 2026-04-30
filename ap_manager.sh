#!/usr/bin/env bash
# ap_manager.sh — WiFi fallback AP manager.
#
# Brings up the MowerBase AP when no home WiFi is connected.
# Brings it down as soon as home WiFi reconnects.
# BCM43430 (Pi Zero W) cannot do concurrent AP+STA on wlan0 — this script
# keeps them mutually exclusive.
#
# Runs as root via mowerbase-ap.service.

AP_CON="mowerbase-ap"
BOOT_GRACE_S=30   # wait for home WiFi to connect before deciding to start AP
CHECK_S=10

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [ap-mgr] $*"; }

log "Starting — waiting ${BOOT_GRACE_S}s for home WiFi to connect..."
sleep "$BOOT_GRACE_S"

while true; do
    # Non-AP WiFi connection active on wlan0?
    HOME_ACTIVE=$(nmcli -t -f NAME,DEVICE,STATE con show --active 2>/dev/null \
        | awk -F: -v ap="$AP_CON" '$2=="wlan0" && $3=="activated" && $1!=ap {print $1; exit}')

    # AP currently active?
    AP_ACTIVE=$(nmcli -t -f NAME,STATE con show --active 2>/dev/null \
        | awk -F: -v ap="$AP_CON" '$1==ap && $2=="activated" {print $1; exit}')

    if [ -n "$HOME_ACTIVE" ]; then
        # Home WiFi is up — AP must be down
        if [ -n "$AP_ACTIVE" ]; then
            log "Home WiFi connected ($HOME_ACTIVE) — stopping AP"
            nmcli con down "$AP_CON" 2>/dev/null
        fi
    else
        # No home WiFi — AP must be up
        if [ -z "$AP_ACTIVE" ]; then
            log "No home WiFi — starting AP (MowerBase)"
            nmcli con up "$AP_CON" 2>/dev/null
        fi
    fi

    sleep "$CHECK_S"
done
