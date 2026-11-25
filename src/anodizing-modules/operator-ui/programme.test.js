// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils';
import { afterEach, expect, test, vi } from 'vitest';
import ProgrammePanel from './ProgrammePanel.vue';
import { request } from './api';

vi.mock('./api', () => ({ request: vi.fn() }));

afterEach(() => vi.clearAllMocks());

test('reviews and saves a draft revision before starting the basket', async () => {
  const step = { id: 'step-1', tank: 'tank-1', role: 'etch', setpoint: 30,
    limits: { min: 25, overstay: 5, air: 15 } };
  const recipe = { id: 'revision-1', state: 'DRAFT', steps: [step] };
  const approval = { boot: 'ipc', session: 'katana', stop_generation: 0 };
  let state = { recipe, run: null, approval, review_cursor: 'before-save' };
  request.mockImplementation(async (_, body) => {
    if (body?.action === 'save') {
      state = { ...state, review_cursor: 'after-save', recipe: { ...recipe, id: 'revision-2',
        steps: [{ ...step, limits: body.limits[step.id] }] } };
    }
    if (body?.action === 'start') {
      state = { ...state, run: { id: 'run-1', basket: body.basket, state: 'running',
        cursor: 0, recipe: state.recipe, steps: [] } };
    }
    return state;
  });
  const wrapper = mount(ProgrammePanel);
  try {
    await flushPromises();
    expect(wrapper.text()).toContain('DRAFT');
    await wrapper.get('[aria-label="step-1 minimum dwell"]').setValue('27');
    await wrapper.get('[data-action="save"]').trigger('click');
    await flushPromises();
    expect(request).toHaveBeenCalledWith('/api/programme', { action: 'save', revision: 'revision-1',
      limits: { 'step-1': { min: 27 } }, request_id: expect.any(String), review_cursor: 'before-save' });
    await wrapper.get('[aria-label="Basket ID"]').setValue('demo-basket');
    await wrapper.get('input[type="checkbox"]').setValue(true);
    await wrapper.get('[data-action="start"]').trigger('click');
    await flushPromises();
    expect(request).toHaveBeenCalledWith('/api/programme', { action: 'start', revision: 'revision-2',
      basket: 'demo-basket', approval, request_id: expect.any(String), review_cursor: 'after-save' });
    const [save, start] = request.mock.calls.filter(([, body]) => body);
    expect(save[1].request_id).not.toBe(start[1].request_id);
    expect(wrapper.text()).toContain('demo-basket');
  } finally {
    wrapper.unmount();
  }
});

const recipe = { id: 'rev', state: 'DRAFT', steps: [] };
const current = { id: 'new-run', basket: 'B', state: 'succeeded', cursor: 0, recipe, steps: [], jobs: [] };

test('labels foreign hold with the basket and run that were actually observed', async () => {
  request.mockResolvedValue({ recipe, run: { ...current, basket_unaccounted: true,
    recovery_observation: { job_id: 'job-foreign', hold: { location: 'output', basket: 'foreign-basket', run_id: 'foreign-run' } } } });
  const wrapper = mount(ProgrammePanel);
  try {
    await flushPromises();
    expect(wrapper.text()).toMatch(/unknown/i);
    expect(wrapper.text()).toContain('foreign-basket');
    expect(wrapper.text()).toContain('foreign-run');
    expect(wrapper.text()).not.toContain('Verified recovery hold output');
  } finally { wrapper.unmount(); }
});

test('selecting an earlier basket reads its record and keeps operator controls on the current run', async () => {
  const old = { ...current, id: 'old-run', basket: 'A', state: 'inspection', steps: [
    { id: 'original-dip', tank: 'old-tank', dwell_s: 33, air_s: null, plc: { cycle: 2 } },
  ] };
  request.mockImplementation(async path => ({ recipe, run: path.includes('?') ? old : current, baskets: [old, current] }));
  const wrapper = mount(ProgrammePanel);
  try {
    await flushPromises();
    await wrapper.get('select[aria-label="Basket record"]').setValue(old.id);
    await flushPromises();
    expect(request).toHaveBeenCalledWith('/api/programme?run_id=old-run');
    expect(wrapper.text()).toContain('original-dip');
    expect(wrapper.text()).toContain('33 s');
    expect(wrapper.find('[data-ui="programme.inspection.record"]').exists()).toBe(false);
  } finally { wrapper.unmount(); }
});


test('after the original basket is found, the next confirmation requests its ordinary unload', async () => {
  let stillMissing = true;
  const bodies = [];
  request.mockImplementation(async (path, body) => {
    if (body) {
      bodies.push(body);
      if (stillMissing) {
        stillMissing = false;
        throw new Error('Original basket found. Confirm its unload.');
      }
    }
    return { recipe, run: { ...current, state: 'recovery required', basket_unaccounted: stillMissing } };
  });
  const wrapper = mount(ProgrammePanel);
  try {
    await flushPromises();
    await wrapper.get('[data-ui="programme.inspection.note"]').setValue('Checked at park');
    await wrapper.get('[data-ui="programme.inspection.unaccounted"]').setValue(true);
    await wrapper.get('[data-ui="programme.inspection.record"]').trigger('click');
    await new Promise(resolve => setTimeout(resolve, 700));
    await flushPromises();
    expect(wrapper.find('[data-ui="programme.inspection.unaccounted"]').exists()).toBe(false);
    await wrapper.get('[data-ui="programme.inspection.record"]').trigger('click');
    await flushPromises();
    expect(bodies.map(body => body.allow_unaccounted)).toEqual([true, false]);
  } finally { wrapper.unmount(); }
});


test('a recovered legacy record also checks the observed identities before labelling a hold verified', async () => {
  request.mockResolvedValue({ recipe, run: { ...current, recovery_observation: {
    job_id: 'old-result', hold: { basket: 'B', run_id: 'different-run', location: 'park' } } } });
  const wrapper = mount(ProgrammePanel);
  try {
    await flushPromises();
    expect(wrapper.text()).toContain('different-run');
    expect(wrapper.text()).not.toContain('Verified recovery hold park');
  } finally { wrapper.unmount(); }
});
