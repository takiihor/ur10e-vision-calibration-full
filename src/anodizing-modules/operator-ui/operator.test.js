// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import ConfirmationService from 'primevue/confirmationservice';
import ToastService from 'primevue/toastservice';
import App from './App.vue';

const mode = { mode: 'simulator', address: '00 01', channel: '04' };
const connected = { connected: true, connection_id: 2, settings: mode, armed: true, log: [], events: [], results: [] };
const commandNames = ['read_speed', 'query_status', 'forward'];
let app;

function open(route, respond) {
  location.hash = route;
  document.head.innerHTML = '<meta name="module-token" content="fixture">';
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
  vi.stubGlobal('fetch', vi.fn(async (path, options) => {
    const body = options.body && JSON.parse(options.body);
    const response = await respond(path, body);
    return { ok: true, json: async () => response };
  }));
  app = mount(App, { attachTo: document.body, global: { plugins: [PrimeVue, ConfirmationService, ToastService] } });
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
        state: 'confirmed by status', confirmed_stopped: true } } : {} };
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
  expect(element('stop-outcome').find('.pi-check-circle[aria-hidden="true"]').exists()).toBe(true);
  expect(element('outcome').text()).toBe(element('stop-outcome').text());
  expect(element('outcome').classes()).toContain('p-message-success');
  expect(element('pause').element.disabled).toBe(false);
  expect(element('outcome').get('.command-toast-inline').text()).toBe(element('stop-outcome').text());
  expect(document.querySelector('.p-toast').textContent.trim()).toBe('');
  expect(element('outcome').attributes('aria-live')).toBe('polite');
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
  expect(element('outcome').classes()).toContain('p-message-info');
  await vi.advanceTimersByTimeAsync(1200);
  expect(element('outcome').classes()).toContain('p-message-warn');
  current = 3;
  await vi.advanceTimersByTimeAsync(800);
  release({ ...connected, sequence: sequence + 1, results: {
    read_speed: { tx_id: 1, state: 'reply: 1234 m/h', sent_at: '2026-11-20T00:00:00Z' },
  } });
  await flushPromises();
  expect(app.text()).not.toContain('1234 m/h');
  expect(element('outcome').text()).toMatch(/connection changed/i);
  expect(element('outcome').classes()).toContain('p-message-error');
});

it('keeps navigation, urgent controls and records available when storage is denied', async () => {
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('denied'); });
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
  open('#/agv', async path => path === '/api/state' ? { variables: [] } :
    path === '/api/status' ? { owner: {} } : { ...connected, armed: false, sequence: 1, commands: [] });
  await flushPromises();
  expect(app.get('a[href="/teaching"]').text()).toMatch(/Teaching\s*Legacy/);
  await element('notes-1').setValue('Simulator only');
  expect(element('record-note').text()).toMatch(/storage is unavailable/i);
  expect(element('stop').element.disabled).toBe(false);
});

it('keeps urgent controls outside the Sakai shell and routes between marked modules', async () => {
  open('#/', async path => {
    if (path === '/api/state') return { variables: [] };
    if (path === '/api/status') return { app_id: 'anodizing_modules_v1' };
    if (path === '/api/scene') return { tanks: [] };
    return { ...connected, armed: false };
  });
  await flushPromises();
  const wrapper = app.get('.layout-wrapper');
  const menu = wrapper.get('nav[aria-label="Modules"]');
  const links = menu.findAll('a');
  expect(links.slice(1).map(link => link.attributes('href'))).toEqual([
    '/calibration', '/teaching', '#/agv', '#/scene', '#/records', '/pick-place',
  ]);
  expect(links.slice(1).map(link => link.findAll('.p-tag').map(tag => tag.text()).join(''))).toEqual(['Legacy', 'Legacy', '', '', '', 'Legacy']);
  expect(wrapper.find('.stop-bar').exists()).toBe(false);
  expect(app.get('.stop-bar').find('#agv-stop').exists()).toBe(true);
  expect(wrapper.findAll('.home-grid .p-card')).toHaveLength(2);
  const toggle = wrapper.get('button[aria-controls="operator-nav"]');
  expect(toggle.attributes('aria-expanded')).toBe('true');
  await toggle.trigger('click');
  expect(toggle.attributes('aria-expanded')).toBe('false');
  expect(element('stop').isVisible()).toBe(true);
  await toggle.trigger('click');
  location.hash = '#/agv';
  window.dispatchEvent(new HashChangeEvent('hashchange'));
  await flushPromises();
  expect(wrapper.get('a[href="#/agv"]').attributes('aria-current')).toBe('page');
  expect(document.activeElement).toBe(wrapper.get('h1').element);
  location.hash = '#/tests/scene';
  window.dispatchEvent(new HashChangeEvent('hashchange'));
  await flushPromises();
  expect(wrapper.get('a[href="#/scene"]').attributes('aria-current')).toBe('page');
  expect(wrapper.get('h1').text()).toBe('Scene');
  expect(app.get('.stop-bar').find('#agv-stop').exists()).toBe(true);
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
  for (const id of ['stop', 'manual', 'teacher']) {
    expect(element(id).find('.pi-stop-circle').exists()).toBe(true);
  }
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

it('lets the operator disarm a stale link without re-enabling movement', async () => {
  vi.useFakeTimers();
  let armed = false, sequence = 0, age = 0;
  const commands = [{ name: 'forward', label: 'Forward', motion: true }];
  open('#/agv', async (path, body) => {
    if (path === '/api/state') return { variables: [] };
    if (path === '/api/status') return { owner: {} };
    if (body?.action === 'arm') armed = body.armed;
    return { ...connected, armed, sequence: ++sequence, status_age_s: age,
      status: { mode: 2, max_stations: 3, stopped: true }, commands, results: {} };
  });
  await flushPromises();
  element('armed').element.click();
  await flushPromises();
  expect(armed).toBe(true);
  age = 5;
  await vi.advanceTimersByTimeAsync(800);
  expect(element('alarms').get('[data-state="failed"]').text()).toMatch(/Link stale/);
  expect(element('alarms').find('.pi-times-circle[aria-hidden="true"]').exists()).toBe(true);
  expect(element('forward').element.disabled).toBe(true);
  expect(element('armed').element.disabled).toBe(false);
  element('armed').element.click();
  await flushPromises();
  expect(armed).toBe(false);
  expect(element('armed').element.disabled).toBe(true);
});

it('disables station-call Pause in standard mode while leaving software Stop available', async () => {
  const sent = [];
  open('#/', async (path, body) => {
    if (path === '/api/state') return { variables: [] };
    if (path === '/api/status') return { owner: {} };
    if (body) sent.push(body);
    return { ...connected, armed: false, sequence: 1, status_age_s: 0.1,
      status: { mode: 0, stopped: false, actual_speed_m_h: 500 }, commands: [], results: {} };
  });
  await flushPromises();
  expect(element('pause').element.disabled).toBe(true);
  expect(element('stop').element.disabled).toBe(false);
  expect(app.get('.stop-bar').text()).toMatch(/station.call/i);
  expect(app.get('.stop-bar').text()).toMatch(/standard mode.*stop/i);
  await element('pause').trigger('click');
  expect(sent.some(body => body.command === 'pause')).toBe(false);
});

it('identifies disconnected AGV transport and enabled manual pick/place capability', async () => {
  open('#/', async path => path === '/api/state' ? { variables: [] } :
    path === '/api/status' ? { app_id: 'anodizing_modules_v1', owner: {}, allow_manual: false,
      allow_manual_pick_place: true } : { connected: false, sequence: 1, commands: [], results: {} });
  await flushPromises();
  expect(app.text()).not.toMatch(/Simulation/);
  expect(app.text()).toMatch(/AGV.*(disconnected|no connection)/i);
  expect(app.text()).toMatch(/pick\/place.*enabled/i);
});

it('opens Recorder and its test alias, starts selected variables, and reattaches after navigation', async () => {
  let active = null;
  const changes = [];
  open('#/records', async (path, body) => {
    if (path === '/api/state') return { agv: { connection_id: 0, sequence: 1 }, variables: [{ id: 'agv.speed', name: 'Actual speed', unit: 'm/h' }] };
    if (path === '/api/status') return { owner: {} };
    if (path === '/api/records') {
      if (body) changes.push(body);
      if (body?.action === 'start') return (active = {
        id: 'fixture', status: 'recording', started_at: '2026-11-21T00:00:00Z',
        rate_hz: body.rate_hz, variables: [ { id: 'agv.speed' } ], agv_transport: 'simulator',
      });
      if (body?.action === 'stop') return (active = { ...active, status: 'stopping' });
      return { active, records: active ? [active] : [], error: null };
    }
    return { connected: false, sequence: 1 };
  });
  await flushPromises();
  expect(app.get('h1').text()).toBe('Recorder');
  expect(app.get('#record-start').element.disabled).toBe(true);
  expect(app.text()).toContain('Choose at least one variable.');
  await app.get('input[type="checkbox"]').setValue(true);
  await app.get('form').trigger('submit');
  await flushPromises();
  expect(changes).toEqual([expect.objectContaining({
    action: 'start', rate_hz: 1, variables: ['agv.speed'],
  })]);
  expect(app.text()).toContain('AGV transport at start: Simulator');
  location.hash = '#/';
  window.dispatchEvent(new HashChangeEvent('hashchange'));
  await flushPromises();
  location.hash = '#/tests/records';
  window.dispatchEvent(new HashChangeEvent('hashchange'));
  await flushPromises();
  expect(app.get('h1').text()).toBe('Recorder');
  expect(app.get('nav a[href="#/records"]').attributes('aria-current')).toBe('page');
  await app.get('#record-stop').trigger('click');
  await flushPromises();
  expect(changes[1]).toEqual({ action: 'stop', id: 'fixture' });
  expect(app.text()).toContain('Finishing recording');
});
