'use strict';

const $ = id => document.getElementById('agv-' + id);
const token = document.querySelector('meta[name="module-token"]').content;
const connectionKey = 'ano-agv-connection';
const sessionKey = 'ano-agv-commissioning';
const journalKey = sessionKey + '-journal';
const urgent = new Set(['stop', 'pause']);
const reads = new Set(['query_status', 'read_speed']);
const controls = new Map(), commands = new Map(), outcomes = new Map();
const linkLost = 'Link lost. The AGV may still be moving. Use its e-stop if needed.';
const portRetry = 'Reseat the USB plug, reload this page, then select Connect.';
const badFrame = 'Invalid AGV reply. Check the radio connection, then query status again.';
const stationCallRequired = 'Station call needs the station-call function, set in the XBOT app. In standard mode, use Forward/Reverse.';
const emptyJournal = () => ({log: [], events: [], commands: {}, transports: {}, link_quality: {}, cursors: {}, log_gap: false});
const isoNow = () => new Date().toISOString();
const localTime = stamp => new Date(stamp).toLocaleTimeString('en-GB', {
  hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3, hour12: false,
});
const seconds = value => Number(value ?? 0).toFixed(1);
const speedText = value => `${value} m/h · ${(value / 1000).toFixed(3).replace(/0+$/, '').replace(/\.$/, '.0')} km/h`;
const display = (id, text) => {
  if ($(id).textContent !== text) $(id).textContent = text;
};

function load(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
  catch { return fallback; }
}

function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); return true; }
  catch { return false; }
}

const newSession = () => ({started_at: isoNow(), settings: {}, checklist: {}});
let session = load(sessionKey, newSession());
const savedJournal = load(journalKey, null);
let journal = savedJournal?.started_at === session.started_at ? savedJournal.journal : emptyJournal();
let savedSettings = load(connectionKey, null);
let state = {connection_id: 0, sequence: -1, results: {}};
let capability, busy = false, polling = false, fetching = false, nextPoll = 0, nextRefresh = 0;
let lastPressed, networkError = false, reportError = '', lastMaximum, renderedStatus, renderedLog, renderedEvent;
let audio, reportErrorUntil = 0;

const fieldLabels = {
  vehicle_id: 'Vehicle ID', set_speed_m_h: 'Set speed', max_stations: 'Learned stations',
  customer_sensors: 'Customer sensor inputs', position_units: 'Route position (device units)',
  leading_end: 'Leading end', obstacle_input: 'Obstacle input',
  laser_blocked: 'Laser eye blocked', roller_center_blocked: 'Roller centre beam blocked',
  roller_left_blocked: 'Roller left beam blocked', roller_right_blocked: 'Roller right beam blocked',
  lift_state: 'Lift endpoint', attachment_active: 'Attachment active', roller_speed: 'Roller speed (device value)',
};
const modes = {0: 'standard', 1: 'station editing', 2: 'station calling'};
const eventFields = {
  current_station: 'Current station', target_station: 'Target station', fault_code: 'Fault code',
  obstacle_stop: 'Obstacle stop', command_stop: 'Command stop', paused: 'Pause flag',
};
const outcomesList = ['Not tested', 'Pass', 'Fail', 'Skipped'];
const checklist = [
  ['Read status and speed', 'Check for fresh status. If the AGV does not send timed reports, turn on auto-poll and select Query status. Select Read speed and compare the reply with the set speed in Machine details.', 'The page shows a fresh status and replies with a speed. The AGV stays parked.'],
  ['Stop and Pause while parked', 'With the AGV parked, press Stop (software) and wait for the status. Press Pause separately and wait for the status.', 'The AGV stays parked. Check the command stop and pause flags after their respective presses.'],
  ['Set speed to 500 m/h', 'Tick Armed and press the 500 m/h preset. Check Set speed in Machine details before moving.', 'The set speed reads 500 m/h (0.5 km/h). Actual speed remains zero while parked.'],
  ['Call the next station and Stop partway', 'Call the next learned station at 500 m/h. Press Stop (software) partway, before the AGV reaches the station.', 'Observe where it stops and how long it takes. Check motion, station and command stop.'],
  ['Pause and Resume partway', 'Call a learned station at 500 m/h. Press Pause partway and wait for the AGV to stop. Once the track is clear, press Resume yourself.', 'Check the pause flag, whether it resumes, and where it stops.'],
  ['Call every learned station', 'Press one learned station button at a time. Wait for the AGV to stop at that station before calling another.', 'Compare the physical station with the current station and arrival time on the page.'],
  ['Physical e-stop during slow motion', 'With a person at the e-stop, start one slow move and press the physical e-stop. Record when it stops. Reset only when the track is clear and the person at the e-stop is ready.', 'Does it restart on reset? Record what the page reports and what you see.'],
  ['Watch for restart after Stop', 'Press Stop (software) during one slow move. When the AGV stops, wait 30 s with a person still at the e-stop.', 'Does it restart on its own? Mark the stop and any restart.'],
  ['Radio link loss during slow motion', 'Run this test last. During one slow move, unplug the host radio USB connection. Keep the person at the AGV’s e-stop ready to stop it. Reconnect only when safe.', 'Does it continue, and for how long? Record the physical stop and the status after the link returns.'],
];
const checklistCommands = [
  ['query_status', 'read_speed'], ['stop', 'pause'], ['set_speed'], ['call_station', 'stop'],
  ['call_station', 'pause', 'resume'], ['call_station'], ['forward', 'reverse', 'call_station', 'stop'],
  ['forward', 'reverse', 'call_station', 'stop'], ['forward', 'reverse', 'call_station', 'query_status', 'stop'],
];
const foundFields = {
  operator: 'Operator', date: 'Date', site: 'Site', serial: 'AGV serial number', radio: 'Radio module model',
  vehicle: 'Vehicle ID [本机编号]', 'radio-channel': 'Radio channel [频道]', reports: '485 timed reports [定时发送485]',
  restart: 'Automatic restart after stop [停止后自动启动]', 'restart-seconds': 'Automatic restart delay (s)',
  memory: 'Track memory [轨道记忆]', 'app-mode': 'Manual or auto [运行模式]', 'button-mode': 'Button mode [按钮模式]',
};

function make(tag, text, attributes = {}) {
  const node = Object.assign(document.createElement(tag), attributes);
  if (text) node.textContent = text;
  return node;
}

function saveSession() {
  if (!save(sessionKey, session)) display('record-note', 'Browser storage is unavailable. Download the record before leaving.');
}

function saveJournal() {
  if (!save(journalKey, {started_at: session.started_at, journal})) {
    display('record-note', 'The full log could not be saved in the browser. Download JSON before leaving.');
  }
}

function buildChecklist() {
  for (const [index, [title, instructions, watch]] of checklist.entries()) {
    const number = index + 1;
    const test = make('details', '', {id: 'agv-test-' + number});
    const summary = make('summary', `${number}. ${title} · Not tested`);
    const result = make('select', '', {id: 'agv-result-' + number});
    result.append(...outcomesList.map(option => new Option(option, option)));
    const notes = make('textarea', '', {id: 'agv-notes-' + number, rows: 3});
    const evidence = make('p', '', {id: 'agv-evidence-' + number, className: 'agv-evidence'});
    const resultLabel = make('label', 'Result');
    const notesLabel = make('label', 'Notes');
    resultLabel.append(result); notesLabel.append(notes);
    test.append(summary, make('p', instructions), make('p', 'Watch for this. ' + watch), resultLabel, notesLabel, evidence);
    $('checklist').append(test);

    function show() {
      const saved = session.checklist[number] ?? {};
      result.value = saved.result ?? 'Not tested';
      notes.value = saved.notes ?? '';
      summary.textContent = `${number}. ${title} · ${result.value}`;
      const proof = saved.evidence;
      evidence.textContent = proof ? `Observed ${localTime(proof.marked_at)} · ${proof.commands.length} command results · ${proof.events.length} events · ${proof.status ? 'status captured' : 'no status available'}` : '';
    }
    show();
    test.addEventListener('agv-reset', show);
    test.addEventListener('toggle', () => {
      if (test.open && !session.checklist[number]?.opened_at) {
        session.checklist[number] = {...session.checklist[number], opened_at: isoNow()};
      }
    });
    result.addEventListener('change', () => {
      const saved = session.checklist[number] ?? {};
      const marked = isoNow();
      const recent = new Date(Date.now() - 120000).toISOString();
      const since = saved.opened_at && saved.opened_at < recent ? saved.opened_at : recent;
      const during = entry => entry.timestamp >= since && entry.timestamp <= marked;
      session.checklist[number] = {...saved, result: result.value, notes: notes.value, evidence: {
        marked_at: marked, status: state.status ?? null, status_age_s: state.status_age_s ?? null,
        connected: !!state.connected, stale: !!state.stale, transport: state.settings?.mode ?? null,
        commands: Object.values(journal.commands).filter(entry => during(entry) && checklistCommands[index].includes(entry.name)), events: journal.events.filter(during),
      }};
      saveSession();
      show();
    });
    function saveNotes() {
      session.checklist[number] = {...session.checklist[number], notes: notes.value};
      saveSession();
    }
    notes.addEventListener('input', saveNotes);
    notes.addEventListener('change', saveNotes);
  }

  for (const [id, label] of Object.entries(foundFields)) {
    const field = make('input', '', {id: 'agv-' + id, value: session.settings[id] ?? ''});
    if (id === 'date') field.type = 'date';
    if (id === 'restart-seconds') Object.assign(field, {type: 'number', min: '0', step: 'any'});
    const container = make('label', label);
    container.append(field);
    $('found-fields').append(container);
    function store() { session.settings[id] = field.value; saveSession(); }
    field.addEventListener('input', store);
    field.addEventListener('change', store);
  }
}

function register(id, button, outcome, why) {
  button.setAttribute('aria-describedby', `${why.id || 'agv-' + id + '-why'} ${outcome.id || 'agv-' + id + '-outcome'}`);
  controls.set(id, {button, outcome, why});
}

function commandButton(id, text, name, value) {
  const box = make('div', '', {className: 'agv-control'});
  const button = make('button', text, {id: 'agv-' + id, className: 'secondary', type: 'button'});
  const why = make('small', '', {id: 'agv-' + id + '-why'});
  const outcome = make('div', '', {id: 'agv-' + id + '-outcome'});
  register(id, button, outcome, why);
  Object.assign(controls.get(id), {name, value});
  button.addEventListener('click', () => sendCommand(name, id, value));
  box.append(button, why, outcome);
  return box;
}

function buildCommands(list) {
  for (const command of list) {
    const {name, label, argument} = command;
    commands.set(name, command);
    if (urgent.has(name)) continue;
    const element = commandButton(name, label, name);
    if (argument) {
      const station = name === 'call_station';
      const label = make('label', station ? 'Manual station number (1–255)' : 'Set speed (500–4000 m/h)');
      command.input = make('input', '', {
        id: 'agv-' + name + '-value', type: 'number', min: argument.min, max: argument.max,
        step: '1', value: argument.default,
      });
      command.input.addEventListener('input', render);
      label.append(command.input);
      const container = $(station ? 'manual-station' : 'speed');
      container.append(label, element);
      if (!station) {
        const presets = make('div', '', {className: 'agv-presets'});
        for (const speed of [500, 1000, 1500]) {
          presets.append(commandButton('speed-' + speed, speedText(speed), 'set_speed', speed));
        }
        container.append(presets);
      }
    } else {
      const container = reads.has(name) ? 'reads' : ['forward', 'reverse', 'resume'].includes(name) ? 'moves' : 'more';
      $(container).append(element);
    }
  }
}

function prefixIsValid() {
  return /^[a-f\d]{2}\s*[a-f\d]{2}$/i.test($('address').value.trim()) && /^[a-f\d]{2}$/i.test($('channel').value.trim());
}

function commandValue(name, preset) {
  if (preset !== undefined) return preset;
  const command = commands.get(name);
  if (!command?.argument) return null;
  const value = command.input.valueAsNumber;
  return Number.isInteger(value) && value >= command.argument.min && value <= command.argument.max ? value : undefined;
}

function showPorts(ports) {
  const select = $('port');
  let selected = state.connected ? state.settings.port : select.value;
  const listed = device => ports.some(port => port.device === device);
  if (!state.connected && !listed(selected)) {
    selected = '';
    if ($('mode').value === 'serial') {
      const savedDevice = savedSettings?.port?.device;
      if (listed(savedDevice)) selected = savedDevice;
      else {
        const described = ports.filter(({description}) => !/^\s*(?:n\/a)?\s*$/i.test(description ?? ''));
        if (described.length === 1) selected = described[0].device;
      }
    }
  }
  const options = ports.map(({device, description}) =>
    new Option(description && description !== 'n/a' ? device + ' · ' + description : device, device));
  if (state.connected && selected && !listed(selected)) options.unshift(new Option(selected, selected));
  select.replaceChildren(new Option('Select a port', ''), ...options);
  select.value = selected || '';
}

function disabled(id, reason) {
  $(id).disabled = !!reason;
  if ($(id + '-why')) display(id + '-why', reason);
}

function commandReason(name, preset) {
  if (!state.connected) return 'Connect first.';
  if (urgent.has(name)) return '';
  if (name === 'call_station' && state.status?.mode === 0) return stationCallRequired;
  if (state.recipe_active && !commands.get(name)?.motion) return 'The recipe is checking AGV status. Follow Current operation on Pick/place.';
  if (commands.get(name)?.motion) {
    if (state.motion_block) return state.motion_block;
    if (!state.armed) return 'Tick Armed to enable movement.';
  }
  if (busy) return 'Wait for the current request.';
  const value = commandValue(name, preset);
  const maximum = state.status?.max_stations;
  if (name === 'call_station' && maximum > 0 && value > maximum) return `Choose a learned station from 1 to ${maximum}.`;
  if (value === undefined) return name === 'set_speed' ? 'Enter a whole number from 500 to 4000 m/h.' : 'Enter a whole number from 1 to 255.';
  return '';
}

function faultText(status) {
  if (!status || status.fault_code == null) return '—';
  return status.fault_code === 0 ? 'No fault' : `Fault ${status.fault_code} · ${status.fault_description} (manual p. ${status.fault_page})`;
}

function renderStatus() {
  const status = state.status;
  const standard = status?.mode === 0;
  const maximum = standard ? undefined : status?.max_stations;
  $('manual-station').hidden = standard;
  $('target-readout').hidden = standard;
  $('direction-note').hidden = !standard;
  if (commands.has('forward')) display('forward', standard ? 'Forward' : 'Forward');
  if (commands.has('reverse')) display('reverse', standard ? 'Reverse' : 'Reverse');
  display('station-note', standard ? stationCallRequired : maximum === 0
    ? 'No route learned yet (AGV reports 0 stations). Teach the route with the AGV’s green button; see the XBOT station guide (V1.61, pp. 2–3). Manual numbers may be unrecognised.'
    : maximum > 0 ? `${maximum} learned stations. Current station is marked on its button.`
      : 'Query status to see whether the AGV has learned a route. Manual station numbers are unverified.');
  if (maximum !== lastMaximum) {
    for (const id of controls.keys()) if (id.startsWith('station-')) controls.delete(id);
    $('stations').replaceChildren(...Array.from({length: maximum || 0}, (_, index) => {
      const station = index + 1;
      return commandButton('station-' + station, 'Go to station ' + station, 'call_station', station);
    }));
    lastMaximum = maximum;
  }
  for (let station = 1; station <= maximum; station++) {
    const button = $('station-' + station);
    const current = status.current_station === station;
    button.textContent = `Go to station ${station}${current ? ' · Current' : ''}`;
    if (current) button.setAttribute('aria-current', 'true');
    else button.removeAttribute('aria-current');
  }
  display('transport', state.connected ? state.settings.mode === 'simulator' ? 'Simulator' : 'Serial AGV' : 'No open connection');
  $('link-quality').hidden = !state.connected || !state.link_quality;
  if (state.link_quality) display('link-quality', linkQualityText(state.link_quality));
  if (renderedStatus === state.status_hex) return;
  renderedStatus = state.status_hex;
  const bool = value => value === true ? 'On' : value === false ? 'Off' : 'Unknown';
  const actual = status?.actual_speed_m_h;
  display('station-label', standard ? 'Stop counter (not a station number in standard mode)'
    : maximum === 0 ? 'Stop counter (no route learned)' : 'Current station');
  display('state-motion', status?.stopped === true ? 'Stopped' : status?.stopped === false ? 'Running' : '—');
  display('state-station', String(status?.current_station ?? '—'));
  display('state-target', String(status?.target_station ?? '—'));
  display('state-speed', actual == null ? '—' : speedText(actual) + (actual ? actual < 0 ? ' · Reverse' : ' · Forward' : ''));
  display('state-battery', status?.battery_percent == null ? '—' : status.battery_percent + '%');
  display('state-fault', faultText(status));
  $('state-fault').dataset.state = status?.fault_code ? 'failed' : '';
  display('state-flags', status ? [
    `Mode ${status.mode} · ${modes[status.mode] ?? 'unknown'}`, `${standard ? 'Stop line' : 'Station'} arrival ${bool(status.station_arrival)}`,
    `Obstacle stop ${bool(status.obstacle_stop)}`, `Command stop ${bool(status.command_stop)}`, `Pause ${bool(status.paused)}`,
  ].join('  /  ') : 'Awaiting a status report.');
  $('status').replaceChildren(...Object.entries(fieldLabels).flatMap(([field, label]) => {
    if (!(field in (status ?? {}))) return [];
    const value = status[field];
    const title = standard && field === 'position_units' ? 'Distance to the stop line (units)' : label;
    return [make('dt', title), make('dd', field === 'set_speed_m_h' ? speedText(value)
      : typeof value === 'boolean' ? bool(value) : String(value))];
  }));
  display('raw', state.status_hex ?? '');
}

function linkQualityText(quality) {
  const {sent, acknowledged, resent, no_reply: noReply, timed_report_interval_s: interval} = quality;
  const counts = `Sent ${sent} · Acknowledged ${acknowledged} · Resent ${resent} · No reply ${noReply}`;
  return interval == null ? counts : `${counts} · Timed reports every ${seconds(interval)} s`;
}

function resultText(name, result, viaEsc) {
  if (!result) return null;
  const {state: stage, elapsed_s: elapsed, ack_elapsed_s: ack, value, remaining_s: remaining} = result;
  const acknowledged = ack == null ? '' : `AGV acknowledged (${seconds(ack)} s)`;
  const confirmation = stage === 'confirmed by status';
  const sent = localTime(result.sent_at);
  const urgentSent = `${name === 'pause' ? 'Pause' : 'Stop'} sent ${sent}${viaEsc ? ' (Esc)' : ''}`;
  let text;
  if (stage.startsWith('reply: ')) {
    const reply = stage.slice(7);
    const amount = Number.parseInt(reply, 10);
    return {text: `AGV replied: ${reply} (${seconds(elapsed)} s)${Number.isFinite(amount) ? ' · ' + speedText(amount).split(' · ')[1] : ''}`, tone: 'done'};
  }
  if (stage === 'waiting for reply') return {text: `Waiting for the AGV to reply… ${seconds(remaining)} s`, tone: 'waiting'};
  if (stage === 'status received') return {text: `Status received (${seconds(elapsed)} s)`, tone: 'done'};
  if (stage === 'acknowledged late') return {text: `AGV acknowledged late (${seconds(ack)} s)`, tone: 'done'};
  if (stage === 'no reply') return {text: reads.has(name) ? `No reply in 1.5 s. Check the radio. ${portRetry}`
    : `No reply from the AGV after ${result.tries} tries. Check the radio, then try again.`, tone: 'failed'};
  if (stage === 'arrived') text = `Arrived at station ${value} (${seconds(elapsed)} s)`;
  else if (confirmation) {
    const reported = result.confirmed_stopped !== false ? 'stopped' : name === 'pause' ? 'paused' : 'command stop active';
    text = urgent.has(name) ? `${urgentSent} · AGV reports ${reported}` : 'Confirmed: AGV status shows the change';
  }
  else if (stage === 'not confirmed') text = 'Not confirmed after 5 s: check the AGV.';
  else if (stage === 'not checked') text = 'Not checked: no confirmation before another action. Check the AGV.';
  else if (stage === 'travelling') text = `Going to station ${value}…${$('auto').checked || state.link_quality?.timed_report_interval_s ? '' : ' Select Query status to check arrival.'}`;
  else {
    const sentText = urgent.has(name) ? urgentSent :
      name === 'call_station' ? `Going to station ${value}…` : `Sent at ${sent}`;
    const waiting = !$('auto').checked && !state.link_quality?.timed_report_interval_s && (ack != null || !remaining)
      ? 'waiting for a status (auto-poll is off). Select Query status.'
      : remaining > 0 ? `waiting for the AGV to confirm… ${Math.ceil(remaining)} s` : 'waiting for a status. Check the link.';
    const attempt = result.tries > 1 ? `Try ${result.tries}` : '';
    return {text: [sentText, attempt, acknowledged, waiting, result.warning].filter(Boolean).join(' · '), tone: 'waiting'};
  }
  return {text: [text, acknowledged, result.warning].filter(Boolean).join(' · '),
    tone: confirmation || stage === 'arrived' ? 'done' : stage === 'travelling' ? 'waiting' : 'failed'};
}

function renderOutcome(id) {
  const element = controls.get(id)?.outcome;
  const outcome = outcomes.get(id);
  const feedback = resultText(outcome?.name, outcome?.result, outcome?.viaEsc) ?? outcome?.direct;
  if (element) {
    if (element.textContent !== (feedback?.text ?? '')) element.textContent = feedback?.text ?? '';
    element.dataset.state = feedback?.tone ?? '';
    controls.get(id).button.setAttribute('aria-busy', String(feedback?.tone === 'running'));
  }
  if (id === lastPressed) {
    display('outcome', feedback?.text ?? '');
    $('outcome').dataset.state = feedback?.tone ?? '';
  }
}

function render() {
  renderStatus();
  const serialMode = $('mode').value === 'serial';
  const connectionLocked = busy ? 'Wait for the current request.' : state.connected ? 'Disconnect to change link settings.' : '';
  for (const id of ['mode', 'address', 'channel', 'stations-config']) {
    $(id).disabled = !!connectionLocked;
    $(id).title = connectionLocked;
  }
  $('port-label').hidden = !serialMode;
  $('stations-config-label').hidden = serialMode;
  const unavailablePort = !capability?.serial_available ? 'Serial support is unavailable. Use the IPC app environment.' :
    !$('port').value ? `Select a serial port. If it is missing, reseat the USB plug and reload.` : '';
  disabled('port', connectionLocked || (!serialMode ? 'Select Serial port first.' : !capability?.serial_available ? unavailablePort : ''));
  const stationCount = Number($('stations-config').value);
  const connectReason = connectionLocked || (!capability ? 'Waiting for the page service.' : !prefixIsValid()
    ? 'Check the address and channel in Link settings.' : serialMode ? unavailablePort
      : !Number.isInteger(stationCount) || stationCount < 0 || stationCount > 255 || $('stations-config').value === ''
        ? 'Enter a simulator station count from 0 to 255.' : '');
  disabled('connect', connectReason);
  disabled('disconnect', !state.connected ? 'Connect first.' : state.recipe_active ? 'Stop the active recipe before disconnecting.' : busy ? 'Wait for the current request.' : '');
  disabled('armed', !state.connected ? 'Connect first.' : busy ? 'Wait for the current request.' : state.motion_block || '');
  disabled('auto', !state.connected ? 'Connect first.' : '');
  if (state.connected && state.link_quality?.timed_report_interval_s && !state.stale) {
    display('auto-why', 'Timed reports are updating status.');
  }
  disabled('silence', busy ? 'Wait for the current request.' : '');
  $('armed').checked = state.armed || false;
  $('movement').dataset.armed = String(!!state.armed);
  $('silence-label').hidden = !state.connected || state.settings.mode !== 'simulator';
  $('silence').checked = state.silent || false;
  display('interlock', state.robot_interlock === false ? 'No robot interlock: AGV-only test bench.'
    : state.robot_interlock ? 'Robot travel pose required for motion. Save the pose on Teaching.'
    : 'Checking robot interlock…');
  display('connection-note', reportError || ($('link-settings').open ? connectionLocked || connectReason : ''));

  for (const [id, control] of controls) {
    if (control.name) disabled(id, commandReason(control.name, control.value));
    renderOutcome(id);
  }
  if (!lastPressed && capability) display('outcome', state.connected ? 'Query status to check the AGV.' : 'Select Connect to begin.');
  const age = state.status_age_s;
  const elapsed = age == null ? 'no status report yet' : `last status report ${seconds(age)} s ago`;
  const error = networkError || state.error;
  display('link', error ? `Error · ${elapsed}` : !state.connected ? 'Disconnected'
    : state.stale ? `Link stale · ${elapsed}` : `Connected · ${elapsed}`);
  $('link').dataset.state = error || state.stale ? 'failed' : '';

  const alarms = [];
  if (state.stale && state.connected) alarms.push('Link stale. The AGV may still be moving. Use its e-stop if needed. Check the radio before moving again.');
  if (error) alarms.push(networkError ? state.connected ? linkLost : 'Page service unavailable. Reload and reconnect when safe.' : state.error);
  if (state.status?.fault_code) alarms.push(faultText(state.status) + '. Check the machine and consult the manual before moving.');
  if (state.status?.obstacle_stop === true) alarms.push('Obstacle stop active. Check the track and clear it when safe.');
  display('alarms', alarms.join(' '));

  const lastEvent = journal.events.at(-1);
  if (renderedEvent !== lastEvent) {
    renderedEvent = lastEvent;
    $('event-feed').replaceChildren(...journal.events.slice(-12).reverse().map(event =>
      make('li', `${localTime(event.timestamp)}  ${event.text}`)));
  }
  $('log-gap').hidden = !journal.log_gap;
  const lastLog = journal.log.at(-1);
  if (renderedLog !== lastLog) {
    renderedLog = lastLog;
    display('log', journal.log.slice(-30).reverse().map(entry =>
      `${localTime(entry.timestamp)}  ${entry.direction}  ${entry.hex}\n  ${JSON.stringify(entry.decode)}`).join('\n'));
  }
}

function eventText(event, status) {
  if (event.field === 'stopped') return event.after === true ? 'AGV stopped' : event.after === false ? 'AGV started moving' : 'Motion state changed to unknown';
  if (event.field === 'link_stale') {
    return event.after ? 'Link stale' : event.disconnected ? 'Stale alarm cleared on disconnect' : 'Link recovered';
  }
  if (event.field === 'transport_error') return event.after ? 'Transport error' : 'Transport recovered';
  const title = event.field === 'current_station' && (status?.mode === 0 || status?.max_stations === 0)
    ? 'Stop counter' : eventFields[event.field];
  const value = item => item === true ? 'on' : item === false ? 'off' : item;
  return `${title}: ${value(event.before)} → ${value(event.after)}`;
}

function soundAlarm() {
  if (!$('sound').checked || !audio || audio.state !== 'running') return;
  const oscillator = audio.createOscillator();
  const gain = audio.createGain();
  gain.gain.value = .08;
  oscillator.frequency.value = 620;
  oscillator.connect(gain).connect(audio.destination);
  oscillator.start();
  oscillator.stop(audio.currentTime + .18);
}

function ingest(next, connection) {
  const cursor = journal.cursors[connection] ?? {log: 0, events: 0};
  const entries = next.log.filter(entry => entry.id > cursor.log);
  if (entries.length && entries[0].id > cursor.log + 1) journal.log_gap = true;
  journal.log.push(...entries.map(entry => ({...entry, connection})));
  cursor.log = entries.at(-1)?.id ?? cursor.log;
  for (const entry of entries) {
    if (entry.direction !== 'RX') continue;
    if (entry.decode.error && entry.hex) {
      reportError = badFrame;
      reportErrorUntil = performance.now() + 5000;
    }
  }
  const events = (next.events ?? []).filter(event => event.id > cursor.events);
  for (const event of events) journal.events.push({...event, connection, text: eventText(event, next.status)});
  cursor.events = events.at(-1)?.id ?? cursor.events;
  journal.cursors[connection] = cursor;
  if (next.settings) journal.transports[connection] = next.settings;
  if (next.settings && next.link_quality) (journal.link_quality ??= {})[connection] = next.link_quality;
  for (const [name, result] of [...(next.replaced ?? []), ...Object.entries(next.results)]) {
    const key = `${connection}/${result.tx_id}`;
    if (result.background || !result.tx_id || (!journal.commands[key] && !entries.some(entry => entry.id === result.tx_id))) continue;
    journal.commands[key] = {connection, name, timestamp: result.sent_at, ...result};
  }
}

function connectionKeyFor(snapshot) {
  return snapshot.connection_started_at ?? String(snapshot.connection_id);
}

function apply(next) {
  if (next.sequence <= state.sequence) return;
  if (next.commands) {
    if (!capability) buildCommands(next.commands);
    capability = next;
  }
  const connection = connectionKeyFor(next);
  const changed = connection !== connectionKeyFor(state);
  if (changed) {
    $('auto').checked = false;
    reportError = '';
    if (next.connected) {
      $('mode').value = next.settings.mode;
      $('address').value = next.settings.address;
      $('channel').value = next.settings.channel;
      const previous = journal.events.findLast(event => event.field === 'transport_error');
      if (previous?.after) journal.events.push({
        field: 'transport_error', before: true, after: false, text: 'Transport recovered', timestamp: isoNow(), connection,
      });
    }
  }
  if ((next.stale && !state.stale) || (next.status?.fault_code && next.status.fault_code !== state.status?.fault_code)) soundAlarm();
  if (next.error && next.error !== state.error) reportError = '';
  if (performance.now() >= reportErrorUntil) reportError = '';
  state = next;
  if (next.ports || changed && capability) showPorts(next.ports ?? capability.ports);
  ingest(next, connection);
  for (const [id, item] of outcomes) {
    if (item.connection !== connection || !item.name) continue;
    const result = next.results[item.name];
    if (!result) continue;
    const matching = item.result ? result.tx_id === item.result.tx_id
      : reads.has(item.name) && !result.background && result.tx_id !== item.previousTx;
    const replaced = next.replaced?.find(([, old]) => old.tx_id === item.result?.tx_id);
    if (matching || replaced) item.result = matching ? result : replaced[1];

  }
  render();
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
  if (!response.ok) throw Object.assign(new Error(value.error || 'AGV request failed.'), {api: true, status: response.status});
  return value;
}

function changeNetworkError(error) {
  if (networkError === error) return;
  networkError = error;
  journal.events.push({field: 'page_error', after: error, timestamp: isoNow(),
    text: error ? 'Page lost contact with its service' : 'Page service recovered'});
}

function requestError(error) {
  if (!error.api) return state.connected ? linkLost : 'Page service unavailable. Reload this page, then select Connect.';
  const raw = error.message;
  if (raw === linkLost || /^(Serial permission denied\.|Serial port is busy or missing\.)/.test(raw)) return raw;
  if (/^(Tick Armed|A robot job is running|Choose a learned station|Connect an AGV|Service is closing|Job storage failed|Robot state is uncertain)/.test(raw)) {
    return 'Refused: ' + raw[0].toLowerCase() + raw.slice(1);
  }
  if (error.status === 403) return 'Refused: page access expired. Reload the page.';
  return 'Refused: check the connection and settings, then query status.';
}

async function post(body, {id, priority = false, background = false, viaEsc = false} = {}) {
  if (!background && busy && !priority) return;
  if (!background && !priority) busy = true;
  let pending;
  if (id) {
    lastPressed = id;
    pending = {name: body.command, connection: connectionKeyFor(state),
      previousTx: state.results[body.command]?.tx_id, direct: {text: 'Sending…', tone: 'running'}};
    outcomes.set(id, pending);
    render();
  }
  try {
    const result = await api({connection_id: state.connection_id, ...body});
    changeNetworkError(false);
    if (body.action === 'connect' && result.connected) {
      const {mode, port, address, channel} = result.settings;
      const description = capability.ports.find(({device}) => device === port)?.description ?? '';
      savedSettings = {mode, port: port ? {device: port, description} : null, address, channel};
      save(connectionKey, savedSettings);
    }
    apply(result);
    if (id && outcomes.get(id) === pending) {
      if (body.command) {
        const resultConnection = connectionKeyFor(result);
        const current = state.results[body.command];
        const fromResponse = result.results[body.command];
        const newest = connectionKeyFor(state) === resultConnection && current?.tx_id === fromResponse.tx_id ? current : fromResponse;
        outcomes.set(id, {name: body.command, result: newest, connection: resultConnection, viaEsc});
      } else {
        const connected = result.settings?.mode === 'simulator' ? 'Simulator' : 'Serial AGV';
        const text = {connect: 'Connected · ' + connected, disconnect: 'Disconnected.',
          arm: result.armed ? 'Armed. Movement controls are enabled.' : 'Disarmed.',
          silence: result.silent ? 'Silent link simulation on.' : 'Silent link simulation off.'};
        outcomes.set(id, {direct: {text: text[body.action], tone: 'done'}});
      }
    }
  } catch (error) {
    if (!error.api) changeNetworkError(true);
    const text = requestError(error);
    if (id && outcomes.get(id) === pending) outcomes.set(id, {direct: {text, tone: 'failed'}});
    if (!background) journal.log.push({timestamp: isoNow(), direction: 'Page', hex: '', decode: {error: error.message}});
    nextRefresh = 0;
  } finally {
    if (!background && !priority) busy = false;
    render();
  }
}

function sendCommand(command, id, preset, viaEsc = false) {
  if (command === 'query_status') nextPoll = performance.now() + 1000;
  return post({action: 'send', command, value: commandValue(command, preset), background: !id},
    {id, priority: urgent.has(command), background: !id, viaEsc});
}

async function tick() {
  const now = performance.now();
  const freshTimedReports = state.link_quality?.timed_report_interval_s && !state.stale;
  if (!busy && !polling && !state.recipe_active && state.connected && $('auto').checked && !freshTimedReports && now >= nextPoll) {
    polling = true;
    try { await sendCommand('query_status'); }
    finally { polling = false; }
    return;
  }
  if (fetching || now < nextRefresh) return;
  fetching = true;
  nextRefresh = now + (state.connected ? 500 : 2000);
  try {
    const next = await api();
    changeNetworkError(false);
    apply(next);
  } catch {
    changeNetworkError(true);
    render();
  } finally {
    fetching = false;
  }
}

function exportRecord() {
  const connection = connectionKeyFor(state);
  const results = Object.fromEntries(Object.entries(state.results).filter(([, result]) =>
    journal.commands[`${connection}/${result.tx_id}`]));
  return {exported_at: isoNow(), started_at: session.started_at, connection: state.settings,
    transports: Object.entries(journal.transports).map(([connection, settings]) => ({connection, ...settings})), auto_poll: $('auto').checked, armed: state.armed,
    link_quality: journal.link_quality ?? {},
    simulator_silent: state.silent, results, settings: session.settings,
    checklist: Object.fromEntries(checklist.map(([title], index) => [index + 1,
      {title, result: 'Not tested', notes: '', ...session.checklist[index + 1]}])),
    commands: Object.values(journal.commands), events: journal.events, log_gap: journal.log_gap, log: journal.log};
}

function markdown(record) {
  const escape = value => String(value ?? '').replace(/[\r\n]+/g, ' ').replace(/[\\|`*_<>\[\]]/g, '\\$&');
  const transportName = mode => mode === 'simulator' ? 'Simulator' : mode === 'serial' ? 'Serial AGV' : 'No connection';
  const transportByConnection = new Map(record.transports.map(item => [item.connection, transportName(item.mode)]));
  const labelTransport = entry => transportByConnection.get(entry.connection) ?? 'No connection';
  const tests = Object.entries(record.checklist);
  const lines = ['| Test | Result |', '| --- | --- |', ...tests.map(([number, test]) => `| ${number}. ${test.title} | ${test.result} |`),
    '', '# AGV commissioning report', '', `Transport — ${record.transports.length ? [...new Set(record.transports.map(item => transportName(item.mode)))].join(', ') : 'No connection'}`,
    '', `Exported — ${record.exported_at}`];
  if (Object.keys(record.link_quality).length) lines.push('', '## Link quality', '',
    ...Object.entries(record.link_quality).map(([connection, quality]) =>
      `- ${transportByConnection.get(connection)} · ${connection} · ${linkQualityText(quality)}`));
  lines.push('', '## Settings as found', '',
    ...Object.entries(foundFields).map(([id, label]) => `- ${label} — ${escape(record.settings[id]) || 'Unrecorded'}`));
  for (const [number, test] of tests) {
    lines.push('', `## ${number}. ${test.title}`, '', `Result — ${test.result}`, '', `Notes — ${escape(test.notes) || 'None'}`);
    const proof = test.evidence;
    if (!proof) continue;
    lines.push('', `Observed — ${proof.marked_at} · ${transportName(proof.transport)}`);
    if (proof.status) {
      const status = proof.status;
      const station = status.mode === 0 || status.max_stations === 0 ? 'stop counter' : 'station';
      const target = status.mode === 0 ? '' : `, target ${status.target_station}`;
      lines.push(`Reported status — ${status.stopped === true ? 'stopped' : status.stopped === false ? 'running' : 'unknown'}, ${station} ${status.current_station}${target}, ${speedText(status.actual_speed_m_h)}, battery ${status.battery_percent}%, ${faultText(status)}. Status age ${seconds(proof.status_age_s)} s${proof.stale ? ' (stale)' : ''}.`);
    }
    for (const command of proof.commands) lines.push(`- ${command.timestamp} · ${labelTransport(command)} · ${commands.get(command.name)?.label ?? command.name} · ${resultText(command.name, command).text}`);
    for (const event of proof.events) lines.push(`- ${event.timestamp} · ${labelTransport(event)} · ${escape(event.text)}`);
  }
  if (record.events.length) lines.push('', '## Session events', '',
    ...record.events.map(event => `- ${event.timestamp} · ${labelTransport(event)} · ${escape(event.text)}`));
  if (record.log_gap) lines.push('', 'Some raw log entries are missing. See the JSON record for available entries.');
  return lines.join('\n') + '\n';
}

function download(content, type, name) {
  const url = URL.createObjectURL(new Blob([content], {type}));
  const link = make('a', '', {href: url, download: name});
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

for (const id of ['connect', 'disconnect', 'armed', ...urgent]) {
  register(id, $(id), $(id + '-outcome'), $(id + '-why'));
  if (urgent.has(id)) {
    controls.get(id).name = id;
    $(id).addEventListener('click', () => sendCommand(id, id));
  }
}
window.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  event.preventDefault();
  if (event.repeat) return;
  if (state.connected) return sendCommand('stop', 'stop', undefined, true);
  lastPressed = 'stop';
  outcomes.set('stop', {direct: {text: commandReason('stop'), tone: 'waiting'}});
  renderOutcome('stop');
}, {capture: true});
buildChecklist();

$('link-settings').addEventListener('toggle', render);
$('mode').addEventListener('input', () => {
  if (capability) showPorts(capability.ports);
  render();
});
for (const id of ['port', 'address', 'channel', 'stations-config']) $(id).addEventListener('input', render);
$('connect').addEventListener('click', () => post({action: 'connect', mode: $('mode').value,
  port: $('port').value, address: $('address').value, channel: $('channel').value,
  stations: Number($('stations-config').value)}, {id: 'connect'}));
$('disconnect').addEventListener('click', () => post({action: 'disconnect'}, {id: 'disconnect'}));
$('armed').addEventListener('change', event => post({action: 'arm', armed: event.currentTarget.checked}, {id: 'armed'}));
$('silence').addEventListener('change', event => post({action: 'silence', silent: event.currentTarget.checked}));
$('auto').addEventListener('change', () => { nextPoll = 0; render(); void tick(); });
$('sound').addEventListener('change', () => {
  if (!$('sound').checked || !window.AudioContext) return;
  audio ??= new AudioContext();
  void audio.resume().catch(() => {});
});

$('event-form').addEventListener('submit', event => {
  event.preventDefault();
  const text = $('mark-text').value.trim();
  if (!text) return;
  const timestamp = isoNow();
  journal.events.push({timestamp, field: 'observed', text, connection: connectionKeyFor(state)});
  $('mark-text').value = '';
  display('record-note', 'Observed event recorded at ' + localTime(timestamp) + '.');
  saveSession();
  render();
});

$('download').addEventListener('click', () => download(JSON.stringify(exportRecord(), null, 2), 'application/json', 'agv-test-record.json'));
$('download-report').addEventListener('click', () => download(markdown(exportRecord()), 'text/markdown', 'agv-test-report.md'));
$('new-session').addEventListener('click', () => {
  if (!confirm('Start a new test session and clear the current checklist, settings and record?')) return;
  session = newSession();
  journal = emptyJournal();
  outcomes.clear();
  lastPressed = undefined;
  journal.cursors[connectionKeyFor(state)] = {log: state.log?.at(-1)?.id ?? 0, events: state.events?.at(-1)?.id ?? 0};
  if (state.connected) journal.transports[connectionKeyFor(state)] = state.settings;
  if (state.connected && state.link_quality) journal.link_quality[connectionKeyFor(state)] = state.link_quality;
  $('sound').checked = false;
  for (const id of Object.keys(foundFields)) $(id).value = '';
  checklist.forEach((_, index) => $('test-' + (index + 1)).dispatchEvent(new Event('agv-reset')));
  saveSession();
  saveJournal();
  display('record-note', 'New test session started.');
  render();
});

if (['simulator', 'serial'].includes(savedSettings?.mode)) $('mode').value = savedSettings.mode;
for (const id of ['address', 'channel']) if (typeof savedSettings?.[id] === 'string') $(id).value = savedSettings[id];
if ('ResizeObserver' in window) new ResizeObserver(entries =>
  document.body.style.setProperty('--agv-top-height', entries[0].target.offsetHeight + 'px')).observe(document.querySelector('.agv-safety'));
window.addEventListener('pagehide', () => { saveSession(); saveJournal(); });
void tick();
setInterval(tick, 200);
