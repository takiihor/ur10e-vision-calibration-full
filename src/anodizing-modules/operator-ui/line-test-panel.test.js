// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import { afterEach, expect, it, vi } from 'vitest';
import LineTestPanel from './LineTestPanel.vue';
import * as api from './api';

const selected = { id: 'fixture', command: 'FixtureCommand', feedback: 'FixtureFeedback',
  seconds: 5, baseline: 0, expected: 1, basis: 'Synthetic mapping' };
const completed = { id: 'fixture-run', case: selected, api: 'Pass', state: 'finished',
  reason: 'Command zero confirmed.', source: 'Simulation' };
let page;
afterEach(() => {
  page?.unmount();
  vi.restoreAllMocks();
});

function open(state) {
  const send = vi.spyOn(api, 'request').mockImplementation(async (path, body) => {
    if (!body) return path.endsWith('/archives') ? { archives: [] } : state;
    if (body.action === 'arm') return { session: 'fixture-arm', seconds: 1800 };
    if (body.action === 'prepare') return { approval: 'fixture-approval', baseline: { id: 12, source_at: '2026-01-01T00:00:04.125Z', values: {} }, seconds: 30, metadata: [] };
    return { id: body.request_id };
  });
  page = mount(LineTestPanel, { props: { quality: 'fresh', systemSignals: { remote_mode: 'ExampleRemote', local_mode: 'ExampleLocal' },
    variable: id => ({ value: id === 'line.ExampleRemote' ? 1 : 0 }) },
    global: { plugins: [PrimeVue] } });
  return send;
}

it('requires a new arm and one explicit test approval before starting', async () => {
  const send = open({ enabled: true, cases: [selected], history: [] });
  await flushPromises();
  expect(page.get('#line-review').attributes('disabled')).toBeDefined();
  await page.get('#line-arm').trigger('click');
  await flushPromises();
  await page.get('#line-case').setValue('fixture');
  await page.get('#line-review').trigger('click');
  await flushPromises();
  expect(page.text()).toContain('FixtureCommand');
  expect(page.text()).toContain('FixtureFeedback');
  expect(page.get('.line-approval').text()).toMatch(/\d{2}:\d{2}:\d{2}[.,]125/);
  expect(page.get('.line-approval').text()).toContain('Approval expires in 30 s');
  expect(send.mock.calls.some(([, body]) => body?.action === 'start')).toBe(false);
  await page.get('#line-confirm').trigger('click');
  await flushPromises();
  const [, body] = send.mock.calls.find(([, body]) => body?.action === 'start');
  expect(body.approval).toBe('fixture-approval');
  expect(body.session).toBe('fixture-arm');
});

it('reattaches to existing evidence without re-arming or replaying Start', async () => {
  const send = open({ enabled: true, cases: [selected], history: [completed], latest: completed });
  await flushPromises();
  expect(page.text()).toContain('API · Pass');
  expect(page.text()).toContain('Human · Not checked');
  await page.get('#line-human-result').setValue('Fail');
  await page.get('#line-human-notes').setValue('Synthetic observation');
  await page.get('#line-save-judgment').trigger('click');
  await flushPromises();
  expect(send.mock.calls.filter(([, body]) => body).map(([, body]) => body)).toEqual([
    { action: 'judge', request_id: 'fixture-run', result: 'Fail', notes: 'Synthetic observation' },
  ]);
});

it('shows the refusal reason even if the simulator flag is disabled', async () => {
  open({ enabled: false, reason: 'Simulator writes are disabled. Check the simulator setup.', history: [] });
  await flushPromises();
  expect(page.get('#line-arm').attributes('disabled')).toBeDefined();
  expect(page.text()).toContain('Simulator writes are disabled');
});
