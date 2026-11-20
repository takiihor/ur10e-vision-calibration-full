const connectionKey = 'ano-agv-connection';
const sessionKey = 'ano-agv-commissioning';
const journalKey = sessionKey + '-journal';
const urgent = new Set(['stop', 'pause']);
const reads = new Set(['query_status', 'read_speed']);
const linkLost = 'Link lost. The AGV may still be moving. Use its e-stop if needed.';
const portRetry = 'Reseat the USB plug, reload this page, then select Connect.';
const badFrame = 'Invalid AGV reply. Check the radio connection, then query status again.';
const stationCallRequired = 'Station call needs the station-call function, set in the XBOT app. In standard mode, use Forward/Reverse.';

function stationLimit(status) {
  const legs = status?.max_stations;
  // XBOT V2.0 manual lines 630–645 include the start as station 1.
  return legs > 0 ? Math.min(legs + 1, 255) : legs;
}

const emptyJournal = () => ({log: [], events: [], commands: {}, transports: {}, link_quality: {}, cursors: {}, log_gap: false});
const isoNow = () => new Date().toISOString();
const localTime = stamp => new Date(stamp).toLocaleTimeString('en-GB', {
  hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3, hour12: false,
});
const seconds = value => Number(value ?? 0).toFixed(1);
const speedText = value => `${value} m/h · ${(value / 1000).toFixed(3).replace(/0+$/, '').replace(/\.$/, '.0')} km/h`;
function load(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
  catch { return fallback; }
}

function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); return true; }
  catch { return false; }
}

const newSession = () => ({started_at: isoNow(), settings: {}, checklist: {}});
const fieldLabels = {
  vehicle_id: 'Vehicle ID', set_speed_m_h: 'Set speed', max_stations: 'Learned legs',
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
  ['Read status and speed', 'Check for fresh status. Select Read speed and compare the reply with the set speed in Machine details.', 'The page shows a fresh status and replies with a speed. The AGV stays parked.'],
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

function faultText(status) {
  if (!status || status.fault_code == null) return '—';
  return status.fault_code === 0 ? 'No fault' : `Fault ${status.fault_code} · ${status.fault_description} (manual p. ${status.fault_page})`;
}

function linkQualityText(quality) {
  const {sent, acknowledged, resent, no_reply: noReply, timed_report_interval_s: interval} = quality;
  const counts = `Sent ${sent} · Acknowledged ${acknowledged} · Resent ${resent} · No reply ${noReply}`;
  return interval == null ? counts : `${counts} · Timed reports every ${seconds(interval)} s`;
}

function resultText(name, result, viaEsc, requested = name) {
  if (!result) return null;
  const {state: stage, elapsed_s: elapsed, ack_elapsed_s: ack, value, remaining_s: remaining} = result;
  const acknowledged = ack == null ? '' : `AGV acknowledged (${seconds(ack)} s)`;
  const confirmation = stage === 'confirmed by status';
  const sent = localTime(result.sent_at);
  const urgentSent = `${requested === 'pause' ? 'Pause' : 'Stop'} sent ${sent}${viaEsc ? ' (Esc)' : ''}`;
  let text;
  if (stage.startsWith('reply: ')) {
    const reply = stage.slice(7);
    const amount = Number.parseInt(reply, 10);
    return {text: `AGV replied: ${reply} (${seconds(elapsed)} s)${Number.isFinite(amount) ? ' · ' + speedText(amount).split(' · ')[1] : ''}`, tone: 'done'};
  }
  if (stage === 'waiting for reply') return {text: `Waiting for the AGV to reply… ${seconds(remaining)} s`, tone: 'waiting'};
  if (stage === 'status received') return {text: `Status received (${seconds(elapsed)} s)`, tone: 'done'};
  if (stage === 'acknowledged late') return {text: `${urgent.has(name) ? urgentSent + ' · ' : ''}AGV acknowledged late (${seconds(ack)} s)`, tone: 'done'};
  if (stage === 'no reply' && result.warning) return {text: 'No reply from the AGV. ' + result.warning, tone: 'failed'};
  if (stage === 'no reply') return {text: reads.has(name) ? `No reply in 1.5 s. Check the radio. ${portRetry}`
    : `${urgent.has(name) ? urgentSent + ' · ' : ''}No reply from the AGV after ${result.tries} tries. Check the radio, then try again.`, tone: 'failed'};
  if (stage === 'arrived') text = `Arrived at station ${value} (${seconds(elapsed)} s)`;
  else if (confirmation) {
    const reported = result.confirmed_stopped === true ? 'stopped' : (result.wire_command ?? name) === 'pause' ? 'paused' : 'command stop active';
    text = urgent.has(name) ? `${urgentSent} · AGV reports ${reported}` : 'Confirmed: AGV status shows the change';
  }
  else if (stage === 'not confirmed') text = 'Not confirmed after 5 s: check the AGV.';
  else if (stage === 'not checked') text = 'Not checked: no confirmation before another action. Check the AGV.';
  else if (stage === 'travelling') text = `Going to station ${value}…`;
  else {
    const sentText = urgent.has(name) ? urgentSent :
      name === 'call_station' ? `Going to station ${value}…` : `Sent at ${sent}`;
    const waiting = remaining > 0 ? `waiting for the AGV to confirm… ${Math.ceil(remaining)} s` : 'waiting for a status. Check the link.';
    const attempt = result.tries > 1 ? `Try ${result.tries}` : '';
    return {text: [sentText, attempt, acknowledged, waiting, result.warning].filter(Boolean).join(' · '), tone: 'waiting'};
  }
  if (urgent.has(name) && !confirmation) text = urgentSent + ' · ' + text;
  return {text: [text, acknowledged, result.warning].filter(Boolean).join(' · '),
    tone: confirmation || stage === 'arrived' ? 'done' : stage === 'travelling' ? 'waiting' : 'failed'};
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

function connectionKeyFor(snapshot) {
  return snapshot.connection_started_at ?? String(snapshot.connection_id);
}

function exportRecord(state, session, journal) {
  const connection = connectionKeyFor(state);
  const results = Object.fromEntries(Object.entries(state.results).filter(([, result]) =>
    journal.commands[`${connection}/${result.tx_id}`]));
  return {exported_at: isoNow(), started_at: session.started_at, connection: state.settings,
    transports: Object.entries(journal.transports).map(([connection, settings]) => ({connection, ...settings})), auto_poll: true, armed: state.armed,
    link_quality: journal.link_quality ?? {},
    simulator_silent: state.silent, results, settings: session.settings,
    checklist: Object.fromEntries(checklist.map(([title], index) => [index + 1,
      {title, result: 'Not tested', notes: '', ...session.checklist[index + 1]}])),
    commands: Object.values(journal.commands), events: journal.events, log_gap: journal.log_gap, log: journal.log};
}

function markdown(record, commands) {
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

export { connectionKey, sessionKey, journalKey, urgent, reads, linkLost, portRetry, badFrame,
  stationCallRequired, stationLimit, emptyJournal, isoNow, localTime, seconds, speedText, load, save, newSession,
  fieldLabels, modes, outcomesList, checklist, checklistCommands, foundFields, faultText,
  linkQualityText, resultText, eventText, connectionKeyFor, exportRecord, markdown };
