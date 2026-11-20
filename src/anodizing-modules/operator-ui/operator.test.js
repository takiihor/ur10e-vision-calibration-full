// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import App from './App.vue';

const mode = { mode: 'simulator', address: '00 01', channel: '04' };
const connected = { connected: true, connection_id: 2, settings: mode, armed: true, log: [], events: [], results: [] };
const commandNames = ['read_speed', 'query_status', 'forward'];
let app;

function open(route, respond) {
  location.hash = route;
  document.head.innerHTML = '<meta name="module-token" content="fixture">';
  vi.stubGlobal('fetch', vi.fn(async (path, options) => {
    const body = options.body && JSON.parse(options.body);
    const response = await respond(path, body);
    return { ok: true, json: async () => response };
  }));
  app = mount(App, { attachTo: document.body, global: { plugins: [PrimeVue] } });
}

function element(id) {
  return app.get('#agv-' + id);
}

afterEach(() => {
  app?.unmount();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
  localStorage.clear();
});

it('disarms on opening an armed connection and only dispatches one labelled Esc Stop during a pending read', async () => {
  const sent = [];
  let sequence = 0;
  open('#/agv', async (path, body) => {
    if (path === '/api/state') return { variables: [] };
    if (path === '/api/status') return { owner: {} };
    if (body) sent.push(body);
    if (body?.command === 'read_speed') return new Promise(() => {});
    return { ...connected, armed: !sent.some(item => item.action === 'arm'), sequence: ++sequence,
      commands: commandNames.map(name => ({ name, label: name.replaceAll('_', ' '), motion: name === 'forward' })),
      results: body?.command === 'stop' ? { stop: { tx_id: 9, sent_at: '2026-11-20T00:00:00Z',
        state: 'confirmed by status' } } : {} };
  });
  await flushPromises();
  expect(sent[0]).toMatchObject({ action: 'arm', armed: false, connection_id: 2 });
  expect(element('forward').element.disabled).toBe(true);
  await element('read_speed').trigger('click');
  element('address').element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', repeat: true }));
  await flushPromises();
  expect(sent.filter(item => item.command === 'stop')).toEqual([
    expect.objectContaining({ connection_id: 2, action: 'send' }),
  ]);
  expect(element('stop-outcome').text()).toMatch(/\(Esc\).*AGV reports stopped/);
  expect(element('outcome').text()).toBe(element('stop-outcome').text());
  expect(element('pause').element.disabled).toBe(false);
});

it('keeps command replies from a previous connection out of the current outcome', async () => {
  vi.useFakeTimers();
  let current = 2, release, sequence = 0;
  open('#/agv', async (path, body) => {
    if (path === '/api/state') return { variables: [] };
    if (path === '/api/status') return { owner: {} };
    if (body?.command === 'read_speed') return new Promise(resolve => { release = resolve; });
    return { ...connected, armed: false, connection_id: current, sequence: ++sequence, results: {},
      commands: [{ name: 'read_speed', label: 'Read speed', motion: false }] };
  });
  await flushPromises();
  await element('read_speed').trigger('click');
  expect(element('read_speed-outcome').text()).toBe('Sending…');
  current = 3;
  await vi.advanceTimersByTimeAsync(800);
  release({ ...connected, sequence: sequence + 1, results: {
    read_speed: { tx_id: 1, state: 'reply: 1234 m/h', sent_at: '2026-11-20T00:00:00Z' },
  } });
  await flushPromises();
  expect(app.text()).not.toContain('1234 m/h');
  expect(element('outcome').text()).toMatch(/connection changed/i);
});

it('keeps navigation, urgent controls and records available when storage is denied', async () => {
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied'); });
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
  open('#/agv', async path => path === '/api/state' ? { variables: [] } :
    path === '/api/status' ? { owner: {} } : { ...connected, armed: false, sequence: 1, commands: [] });
  await flushPromises();
  expect(app.get('a[href="/teaching"]').text()).toBe('Teaching');
  await element('notes-1').setValue('Simulator only');
  expect(element('record-note').text()).toMatch(/storage is unavailable/i);
  expect(element('stop').element.disabled).toBe(false);
});

it('keeps Manual and Teacher Stop bound to their jobs while Escape targets the AGV across navigation', async () => {
  const sent = [];
  let sequence = 0;
  open('#/', async (path, body) => {
    if (body) sent.push([path, body]);
    if (path === '/api/status') return {
      owner: { stop_targets: [{ kind: 'manual', job_id: 'recipe-01' }, { kind: 'teacher', job_id: 'session-02' }] },
    };
    if (path === '/api/state') return { variables: [] };
    return { ...connected, armed: false, sequence: ++sequence, commands: [], results: body?.command === 'stop' ? {
      stop: { tx_id: 9, state: 'confirmed by status', sent_at: '2026-11-20T00:00:00Z' },
    } : {} };
  });
  await flushPromises();
  expect(element('manual').text()).toMatch(/Pick\/place Stop/);
  expect(element('teacher').text()).toMatch(/Teacher Stop/);
  await element('manual').trigger('click');
  await element('teacher').trigger('click');
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await flushPromises();
  expect(sent).toEqual([
    ['/api/manual-control', { action: 'stop', job_id: 'recipe-01' }],
    ['/api/teacher-control', { action: 'stop', job_id: 'session-02' }],
    ['/api/agv', expect.objectContaining({ command: 'stop', connection_id: 2 })],
  ]);
  location.hash = '#/agv';
  window.dispatchEvent(new HashChangeEvent('hashchange'));
  await flushPromises();
  expect(app.get('h1').text()).toBe('AGV commissioning');
  expect(element('manual').text()).toMatch(/Pick\/place Stop/);
  expect(element('teacher').text()).toMatch(/Teacher Stop/);
  expect(element('outcome').text()).toContain('(Esc)');
});
