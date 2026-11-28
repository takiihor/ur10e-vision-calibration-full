import { computed, onMounted, onUnmounted, reactive, shallowRef, ref } from 'vue';
import { request, requestError, simulationEnabled } from './api';
import { guidance, learnedRange, refused, teachStations } from './agvFeedback';
import {
  badFrame, connectionKey, emptyJournal, eventText, faultText, isoNow, journalKey,
  load, newSession, reads, resultText, save, seconds, sessionKey, stationCallRequired, stationLimit, urgent,
  connectionKeyFor, pauseStationOnly, mergeJournal, savedRadioFields, rememberRadioOverrides, radioRouteError,
} from '../app/anodizing_web/web/agv-record.mjs';

const initialState = { connection_id: 0, sequence: -1, results: {}, log: [] };
const pendingWait = 'Wait for the current request.';
const connectionChanged = 'Connection changed. Check the AGV before trying again.';

export function useAgv(isAgv, isScene, alternateStop, pickRoute, operatorReadOnly) {
  const state = shallowRef(initialState);
  const capability = shallowRef(null);
  const report = shallowRef(null);
  const clock = ref(performance.now());
  let snapshotRequestedAt = clock.value;
  const stateError = ref('');
  const serviceError = ref('');
  const portError = ref('');
  const outcomes = reactive(new Map());
  const latest = ref('');
  const busy = ref(false);
  const haltRevision = ref(0);
  const armedHere = ref(false);
  let resetPending = false;
  let firstAgvRead = true;
  let fetching = false;
  let lastPoll = -Infinity;
  let statePoll = false;
  let lastStatePoll = -Infinity;
  let badFrameUntil = 0;
  let retired = false;

  const savedSettings = ref(load(connectionKey, null));
  const initialMode = !simulationEnabled() || savedSettings.value?.mode === 'serial' ? 'serial' : 'simulator';
  const form = reactive({
    mode: initialMode, port: '', ...savedRadioFields(savedSettings.value, initialMode),
    stations: 3, speed: 500, station: 1,
  });
  const session = ref(load(sessionKey, newSession()));
  const savedJournal = load(journalKey, null);
  const journal = ref(savedJournal?.started_at === session.value.started_at && savedJournal.journal
    ? savedJournal.journal : emptyJournal());
  const recordNote = ref('');
  const sound = ref(false);
  let audio;

  const commands = computed(() => new Map((capability.value?.commands ?? []).map(item => [item.name, item])));
  const ports = computed(() => {
    const found = capability.value?.ports ?? [];
    const linked = state.value.connected && state.value.settings?.port;
    return linked && !found.some(port => port.device === linked) ? [{ device: linked }, ...found] : found;
  });
  const age = computed(() => state.value.status_age_s == null ? null :
    state.value.status_age_s + Math.max(0, clock.value - snapshotRequestedAt) / 1000);
  const manualTravel = computed(() => state.value.executor_link &&
    Number.isFinite(state.value.executor_health?.manual_travel_until));
  const isolatedTrack = computed(() => manualTravel.value && state.value.executor_health.manual_travel_isolated_track === true &&
    (performance.timeOrigin + clock.value) / 1000 < state.value.executor_health.manual_travel_until);
  const supervisedRange = computed(() => manualTravel.value ||
    (state.value.executor_link && state.value.executor_health?.simulated === false));
  const stale = computed(() => Boolean(state.value.connected && (
    state.value.stale || (age.value ?? (state.value.connection_age_s ?? 0) + Math.max(0, clock.value - snapshotRequestedAt) / 1000) >=
      (supervisedRange.value || state.value.recipe_active || pickRoute?.value ? 3 : 5))));
  const freshReport = computed(() => state.value.connected && !stale.value ? state.value.status : null);
  const maximumStation = computed(() => stationLimit(freshReport.value));

  const blockText = (key, raw = state.value[key]) => guidance(state.value.feedback?.[key], raw);
  const executorDownReason = computed(() => blockText('error') ||
    'The IPC on the AGV is not answering. Check AGV power.');
  function staleReason() {
    const elapsed = age.value == null ? 'There is no current status reading.' :
      `The last status reading is ${seconds(age.value)} seconds old.`;
    return `${elapsed} Press Query status, or check the link if no reply arrives.`;
  }
  function faultReason(report) {
    if (report.fault_code !== 0) {
      return `${report.fault_code == null ? 'The fault reading is unavailable.' : faultText(report) + '.'} Check the AGV, clear the fault when safe, then press Query status.`;
    }
    if (report.obstacle_stop !== false) {
      const reason = report.obstacle_stop ? 'The AGV reports an obstacle stop.' : 'The obstacle-stop reading is unavailable.';
      return `${reason} Check the track, clear it when safe, then press Query status.`;
    }
    return '';
  }
  function manualFeedbackReason() {
    const report = freshReport.value;
    if (!report || age.value == null) return staleReason();
    const fault = faultReason(report);
    if (fault) return fault;
    const floor = state.value.executor_health?.manual_travel_min_battery;
    const battery = report.battery_percent;
    if (Number.isFinite(floor) && (!Number.isFinite(battery) || battery < floor || battery > 100)) {
      const reading = Number.isFinite(battery) ? `${battery}%` : 'unavailable';
      return `The reported battery is ${reading}. Manual travel requires at least ${floor}%. Charge if needed, then press Query status.`;
    }
    return '';
  }

  const connectionState = computed(() => serviceError.value || state.value.error || state.value.executor_reachable === false
    ? 'Link lost' : !state.value.connected ? 'Disconnected' : stale.value ? 'Link stale' : 'Connected');
  const connectionLabel = computed(() => state.value.connected && age.value != null
    ? `${connectionState.value} · ${Math.round(age.value)} s` : connectionState.value);
  const connectionDetail = computed(() => connectionState.value + (state.value.connected
    ? age.value == null ? ' · no status report yet' : ` · last status report ${seconds(age.value)} s ago`
    : ''));
  const alarms = computed(() => [
    [stale.value && 'Link stale. The AGV may still be moving. Use its e-stop if needed. Check the radio before moving again.', 'failed'],
    [serviceError.value || blockText('error'), 'failed'],
    [state.value.connected && state.value.status?.fault_code && faultText(state.value.status) +
      '. Check the machine and consult the manual before moving.', 'failed'],
    [state.value.connected && state.value.status?.obstacle_stop === true &&
      'Obstacle stop active. Check the track and clear it when safe.', 'serious'],
  ].filter(([text]) => text).map(([text, tone]) => ({ text, tone })));
  const operatorKeyReason = computed(() => operatorReadOnly?.value ? 'Enroll this browser to use operator controls.' : '');
  const connectionLocked = computed(() => operatorKeyReason.value || (busy.value ? pendingWait :
    state.value.connected ? 'Disconnect to change link settings.' : ''));
  const connectReason = computed(() => {
    if (state.value.bluetooth_block) return blockText('bluetooth_block');
    if (connectionLocked.value) return connectionLocked.value;
    if (state.value.executor_reachable === false) return executorDownReason.value;
    if (!capability.value) return serviceError.value || 'Waiting for the page service.';
    const routeError = radioRouteError(form.mode, form, capability.value.radio_profile);
    if (routeError) return routeError;
    if (form.mode === 'serial' || form.mode === 'wired') {
      if (!capability.value.serial_available) return 'Serial support is unavailable. Open AGV test in the serial app environment.';
      if (form.mode === 'wired' && !capability.value.wired_port) return 'Configure a wired port on the service.';
      if (form.mode === 'serial' && !form.port) return 'Select a serial port. Reseat the USB plug and refresh ports if needed.';
    } else if (!Number.isInteger(form.stations) || form.stations < 0 || form.stations > 255) {
      return 'Enter a simulator station count from 0 to 255.';
    }
    return '';
  });
  const armed = computed(() => state.value.armed && (state.value.executor_link || armedHere.value));
  const armReason = computed(() => {
    if (operatorKeyReason.value) return operatorKeyReason.value;
    if (state.value.bluetooth_block && !armed.value) return blockText('bluetooth_block');
    if (!armed.value && state.value.executor_reachable === false) return executorDownReason.value;
    if (!state.value.connected) return 'The AGV connection is closed. Choose Wired and press Connect.';
    if (busy.value || resetPending) return pendingWait;
    if (armed.value) return '';
    if (state.value.executor_link) {
      if (state.value.stop_unconfirmed) return 'Stop unconfirmed. Verify a safe halt first.';
      if (state.value.job_gateway && !state.value.owner) return 'Choose a job owner before arming.';
      if (state.value.programme_motion_block && !state.value.calibration_awaiting_arm) return blockText('programme_motion_block');
      if (state.value.operator_check) return blockText('operator_check', state.value.operator_check.reason);
      if (state.value.recovery_required) return blockText('motion_block') || 'A previous operation needs a physical check. Complete Operator check before arming.';
      if (supervisedRange.value) return manualFeedbackReason();
      return freshReport.value ? '' : staleReason();
    }
    return blockText('motion_block') || (stale.value ? staleReason() : '');
  });

  function setServiceError(error) {
    if (serviceError.value === error) return;
    serviceError.value = error;
    if (isAgv.value) journal.value.events.push({ field: 'page_error', after: Boolean(error),
      timestamp: isoNow(), text: error ? 'Page lost contact with its service' : 'Page service recovered' });
  }

  function ingest(next) {
    if (!next.settings) return;
    const connection = connectionKeyFor(next);
    const data = journal.value;
    const cursor = data.cursors[connection] ?? { log: 0, events: 0 };
    const entries = next.log.filter(entry => entry.id > cursor.log);
    if (entries.length && entries[0].id > cursor.log + 1) data.log_gap = true;
    data.log.push(...entries.map(entry => ({ ...entry, connection })));
    if (entries.some(entry => entry.direction === 'RX' && entry.decode.error && entry.hex)) badFrameUntil = performance.now() + 5000;
    const events = (next.events ?? []).filter(event => event.id > cursor.events);
    data.events.push(...events.map(event => ({ ...event, connection, text: eventText(event, next.status) })));
    data.cursors[connection] = { log: entries.at(-1)?.id ?? cursor.log, events: events.at(-1)?.id ?? cursor.events };
    data.transports[connection] = next.settings;
    if (next.link_quality) (data.link_quality ??= {})[connection] = next.link_quality;
    const newTx = new Set(entries.map(entry => entry.id));
    for (const [name, result] of [...(next.replaced ?? []), ...Object.entries(next.results)]) {
      const key = `${connection}/${result.tx_id}`;
      if (result.background || !result.tx_id || (!data.commands[key] && !newTx.has(result.tx_id))) continue;
      data.commands[key] = { ...data.commands[key], connection, name, timestamp: result.sent_at, ...result };
    }
  }

  function apply(next, requestedAt) {
    if (retired || next.connection_id < state.value.connection_id || next.sequence <= state.value.sequence) return false;
    const changed = next.connection_id !== state.value.connection_id;
    if (changed || !next.connected || !next.armed) armedHere.value = false;
    if (changed && next.connected) {
      Object.assign(form, { mode: next.settings.mode, port: next.settings.port ?? '',
        address: next.settings.address ?? '', channel: next.settings.channel ?? '' });
    } else if (!next.connected && state.value.connected) {
      Object.assign(form, savedRadioFields(savedSettings.value, form.mode));
    }
    const wasStale = stale.value;
    const fault = state.value.status?.fault_code;
    if (!changed && next.connected) {
      const elapsed = (requestedAt - snapshotRequestedAt) / 1000;
      if (next.status_time && next.status_time === state.value.status_time &&
          next.status_age_s != null && state.value.status_age_s != null) {
        next = { ...next, status_age_s: Math.max(next.status_age_s, state.value.status_age_s + elapsed) };
      }
      if (next.connection_age_s != null && state.value.connection_age_s != null) {
        next = { ...next, connection_age_s: Math.max(next.connection_age_s, state.value.connection_age_s + elapsed) };
      }
    }
    snapshotRequestedAt = requestedAt;
    clock.value = performance.now();
    state.value = next;
    if (isAgv.value) ingest(next);
    if ((!wasStale && stale.value) || (next.status?.fault_code && fault !== next.status.fault_code)) playAlarm();
    for (const [id, item] of outcomes) {
      if (changed && item.generation != null && item.generation !== next.connection_id && id !== 'connect') {
        outcomes.set(id, { direct: { text: connectionChanged, tone: 'failed' } });
      } else if (item.result) {
        const current = next.results[item.name];
        const matching = current?.tx_id === item.result.tx_id ? current :
          next.replaced?.find(([, old]) => old.tx_id === item.result.tx_id)?.[1];
        if (matching) item.result = matching;
      }
    }
    return true;
  }

  function changeTransport(mode) {
    Object.assign(form, { mode, ...savedRadioFields(savedSettings.value, mode) });
    pickPort();
  }

  function pickPort() {
    if (state.value.connected || form.mode !== 'serial') return;
    const listed = device => ports.value.some(port => port.device === device);
    if (listed(form.port)) return;
    const saved = savedSettings.value?.port?.device;
    const described = ports.value.filter(({ description }) => !/^\s*(?:n\/a)?\s*$/i.test(description ?? ''));
    form.port = listed(saved) ? saved : described.length === 1 ? described[0].device : '';
  }

  async function refreshPorts() {
    const requestedAt = performance.now();
    try {
      const next = await request('/api/agv?ports=1');
      if (retired) return;
      if (!capability.value || next.sequence > capability.value.sequence) {
        if (!capability.value && !next.connected && next.wired_port) changeTransport('wired');
        capability.value = next;
        portError.value = next.port_error ? 'Ports could not be listed. Reseat the USB plug, then refresh ports.' : '';
        apply(next, requestedAt);
        pickPort();
      }
      setServiceError('');
    } catch (error) {
      setServiceError(requestError(error, state.value.connected));
    }
  }

  function feedback(id) {
    const item = outcomes.get(id);
    if (id === 'stop' && state.value.executor_link && item?.started == null) {
      const report = state.value.executor_stop;
      if (!report && !state.value.stop_unconfirmed) return item?.direct ?? null;
      const receipt = report?.received ? 'Stop received' : 'Stop receipt unconfirmed';
      const halt = report?.halt_attempted ? 'Halt attempted' : 'Halt attempt unconfirmed';
      const outcome = report?.confirmed ? 'Stopped status confirmed' : 'Stop unconfirmed';
      return { text: [receipt, halt, outcome].join(' · '), tone: report?.confirmed ? 'done' : 'failed' };
    }
    if (!item) return null;
    if (item.result) return resultText(item.name, item.result, item.viaEsc, item.requested);
    if (item.started != null) {
      const elapsed = (clock.value - item.started) / 1000;
      return elapsed < 1 ? { text: 'Sending…', tone: 'running', pending: true } :
        { text: `Waiting for the AGV… ${Math.max(0, Math.ceil(item.timeout / 1000 - elapsed))} s`, tone: 'waiting', pending: true };
    }
    return item.direct;
  }

  async function post(body, id, viaEsc = false) {
    if (operatorReadOnly?.value && !(body.action === 'send' && body.command === 'stop')) return;
    const priority = urgent.has(body.command);
    if ((!priority && busy.value) || (priority && outcomes.get(id)?.started != null)) return;
    if (priority) haltRevision.value++;
    if (!priority) busy.value = true;
    const generation = state.value.connection_id;
    if (state.value.executor_link && body.command !== 'query_status') {
      body = { ...body, request_id: crypto.randomUUID(), review_cursor: state.value.review_cursor };
      if (body.armed === true || body.command === 'call_station' || ['operator_check', 'claim_owner'].includes(body.action)) {
        body = { executor_boot: state.value.executor_boot, executor_session: state.value.executor_session,
          stop_generation: state.value.stop_generation, executor_owner: state.value.owner ?? null, ...body };
      }
    }
    const timeout = priority ? 4000 : 8000;
    const pending = reactive({ name: body.command, generation, started: performance.now(), timeout, viaEsc });
    latest.value = id;
    outcomes.set(id, pending);
    try {
      const next = await request('/api/agv', { connection_id: generation, ...body }, timeout);
      const applied = apply(next, pending.started);
      const current = state.value;
      if (retired || outcomes.get(id) !== pending) return;
      if (next.connection_id !== current.connection_id || (body.action !== 'connect' && generation !== current.connection_id)) {
        outcomes.set(id, { direct: { text: connectionChanged, tone: 'failed' } });
        return;
      }
      setServiceError('');
      if ((next.executor_link && ['stop', 'call_station'].includes(body.command)) ||
          ['acknowledge_station', 'operator_check'].includes(body.action)) {
        outcomes.delete(id);
      } else if (body.command) {
        const name = next.urgent_command ?? body.command;
        const sent = next.results[name];
        const observed = current.results[name];
        const result = sent?.tx_id === observed?.tx_id ? observed :
          current.replaced?.find(([, old]) => old.tx_id === sent?.tx_id)?.[1] ?? sent;
        if (!result) throw new Error('Missing command outcome');
        outcomes.set(id, { name, requested: body.command, generation, result, viaEsc });
        const entry = journal.value.commands[`${connectionKeyFor(next)}/${result.tx_id}`];
        if (priority && entry) {
          entry.requested_commands = [...new Set([...(entry.requested_commands ?? []), body.command])];
          persistJournal();
        }
      } else {
        if (body.action === 'connect' && current.connected) {
          const { mode, port } = current.settings;
          savedSettings.value = { mode, port: port ? { device: port,
            description: ports.value.find(item => item.device === port)?.description ?? '' } : null,
          ...rememberRadioOverrides(body, current.settings) };
          save(connectionKey, savedSettings.value);
        }
        if (body.action === 'arm') armedHere.value = Boolean(body.armed && current.armed && next.armed);
        const text = {
          connect: current.connected ? 'Connection opened.' :
            'The link closed. Inspect the connection before trying again.',
          disconnect: current.connected ? 'The link changed. Inspect the connection.' : 'Disconnected.',
          arm: armed.value ? (next.executor_link && next.motion_block ? 'Armed · commissioning' : 'Armed. Movement controls are enabled.') : 'Disarmed.',
          silence: current.silent ? 'Silent link simulation on.' : 'Silent link simulation off.',
          claim_owner: 'Job ownership updated.',
        };
        outcomes.set(id, { direct: { text: text[body.action], tone: 'done' } });
      }
      if (!applied) lastPoll = -Infinity;
    } catch (error) {
      const action = body.command === 'call_station' ? `Call to station ${body.value}` : body.command ?
        (commands.value.get(body.command)?.label ?? body.command) : ({
          arm: body.armed ? 'Arm' : 'Disarm', connect: 'Connect', disconnect: 'Disconnect',
          silence: 'Change link simulation', claim_owner: 'Change job owner',
          acknowledge_station: 'Acknowledge station result', operator_check: 'Confirm Operator check',
        })[body.action] ?? 'AGV action';
      const text = refused(action, error, requestError(error, state.value.connected));
      if (!error.status || error.status === 403) setServiceError(text);
      if (outcomes.get(id) === pending) outcomes.set(id, { direct: { text, tone: 'failed' } });
      journal.value.log.push({ timestamp: isoNow(), direction: 'Page', hex: '', decode: { error: error.message } });
      lastPoll = -Infinity;
    } finally {
      if (!priority) busy.value = false;
    }
  }

  function commandReason(name, value) {
    if (name !== 'stop' && operatorKeyReason.value) return operatorKeyReason.value;
    if (state.value.executor_link) {
      if (name === 'stop') return '';
      if (state.value.executor_reachable === false) return executorDownReason.value;
      if (!['query_status', 'call_station'].includes(name)) return 'This control is unavailable on the executor link.';
      if (name === 'call_station') {
        if (state.value.job_gateway && state.value.armed_for !== 'agv-page') {
          const owner = state.value.armed_for ? `Armed for ${state.value.armed_for}.` : 'The AGV is disarmed.';
          return `${owner} Select the AGV page as job owner and Arm when the AGV is safely stopped.`;
        }
        if (manualTravel.value && Date.now() / 1000 >= state.value.executor_health.manual_travel_until) {
          const when = new Date(state.value.executor_health.manual_travel_until * 1000).toLocaleTimeString();
          return `The manual travel window ended at ${when} (local time). Ask the supervisor to review it.`;
        }
        if (state.value.programme_motion_block) return blockText('programme_motion_block');
        if (state.value.stop_unconfirmed) return 'Stop unconfirmed. Verify a safe halt first.';
        const visit = state.value.manual_visit;
        if (visit && (visit.station !== value || visit.result?.outcome)) {
          return 'Review the previous station visit before starting another.';
        }
      }
    }
    if (!state.value.connected) return 'The AGV connection is closed. Choose a transport and press Connect.';
    if (name === 'pause' && freshReport.value?.mode === 0) return pauseStationOnly;
    if (urgent.has(name)) return '';
    if (busy.value) return pendingWait;
    if (state.value.recipe_active) return 'The recipe is checking AGV status. Follow Current operation on Pick/place.';
    if (reads.has(name)) return '';
    if (state.value.motion_block) return blockText('motion_block');
    if (stale.value || !freshReport.value) return staleReason();
    if (!armed.value) return 'The AGV is disarmed. Verify it is safely stopped before arming.';
    const { mode, fault_code: fault, obstacle_stop: obstacle } = freshReport.value;
    if (![0, 2].includes(mode)) return 'Select standard or station-call mode in the XBOT app first.';
    if (fault || obstacle) return faultReason(freshReport.value);
    if (name === 'resume' && mode !== 2) return 'Resume requires station-call mode.';
    if (name === 'call_station') {
      if (mode !== 2) return stationCallRequired;
      const manualReason = manualTravel.value && manualFeedbackReason();
      if (manualReason) return manualReason;
      const maximum = maximumStation.value;
      if (!maximum || !Number.isInteger(value) || value < 1 || value > maximum) {
        const range = learnedRange(freshReport.value?.max_stations);
        return range ? `${range} ${teachStations}` : 'The learned range is unknown. Press Query status.';
      }
    }
    if (name === 'set_speed' && (!Number.isInteger(value) || value < 500 || value > 4000)) {
      return 'Enter a whole number from 500 to 4000 m/h.';
    }
    return '';
  }

  function send(name, id = name, value = null, viaEsc = false, attestation = null) {
    const why = commandReason(name, value);
    if (!why) {
      if (name === 'call_station' && manualTravel.value && !attestation) return;
      const body = { action: 'send', command: name, value };
      if (attestation) body.attestation = attestation;
      if (commands.value.get(name)?.motion) body.stop_generation = state.value.stop_generation;
      return post(body, id, viaEsc);
    }
    if (viaEsc) {
      latest.value = id;
      outcomes.set(id, { direct: { text: why, tone: 'waiting' } });
    }
  }

  function escape(event) {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    if (!event.repeat && !alternateStop?.()) void send('stop', 'stop', null, true);
  }

  function playAlarm() {
    if (!sound.value || !audio || audio.state !== 'running') return;
    const oscillator = audio.createOscillator();
    const gain = audio.createGain();
    gain.gain.value = .08;
    oscillator.frequency.value = 620;
    oscillator.connect(gain).connect(audio.destination);
    oscillator.start();
    oscillator.stop(audio.currentTime + .18);
  }

  function setSound(checked) {
    sound.value = checked;
    if (!checked || !window.AudioContext) return;
    audio ??= new AudioContext();
    void audio.resume().catch(() => {});
  }

  async function refresh() {
    if (fetching || clock.value - lastPoll < 500) return;
    fetching = true;
    lastPoll = clock.value;
    const requestedAt = performance.now();
    try {
      const next = await request('/api/agv?cached=1', null, 3000);
      apply(next, requestedAt);
      setServiceError('');
      if (!retired && isAgv.value && firstAgvRead) {
        firstAgvRead = false;
        if (!state.value.executor_link && state.value.connected && state.value.armed && !armedHere.value) {
          resetPending = true;
          try {
            await post({ action: 'arm', armed: false }, 'armed');
          } finally {
            resetPending = false;
          }
        }
      }
    } catch (error) {
      setServiceError(requestError(error, state.value.connected));
    } finally {
      fetching = false;
    }
  }

  async function refreshState() {
    if (statePoll || clock.value - lastStatePoll < (isScene?.value ? 190 : 990)) return;
    statePoll = true;
    lastStatePoll = clock.value;
    const requestedAt = performance.now();
    try {
      const response = await request('/api/state', null, 3000);
      const previous = report.value;
      if (!retired && response.agv && (!previous || response.agv.connection_id >= previous.connection_id &&
          response.agv.sequence >= previous.sequence)) {
        const variables = new Map(response.variables.map(item => {
          const prior = previous?.variables.get(item.id);
          // A faster reply must not make an already expired sample appear fresh again.
          if (item.age_ms != null && prior?.age_ms != null && item.read_at != null && item.read_at === prior.read_at &&
              item.source === prior.source && item.source_at === prior.source_at && item.row_id === prior.row_id &&
              item.model_sha256 === prior.model_sha256 &&
              (!item.id.startsWith('agv.') || response.agv.connection_id === previous.connection_id)) {
            item = { ...item, age_ms: Math.max(item.age_ms, prior.age_ms + requestedAt - previous.requestedAt) };
          }
          return [item.id, item];
        }));
        clock.value = performance.now();
        report.value = { ...response.agv, line: response.line, requestedAt, variables };
      }
      stateError.value = '';
    } catch (error) {
      stateError.value = requestError(error, state.value.connected);
    } finally {
      statePoll = false;
    }
  }

  function persistSession() {
    if (!save(sessionKey, session.value)) recordNote.value = 'Browser storage is unavailable. Download the record before leaving.';
  }

  function persistJournal() {
    if (!isAgv.value && !Object.keys(journal.value.cursors).length) return;
    const stored = load(journalKey, null);
    const storedSession = load(sessionKey, null);
    if (storedSession && storedSession.started_at !== session.value.started_at) return;
    if (stored?.started_at === session.value.started_at) journal.value = mergeJournal(stored.journal, journal.value);
    if (!save(journalKey, { started_at: session.value.started_at, journal: journal.value })) {
      recordNote.value = 'The full log could not be saved. Download JSON before leaving.';
    }
  }

  function storageChanged(event) {
    if (event.key === sessionKey && event.newValue) {
      const previous = session.value.started_at;
      session.value = load(sessionKey, session.value);
      if (session.value.started_at !== previous) journal.value = emptyJournal();
    }
    if (event.key === journalKey && event.newValue) {
      const stored = load(journalKey, null);
      if (stored?.started_at === session.value.started_at) journal.value = mergeJournal(stored.journal, journal.value);
    }
  }

  function newRecord() {
    session.value = newSession();
    journal.value = emptyJournal();
    if (state.value.settings) {
      const connection = connectionKeyFor(state.value);
      journal.value.cursors[connection] = { log: state.value.log.at(-1)?.id ?? 0, events: state.value.events?.at(-1)?.id ?? 0 };
      if (state.value.connected) {
        journal.value.transports[connection] = state.value.settings;
        journal.value.link_quality[connection] = state.value.link_quality;
      }
    }
    sound.value = false;
    recordNote.value = 'New test session started.';
    persistSession();
    persistJournal();
  }

  let timer;
  onMounted(() => {
    window.addEventListener('keydown', escape, true);
    window.addEventListener('pagehide', persistJournal);
    window.addEventListener('storage', storageChanged);
    void refresh();
    void refreshState();
    if (isAgv.value) void refreshPorts();
    timer = setInterval(() => {
      clock.value = performance.now();
      void refresh();
      void refreshState();
    }, 200);
  });
  onUnmounted(() => {
    retired = true;
    clearInterval(timer);
    window.removeEventListener('keydown', escape, true);
    window.removeEventListener('pagehide', persistJournal);
    window.removeEventListener('storage', storageChanged);
    void audio?.close().catch(() => {});
  });

  function enterAgv() {
    if (!capability.value) void refreshPorts();
    lastPoll = -Infinity;
    void refresh();
  }

  function variableInfo(id) {
    if (!report.value || (id.startsWith('agv.') && report.value.connection_id !== state.value.connection_id)) return null;
    const item = report.value.variables.get(id);
    if (!item || item.age_ms == null) return item;
    const age_ms = item.age_ms + Math.max(0, clock.value - report.value.requestedAt);
    if (item.freshness.kind === 'max_age_ms' && age_ms >= item.freshness.max_age_ms) {
      return { ...item, age_ms, quality: 'stale', value: null };
    }
    return { ...item, age_ms };
  }

  return reactive({
    state, capability, form, commands, ports, age, stale, freshReport, connectionState, connectionLabel, connectionDetail, alarms,
    connectionLocked, connectReason, armed, armReason, executorDownReason, busy, haltRevision, stateError, portError, operatorReadOnly, operatorKeyReason,
    manualTravel, isolatedTrack, supervisedRange, maximumStation, variableInfo,
    line: computed(() => report.value?.line),
    variables: computed(() => [...(report.value?.variables.values() ?? [])]),
    variable: name => variableInfo('agv.' + name)?.value ?? null,
    session, journal, recordNote, sound, serviceError,
    currentFeedback: computed(() => {
      const outcome = feedback(latest.value);
      return latest.value === 'connect' && state.value.connected && outcome?.tone === 'done' ? null : outcome;
    }),
    frameError: computed(() => clock.value < badFrameUntil ? badFrame : ''),
    refreshPorts, changeTransport, blockText, commandReason, send, post, feedback, setSound,
    enterAgv, persistSession, persistJournal, newRecord,
    showOutcome(id, outcome, select) {
      if (select) latest.value = id;
      outcomes.set(id, { direct: outcome });
    },
    retireOutcome(id) {
      if (['running', 'waiting'].includes(outcomes.get(id)?.direct?.tone)) outcomes.delete(id);
    },
  });
}
