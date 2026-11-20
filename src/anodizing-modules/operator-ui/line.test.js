// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import { afterEach, expect, it, vi } from 'vitest';
import { nextTick, reactive } from 'vue';
import * as api from './api';
import LinePage from './LinePage.vue';
import { sceneLabels } from './sceneState';

let page;
afterEach(() => {
  page?.unmount();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const tags = [
  { tag_name: 'Tank22_DoorOpened', group: 'Tank22', device_address: 'M5', description: 'Synthetic door',
    read_write: 'R', data_type: 'Bool', multiplier: 1 },
  { tag_name: 'Tank21_HaveBasket', group: 'Tank21', device_address: 'M4', description: 'Synthetic basket',
    read_write: 'R', data_type: 'Bool', multiplier: 1 },
  { tag_name: 'Tank21A_Command', group: 'Tank21A', device_address: 'M8', description: 'Synthetic command',
    read_write: 'W', data_type: 'Bool', multiplier: 1 },
];

function open(test = false) {
  const state = reactive({ mode: 'simulation', quality: 'fresh', catalog_revision: 1,
    refresh_available: true, row_id: 2, source_at: '2026-01-01T12:00:00+00:00', tags_ok: 3, tags_failed: 0 });
  const sample = reactive({ age_ms: 600, quality: 'fresh', value: 1 });
  const variable = id => id === 'line.api_reachable' ? { value: true } : sample;
  const outcomes = vi.fn();
  document.head.innerHTML = '<meta name="module-token" content="fixture">';
  vi.stubGlobal('fetch', vi.fn(async (path, options) => ({
    ok: true, json: async () => options.method === 'POST' ? { id: 1 } :
      path === '/api/line/diagnostics' ? { events: [{ operation: 'Read', outcome: 'Replied', row_id: 2 }] } :
      { revision: 1, tags, tanks: { Tank21: { basket: 'Tank21_HaveBasket', door_opened: 'Tank22_DoorOpened' } },
        warnings: [{code: 'alias', message: 'Tank21 door uses Tank22_DoorOpened.' }] },
  })));
  page = mount(LinePage, { global: { plugins: [PrimeVue] }, props: { test, state, variable, onOutcome: outcomes } });
  return { state, sample, variable, outcomes };
}

it('browses raw readbacks with combined filters, advanced metadata and stable selection', async () => {
  open(true);
  await flushPromises();
  expect(page.text()).toContain('Simulation');
  expect(page.text()).not.toContain('Synthetic command');
  await page.get('#line-view').setValue('catalog');
  await page.get('#line-access').setValue('W');
  await page.get('#line-query').setValue('m8');
  await page.get('#line-type').setValue('Bool');
  expect(page.text()).toContain('Tank21A_Command');
  expect(page.text()).not.toContain('Synthetic basket');
  await page.get('#line-advanced').setValue(true);
  expect(page.text()).toContain('not published');
  expect(page.text()).toContain('M8');
  expect(page.findAll('button').map(item => item.text())).not.toContain('Write');
});

it('shows the mapped values with the same expiry as Scene and queues one catalog refresh', async () => {
  const { state, sample, variable, outcomes } = open();
  await flushPromises();
  await page.get('#line-view').setValue('tank');
  expect(page.text()).toContain('Tank22_DoorOpened');
  expect(page.text()).toContain('0.6 s ago');
  const config = { tanks: [{ id: 'Tank21', label: 'Tank 21' }] };
  const mappings = { Tank21: { basket: 'Tank21_HaveBasket' } };
  expect(sceneLabels(config, variable, null, mappings).at(-1)).toMatchObject({ state: 'Occupied', age: '0.6 s ago' });
  Object.assign(sample, { age_ms: 3200, quality: 'stale', value: null });
  await nextTick();
  expect(page.text()).toContain('Unknown');
  expect(page.text()).toContain('3.2 s ago');
  expect(sceneLabels(config, variable, null, mappings).at(-1)).toMatchObject({ state: 'Unknown', age: '3.2 s ago' });
  await page.get('#line-refresh').trigger('click');
  await flushPromises();
  expect(fetch.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1);
  state.refresh = { id: 1, outcome: 'done' };
  await nextTick();
  expect(page.text()).toContain('Catalog replied');
  expect(outcomes.mock.lastCall[0].text).toContain('Catalog replied');
});

it('exports communication diagnostics in both legacy formats', async () => {
  const download = vi.spyOn(api, 'downloadBlob').mockImplementation(() => {});
  open(true);
  await flushPromises();
  await page.get('#line-view').setValue('diagnostics');
  await flushPromises();
  for (const [format, mime] of [['JSON', 'application/json'], ['CSV', 'text/csv']]) {
    await page.findAll('button').find(button => button.text() === 'Download log ' + format).trigger('click');
    const [blob, name] = download.mock.lastCall;
    expect(name).toBe('line-read-log.' + format.toLowerCase());
    expect(blob.type).toBe(mime);
  }
});

it('gives line recovery instructions beside the disabled refresh after losing the service', async () => {
  open();
  await flushPromises();
  await page.setProps({ stateError: 'Page service unavailable. Reload the page and select Connect.' });
  expect(page.get('#line-refresh').element.disabled).toBe(true);
  expect(page.get('.line-actions').text()).toContain('Line status unavailable. Check the service and reload.');
});
