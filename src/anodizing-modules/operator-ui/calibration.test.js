// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import { reactive } from 'vue';
import CalibrationPage from './CalibrationPage.vue';

let page;
const service = { owner: { jobs: [] }, allow_camera: false, allow_manual: false, allow_auto_calibration: false };
const button = id => page.get('#agv-cal-' + id);
async function open(props = {}, respond = () => ({})) {
  document.head.innerHTML = '<meta name="module-token" content="fixture">';
  vi.stubGlobal('fetch', vi.fn(async (path, options) => ({
    ok: true,
    json: async () => respond(path, options.body && JSON.parse(options.body)),
  })));
  page = mount(CalibrationPage, { props: { service, agv: { state: {} }, ...props }, global: { plugins: [PrimeVue] } });
  await flushPromises();
}

beforeEach(() => vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
afterEach(() => {
  page?.unmount();
  vi.unstubAllGlobals();
  localStorage.clear();
});

it('keeps camera, pendant capture and automatic capability separate while allowing offline fit', async () => {
  await open();
  expect(button('camera-preview').attributes()).toHaveProperty('disabled');
  expect(page.text()).toContain('Camera access is disabled.');
  await button('step-capture').trigger('click');
  expect(button('manual').attributes()).toHaveProperty('disabled');
  expect(page.text()).toContain('Pendant capture is disabled.');
  expect(page.text()).toContain('Automatic calibration is disabled.');
  await button('step-results').trigger('click');
  expect(button('simulate').attributes()).not.toHaveProperty('disabled');
});

it('binds a manual answer to the active job and the current request', async () => {
  const sent = [];
  const active = { id: 'manual-1234', state: 'waiting', module: 'calibration',
    pending: { id: 8, kind: 'manual', summary: 'Position the robot with the pendant.' }, events: [] };
  await open({ service: { ...service, owner: { active: active.id, jobs: [active] } } }, (path, body) => {
    if (path === '/api/jobs/' + active.id) return active;
    if (path === '/api/answer') {
      sent.push(body);
      return { ...active, state: 'running', pending: null };
    }
    return {};
  });
  await button('capture-view').trigger('click');
  await flushPromises();
  expect(sent).toEqual([{ job_id: active.id, request_id: 8, value: '' }]);
  expect(page.find('#agv-cal-capture-view').exists()).toBe(false);
});

it('clears the AGV switched-off declaration on a link change and keeps it through unchanged reports', async () => {
  const agv = reactive({ state: { connected: false, connection_id: 4, stop_generation: 2, sequence: 1 } });
  await open({ agv });
  await button('step-capture').trigger('click');
  const declaration = page.get('#cal-agv-off');
  await declaration.setValue(true);
  agv.state = { ...agv.state, sequence: 2 };
  await flushPromises();
  expect(declaration.element.checked).toBe(true);
  agv.state = { ...agv.state, connected: true, connection_id: 5 };
  await flushPromises();
  expect(declaration.element.checked).toBe(false);
});

it('keeps the checked calibration selected when handoff storage is blocked', async () => {
  const fit = { id: 'fit-fixture', module: 'calibration', state: 'done', result: {
    path: 'calibration/fixture.json', result: { type: 'eye_in_hand', verdict: 'good' },
  } };
  localStorage.setItem('ano-vue-calibration-fit', fit.id);
  localStorage.setItem('ano-calibration-form', JSON.stringify({ calibration: fit.result.path }));
  await open({}, (path) => path.startsWith('/api/calibration-handoff?') ?
    { calibration: fit.result.path } : path === '/api/jobs/' + fit.id ? fit : {});
  location.hash = '#/calibration';
  const storage = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
  try {
    await button('step-results').trigger('click');
    await button('teach').trigger('click');
    await flushPromises();
    expect(location.hash).toBe('#/calibration');
    expect(button('teach-outcome').text()).toContain('Browser storage');
  } finally {
    storage.mockRestore();
  }
});
