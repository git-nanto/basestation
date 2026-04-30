/**
 * dashboard.js — Live dashboard updates for BaseStation.
 *
 * Polls /api/state every 3 seconds and updates DOM elements in place.
 * No external dependencies.
 */

const POLL_INTERVAL_MS = 3000;

// ── Badge state config ────────────────────────────────────────────────────────
const SM_BADGE = {
  FIXED:            { text: 'FIXED',          cls: 'badge-fixed' },
  SURVEYING:        { text: 'SURVEYING',       cls: 'badge-survey' },
  BOOT:             { text: 'BOOT',            cls: 'badge-boot' },
  ERROR:            { text: 'ERROR',           cls: 'badge-error' },
  RESURVEY_PENDING: { text: 'REVIEW REQUIRED', cls: 'badge-warning' },
  NO_WIFI:          { text: 'NO WIFI',         cls: 'badge-error' },
  GPS_NOT_FOUND:    { text: 'NO GPS',          cls: 'badge-error' },
};

// ── Helpers ───────────────────────────────────────────────────────────────────
function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value ?? '---';
}

function show(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = '';
}

function hide(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = 'none';
}

function setBadge(id, text, cls) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'badge ' + cls;
}

function formatKBps(bytesPerSec) {
  if (bytesPerSec == null) return '---';
  return (bytesPerSec / 1024).toFixed(1) + ' KB/s';
}

function formatSecondsToMM(secs) {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function formatLatLon(lat, lon) {
  if (lat == null || lon == null) return '---';
  return `${lat.toFixed(6)}, ${lon.toFixed(6)}`;
}

// ── Hardware state banner ─────────────────────────────────────────────────────
function updateHardwareBanner(state) {
  const banner = document.getElementById('hw-banner');
  if (!banner) return;

  const serialOk = state.gps ? state.gps.serial_ok !== false : true;
  const wifiOk   = state.wifi ? !!state.wifi.home_connected : true;

  let html = '';
  if (!serialOk && !wifiOk) {
    html = `<div class="alert alert-error">
      <strong>No GPS + No home WiFi.</strong>
      Connect the LC29H(BS) HAT to the GPIO header (UART jumper in Position B) and
      go to <a href="/network">Network</a> to connect to your home WiFi.
      The BaseStation hotspot (<strong>BaseStation</strong>, no password) is always available for setup.
    </div>`;
  } else if (!serialOk) {
    html = `<div class="alert alert-warning">
      <strong>GPS HAT not detected.</strong>
      Check the LC29H(BS) HAT is seated on the GPIO header and the UART jumper is in Position B.
      NTRIP relay mode is still available if home WiFi is connected.
    </div>`;
  } else if (!wifiOk) {
    html = `<div class="alert alert-info">
      Running GPS-only mode.
      <a href="/network">Connect home WiFi</a> to enable NTRIP relay mode.
    </div>`;
  }

  banner.innerHTML = html;
  banner.style.display = html ? '' : 'none';
}

// ── Main update function ──────────────────────────────────────────────────────
function updateDashboard(state) {
  const gps = state.gps || {};
  const sik = state.sik || {};
  const smState = gps.sm_state || 'BOOT';

  // Fix status badge
  const badgeCfg = SM_BADGE[smState] || { text: smState, cls: 'badge-boot' };
  setBadge('fix-badge', badgeCfg.text, badgeCfg.cls);

  // Survey-In progress section
  if (smState === 'SURVEYING') {
    show('survey-progress-section');
    const elapsed = gps.svin_elapsed_s || 0;
    const target = gps.svin_min_duration_s || 300;
    const progress = Math.min(100, (elapsed / target) * 100);

    const bar = document.getElementById('survey-progress-bar');
    if (bar) bar.style.width = progress.toFixed(1) + '%';

    setText('survey-elapsed', formatSecondsToMM(elapsed));
    setText('survey-target', '/ ' + formatSecondsToMM(target));
    const acc = gps.accuracy_m;
    setText('survey-accuracy', acc != null ? acc.toFixed(1) + ' m' : '--- m');
    setText('survey-sats', `Sats: ${gps.num_sats ?? '---'}`);
  } else {
    hide('survey-progress-section');
  }

  // Re-survey pending section
  if (smState === 'RESURVEY_PENDING' && state.resurvey_new_position) {
    show('resurvey-pending-section');
    const table = document.getElementById('resurvey-comparison');
    if (table) {
      const n = state.resurvey_new_position;
      const o = state.resurvey_old_position || {};
      const delta = state.resurvey_delta_m;
      table.innerHTML = `
        <tr><th></th><th>Old</th><th>New</th></tr>
        <tr><td>Lat</td><td>${o.lat?.toFixed(7) ?? '--'}</td><td>${n.lat?.toFixed(7) ?? '--'}</td></tr>
        <tr><td>Lon</td><td>${o.lon?.toFixed(7) ?? '--'}</td><td>${n.lon?.toFixed(7) ?? '--'}</td></tr>
        <tr><td>Alt</td><td>${o.alt_m?.toFixed(3) ?? '--'} m</td><td>${n.alt_m?.toFixed(3) ?? '--'} m</td></tr>
        <tr><td>Accuracy</td><td>${o.accuracy_m?.toFixed(4) ?? '--'} m</td><td>${n.accuracy_m?.toFixed(4) ?? '--'} m</td></tr>
        <tr><td colspan="3"><strong>Displacement: ${delta != null ? (delta * 1000).toFixed(0) + ' mm' : '--'}</strong></td></tr>
      `;
    }
  } else {
    hide('resurvey-pending-section');
  }

  // Drift
  const driftM = gps.drift_current_m;
  const driftMm = driftM != null ? (driftM * 1000).toFixed(1) : '---';
  setText('drift-value', driftMm + ' mm');
  const driftAlertEl = document.getElementById('drift-alert-badge');
  if (driftAlertEl) {
    driftAlertEl.style.display = gps.drift_alert ? 'inline-block' : 'none';
  }

  // SiK radio status — three distinct states
  const sikRate = document.getElementById('sik-rate');
  if (!sik.usb_present) {
    setBadge('sik-status', 'Not on USB', 'badge-error');
    if (sikRate) { sikRate.textContent = '---'; sikRate.title = `Port: ${sik.port || '/dev/ttyUSB0'}`; }
  } else if (!sik.connected) {
    setBadge('sik-status', 'Found, not open', 'badge-warning');
    if (sikRate) { sikRate.textContent = '0.0 KB/s'; sikRate.title = sik.error || ''; }
  } else if ((sik.bytes_per_sec || 0) < 0.1) {
    setBadge('sik-status', 'Found, idle', 'badge-warning');
    if (sikRate) { sikRate.textContent = '0.0 KB/s'; sikRate.title = 'No RTCM data flowing — GPS may not be fixed yet'; }
  } else {
    setBadge('sik-status', 'Active', 'badge-ok');
    if (sikRate) { sikRate.textContent = formatKBps(sik.bytes_per_sec); sikRate.title = ''; }
  }

  // GPS info
  setText('num-sats', gps.num_sats ?? '---');
  setText('hdop', gps.hdop != null ? gps.hdop.toFixed(1) : '---');
  setText('position', formatLatLon(gps.lat, gps.lon));
  setText('altitude', gps.alt_m != null ? gps.alt_m.toFixed(1) + ' m' : '---');

  // Hardware state banner
  updateHardwareBanner(state);
}

// ── Polling loop ──────────────────────────────────────────────────────────────
let pollActive = true;

async function pollState() {
  while (pollActive) {
    try {
      const resp = await fetch('/api/state');
      if (resp.ok) {
        const data = await resp.json();
        updateDashboard(data);
      }
    } catch (e) {
      // Network error — will retry
      const badge = document.getElementById('fix-badge');
      if (badge) {
        badge.textContent = 'NO CONNECTION';
        badge.className = 'badge badge-error';
      }
    }

    await new Promise(resolve => setTimeout(resolve, POLL_INTERVAL_MS));
  }
}

// Only run on the dashboard page
if (document.getElementById('fix-badge')) {
  pollState();
}
