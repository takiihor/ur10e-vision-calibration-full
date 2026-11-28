const FRESH_SECONDS = 3;
const UNKNOWN = '—';
const BATTERY_TRENDS = new Map([['charging', 'Charging'], ['in_use', 'In use'], ['steady', 'Steady']]);
const STATES = {
  ready: ['✓', 'Ready', 'Vehicle is idle'],
  armed: ['◆', 'Armed', 'Stay clear'],
  alarm: ['■', 'Stopped', 'Please call the operator'],
  recovery: ['!', 'Connection lost', 'Stay clear. The vehicle may still be moving.'],
  off: ['○', 'Not connected', ''],
  stale: ['!', 'Screen not updating', 'Ask the operator'],
};
const LIGHT = {
  disabled: 'Disabled', unavailable: 'Unavailable', off: 'Off', ready: 'Green',
  armed: 'Amber', moving: 'Pulsing amber', alarm: 'Red', recovery: 'Pulsing red',
};
const JOB_RESULT = {
  queued: 'waiting', running: 'running', succeeded: 'finished', stopped: 'stopped',
  failed: 'needs attention', unknown: 'needs attention', interrupted: 'needs attention',
  refused: 'needs attention',
};

function text(id, value) {
  const element = document.getElementById(id);
  if (element.textContent !== value) element.textContent = value;
}

function seconds(value) {
  return Number.isFinite(value) && value >= 0 ? Math.round(value) + ' seconds' : UNKNOWN;
}

function station(value) {
  return Number.isInteger(value) && value > 0 && value <= 255 ? value : null;
}

function draw(state, elapsed) {
  const device = state?.device || {};
  const report = device.status || {};
  const age = Number.isFinite(device.status_age_s) && device.status_age_s >= 0
    ? device.status_age_s + elapsed : null;
  const cacheStale = elapsed >= FRESH_SECONDS;
  const fresh = !cacheStale && device.connected === true && !device.stale && age !== null && age < FRESH_SECONDS;
  const feedbackStale = device.connected === true && (device.stale || age >= FRESH_SECONDS);
  const pattern = state?.display_pattern;
  let mode = 'off';
  if (cacheStale) mode = 'stale';
  else if (pattern === 'alarm' || pattern === 'recovery') mode = pattern;
  else if (feedbackStale) mode = 'stale';
  else if (fresh && ['ready', 'armed', 'moving'].includes(pattern)) mode = pattern;

  const current = fresh ? station(report.current_station) : null;
  const target = fresh ? station(report.target_station) : null;
  const battery = fresh && Number.isFinite(report.battery_percent) &&
    report.battery_percent >= 0 && report.battery_percent <= 100 ? report.battery_percent : null;
  const broken = fresh && Number.isInteger(report.fault_code) && report.fault_code !== 0;
  let [icon, title, instruction] = STATES[mode] || ['→', target ? 'Moving to station ' + target : 'Moving', 'Stay clear'];
  if (mode === 'off' && (device.connected || !state)) title = 'Waiting for updates';
  if (mode === 'alarm' && !state?.stop_latched &&
    (broken || fresh && report.obstacle_stop || state?.quality_alarm || state?.recovery_required)) {
    title = 'Needs attention';
    instruction = 'Stay clear. Please call the operator.';
  }
  document.body.dataset.state = mode;
  text('icon', icon);
  text('state-word', title);
  text('instruction', instruction);

  const shownAge = cacheStale ? elapsed : age;
  text('stale-age', shownAge === null ? 'STALE' : 'STALE · ' + Math.floor(shownAge) + ' s');
  document.getElementById('stale-age').hidden = mode !== 'stale';
  text('station', target && current !== target ? (current ?? UNKNOWN) + ' → ' + target : String(current ?? UNKNOWN));
  text('battery', battery === null ? UNKNOWN : battery + ' %');
  const trend = battery === null ? '' : BATTERY_TRENDS.get(device.battery_trend) || '';
  const direction = document.getElementById('battery-direction');
  direction.hidden = !trend;
  direction.dataset.trend = trend ? device.battery_trend : '';
  document.getElementById('charging-icon').hidden = trend !== 'Charging';
  text('battery-trend', trend);
  const warning = document.getElementById('battery-warning');
  warning.hidden = battery === null || battery >= 20;
  if (warning.hidden) delete warning.dataset.batteryLevel;
  else warning.dataset.batteryLevel = battery < 10 ? 'critical' : 'low';
  text('fault', !fresh ? UNKNOWN : report.obstacle_stop === true ? 'Obstacle' :
    broken || state?.quality_alarm ? 'Check needed' : report.fault_code === 0 ? 'Clear' : UNKNOWN);
  const job = state?.job || state?.last_job;
  text('job', mode === 'stale' || !state ? 'Job status unavailable' :
    job ? (state.job ? 'Job ' : 'Last job ') + (JOB_RESULT[job.outcome || job.state] || 'needs attention') : 'No current job');

  const footer = cacheStale ? null : state;
  text('owner', footer?.owner || (footer ? 'None' : UNKNOWN));
  text('lease', footer ? (footer.lease_alive ? 'Active' : 'Inactive') : UNKNOWN);
  text('heartbeat', seconds(footer?.heartbeat_age_s));
  text('arm', footer?.arm_confirmation_remaining_s == null
    ? (footer ? (footer.armed ? 'Armed' : 'Disarmed') : UNKNOWN) : seconds(footer.arm_confirmation_remaining_s));
  text('tower-light', LIGHT[footer?.tower_light?.pattern] || UNKNOWN);
  text('age', seconds(shownAge));
  text('build', typeof footer?.build === 'string' ? footer.build.slice(0, 12) : UNKNOWN);
}

let snapshot = null;
let sampledAt = performance.now();
function render() {
  draw(snapshot, (performance.now() - sampledAt) / 1000);
}

async function refresh() {
  const requestedAt = performance.now();
  try {
    const response = await fetch('/status', { cache: 'no-store', signal: AbortSignal.timeout(2000) });
    if (response.ok) {
      const state = await response.json();
      if (state?.device) {
        snapshot = state;
        sampledAt = requestedAt;
      }
    }
  } catch {
    // Keep ageing the last received snapshot while the read-only endpoint recovers.
  }
  render();
  setTimeout(refresh, 1000);
}
setInterval(render, 500);
refresh();
