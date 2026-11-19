'use strict';

const $ = id => document.getElementById('agv-' + id);
const token = document.querySelector('meta[name="module-token"]').content;
const urgent = new Set(['stop', 'pause']);
const rows = new Map();
let state = {connection_id: 0, sequence: -1, results: {}};
let capability, busy = false, fetching = false, nextPoll = 0, nextRefresh = 0;
let record = [], logGap = false, renderedLog = null, renderedStatus = null;

const fieldLabels = {
  header: 'Header (hex)', length: 'Frame length (bytes)', command: 'Command (hex)',
  vehicle_id: 'Vehicle ID', battery_percent: 'Battery (%)', set_speed_m_h: 'Set speed (m/h)',
  stopped: 'Stopped', obstacle_stop: 'Obstacle stop', station_arrival: 'Station arrival',
  command_stop: 'Command or onboard-button stop', actual_speed_m_h: 'Actual speed (m/h)',
  current_station: 'Current station', target_station: 'Target station', fault_code: 'Fault code',
  max_stations: 'Maximum stations', mode: 'Mode', customer_sensors: 'Customer sensor inputs (hex)',
  position_units: 'Position (device units)', paused: 'Paused by host', leading_end: 'Leading end',
  obstacle_input: 'Obstacle input (hex)', laser_blocked: 'Laser eye blocked',
  roller_center_blocked: 'Roller centre beam blocked', roller_left_blocked: 'Roller left beam blocked',
  roller_right_blocked: 'Roller right beam blocked', lift_state: 'Lift endpoint',
  attachment_active: 'Attachment active', roller_speed: 'Roller speed (device value)', checksum: 'Checksum (hex)',
};
const statusValues = {
  mode: {0: 'Normal', 1: 'Station editing', 2: 'Station calling'},
  leading_end: {0: 'Unknown', 1: 'Display end', 255: 'Tail'},
  lift_state: {0: 'Neither endpoint confirmed', 1: 'Fully raised', 2: 'Fully lowered'},
};

const hex = value => value.toString(16).toUpperCase().padStart(2, '0');
const parseHex = text => text.match(/[a-f\d]{2}/gi).map(byte => parseInt(byte, 16));

function prefix() {
  const address = $('address').value.trim(), channel = $('channel').value.trim();
  if (!/^[a-f\d]{2}\s*[a-f\d]{2}$/i.test(address) || !/^[a-f\d]{2}$/i.test(channel)) return null;
  return [...parseHex(address), ...parseHex(channel)];
}

function commandValue(row) {
  if (!row.input) return null;
  const value = row.input.valueAsNumber, {min, max} = row.command.argument;
  return Number.isInteger(value) && value >= min && value <= max ? value : undefined;
}

function preview(row, radioPrefix, value) {
  if (!radioPrefix || value === undefined) return '—';
  const payload = parseHex(row.command.payload);
  if (value !== null) payload.splice(2, 2, value & 0xFF, value >> 8);
  const inner = [0xA5, 0x5A, payload.length + 4, ...payload];
  return [...radioPrefix, ...inner, inner.reduce((sum, byte) => sum + byte, 0) & 0xFF].map(hex).join(' ');
}

function buildRows(commands) {
  for (const command of commands) {
    const {name, label, argument} = command;
    const row = document.createElement('tr');
    const title = document.createElement('th');
    title.scope = 'row';
    title.textContent = label;
    let input = null;
    if (argument) {
      input = document.createElement('input');
      Object.assign(input, {type: 'number', step: '1', min: argument.min, max: argument.max, value: argument.default});
      input.setAttribute('aria-label', argument.label);
      input.addEventListener('input', render);
      title.append(input);
    }
    const frame = document.createElement('code');
    const send = document.createElement('button');
    send.className = 'secondary';
    send.textContent = 'Send';
    send.setAttribute('aria-label', 'Send ' + label);
    send.addEventListener('click', () => sendCommand(name));
    const result = document.createElement('span');
    const observed = document.createElement('input');
    observed.type = 'checkbox';
    observed.setAttribute('aria-label', label + ' observed OK');
    row.append(title);
    for (const element of [frame, send, result, observed]) row.insertCell().append(element);
    $('commands').append(row);
    rows.set(name, {command, input, frame, send, result, observed});
  }
}

function showPorts(ports) {
  const select = $('port');
  const selected = state.connected ? state.settings.port : select.value;
  const options = ports.map(({device, description}) =>
    new Option(description && description !== 'n/a' ? device + ' · ' + description : device, device));
  if (state.connected && selected && !ports.some(port => port.device === selected)) {
    options.unshift(new Option(selected, selected));
  }
  select.replaceChildren(new Option('Select a port', ''), ...options);
  select.value = selected || '';
}

function labelValue(key, value) {
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (key === 'obstacle_input' || key === 'customer_sensors') return '0x' + hex(value);
  return statusValues[key]?.[value] ?? String(value);
}

function render() {
  const radioPrefix = prefix();
  const serialMode = $('mode').value === 'serial';
  for (const id of ['mode', 'address', 'channel']) $(id).disabled = busy || !!state.connected;
  $('port').disabled = busy || !!state.connected || !serialMode || !capability?.serial_available;
  $('connect').disabled = busy || !!state.connected || !capability || !radioPrefix ||
    (serialMode && (!capability.serial_available || !$('port').value));
  $('disconnect').disabled = busy || !state.connected;
  $('armed').disabled = busy || !state.connected;
  $('armed').checked = state.armed || false;
  $('silence-label').hidden = !state.connected || state.settings.mode !== 'simulator';
  $('silence').disabled = busy;
  $('silence').checked = state.silent || false;
  $('auto').disabled = !state.connected;
  $('stop').disabled = $('pause').disabled = !state.connected;
  $('download').disabled = !state.settings;
  $('connection-note').textContent = state.motion_block ||
    (serialMode && (!capability?.serial_available ? 'Serial mode requires pyserial.' : capability.port_error)) ||
    'Arm the current connection to send movement and speed commands.';

  for (const [name, row] of rows) {
    const value = commandValue(row);
    row.frame.textContent = preview(row, radioPrefix, value);
    row.send.disabled = !state.connected || (!urgent.has(name) && (busy || value === undefined ||
      (row.command.motion && (!state.armed || !!state.motion_block))));
    const outcome = state.results[name];
    row.result.textContent = outcome ? [outcome.state, outcome.warning].filter(Boolean).join(' · ') : '—';
  }

  const age = state.status_age_s;
  const elapsed = age == null ? 'No valid report yet' : `Last report ${age.toFixed(1)} s ago`;
  $('link').textContent = !state.connected ? 'Disconnected' : state.stale ? 'Stale · ' + elapsed : elapsed;
  $('link').className = 'light ' + (state.stale ? 'failed' : state.connected && age != null ? 'ready' : '');
  const raw = state.status_hex || '';
  if (renderedStatus !== raw) {
    const fields = Object.entries(fieldLabels).flatMap(([key, label]) => {
      if (!(key in (state.status || {}))) return [];
      const term = document.createElement('dt'), value = document.createElement('dd');
      term.textContent = label;
      value.textContent = labelValue(key, state.status[key]);
      return [term, value];
    });
    $('status').replaceChildren(...fields);
    $('raw').textContent = raw;
    renderedStatus = raw;
  }
  $('log-gap').hidden = !logGap;
  const last = record.at(-1);
  if (renderedLog !== last) {
    $('log').textContent = record.slice(-30).reverse().map(entry =>
      `${entry.timestamp}  ${entry.direction}  ${entry.hex}\n  ${JSON.stringify(entry.decode)}`).join('\n');
    renderedLog = last;
  }
}

function apply(next) {
  if (next.sequence <= state.sequence) return;
  if (next.commands) {
    capability = next;
    if (!rows.size) buildRows(next.commands);
  }
  if (next.connection_id !== state.connection_id) {
    record = [];
    logGap = false;
    $('auto').checked = false;
    for (const row of rows.values()) row.observed.checked = false;
    if (next.settings) {
      $('mode').value = next.settings.mode;
      $('address').value = next.settings.address;
      $('channel').value = next.settings.channel;
    }
  }
  state = next;
  if (next.ports) showPorts(next.ports);
  else if (state.connected && state.settings.port) $('port').value = state.settings.port;
  const events = next.log.filter(event => event.id > (record.at(-1)?.id || 0));
  if (events.length && record.length && events[0].id !== record.at(-1).id + 1) logGap = true;
  if (events.length && !record.length && next.log_truncated) logGap = true;
  record.push(...events);
  render();
}

function message(text, error = false) {
  const element = document.getElementById('message');
  element.textContent = text;
  element.classList.toggle('error', error);
}

async function api(body) {
  const options = {headers: {'X-Module-Token': token}};
  if (body) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch('/api/agv', options);
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'AGV request failed.');
  return value;
}

async function post(body, {priority = false, background = false} = {}) {
  if (busy && !priority) return;
  if (!priority) busy = true;
  render();
  try {
    const result = await api({connection_id: state.connection_id, ...body});
    apply(result);
    if (!background) message(result.connected ? 'Connected · ' +
      (result.settings.mode === 'simulator' ? 'Simulator' : result.settings.port) : 'Disconnected.');
  } catch (error) {
    message(error.message, true);
    nextRefresh = 0;
  } finally {
    if (!priority) busy = false;
    render();
  }
}

function sendCommand(command, background = false) {
  const row = rows.get(command), value = commandValue(row);
  if (value === undefined) return;
  if (command === 'query_status') nextPoll = performance.now() + 1000;
  return post({action: 'send', command, value}, {priority: urgent.has(command), background});
}

async function tick() {
  const now = performance.now();
  if (busy || fetching) return;
  if (state.connected && $('auto').checked && now >= nextPoll) {
    await sendCommand('query_status', true);
    return;
  }
  if (now < nextRefresh) return;
  fetching = true;
  nextRefresh = now + (state.connected ? 500 : 2000);
  try {
    const next = await api();
    const first = !capability, changed = next.connected !== state.connected;
    apply(next);
    if (next.error) message(next.error, true);
    else if (first || changed) {
      message(state.connected ? 'Connected · ' + (state.settings.mode === 'simulator' ? 'Simulator' : state.settings.port) : 'Disconnected.');
    }
  } catch (error) {
    message(error.message, true);
  } finally {
    fetching = false;
  }
}

for (const id of ['mode', 'port', 'address', 'channel']) $(id).addEventListener('input', render);
$('connect').addEventListener('click', () => post({action: 'connect', mode: $('mode').value,
  port: $('port').value, address: $('address').value, channel: $('channel').value}));
$('disconnect').addEventListener('click', () => post({action: 'disconnect'}));
$('armed').addEventListener('change', event => post({action: 'arm', armed: event.currentTarget.checked}));
$('silence').addEventListener('change', event => post({action: 'silence', silent: event.currentTarget.checked}));
$('auto').addEventListener('change', () => { nextPoll = 0; void tick(); });
for (const name of urgent) $(name).addEventListener('click', () => sendCommand(name));

$('download').addEventListener('click', () => {
  const testRecord = {
    exported_at: new Date().toISOString(), connection: state.settings, auto_poll: $('auto').checked,
    observed_ok: Object.fromEntries([...rows].map(([name, row]) => [name, row.observed.checked])),
    results: state.results, log_gap: logGap, log: record,
  };
  const url = URL.createObjectURL(new Blob([JSON.stringify(testRecord, null, 2)], {type: 'application/json'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = 'agv-test-record.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

void tick();
setInterval(tick, 200);
