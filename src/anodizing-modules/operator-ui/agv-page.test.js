// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { h, ref } from 'vue';
import PrimeVue from 'primevue/config';
import ConfirmationService from 'primevue/confirmationservice';
import AgvPage from './AgvPage.vue';
import { useAgv } from './useAgv.js';
import { newSession, sessionKey } from '../app/anodizing_web/web/agv-record.mjs';

const stamp = '2026-11-20T08:00:00.000Z';
const settings = { mode: 'simulator', address: '00 01', channel: '04' };
const status = {
  mode: 2, max_stations: 3, stopped: true, actual_speed_m_h: 0,
  current_station: 1, target_station: 1, fault_code: 0, obstacle_stop: false,
};
let app;

async function open(respond = () => ({})) {
  let agv, sequence = 0;
  document.head.innerHTML = '<meta name="module-token" content="fixture">';
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
      return () => h(AgvPage, { agv });
    },
  }, { attachTo: document.body, global: { plugins: [PrimeVue, ConfirmationService] } });
  await flushPromises();
  return agv;
}

afterEach(() => {
  app?.unmount();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  localStorage.clear();
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
  expect(agv.feedback('armed').text).toMatch(/Refused.*robot job is running/);
  expect(armed.checked).toBe(false);
});

it('keeps unconfirmed motion distinct from a stopped report', async () => {
  await open(() => ({ status: { ...status, actual_speed_m_h: 250 } }));
  expect(app.get('#agv-motion').text()).toBe('Running');
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
  expect(agv.commandReason('call_station', 3)).toBe('Choose a learned station from 1 to 2.');
  expect(app.get('#agv-station-1').text()).toContain('Current');
  expect(app.get('label[for="agv-manual-station"]').text()).toContain('(1–2)');
  await app.get('#agv-station-2').trigger('click');
  await flushPromises();
  expect(requests[1]).toMatchObject({ command: 'call_station', value: 2, stop_generation: 7 });
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
  expect(app.findAll('.p-timeline-event')).toHaveLength(9);
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
