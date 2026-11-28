// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { h, ref } from 'vue';
import PrimeVue from 'primevue/config';
import ConfirmationService from 'primevue/confirmationservice';
import ToastService from 'primevue/toastservice';
import StopBar from './StopBar.vue';
import AgvPage from './AgvPage.vue';
import { useAgv } from './useAgv.js';
import { connectionKey, journalKey, newSession, sessionKey } from '../app/anodizing_web/web/agv-record.mjs';

const stamp = '2026-11-20T08:00:00.000Z';
const settings = { mode: 'simulator', address: '42 19', channel: '0D' };
const status = {
  mode: 2, max_stations: 3, stopped: true, actual_speed_m_h: 0,
  current_station: 1, target_station: 1, fault_code: 0, obstacle_stop: false,
};
let app;

async function open(respond = () => ({}), simulation = 'on') {
  let agv, sequence = 0;
  document.head.innerHTML = '<meta name="module-token" content="fixture">' +
    `<meta name="module-simulation" content="${simulation}">`;
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal('fetch', vi.fn(async (path, options) => {
    const body = options.body && JSON.parse(options.body);
    const response = path === '/api/state' ? { variables: [] } : {
      sequence: ++sequence, connected: true, connection_id: 2, stop_generation: 7,
      status, status_age_s: .1, settings, armed: false, log: [], results: {},
      commands: [
        { name: 'query_status', motion: false, label: 'Query status' },
        { name: 'set_speed', motion: true, label: 'Set speed' },
        { name: 'call_station', motion: true, label: 'Call station' },
      ],
      ...await respond(body),
    };
    return { ok: !response.error, status: response.error ? 400 : 200, json: async () => response };
  }));
  app = mount({
    setup() {
      agv = useAgv(ref(true));
      return () => h('div', [h(StopBar, { agv, agvPage: true, showOwnership: true }), h(AgvPage, { agv })]);
    },
  }, { attachTo: document.body, global: { plugins: [PrimeVue, ConfirmationService, ToastService] } });
  await flushPromises();
  return agv;
}

afterEach(() => {
  app?.unmount();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  localStorage.clear();
});

it('keeps the stop badge concise while retaining report and transport details', async () => {
  await open(() => ({ status_age_s: 2.1, executor_link: true }));
  const badge = app.get('#agv-link');
  expect(badge.text()).toBe('Connected · 2 s');
  expect(badge.attributes('aria-label')).toContain('last status report 2.1 s ago');
  expect(app.find('.pause-notice').exists()).toBe(false);
  expect(app.get('#agv-pause-why').text()).toBeTruthy();
});

it('keeps the AGV readouts and ownership visible as tabs switch and persists the selected tab', async () => {
  const hash = location.hash;
  await open(() => ({ job_gateway: true, owner: 'agv-page', calibration_available: true }));
  const overview = app.get('[data-ui="agv.bluetooth.wiredtab"]');
  const connection = app.get('[data-ui="agv.tabs.connection"]');
  expect(overview.attributes('aria-selected')).toBe('true');
  await connection.trigger('click');
  expect(connection.attributes('aria-selected')).toBe('true');
  expect(document.getElementById(connection.attributes('aria-controls')).contains(app.get('[data-ui="agv.link.panel"]').element)).toBe(true);
  for (const id of ['safety.panel', 'agv.status.panel', 'agv.owner.panel', 'agv.movement.arm']) {
    expect(app.get(`[data-ui="${id}"]`).isVisible()).toBe(true);
  }
  expect(location.hash).toBe(hash);
  app.unmount();
  await open(() => ({ job_gateway: true, owner: 'agv-page' }));
  expect(app.get('[data-ui="agv.tabs.connection"]').attributes('aria-selected')).toBe('true');
});

it('shows the switch and status as armed on an executor without job ownership', async () => {
  await open(() => ({ executor_link: true, armed: true }));
  const card = app.get('[data-ui="agv.owner.panel"]');
  expect(card.get('p').text()).toBe('Owner None · Armed');
  expect(card.get('input[role="switch"]').element.checked).toBe(true);
});

it.each([
  [25, 'charging', 'Charging', false],
  [15, 'in_use', 'In use', true],
  [5, 'steady', 'Steady', true],
])('shows battery trend and warning at %s%% in AGV Overview', async (battery, trend, label, low) => {
  await open(() => ({ status: { ...status, battery_percent: battery }, battery_trend: trend }));
  const card = app.get('[data-ui-key="battery"]').element.parentElement;
  expect(card.textContent).toContain(battery + '%');
  expect(card.textContent).toContain(label);
  expect(card.textContent.includes('Battery low. Please charge.')).toBe(low);
});

it('hides the trend and low warning when the AGV report is stale', async () => {
  await open(() => ({ status: { ...status, battery_percent: 5 }, battery_trend: 'charging', stale: true }));
  const card = app.get('[data-ui-key="battery"]').element.parentElement;
  expect(card.textContent).toContain('Unknown');
  expect(card.textContent).not.toContain('Charging');
  expect(card.textContent).not.toContain('Battery low.');
});

it('marks tabs with active warnings and leaves the recovery notice visible on Overview', async () => {
  await open(() => ({
    operator_check: { reason: 'Review the last move.', technician: true },
    bluetooth_block: 'Bluetooth maintenance is active.',
  }));
  expect(app.get('[data-ui="agv.tabs.stations"]').text()).toContain('Warning');
  expect(app.get('[data-ui="agv.bluetooth.tab"]').text()).toContain('Warning');
  expect(app.get('[data-ui="agv.check.panel"]').isVisible()).toBe(true);
});

it('shows running motion as a labelled normal state', async () => {
  await open(() => ({ status: { ...status, stopped: false, actual_speed_m_h: 500 } }));
  const motion = app.get('#agv-motion');
  expect(motion.text()).toBe('Running');
  expect(motion.attributes('data-state')).toBe('running');
});

it('shows the fault code and private description without a supplier page reference', async () => {
  await open(() => ({ status: { ...status, fault_code: 61, fault_description: 'Example guide fault' } }));
  expect(app.text()).toContain('Fault 61 · Example guide fault');
  expect(app.text()).not.toContain('(manual p.');
});

it.each(['on', 'off'])('offers the configured transports with simulation %s', async simulation => {
  localStorage.setItem(connectionKey, JSON.stringify(settings));
  const agv = await open(() => ({ connected: false, status: null, settings: null }), simulation);
  const transport = app.get('[data-ui="agv.link.transport"]');
  await transport.trigger('click');
  const options = [...document.querySelectorAll('[role="option"]')].map(option => option.textContent);
  expect(options).toEqual(simulation === 'on' ? ['Simulator', 'Serial port'] : ['Serial port']);
  expect(agv.form.mode).toBe(simulation === 'on' ? 'simulator' : 'serial');
  expect(fetch.mock.calls.every(([, request]) => request.method === undefined)).toBe(true);
});

it('uses blank profile fields on live serial and remembers only an explicit override', async () => {
  localStorage.setItem(connectionKey, JSON.stringify(settings));
  const port = { device: '/dev/example-radio', description: 'Example adapter' };
  const agv = await open(body => ({
    connected: body?.action === 'connect', connection_id: body?.action === 'connect' ? 3 : 2, status: null,
    settings: { mode: 'serial', port: port.device, address: '26 35', channel: '1B' },
    serial_available: true, ports: [port], radio_profile: { address: true, channel: true },
  }), 'off');
  expect(agv.form.address).toBe('');
  expect(agv.form.channel).toBe('');
  expect(app.get('#agv-address').attributes('placeholder')).toBe('From site profile');
  await app.get('#agv-channel').setValue('1b');
  await app.get('[data-ui="agv.link.connect"]').trigger('click');
  await flushPromises();
  expect(app.get('#agv-connect-outcome').text()).toContain('Connection opened.');
  expect(app.get('#agv-outcome').text()).not.toMatch(/Connection opened|Connected · Serial AGV/);
  const sent = fetch.mock.calls.map(([, options]) => options.body && JSON.parse(options.body)).find(Boolean);
  expect(sent).toMatchObject({ action: 'connect', mode: 'serial', channel: '1b' });
  expect(sent).not.toHaveProperty('address');
  expect(JSON.parse(localStorage.getItem(connectionKey))).toEqual({ mode: 'serial', port, channel: '1B' });
});

it('defaults to the configured status-only wired link while simulation is hidden', async () => {
  const agv = await open(() => ({
    connected: false, status: null, settings: null, wired_port: '/dev/example',
    serial_available: true, motion_block: 'AGV link is status-only.',
  }), 'off');
  await app.get('[data-ui="agv.link.transport"]').trigger('click');
  const options = [...document.querySelectorAll('[role="option"]')].map(option => option.textContent);
  expect(options).toContain('Wired RS-485');
  expect(options).not.toContain('Simulator');
  expect(agv.form.mode).toBe('wired');
  expect(agv.connectReason).toBe('');
  expect(fetch.mock.calls.every(([, options]) => options.method === undefined)).toBe(true);
});

it('renders stable selectors on the actual controls and gives every station its own key', async () => {
  await open();
  expect(app.get('[data-ui="agv.link.transport"]').attributes('role')).toBe('combobox');
  expect(app.get('[data-ui="agv.movement.arm"]').element.tagName).toBe('INPUT');
  const stations = app.findAll('[data-ui="agv.call.station"]');
  expect(stations.length).toBeGreaterThan(1);
  expect(stations.map(button => button.attributes('data-ui-key')))
    .toEqual(stations.map((_, index) => String(index + 1)));
  expect(stations.every(button => button.element.tagName === 'BUTTON')).toBe(true);
  const selectors = app.findAll('[data-ui]').map(element =>
    JSON.stringify([element.attributes('data-ui'), element.attributes('data-ui-key')]));
  expect(new Set(selectors).size).toBe(selectors.length);
});

it('uses the latest stop generation for one movement and leaves an inquiry independent of it', async () => {
  const requests = [];
  let armed = false;
  await open(body => {
    if (body) requests.push(body);
    if (body?.action === 'arm') armed = body.armed;
    return { armed, stop_generation: armed ? 9 : 7 };
  });
  app.get('#agv-armed').element.click();
  await flushPromises();
  await app.get('#agv-speed-500').trigger('click');
  await flushPromises();
  await app.get('#agv-query_status').trigger('click');
  await flushPromises();
  expect(requests[1]).toMatchObject({ command: 'set_speed', value: 500, stop_generation: 9 });
  expect(requests[2]).toMatchObject({ command: 'query_status' });
  expect(requests[2]).not.toHaveProperty('stop_generation');
});

it('labels the requested Esc Stop, follows its mapped Pause result, and records it for a Stop checklist step', async () => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(stamp));
  const requests = [];
  const reply = { tx_id: 4, sent_at: stamp, state: 'confirmed by status', wire_command: 'pause' };
  const agv = await open(body => {
    if (body) requests.push(body);
    if (body?.command === 'stop') {
      return { urgent_command: 'pause', results: { pause: reply },
        log: [{ id: 4, timestamp: stamp, direction: 'TX', hex: '01 02', decode: { command: 'pause', try: 1 } }] };
    }
    return {};
  });
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await flushPromises();
  expect(requests).toEqual([expect.objectContaining({ command: 'stop', connection_id: 2 })]);
  expect(agv.feedback('stop').text).toMatch(/Stop sent .*\(Esc\).*AGV reports paused/);
  expect(agv.feedback('stop').text).not.toContain('stopped');

  await app.get('#agv-test-4 button').trigger('click');
  await app.get('#agv-result-4').trigger('click');
  [...document.querySelectorAll('[role="option"]')].find(option => option.textContent === 'Pass')
    .dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  await flushPromises();
  expect(agv.session.checklist[4].evidence.commands).toEqual([
    expect.objectContaining({ name: 'pause', wire_command: 'pause', requested_commands: ['stop'] }),
  ]);
});

it('keeps Armed off until the service confirms the change', async () => {
  let refuse;
  const agv = await open(body => body?.action === 'arm' ? new Promise(resolve => { refuse = resolve; }) : {});
  const armed = app.get('#agv-armed').element;
  armed.click();
  await flushPromises();
  expect(agv.feedback('armed').text).toBe('Sending…');
  expect(armed.checked).toBe(false);
  expect(app.get('#agv-speed-500').element.disabled).toBe(true);

  refuse({ error: 'A robot job is running. Wait for it to finish.' });
  await flushPromises();
  expect(agv.feedback('armed').text).toMatch(/Arm refused.*robot job is running/i);
  expect(armed.checked).toBe(false);
});

it('keeps unconfirmed motion distinct from a stopped report', async () => {
  await open(() => ({ status: { ...status, actual_speed_m_h: 250 } }));
  expect(app.get('#agv-motion').text()).toBe('Running');
});

it('shows the learned legs, callable range, current station and read time', async () => {
  await open(() => ({
    status: { ...status, max_stations: 16, current_station: 4 },
    status_time: '2026-11-26T14:45:06.123',
  }));
  const panel = app.get('[data-ui="agv.call.panel"]');
  expect(panel.text()).toContain('The AGV reports 16 learned legs: stations 1 to 17. It is at station 4 (read 14:45:06).');
  expect(panel.find('[data-ui="agv.call.range.warning"]').exists()).toBe(false);
});

it.each([0, 1])('shows teaching instructions when the AGV reports %i learned legs', async count => {
  await open(() => ({ status: { ...status, max_stations: count } }));
  const guidance = app.get('[data-ui="agv.call.range.warning"]').text();
  expect(guidance).toContain(count === 0 ? 'All station calls blocked.' : 'Calls above station 2 blocked.');
  expect(guidance).toContain('then press Query status.');
});

it('allows both stations when the AGV reports one learned leg', async () => {
  const requests = [];
  let armed = false;
  const agv = await open(body => {
    if (body) requests.push(body);
    if (body?.action === 'arm') armed = body.armed;
    return { armed, status: { ...status, max_stations: 1 } };
  });
  app.get('#agv-armed').element.click();
  await flushPromises();

  expect(agv.commandReason('call_station', 2)).toBe('');
  expect(agv.commandReason('call_station', 3)).toContain('The AGV reports 1 learned leg: stations 1 to 2.');
  expect(app.get('#agv-station-1').text()).toContain('Current');
  expect(app.get('label[for="agv-manual-station"]').text()).toContain('(1–2)');
  await app.get('#agv-station-2').trigger('click');
  await flushPromises();
  expect(requests[1]).toMatchObject({ command: 'call_station', value: 2, stop_generation: 7 });
});

it('shows sixteen station calls and a separate Parking call for sixteen learned legs', async () => {
  const requests = [];
  let armed = false, target = 16;
  await open(body => {
    if (body) requests.push(body);
    if (body?.action === 'arm') armed = body.armed;
    if (body?.command === 'call_station') target = body.value;
    return { armed, status: { ...status, max_stations: 16, target_station: target } };
  });
  const grid = app.get('[data-ui="agv.call.grid"]');
  const stations = grid.findAll('[data-ui="agv.call.station"]');
  expect(stations.map(button => button.attributes('data-ui-key')))
    .toEqual(Array.from({ length: 16 }, (_, index) => String(index + 1)));
  expect(stations[15].text()).toContain('Station 16');
  expect(grid.find('[data-ui="agv.call.parking"]').exists()).toBe(false);
  const parking = app.get('[data-ui="agv.call.parking"]');
  expect(parking.text()).toContain('Parking');
  expect(app.get('[data-ui-key="current"]').text()).toContain('Station 1');
  expect(app.get('[data-ui-key="target"]').text()).toContain('Station 16');

  app.get('#agv-armed').element.click();
  await flushPromises();
  expect([...stations, parking].every(button => !button.element.disabled)).toBe(true);
  await parking.trigger('click');
  await flushPromises();
  expect(requests[1]).toMatchObject({ command: 'call_station', value: 17, stop_generation: 7 });
  expect(app.get('[data-ui-key="target"]').text()).toContain('Parking');
});

it('uses local station labels and a different parking number without widening a shorter learned route', async () => {
  await open(() => ({
    station_profile: { parking_station: 2, station_labels: { 1: 'Load bay' } },
    status: { ...status, max_stations: 1, target_station: 2 },
  }));
  expect(app.get('[data-ui="agv.call.station"][data-ui-key="1"]').text()).toContain('Load bay');
  expect(app.get('[data-ui="agv.call.parking"]').attributes('id')).toBe('agv-station-2');
  expect(app.get('[data-ui-key="current"]').text()).toContain('Load bay');
  expect(app.get('[data-ui-key="target"]').text()).toContain('Parking');
  const unlearned = app.get('[data-ui="agv.call.station"][data-ui-key="17"]');
  expect(unlearned.element.disabled).toBe(true);
  const why = unlearned.attributes('aria-describedby').split(' ')[0];
  expect(app.get('#' + why).text()).toContain('The AGV reports 1 learned leg: stations 1 to 2.');
  expect(app.get('[data-ui="agv.call.range.warning"]').text()).toContain('The AGV reports 1 learned leg: stations 1 to 2.');
});

it('shows a spinner beside a pending connection control without disabling urgent sends', async () => {
  const agv = await open(body => body?.command === 'query_status' ? new Promise(() => {}) : {});
  await app.get('#agv-query_status').trigger('click');
  expect(app.get('#agv-query_status-outcome').text()).toBe('Sending…');
  expect(app.get('#agv-query_status-outcome [role="progressbar"]').exists()).toBe(true);
  expect(agv.commandReason('stop')).toBe('');
});

it('presents the checklist and record with expandable controls and asks before clearing observations', async () => {
  const agv = await open();
  await app.get('#agv-mark-text').setValue('Observed the simulator parked');
  await app.get('#agv-mark').trigger('submit');
  expect(app.get('#agv-event-feed').text()).toContain('Observed the simulator parked');
  expect(app.findAll('[data-ui="agv.record.checklist.toggle"]')).toHaveLength(9);
  await app.findAll('button').find(button => button.text() === 'Recent command results').trigger('click');
  expect(app.get('#agv-command-results table').text()).toContain('Outcome');
  await app.findAll('button').find(button => button.text() === 'Raw TX/RX log').trigger('click');
  expect(app.get('#agv-log table').text()).toContain('Frame');
  await app.get('#agv-new-session').trigger('click');
  const dialog = document.querySelector('[role="alertdialog"]');
  expect(dialog.textContent).toContain('This clears the current checklist, settings and record.');
  [...dialog.querySelectorAll('button')].find(button => button.textContent === 'Cancel').click();
  expect(agv.journal.events).toHaveLength(1);

  await app.get('#agv-new-session').trigger('click');
  [...document.querySelectorAll('[role="alertdialog"] button')]
    .find(button => button.textContent === 'Start new session').click();
  await flushPromises();
  expect(agv.journal.events).toHaveLength(0);
  expect(app.get('#agv-record-note').text()).toContain('New test session started.');
});

it('preserves the restart delay exactly when displaying and recording it', async () => {
  const session = newSession();
  session.settings['restart-seconds'] = '0.1234567890';
  localStorage.setItem(sessionKey, JSON.stringify(session));
  await open();
  await app.findAll('button').find(button => button.text() === 'Settings as found').trigger('click');

  const field = app.get('#agv-found-restart-seconds');
  expect(field.element.value).toBe('0.1234567890');
  expect(field.attributes('type')).toBe('number');
  expect(field.attributes('step')).toBe('any');
  await field.setValue('0.9876543210');
  expect(JSON.parse(localStorage.getItem(sessionKey)).settings['restart-seconds']).toBe('0.9876543210');
  await field.setValue('');
  expect(JSON.parse(localStorage.getItem(sessionKey)).settings['restart-seconds']).toBe('');
});

it('keeps a flag-only halt result in the waiting state', async () => {
  const { resultText } = await import('../app/anodizing_web/web/agv-record.mjs');
  const result = resultText('pause', { state: 'confirmed by status', confirmed_stopped: false,
    sent_at: '2026-11-20T00:00:00Z' }, false, 'stop');
  expect(result.tone).toBe('waiting');
  expect(result.text).not.toMatch(/reports stopped/i);
});


it('preserves another tab’s observation and command annotation when closing at the same cursor', async () => {
  const staleTab = await open();
  staleTab.persistSession();
  staleTab.persistJournal();
  const firstApp = app;
  const otherTab = await open();
  expect(otherTab.journal.cursors).toEqual(staleTab.journal.cursors);
  otherTab.journal.commands['2/4'] = { connection: '2', name: 'pause', tx_id: 4,
    timestamp: stamp, state: 'sent', sent_at: stamp, requested_commands: ['stop'] };
  await app.get('#agv-mark-text').setValue('Observed a parked vehicle');
  await app.get('#agv-mark').trigger('submit');
  const update = localStorage.getItem(journalKey);
  expect(JSON.parse(update).journal.events).toHaveLength(1);
  app.unmount();
  app = firstApp;
  window.dispatchEvent(new StorageEvent('storage', { key: journalKey, newValue: update }));
  window.dispatchEvent(new Event('pagehide'));
  app.unmount();
  const reloaded = await open();
  expect(reloaded.journal.events.map(event => event.text)).toContain('Observed a parked vehicle');
  expect(reloaded.journal.commands['2/4'].requested_commands).toEqual(['stop']);
});

it('shows a pending executor Stop on status polls before any button response', async () => {
  const agv = await open(() => ({
    executor_link: true, stop_unconfirmed: true, executor_stop: { confirmed: false },
  }));
  expect(app.text()).toContain('Stop unconfirmed');
  expect(agv.armReason).toContain('Stop unconfirmed');
});

it('shows a recovered station visit outcome and requires acknowledgement before another visit', async () => {
  let visit = { id: 'persisted', station: 2, result: { state: 'succeeded', outcome: 'succeeded' } };
  const requests = [];
  const agv = await open(body => {
    if (body) requests.push(body);
    if (body?.action === 'acknowledge_station') visit = null;
    return { executor_link: true, manual_visit: visit };
  });
  expect(app.text()).toContain('Station 2 visit succeeded');
  expect(agv.commandReason('call_station', 3)).toContain('previous station visit');
  await app.get('#agv-acknowledge-station').trigger('click');
  await flushPromises();
  expect(requests.at(-1)).toMatchObject({ action: 'acknowledge_station', job_id: 'persisted' });
});

it('shows the named technician fence and suppresses operator repair of damaged files', async () => {
  const message = 'IPC evidence is missing in .jobs/missing-visit.json. Please call a technician.';
  await open(() => ({ executor_link: true, recovery_ids: ['missing-visit'], recovery_required: true,
    evidence_faults: { '.jobs/missing-visit.json': message }, motion_block: message }));
  expect(app.find('[role="alert"]').exists()).toBe(true);
  expect(app.text()).toContain(message);
  expect(app.find('[data-ui="agv.marker.reconcile"]').exists()).toBe(false);
});

it.each(['station 2', 'between stations 2 and 3'])('requires a physical operator check to abort an unknown station visit at %s', async location => {
  const requests = [];
  await open(body => {
    if (body) requests.push(body);
    return { executor_link: true, executor_boot: 'boot-1', executor_session: 'session-1', review_cursor: 'cursor-7',
      manual_visit: { id: 'visit-1', station: 2, result: { outcome: 'unknown' } },
      operator_check: { reason: 'Reply lost', location, job_id: 'visit-1', basket: null } };
  });
  for (const fact of ['Operator check', 'Reply lost', location, 'visit-1']) expect(app.text()).toContain(fact);
  expect(app.find('[data-ui="agv.marker.reconcile"]').exists()).toBe(false);
  const button = app.get('[data-ui="agv.check.abort"]');
  expect(button.element.disabled).toBe(true);
  await app.get('[data-ui="agv.check.operator"]').setValue('Operator A');
  await app.get('[data-ui="agv.check.location"]').setValue(location);
  await app.get('[data-ui="agv.check.confirmed"]').setValue(true);
  await button.trigger('click');
  await flushPromises();
  expect(requests.at(-1)).toMatchObject({ action: 'operator_check', choice: 'abort', job_id: 'visit-1',
    basket: null, operator: 'Operator A', location, confirmed: true,
    executor_boot: 'boot-1', executor_session: 'session-1', stop_generation: 7,
    connection_id: 2, request_id: expect.any(String), review_cursor: 'cursor-7' });
});

it('clears the station check form when Stop invalidates its reviewed controls', async () => {
  let generation = 7;
  const agv = await open(() => ({
    executor_link: true, executor_boot: 'boot-1', executor_session: 'session-1',
    stop_generation: generation, review_cursor: 'cursor-' + generation,
    manual_visit: { id: 'visit-1', station: 2, result: { outcome: 'unknown' } },
    operator_check: { reason: 'Reply lost', job_id: 'visit-1' },
  }));
  await app.get('[data-ui="agv.check.operator"]').setValue('Operator A');
  await app.get('[data-ui="agv.check.location"]').setValue('station 2');
  await app.get('[data-ui="agv.check.confirmed"]').setValue(true);
  generation++;
  await agv.send('query_status');
  await flushPromises();
  expect(app.get('[data-ui="agv.check.abort"]').element.disabled).toBe(true);
  expect(app.get('[data-ui="agv.check.confirmed"]').element.checked).toBe(false);
});

it.each([false, true])('only shows an idle operator check on the AGV page when no programme owns the IPC (%s)', async owned => {
  await open(() => ({
    executor_link: true, programme_motion_block: owned ? 'The programme owns the executor.' : null,
    operator_check: { reason: 'Lease expired', location: 'station 2', job_id: null, run_id: null }
  }));
  expect(app.find('[data-ui="agv.check.panel"]').exists()).toBe(!owned);
  if (!owned) expect(app.get('[data-ui="agv.check.abort"]').text()).toContain('Confirm safe hold');
});

it('shows the newly observed station and the earlier station separately', async () => {
  await open(() => ({
    operator_check: {
      reason: 'Station move settled after Stop', job_id: 'manual-station-job',
      location: 'station 3', earlier_location: 'station 0', technician: false,
    },
  }));
  const check = app.get('[data-ui="agv.check.panel"]');
  expect(check.text()).toContain('Last observed location station 3');
  expect(check.text()).toContain('Earlier observed location station 0');
});

it('asks for fresh station confirmation while Stop remains available', async () => {
  const requests = [];
  await open(body => {
    if (body) requests.push(body);
    return { armed: true, executor_link: true,
      executor_health: { manual_travel_until: Date.now() / 1000 + 3600, manual_travel_min_battery: 20 },
      status: { ...status, max_stations: 1, battery_percent: 80 } };
  });
  await app.get('#agv-station-2').trigger('click');
  expect(requests.filter(body => body.command === 'call_station')).toHaveLength(0);
  expect(app.get('[data-ui="agv.call.attestation"]').text()).toMatch(/e-stop.*arm.*folded.*path.*clear/i);
  expect(app.get('[data-ui="agv.stop"]').element.disabled).toBe(false);
  await app.get('[data-ui="agv.call.confirm"]').trigger('click');
  await flushPromises();
  expect(requests.at(-1)).toMatchObject({ command: 'call_station', value: 2,
    attestation: { station: 2, id: expect.any(String), confirmed_at: expect.any(Number) } });
});

it('clears pending station confirmation when Stop is pressed', async () => {
  const requests = [];
  await open(body => {
    if (body) requests.push(body);
    return { armed: true, executor_link: true,
      executor_health: { manual_travel_until: Date.now() / 1000 + 3600, manual_travel_min_battery: 20 },
      status: { ...status, battery_percent: 80 } };
  });
  await app.get('#agv-station-2').trigger('click');
  expect(app.find('[data-ui="agv.call.attestation"]').exists()).toBe(true);
  await app.get('[data-ui="agv.stop"]').trigger('click');
  await flushPromises();
  expect(requests.at(-1)).toMatchObject({ command: 'stop' });
  expect(app.find('[data-ui="agv.call.attestation"]').exists()).toBe(false);
  await app.get('#agv-station-2').trigger('click');
  expect(app.find('[data-ui="agv.call.attestation"]').exists()).toBe(true);
});

it('shows both stations, the supervised deadline and draft battery floor for one learned leg', async () => {
  const deadline = Date.now() / 1000 + 3600;
  const agv = await open(() => ({ armed: true, executor_link: true,
    executor_health: { manual_travel_until: deadline, manual_travel_min_battery: 20 },
    status: { ...status, max_stations: 1, battery_percent: 80 } }));
  expect(agv.commandReason('call_station', 2)).toBe('');
  expect(app.get('#agv-station-2').element.disabled).toBe(false);
  expect(app.text()).toContain('The AGV reports 1 learned leg: stations 1 to 2.');
  expect(app.text()).toContain('Manual travel deadline');
  expect(app.text()).toContain('DRAFT battery floor 20%');
  expect(app.get('#agv-station-3').element.disabled).toBe(true);
});

it.each([[0, 0], [255, 255]])('uses the shared range for %i reported wired legs', async (legs, maximum) => {
  const agv = await open(() => ({ armed: true, executor_link: true,
    executor_health: { simulated: false }, status: { ...status, max_stations: legs } }));
  expect(agv.maximumStation).toBe(maximum);
  expect(agv.commandReason('call_station', maximum + 1)).not.toBe('');
});

it('explains the visual checks before each line-tape call', async () => {
  await open(() => ({ armed: true, executor_link: true,
    executor_health: { manual_travel_until: Date.now() / 1000 + 3600,
      manual_travel_min_battery: 20, manual_travel_isolated_track: true },
    status: { ...status, battery_percent: 80 } }));
  expect(app.get('[data-ui="agv.call.window"]').text()).toContain(
    'Supervised folded-arm travel on the line tape. Tank-clear and equipment-off signals are not connected yet: check the path and the tank equipment by eye before each call.');
});

it('shows the full per-call confirmation in the station dialog', async () => {
  await open(() => ({ armed: true, executor_link: true,
    executor_health: { manual_travel_until: Date.now() / 1000 + 3600,
      manual_travel_min_battery: 20, manual_travel_isolated_track: true },
    status: { ...status, battery_percent: 80 } }));
  await app.get('#agv-station-2').trigger('click');
  expect(app.get('[data-ui="agv.call.attestation"]').text()).toContain(
    'I am at the e-stop, the arm is folded, and the path and tank equipment are clear.');
  expect(app.text()).toMatch(/UR10e/i);
});

it('shows the armed owner and client jobs from the IPC gateway', async () => {
  await open(() => ({
    executor_link: true, job_gateway: true, owner: 'calibration', armed_for: 'calibration', armed: true,
    executor_job: { job_id: 'current-visit', client_name: 'calibration', state: 'running' },
    recent_jobs: [{ job_id: 'earlier-visit', client_name: 'demo-module', state: 'succeeded' }],
    recent_raw: [{ id: 'raw-speed', command: 'set_speed', client_name: 'diagnostics', utc: stamp, outcome: 'verified' }],
    raw_settings: [{ id: 'raw-speed', before: 1250, after: 500 }],
  }));
  expect(app.get('[data-ui="agv.raw.activity"]').text()).toContain('diagnostics');
  expect(app.get('[data-ui="agv.raw.settings"]').text()).toContain('1250 → 500 m/h');
  expect(app.text()).toContain('Armed for Calibration module');
  expect(app.text()).toContain('current-visit');
  expect(app.text()).toContain('demo-module');
  expect(app.text()).toContain('earlier-visit');
});

it('labels the station and preserves the API explanation and next step on refusal', async () => {
  await open(body => body?.command === 'call_station' ? {
    error: 'bare fixture reason', code: 'station_range',
    message: 'The AGV reports 1 learned leg: stations 1 to 2.',
    hint: 'Teach the missing legs, then press Query status.',
  } : { executor_link: true, armed: true, status: { ...status, max_stations: 1 } });
  await app.get('#agv-station-2').trigger('click');
  await flushPromises();
  expect(app.get('#agv-station-2-outcome').text()).toContain(
    'Call to station 2 refused. The AGV reports 1 learned leg: stations 1 to 2. Teach the missing legs, then press Query status.');
});

it('marks a retained station report as last known and shows how to get a first report', async () => {
  await open(() => ({ status: null, connected: false }));
  expect(app.text()).toContain('Station count unknown: on the AGV page choose Wired, Connect, then Query status.');
  app.unmount();

  await open(() => ({ status: null, connected: false, wired_station: {
    value: 1, current_station: 2, observed_at: '2026-11-26T14:45:06.123', stale: true,
  } }));
  expect(app.get('[data-ui="agv.call.panel"]').text()).toContain(
    'The AGV reports 1 learned leg: stations 1 to 2. It was at station 2 (read 14:45:06). Last known. Press Query status for a new reading.');
});

it('requires a named module Arm confirmation and sends the reviewed owner and lease', async () => {
  const requests = [];
  await open(body => {
    if (body) requests.push(body);
    return { executor_link: true, job_gateway: true, owner: 'inspection', module_lease_id: 'reviewed-lease',
      module_arm_enrolled: true, executor_boot: 'fixture-boot', executor_session: 'fixture-session' };
  });
  await app.get('[data-ui="agv.movement.arm"]').trigger('click');
  expect(app.get('[data-ui="agv.arm.attestation"]').text()).toMatch(/inspection.*e-stop.*arm.*folded.*path.*tank.*30 minutes/is);
  expect(requests).toEqual([]);
  await app.get('[data-ui="agv.arm.operator"]').setValue('shift operator');
  await app.get('[data-ui="agv.arm.confirm"]').trigger('click');
  await flushPromises();
  expect(requests.at(-1)).toMatchObject({ action: 'arm', armed: true,
    module_attestation: { confirmed: true, operator: 'shift operator', owner: 'inspection', lease_id: 'reviewed-lease' } });
});

it('shows the module confirmation deadline, countdown and expiry explanation', async () => {
  await open(() => ({ executor_link: true, job_gateway: true, owner: 'inspection', armed_for: 'inspection', armed: true,
    module_attestation: { owner: 'inspection', expires_at: 1795689600, remaining_s: 125 } }));
  expect(app.text()).toMatch(/Armed for inspection.*confirmation valid until/i);
  expect(app.text()).toContain('2:05');
});
