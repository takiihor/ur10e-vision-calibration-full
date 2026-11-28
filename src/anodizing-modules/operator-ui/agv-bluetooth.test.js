// @vitest-environment jsdom
import { selectOption } from './test-select';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import AgvBluetooth from './AgvBluetooth.vue';

let app;
beforeEach(() => vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
afterEach(() => { app?.unmount(); vi.unstubAllGlobals(); });

async function openWithCachedStatus(station) {
  document.head.innerHTML = '<meta name="module-token" content="fixture">';
  vi.stubGlobal('fetch', vi.fn(async (path, options) => {
    expect(options.method).toBeUndefined();
    expect(path).toBe('/api/agv-bluetooth');
    return { ok: true, json: async () => ({ max_stations: station }) };
  }));
  app = mount(AgvBluetooth, { attachTo: document.body, global: { plugins: [PrimeVue] } });
  await flushPromises();
}

it.each([false, true])('shows the cached wired report with stale=%s', async stale => {
  await openWithCachedStatus({
    value: 1, current_station: 2, observed_at: '2026-11-26T14:45:06.123', stale,
  });
  expect(app.text()).toContain(
    `The AGV reports 1 learned leg: stations 1 to 2. It ${stale ? 'was' : 'is'} at station 2 (read 14:45:06).`);
  expect(app.text().includes('Last known.')).toBe(stale);
});

it('shows how to get a station count when wired status is absent', async () => {
  await openWithCachedStatus(null);
  expect(app.text()).toContain('Station count unknown: on the AGV page choose Wired, Connect, then Query status.');
});

it('explains how to read the stored route over Bluetooth', async () => {
  await openWithCachedStatus(null);
  expect(app.text()).toContain('Connect and read settings to see the stored route.');
  expect(app.text()).toContain('Bluetooth can read raw route records. It cannot show the decoded learned route.');
  expect(app.text()).toContain('Not available: the Bluetooth stop is not verified yet.');
});

it('reviews old and new values before a confirmed write and closes the session', async () => {
  document.head.innerHTML = '<meta name="module-token" content="fixture">';
  const sent = [];
  let connected = false;
  vi.stubGlobal('fetch', vi.fn(async (_path, options) => {
    const body = options.body && JSON.parse(options.body);
    if (body) sent.push(body);
    if (body?.action === 'connect') connected = true;
    if (body?.action === 'disconnect') connected = false;
    return { ok: true, json: async () => ({ connected, active: connected, busy: false, configured: true,
      blocked: null, undo: null, max_stations: null,
      fields: [{ field: 'A7', name: 'Send 485 regularly', meaning: 'Example', value: 0, raw: '00 00', writable: true, minimum: 0, maximum: 1 }] }) };
  }));
  app = mount(AgvBluetooth, { attachTo: document.body, global: { plugins: [PrimeVue] } });
  await flushPromises();
  const click = async text => {
    await app.findAll('button').find(button => button.text().trim() === text).trigger('click');
    await flushPromises();
  };
  await click('Connect');
  await click('Read settings');
  await selectOption(app, '#bluetooth-field', 'A7 · Send 485 regularly');
  const value = app.get('#bluetooth-value');
  await value.setValue('2');
  expect(value.attributes('aria-invalid')).toBe('true');
  expect(value.attributes('aria-describedby')).toContain('bluetooth-value-error');
  expect(app.get('#bluetooth-value-error').text()).toContain('between 0 and 1');
  expect(app.get('[data-ui="agv.bluetooth.reviewwrite"]').element.disabled).toBe(true);
  await value.setValue('1');
  expect(app.find('#bluetooth-value-error').exists()).toBe(false);
  await click('Review write');
  expect(app.text()).toMatch(/0/);
  expect(app.text()).toMatch(/1/);
  expect(sent.some(body => body.action === 'write')).toBe(false);
  await click('Confirm write');
  expect(sent.find(body => body.action === 'write')).toMatchObject({ field: 'A7', old: 0, value: 1, confirmed: true });
  await click('Disconnect');
  expect(sent.at(-1).action).toBe('disconnect');
});

it('requires review and confirmation to erase the route and displays the read-back', async () => {
  document.head.innerHTML = '<meta name="module-token" content="fixture">';
  const sent = [];
  let active = false;
  let counts = [2, 1];
  vi.stubGlobal('fetch', vi.fn(async (_path, options) => {
    const body = options.body && JSON.parse(options.body);
    if (body) sent.push(body);
    if (body?.action === 'connect') active = true;
    if (body?.action === 'clear') counts = [0, 0];
    return { ok: true, json: async () => ({ configured: true, connected: active, active, fields: [],
      route: { counts },
      clear_review: body?.action === 'review-clear' ? { token: 'fixture', before: counts } : null,
      clear_result: body?.action === 'clear' ? { before: [2, 1], after: counts } : null,
    }) };
  }));
  app = mount(AgvBluetooth, { attachTo: document.body, global: { plugins: [PrimeVue] } });
  await flushPromises();
  const click = async selector => {
    await app.get(`[data-ui="agv.bluetooth.${selector}"]`).trigger('click');
    await flushPromises();
  };
  await click('connect');
  await app.setProps({ readOnly: true });
  expect(app.get('[data-ui="agv.bluetooth.reviewclear"]').element.disabled).toBe(true);
  await app.setProps({ readOnly: false });
  await click('reviewclear');
  expect(app.text()).toContain('There is no undo.');
  await app.setProps({ readOnly: true });
  expect(app.find('[data-ui="agv.bluetooth.confirm"]').exists()).toBe(false);
  await app.setProps({ readOnly: false });
  expect(app.text()).toContain('Outbound 2 · Return 1');
  expect(sent.some(body => body.action === 'clear')).toBe(false);
  await click('cancel');
  expect(sent.some(body => body.action === 'clear')).toBe(false);
  await click('reviewclear');
  await click('confirm');
  expect(sent.at(-1)).toMatchObject({ action: 'clear', token: 'fixture', confirmed: true });
  expect(app.text()).toContain('After clear: Outbound 0 · Return 0');
  expect(app.text()).toContain('green button');
});

it('shows why an action was refused and what to do next', async () => {
  vi.stubGlobal('fetch', vi.fn(async (_path, options) => options.body
    ? { ok: false, status: 400, json: async () => ({ error: 'future_ble_code', code: 'future_ble_code',
      message: 'Reason reported: future_ble_code.', hint: 'Tell the technician.' }) }
    : { ok: true, json: async () => ({ configured: true, active: false }) }));
  app = mount(AgvBluetooth, { attachTo: document.body, global: { plugins: [PrimeVue] } });
  await flushPromises();
  await app.get('[data-ui="agv.bluetooth.connect"]').trigger('click');
  await flushPromises();
  expect(app.get('[role="alert"]').text()).toContain(
    'Connect Bluetooth refused. Reason reported: future_ble_code. Tell the technician.');
});
