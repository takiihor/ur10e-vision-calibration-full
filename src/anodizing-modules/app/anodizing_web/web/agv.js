import { connectionKey, sessionKey, journalKey, urgent, reads, linkLost, badFrame, motionText, pauseStationOnly, mergeJournal,
  stationCallRequired, stationLimit, emptyJournal, isoNow, localTime, seconds, speedText, load, save, newSession,
  fieldLabels, modes, outcomesList, checklist, checklistCommands, foundFields, faultText,
  linkQualityText, resultText, eventText, connectionKeyFor, exportRecord, markdown,
  radioOverrides, savedRadioFields, rememberRadioOverrides, radioRouteError, radioPlaceholder } from './agv-record.mjs';

'use strict';

const $ = id => document.getElementById('agv-' + id);
const token = document.querySelector('meta[name="module-token"]').content;
const controls = new Map(), commands = new Map(), outcomes = new Map();
const display = (id, text) => {
  if ($(id).textContent !== text) $(id).textContent = text;
};

let session = load(sessionKey, newSession());
const savedJournal = load(journalKey, null);
let journal = savedJournal?.started_at === session.started_at ? savedJournal.journal : emptyJournal();
let savedSettings = load(connectionKey, null);
let state = {connection_id: 0, sequence: -1, results: {}};
let capability, busy = false, fetching = false, nextRefresh = 0;
let lastPressed, networkError = false, reportError = '', lastMaximum, renderedStatus, renderedLog, renderedEvent;
let audio, reportErrorUntil = 0;
let receivedAt = performance.now();

function statusAge() {
  return state.status_age_s == null ? null : state.status_age_s + (performance.now() - receivedAt) / 1000;
}

function statusStale() {
  return state.stale || statusAge() >= (state.recipe_active ? 3 : 5);
}

function make(tag, text, attributes = {}) {
  const node = Object.assign(document.createElement(tag), attributes);
  if (text) node.textContent = text;
  return node;
}

function saveSession() {
  if (!save(sessionKey, session)) display('record-note', 'Browser storage is unavailable. Download the record before leaving.');
}

function saveJournal() {
  const currentSession = load(sessionKey, null);
  if (currentSession && currentSession.started_at !== session.started_at) return;
  const stored = load(journalKey, null);
  if (stored?.started_at === session.started_at) journal = mergeJournal(stored.journal, journal);
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
        commands: Object.values(journal.commands).filter(entry => during(entry) &&
          (entry.requested_commands ?? [entry.requested_command ?? entry.name])
            .some(command => checklistCommands[index].includes(command))), events: journal.events.filter(during),
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

function radioFieldsInForm() {
  return {address: $('address').value, channel: $('channel').value};
}

function restoreRadioFields() {
  for (const [field, value] of Object.entries(savedRadioFields(savedSettings, $('mode').value))) {
    $(field).value = value;
  }
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
  if ($('mode').value === 'wired') {
    const port = state.wired_port;
    select.replaceChildren(new Option(port, port));
    select.value = port;
    return;
  }
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
  if (state.wired_port && !reads.has(name)) return state.motion_block;
  if (name === 'pause' && !statusStale() && state.status?.mode === 0) return pauseStationOnly;
  if (urgent.has(name)) return '';
  if (name === 'call_station' && state.status?.mode === 0) return stationCallRequired;
  if (state.recipe_active && !commands.get(name)?.motion) return 'The recipe is checking AGV status. Follow Current operation on Pick/place.';
  if (commands.get(name)?.motion) {
    if (state.motion_block) return state.motion_block;
    if (!state.armed) return 'Tick Armed to enable movement.';
    if (statusStale() || networkError || !state.status) return 'Wait for a fresh AGV status report.';
    if (![0, 2].includes(state.status.mode)) return 'Select standard or station-call mode in the XBOT app first.';
    if (state.status.fault_code || state.status.obstacle_stop) return 'Clear the reported AGV fault or obstacle before continuing.';
    if (name === 'resume' && state.status.mode === 0) return 'Resume requires station-call mode.';
    if (name === 'call_station' && !state.status.max_stations) return 'Learn the route before calling a station.';
  }
  if (busy) return 'Wait for the current request.';
  const value = commandValue(name, preset);
  const maximum = stationLimit(state.status);
  if (name === 'call_station' && maximum > 0 && value > maximum) return `Choose a learned station from 1 to ${maximum}.`;
  if (value === undefined) return name === 'set_speed' ? 'Enter a whole number from 500 to 4000 m/h.' : 'Enter a whole number from 1 to 255.';
  return '';
}

function transportLabel({mode, port}) {
  if (mode === 'wired') return `Wired RS485 · ${port}`;
  return mode === 'simulator' ? 'Simulator' : 'Serial AGV';
}

function renderStatus() {
  const status = state.connected && !statusStale() && !networkError ? state.status : null;
  const standard = status?.mode === 0;
  const maximum = standard ? undefined : stationLimit(status);
  $('manual-station').hidden = standard;
  $('target-readout').hidden = standard;
  display('station-note', standard ? stationCallRequired : maximum === 0
    ? 'No route learned yet. Teach the route with the AGV’s green button; see the XBOT station guide (V1.61, pp. 2–3).'
    : maximum > 0 ? `${maximum} stations on the learned route. Current station is marked on its button.`
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
  display('transport', state.connected ? transportLabel(state.settings) : 'No open connection');
  $('link-quality').hidden = !state.connected || !state.link_quality;
  if (state.link_quality) display('link-quality', linkQualityText(state.link_quality));
  const liveStatus = status ? state.status_hex : null;
  if (renderedStatus === liveStatus) return;
  renderedStatus = liveStatus;
  const bool = value => value === true ? 'On' : value === false ? 'Off' : 'Unknown';
  const actual = status?.actual_speed_m_h;
  display('station-label', standard ? 'Stop counter (not a station number in standard mode)'
    : maximum === 0 ? 'Stop counter (no route learned)' : 'Current station');
  const motion = motionText(status);
  display('state-motion', motion);
  $('state-motion').dataset.tone = motion === 'Running' ? 'running' : '';
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
  display('raw', liveStatus ?? '');
}

function renderOutcome(id) {
  const element = controls.get(id)?.outcome;
  const outcome = outcomes.get(id);
  const stop = id === 'stop' && state.executor_link && state.executor_stop;
  const feedback = stop && outcome?.direct?.tone !== 'running' ? {
    text: [stop.received ? 'Stop received' : 'Stop receipt unconfirmed',
      stop.halt_attempted ? 'Halt attempted' : 'Halt attempt unconfirmed',
      stop.confirmed ? 'Stopped status confirmed' : 'Stop unconfirmed'].join(' · '),
    tone: stop.confirmed ? 'done' : 'failed',
  } : resultText(outcome?.name, outcome?.result, outcome?.viaEsc, outcome?.requested) ?? outcome?.direct;
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
  const mode = $('mode').value;
  const wiredMode = mode === 'wired';
  const serialMode = wiredMode || mode === 'serial';
  for (const field of ['address', 'channel']) {
    $(field).placeholder = radioPlaceholder(mode, capability?.radio_profile, field);
  }
  const connectionLocked = busy ? 'Wait for the current request.' : state.connected ? 'Disconnect to change link settings.' : '';
  for (const id of ['mode', 'address', 'channel', 'stations-config']) {
    $(id).disabled = !!connectionLocked;
    $(id).title = connectionLocked;
  }
  $('port-label').hidden = !serialMode;
  $('stations-config-label').hidden = serialMode;
  const unavailablePort = !capability?.serial_available ? 'Serial support is unavailable. Use the IPC app environment.' :
    !$('port').value ? `Select a serial port. If it is missing, reseat the USB plug and reload.` : '';
  disabled('port', connectionLocked || (wiredMode ? 'Port is set by the IPC site profile.'
    : !serialMode ? 'Select Serial port first.' : !capability?.serial_available ? unavailablePort : ''));
  const stationCount = Number($('stations-config').value);
  const connectReason = connectionLocked || (!capability ? 'Waiting for the page service.' :
    radioRouteError(mode, radioFieldsInForm(), capability.radio_profile) || (serialMode ? unavailablePort
      : !Number.isInteger(stationCount) || stationCount < 0 || stationCount > 255 || $('stations-config').value === ''
        ? 'Enter a simulator station count from 0 to 255.' : ''));
  disabled('connect', connectReason);
  disabled('disconnect', !state.connected ? 'Connect first.' : state.recipe_active ? 'Stop the active recipe before disconnecting.' :
    state.halt_pending ? 'AGV Stop or Pause is awaiting delivery. Wait before disconnecting.' : busy ? 'Wait for the current request.' : '');
  disabled('armed', !state.connected ? 'Connect first.' : busy ? 'Wait for the current request.' : state.motion_block || '');
  disabled('silence', busy ? 'Wait for the current request.' : '');
  $('armed').checked = state.armed || false;
  $('movement').dataset.armed = String(!!state.armed);
  $('silence-label').hidden = !state.connected || state.settings.mode !== 'simulator';
  $('silence').checked = state.silent || false;
  display('interlock', state.wired_port ? state.motion_block
    : state.robot_interlock === false ? 'No robot interlock: AGV-only test bench.'
    : state.robot_interlock ? 'Robot travel pose required for motion. Save the pose on Teaching.'
    : 'Checking robot interlock…');
  display('connection-note', reportError || ($('link-settings').open ? connectionLocked || connectReason : ''));

  for (const [id, control] of controls) {
    if (control.name) disabled(id, commandReason(control.name, control.value));
    renderOutcome(id);
  }
  if (!lastPressed && capability) display('outcome', state.connected ? '' : 'Select Connect to begin.');
  const age = statusAge();
  const stale = state.connected && statusStale();
  const elapsed = age == null ? 'no status report yet' : `last status report ${seconds(age)} s ago`;
  const error = networkError || state.error;
  display('link', error ? `Error · ${elapsed}` : !state.connected ? 'Disconnected'
    : stale ? `Link stale · ${elapsed}` : `Connected · ${elapsed}`);
  $('link').dataset.state = error || stale ? 'failed' : '';

  const alarms = [];
  if (stale) alarms.push('Link stale. The AGV may still be moving. Use its e-stop if needed. Check the radio before moving again.');
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
    journal.commands[key] = {...journal.commands[key], connection, name, timestamp: result.sent_at, ...result};
  }
}

function apply(next) {
  if (next.connection_id < state.connection_id || next.sequence <= state.sequence) return;
  if (next.commands) {
    if (!capability) {
      buildCommands(next.commands);
      $('wired-mode').disabled = $('wired-mode').hidden = !next.wired_port;
      if (next.wired_port) {
        const serialOption = $('wired-mode').nextElementSibling;
        if (serialOption) serialOption.textContent = 'Serial port (LoRa)';
        $('stop-shortcut').hidden = true;
        $('stop').removeAttribute('aria-keyshortcuts');
      }
      if (next.wired_port && !next.connected) {
        $('mode').value = 'wired';
        restoreRadioFields();
      }
    }
    capability = next;
  }
  const connection = connectionKeyFor(next);
  const changed = connection !== connectionKeyFor(state);
  if (changed) {
    reportError = '';
    if (next.connected) {
      $('mode').value = next.settings.mode;
      $('address').value = next.settings.address ?? '';
      $('channel').value = next.settings.channel ?? '';
      const previous = journal.events.findLast(event => event.field === 'transport_error');
      if (previous?.after) journal.events.push({
        field: 'transport_error', before: true, after: false, text: 'Transport recovered', timestamp: isoNow(), connection,
      });
    }
  }
  if (!next.connected && state.connected) restoreRadioFields();
  if ((next.stale && !state.stale) || (next.status?.fault_code && next.status.fault_code !== state.status?.fault_code)) soundAlarm();
  if (next.error && next.error !== state.error) reportError = '';
  if (performance.now() >= reportErrorUntil) reportError = '';
  state = next;
  receivedAt = performance.now();
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

async function api(body, refreshPorts = false) {
  const options = {headers: {'X-Module-Token': token}};
  if (body) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const route = body ? '/api/agv' : refreshPorts ? '/api/agv?ports=1' : '/api/agv?cached=1';
  const response = await fetch(route, options);
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
  if (raw === 'AGV link is status-only.') return raw;
  if (raw === linkLost || /^(Serial permission denied\.|Serial port is busy or missing\.)/.test(raw)) return raw;
  if (/^(Tick Armed|A robot job is running|Choose a learned station|Connect an AGV|Service is closing|Job storage failed|Robot state is uncertain|AGV movement needs|Stop, Pause or disarming cancelled|AGV Stop or Pause is awaiting)/.test(raw)) {
    return 'Refused: ' + raw[0].toLowerCase() + raw.slice(1);
  }
  if (error.status === 403) return 'Refused: page access expired. Reload the page.';
  return 'Refused: check the connection and settings, then query status.';
}

async function post(body, {id, priority = false, viaEsc = false} = {}) {
  if (busy && !priority) return;
  if (!priority) busy = true;
  let pending;
  if (id) {
    lastPressed = id;
    pending = {name: body.command, connection: connectionKeyFor(state),
      previousTx: state.results[body.command]?.tx_id, direct: {text: 'Sending…', tone: 'running'}};
    outcomes.set(id, pending);
    render();
  }
  try {
    if (state.executor_link && (body.armed === true || body.command === 'call_station')) {
      body = { ...body, executor_boot: state.executor_boot, executor_session: state.executor_session,
        stop_generation: state.stop_generation };
    }
    const result = await api({connection_id: state.connection_id, ...body});
    changeNetworkError(false);
    if (body.action === 'connect' && result.connected) {
      const {mode, port} = result.settings;
      const description = capability.ports.find(({device}) => device === port)?.description ?? '';
      savedSettings = {mode, port: port ? {device: port, description} : null, ...rememberRadioOverrides(body, result.settings)};
      save(connectionKey, savedSettings);
    }
    apply(result);
    if (id && outcomes.get(id) === pending) {
      const name = result.urgent_command ?? body.command;
      if (result.executor_link && ['stop', 'call_station'].includes(body.command)) {
        outcomes.delete(id);
      } else if (result.connection_id !== state.connection_id || (name && !result.results[name])) {
        outcomes.set(id, {direct: {text: 'Connection changed. Check the AGV before trying again.', tone: 'failed'}});
      } else if (body.command) {
        const resultConnection = connectionKeyFor(result);
        const current = state.results[name];
        const fromResponse = result.results[name];
        const newest = connectionKeyFor(state) === resultConnection && current?.tx_id === fromResponse?.tx_id ? current : fromResponse;
        outcomes.set(id, {name, requested: body.command, result: newest, connection: resultConnection, viaEsc});
        const entry = journal.commands[`${resultConnection}/${newest.tx_id}`];
        if (priority && entry) {
          entry.requested_commands = [...new Set([...(entry.requested_commands ?? []), body.command])];
          saveJournal();
        }
      } else {
        const text = {connect: result.settings ? 'Connected · ' + transportLabel(result.settings) : '', disconnect: 'Disconnected.',
          arm: result.armed ? 'Armed. Movement controls are enabled.' : 'Disarmed.',
          silence: result.silent ? 'Silent link simulation on.' : 'Silent link simulation off.'};
        outcomes.set(id, {direct: {text: text[body.action], tone: 'done'}});
      }
    }
  } catch (error) {
    if (!error.api) changeNetworkError(true);
    const text = requestError(error);
    if (id && outcomes.get(id) === pending) outcomes.set(id, {direct: {text, tone: 'failed'}});
    journal.log.push({timestamp: isoNow(), direction: 'Page', hex: '', decode: {error: error.message}});
    nextRefresh = 0;
  } finally {
    if (!priority) busy = false;
    render();
  }
}

function sendCommand(command, id, preset, viaEsc = false) {
  if ((command === 'pause' || (state.wired_port && !reads.has(command))) && commandReason(command, preset)) return;
  const body = {action: 'send', command, value: commandValue(command, preset), background: false};
  if (commands.get(command)?.motion) body.stop_generation = state.stop_generation;
  return post(body, {id, priority: urgent.has(command), viaEsc});
}

async function tick() {
  const now = performance.now();
  if (renderedStatus != null && statusStale()) render();
  if (fetching || now < nextRefresh) return;
  fetching = true;
  nextRefresh = now + (state.connected ? 500 : 2000);
  try {
    const next = await api(null, !capability);
    changeNetworkError(false);
    apply(next);
  } catch {
    changeNetworkError(true);
    render();
  } finally {
    fetching = false;
  }
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
  if (state.connected && !state.wired_port) return sendCommand('stop', 'stop', undefined, true);
  lastPressed = 'stop';
  outcomes.set('stop', {direct: {text: commandReason('stop'), tone: 'waiting'}});
  renderOutcome('stop');
}, {capture: true});
buildChecklist();

$('link-settings').addEventListener('toggle', render);
$('mode').addEventListener('input', () => {
  restoreRadioFields();
  if ($('port').value === state.wired_port) $('port').value = '';
  if (capability) showPorts(capability.ports);
  if ($('mode').value === 'serial') void api(null, true).then(apply, () => {});
  render();
});
for (const id of ['port', 'address', 'channel', 'stations-config']) $(id).addEventListener('input', render);
$('connect').addEventListener('click', () => post({action: 'connect', mode: $('mode').value,
  port: $('port').value, stations: Number($('stations-config').value),
  ...radioOverrides(radioFieldsInForm())}, {id: 'connect'}));
$('disconnect').addEventListener('click', () => post({action: 'disconnect'}, {id: 'disconnect'}));
$('armed').addEventListener('change', event => post({action: 'arm', armed: event.currentTarget.checked}, {id: 'armed'}));
$('silence').addEventListener('change', event => post({action: 'silence', silent: event.currentTarget.checked}));
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

$('download').addEventListener('click', () => download(JSON.stringify(exportRecord(state, session, journal), null, 2), 'application/json', 'agv-test-record.json'));
$('download-report').addEventListener('click', () => download(markdown(exportRecord(state, session, journal), commands), 'text/markdown', 'agv-test-report.md'));
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

if (['simulator', 'serial'].includes(savedSettings?.mode)) {
  const defaultMode = $('mode').value;
  $('mode').value = savedSettings.mode;
  if (!$('mode').value) $('mode').value = defaultMode;
}
restoreRadioFields();
if ('ResizeObserver' in window) new ResizeObserver(entries =>
  document.body.style.setProperty('--agv-top-height', entries[0].target.offsetHeight + 'px')).observe(document.querySelector('.agv-safety'));
window.addEventListener('pagehide', saveJournal);
void tick();
setInterval(tick, 200);
