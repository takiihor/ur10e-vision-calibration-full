// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
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
