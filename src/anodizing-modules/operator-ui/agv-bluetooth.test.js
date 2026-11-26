// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import AgvBluetooth from './AgvBluetooth.vue';

let app;
afterEach(() => { app?.unmount(); vi.unstubAllGlobals(); });

async function openWithCachedStatus(wired) {
  document.head.innerHTML = '<meta name="module-token" content="fixture">';
  vi.stubGlobal('fetch', vi.fn(async (path, options) => {
    expect(options.method).toBeUndefined();
    if (path === '/api/agv-bluetooth') return { ok: true, json: async () => ({ max_stations: null }) };
    expect(path).toBe('/api/agv?cached=1');
    return { ok: true, json: async () => wired };
  }));
  app = mount(AgvBluetooth, { attachTo: document.body });
  await flushPromises();
}

it('falls back to the latest cached wired station report', async () => {
  await openWithCachedStatus({
    settings: { mode: 'wired' }, connected: false, stale: true,
    status: { max_stations: 1, current_station: 1 }, status_time: '2026-11-26T14:45:06.123',
  });
  expect(app.text()).toContain('The AGV has learned 1 leg: stations 1 to 2. It was at station 1 (wired status read 14:45:06).');
});

it('shows how to get a station count when wired status is absent', async () => {
  await openWithCachedStatus({ status: null });
  expect(app.text()).toContain('Station count unknown: on the AGV page choose Wired, Connect, then Query status.');
});

it('explains that the stored route cannot be read over Bluetooth', async () => {
  await openWithCachedStatus({ status: null });
  expect(app.text()).toContain("The AGV's stored route itself cannot be read over Bluetooth.");
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
      fields: [{ field: 'A7', name: 'Send 485 regularly', meaning: 'Example', value: 0, raw: '00 00', writable: true }] }) };
  }));
  app = mount(AgvBluetooth, { attachTo: document.body });
  await flushPromises();
  const click = async text => {
    await app.findAll('button').find(button => button.text().trim() === text).trigger('click');
    await flushPromises();
  };
  await click('Connect');
  await click('Read settings');
  await app.get('#bluetooth-field').setValue('A7');
  await app.get('#bluetooth-value').setValue('1');
  await click('Review write');
  expect(app.text()).toMatch(/0/);
  expect(app.text()).toMatch(/1/);
  expect(sent.some(body => body.action === 'write')).toBe(false);
  await click('Confirm write');
  expect(sent.find(body => body.action === 'write')).toMatchObject({ field: 'A7', old: 0, value: 1, confirmed: true });
  await click('Disconnect');
  expect(sent.at(-1).action).toBe('disconnect');
});
