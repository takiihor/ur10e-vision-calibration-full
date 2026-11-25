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
