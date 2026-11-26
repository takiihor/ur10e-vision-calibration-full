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


for (const action of ['resume', 'abort']) {
  test(`operator check shows observed facts and submits a confirmed ${action} choice`, async () => {
    const approval = { boot: 'ipc', session: 'katana', stop_generation: 0 };
    const checkedRecipe = { id: 'rev', state: 'DRAFT', steps: [{ id: 's1', limits: {} }, { id: 's2', limits: {} }] };
    const checkedRun = { ...current, state: 'operator_check', recipe: checkedRecipe, operator_check: {
      reason: 'Reply lost', location: 'tank-1', basket: 'foreign-basket', job_id: 'physical-job', step: 2,
    } };
    request.mockResolvedValue({ recipe: checkedRecipe, run: checkedRun, approval, review_cursor: 'review-1' });
    const wrapper = mount(ProgrammePanel);
    try {
      await flushPromises();
      for (const fact of ['Operator check', 'Reply lost', 'tank-1', 'foreign-basket', 'physical-job']) {
        expect(wrapper.text()).toContain(fact);
      }
      const button = wrapper.get(`[data-ui="programme.run.${action}"]`);
      expect(button.element.disabled).toBe(true);
      await wrapper.get('[data-ui="programme.check.operator"]').setValue('Operator A');
      await wrapper.get('[data-ui="programme.check.basket"]').setValue('B');
      await wrapper.get('[data-ui="programme.check.location"]').setValue('park');
      await wrapper.get('[data-ui="programme.check.confirmed"]').setValue(true);
      if (action === 'resume') await wrapper.get('[data-ui="programme.check.step"]').setValue('2');
      await button.trigger('click');
      await flushPromises();
      expect(request).toHaveBeenCalledWith('/api/programme', {
        action, job_id: 'new-run', approval, operator: 'Operator A', basket: 'B', location: 'park', confirmed: true,
        ...(action === 'resume' ? { step: 2 } : {}), request_id: expect.any(String), review_cursor: 'review-1',
      });
    } finally { wrapper.unmount(); }
  });
}

test('a technician-only operator check offers no resume or abort', async () => {
  request.mockResolvedValue({ recipe, run: { ...current, state: 'operator_check', operator_check: {
    reason: 'Unreadable journal', technician: true, location: null, basket: null, job_id: null, step: null,
  } } });
  const wrapper = mount(ProgrammePanel);
  try {
    await flushPromises();
    expect(wrapper.text()).toMatch(/call a technician/i);
    expect(wrapper.find('[data-ui="programme.run.resume"]').exists()).toBe(false);
    expect(wrapper.find('[data-ui="programme.run.abort"]').exists()).toBe(false);
  } finally { wrapper.unmount(); }
});

test('a changed review clears the physical confirmation before a delayed choice', async () => {
  let cursor = 'before';
  const approval = { boot: 'ipc', session: 'katana', stop_generation: 0 };
  const checkedRun = { ...current, state: 'operator_check', operator_check: {
    reason: 'Reply lost', location: 'park', basket: 'B', job_id: 'physical-job', step: 1,
  } };
  request.mockImplementation(async () => ({ recipe, run: checkedRun, approval, review_cursor: cursor }));
  const wrapper = mount(ProgrammePanel);
  try {
    await flushPromises();
    await wrapper.get('[data-ui="programme.check.operator"]').setValue('Operator A');
    await wrapper.get('[data-ui="programme.check.basket"]').setValue('B');
    await wrapper.get('[data-ui="programme.check.location"]').setValue('park');
    await wrapper.get('[data-ui="programme.check.confirmed"]').setValue(true);
    cursor = 'after';
    await new Promise(resolve => setTimeout(resolve, 700));
    await flushPromises();
    expect(wrapper.get('[data-ui="programme.check.confirmed"]').element.checked).toBe(false);
    expect(wrapper.get('[data-ui="programme.run.abort"]').element.disabled).toBe(true);
  } finally { wrapper.unmount(); }
});


test('a basket with unresolved original evidence can refresh its own record', async () => {
  let run = { ...current, state: 'aborted', operator_choices: [
    { choice: 'abort', operator: 'Inspector', basket: 'B', location: 'output', applied: true,
      evidence_note: 'The executor omitted the original job.' },
  ] };
  request.mockImplementation(async path => {
    if (path === '/api/programme?run_id=new-run') {
      run = { ...run, operator_choices: [{ ...run.operator_choices[0], evidence_note: null }],
        steps: [{ id: 'step-0', tank: 'tank-0', dwell_s: 34, air_s: null, plc: { cycle: 4 } }] };
    }
    return { recipe, run };
  });
  const wrapper = mount(ProgrammePanel);
  try {
    await flushPromises();
    expect(wrapper.text()).toContain('The executor omitted the original job.');
    await wrapper.get('[data-ui="programme.record.refresh"]').trigger('click');
    await flushPromises();
    expect(request).toHaveBeenCalledWith('/api/programme?run_id=new-run');
    expect(wrapper.text()).toContain('34 s');
    expect(wrapper.text()).not.toContain('The executor omitted the original job.');
  } finally { wrapper.unmount(); }
});

test('Programme Stop reaches the programme while Start is awaiting its reply', async () => {
  let completeStart;
  const started = new Promise(resolve => { completeStart = resolve; });
  const approval = { boot: 'ipc', session: 'katana', stop_generation: 0 };
  request.mockImplementation(async (_, body) => {
    if (body?.action === 'start') await started;
    return { recipe, run: null, approval, review_cursor: 'review-start' };
  });
  const wrapper = mount(ProgrammePanel);
  try {
    await flushPromises();
    await wrapper.get('[data-ui="programme.basket.id"]').setValue('pending-basket');
    await wrapper.get('[data-ui="programme.basket.ready"]').setValue(true);
    await wrapper.get('[data-action="start"]').trigger('click');
    await wrapper.get('[data-action="stop"]').trigger('click');
    expect(request).toHaveBeenCalledWith('/api/programme', expect.objectContaining({ action: 'stop' }));
    expect(wrapper.get('[data-action="start"]').element.disabled).toBe(true);
  } finally {
    completeStart();
    wrapper.unmount();
  }
});
