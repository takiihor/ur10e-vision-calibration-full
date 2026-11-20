import { computed, onMounted, onUnmounted, reactive, shallowRef, ref } from 'vue';
import { request, requestError } from './api';
import {
  badFrame, connectionKey, emptyJournal, eventText, faultText, isoNow, journalKey,
  load, newSession, reads, resultText, save, seconds, sessionKey, stationCallRequired, stationLimit, urgent,
  connectionKeyFor, pauseStationOnly, mergeJournal,
} from '../app/anodizing_web/web/agv-record.mjs';

const initialState = { connection_id: 0, sequence: -1, results: {}, log: [] };
const pendingWait = 'Wait for the current request.';
const connectionChanged = 'Connection changed. Check the AGV before trying again.';

export function useAgv(isAgv, isScene, alternateStop) {
  const state = shallowRef(initialState);
  const capability = shallowRef(null);
  const report = shallowRef(null);
  const clock = ref(performance.now());
  let receivedAt = clock.value;
  const stateError = ref('');
  const serviceError = ref('');
  const portError = ref('');
  const outcomes = reactive(new Map());
  const latest = ref('');
  const busy = ref(false);
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
  const form = reactive({
    mode: savedSettings.value?.mode === 'serial' ? 'serial' : 'simulator',
    port: '', address: savedSettings.value?.address ?? '00 01',
    channel: savedSettings.value?.channel ?? '04', stations: 3, speed: 500, station: 1,
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
    state.value.status_age_s + Math.max(0, clock.value - receivedAt) / 1000);
  const stale = computed(() => Boolean(state.value.connected && (
    state.value.stale || (age.value ?? (state.value.connection_age_s ?? 0) + Math.max(0, clock.value - receivedAt) / 1000) >=
      (state.value.recipe_active ? 3 : 5))));
  const freshReport = computed(() => state.value.connected && !stale.value ? state.value.status : null);
  const connectionLabel = computed(() => {
    const elapsed = age.value == null ? 'no status report yet' : `last status report ${seconds(age.value)} s ago`;
    if (serviceError.value || state.value.error) return 'Error · ' + elapsed;
    if (!state.value.connected) return 'Disconnected';
    return (stale.value ? 'Link stale · ' : 'Connected · ') + elapsed;
  });
  const alarms = computed(() => [
    [stale.value && 'Link stale. The AGV may still be moving. Use its e-stop if needed. Check the radio before moving again.', 'failed'],
    [serviceError.value || state.value.error, 'failed'],
    [state.value.connected && state.value.status?.fault_code && faultText(state.value.status) +
      '. Check the machine and consult the manual before moving.', 'failed'],
    [state.value.connected && state.value.status?.obstacle_stop === true &&
      'Obstacle stop active. Check the track and clear it when safe.', 'serious'],
  ].filter(([text]) => text).map(([text, tone]) => ({ text, tone })));
  const connectionLocked = computed(() => busy.value ? pendingWait :
    state.value.connected ? 'Disconnect to change link settings.' : '');
  const connectReason = computed(() => {
    if (connectionLocked.value) return connectionLocked.value;
    if (!capability.value) return serviceError.value || 'Waiting for the page service.';
    if (!/^[a-f\d]{2}\s*[a-f\d]{2}$/i.test(String(form.address).trim()) || !/^[a-f\d]{2}$/i.test(String(form.channel).trim())) {
      return 'Enter two address bytes and one channel byte in Link settings.';
    }
    if (form.mode === 'serial') {
      if (!capability.value.serial_available) return 'Serial support is unavailable. Open AGV test in the serial app environment.';
      if (!form.port) return 'Select a serial port. Reseat the USB plug and refresh ports if needed.';
    } else if (!Number.isInteger(form.stations) || form.stations < 0 || form.stations > 255) {
      return 'Enter a simulator station count from 0 to 255.';
    }
    return '';
  });
  const armed = computed(() => state.value.armed && armedHere.value);
  const armReason = computed(() => {
    if (!state.value.connected) return 'Connect first.';
    if (busy.value || resetPending) return pendingWait;
    if (armed.value) return '';
    return state.value.motion_block || (stale.value ? 'Wait for a fresh status report.' : '');
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

  function apply(next) {
    if (retired || next.connection_id < state.value.connection_id || next.sequence <= state.value.sequence) return false;
    const changed = next.connection_id !== state.value.connection_id;
    if (changed || !next.connected || !next.armed) armedHere.value = false;
    if (changed && next.connected) {
      Object.assign(form, { mode: next.settings.mode, port: next.settings.port ?? '',
        address: next.settings.address, channel: next.settings.channel });
    }
    const wasStale = stale.value;
    const fault = state.value.status?.fault_code;
    receivedAt = performance.now();
    clock.value = receivedAt;
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

  function pickPort() {
    if (state.value.connected || form.mode !== 'serial') return;
    const listed = device => ports.value.some(port => port.device === device);
    if (listed(form.port)) return;
    const saved = savedSettings.value?.port?.device;
    const described = ports.value.filter(({ description }) => !/^\s*(?:n\/a)?\s*$/i.test(description ?? ''));
    form.port = listed(saved) ? saved : described.length === 1 ? described[0].device : '';
  }

  async function refreshPorts() {
    try {
      const next = await request('/api/agv?ports=1');
      if (retired) return;
      if (!capability.value || next.sequence > capability.value.sequence) {
        capability.value = next;
        portError.value = next.port_error ? 'Ports could not be listed. Reseat the USB plug, then refresh ports.' : '';
        apply(next);
        pickPort();
      }
      setServiceError('');
    } catch (error) {
      setServiceError(requestError(error, state.value.connected));
    }
  }

  function feedback(id) {
    const item = outcomes.get(id);
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
    const priority = urgent.has(body.command);
    if ((!priority && busy.value) || (priority && outcomes.get(id)?.started != null)) return;
    if (!priority) busy.value = true;
    const generation = state.value.connection_id;
    const timeout = priority ? 4000 : 8000;
    const pending = reactive({ name: body.command, generation, started: performance.now(), timeout, viaEsc });
    latest.value = id;
    outcomes.set(id, pending);
    try {
      const next = await request('/api/agv', { connection_id: generation, ...body }, timeout);
      const applied = apply(next);
      const current = state.value;
      if (retired || outcomes.get(id) !== pending) return;
      if (next.connection_id !== current.connection_id || (body.action !== 'connect' && generation !== current.connection_id)) {
        outcomes.set(id, { direct: { text: connectionChanged, tone: 'failed' } });
        return;
      }
      setServiceError('');
      if (body.command) {
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
          const { mode, port, address, channel } = current.settings;
          savedSettings.value = { mode, port: port ? { device: port,
            description: ports.value.find(item => item.device === port)?.description ?? '' } : null, address, channel };
          save(connectionKey, savedSettings.value);
        }
        if (body.action === 'arm') armedHere.value = Boolean(body.armed && current.armed && next.armed);
        const text = {
          connect: current.connected ? `Connected · ${current.settings?.mode === 'simulator' ? 'Simulator' : 'Serial AGV'}` :
            'The link closed. Inspect the connection before trying again.',
          disconnect: current.connected ? 'The link changed. Inspect the connection.' : 'Disconnected.',
          arm: armed.value ? 'Armed. Movement controls are enabled.' : 'Disarmed.',
          silence: current.silent ? 'Silent link simulation on.' : 'Silent link simulation off.',
        };
        outcomes.set(id, { direct: { text: text[body.action], tone: 'done' } });
      }
      if (!applied) lastPoll = -Infinity;
    } catch (error) {
      const text = requestError(error, state.value.connected);
      if (!error.status || error.status === 403) setServiceError(text);
      if (outcomes.get(id) === pending) outcomes.set(id, { direct: { text, tone: 'failed' } });
      journal.value.log.push({ timestamp: isoNow(), direction: 'Page', hex: '', decode: { error: error.message } });
      lastPoll = -Infinity;
    } finally {
      if (!priority) busy.value = false;
    }
  }

  function commandReason(name, value) {
    if (!state.value.connected) return 'Connect first.';
    if (name === 'pause' && freshReport.value?.mode === 0) return pauseStationOnly;
    if (urgent.has(name)) return '';
    if (busy.value) return pendingWait;
    if (state.value.recipe_active) return 'The recipe is checking AGV status. Follow Current operation on Pick/place.';
    if (reads.has(name)) return '';
    if (state.value.motion_block) return state.value.motion_block;
    if (stale.value || !freshReport.value) return 'Wait for a fresh status report. Query status or check the radio.';
    if (!armed.value) return 'Tick Armed to enable movement.';
    const { mode, fault_code: fault, obstacle_stop: obstacle } = freshReport.value;
    if (![0, 2].includes(mode)) return 'Select standard or station-call mode in the XBOT app first.';
    if (fault || obstacle) return 'Clear the reported AGV fault or obstacle before continuing.';
    if (name === 'resume' && mode !== 2) return 'Resume requires station-call mode.';
    if (name === 'call_station') {
      if (mode !== 2) return stationCallRequired;
      const maximum = stationLimit(freshReport.value);
      if (!maximum) return 'Teach a route in the XBOT app before calling a station.';
      if (!Number.isInteger(value) || value < 1 || value > maximum) return `Choose a learned station from 1 to ${maximum}.`;
    }
    if (name === 'set_speed' && (!Number.isInteger(value) || value < 500 || value > 4000)) {
      return 'Enter a whole number from 500 to 4000 m/h.';
    }
    return '';
  }

  function send(name, id = name, value = null, viaEsc = false) {
    const why = commandReason(name, value);
    if (!why) {
      const body = { action: 'send', command: name, value };
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
    try {
      const next = await request('/api/agv?cached=1', null, 3000);
      apply(next);
      setServiceError('');
      if (!retired && isAgv.value && firstAgvRead) {
        firstAgvRead = false;
        if (state.value.connected && state.value.armed && !armedHere.value) {
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
    state, capability, form, commands, ports, age, stale, freshReport, connectionLabel, alarms,
    connectionLocked, connectReason, armed, armReason, busy, stateError, portError,
    variableInfo,
    line: computed(() => report.value?.line),
    variables: computed(() => [...(report.value?.variables.values() ?? [])]),
    variable: name => variableInfo('agv.' + name)?.value ?? null,
    session, journal, recordNote, sound, serviceError,
    currentFeedback: computed(() => feedback(latest.value)),
    frameError: computed(() => clock.value < badFrameUntil ? badFrame : ''),
    refreshPorts, pickPort, commandReason, send, post, feedback, setSound,
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
