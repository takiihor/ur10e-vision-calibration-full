// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { showUiIds } from './ui-ids-overlay.js';

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

it('shows opt-in labels on rendered elements and updates when the route changes', async () => {
  document.body.innerHTML = '<main><button data-ui="agv.stop">Stop</button><button data-ui="agv.pause" hidden>Pause</button></main>';
  const [stop, pause] = document.querySelectorAll('button');
  const visible = { top: 30, right: 200, left: 10, bottom: 80, width: 190, height: 50 };
  vi.spyOn(stop, 'getBoundingClientRect').mockReturnValue(visible);
  vi.spyOn(pause, 'getBoundingClientRect').mockReturnValue({ width: 0, height: 0 });

  showUiIds('');
  expect(document.querySelector('.ui-ids-overlay')).toBeNull();

  showUiIds('?ui=ids');
  const overlay = document.querySelector('.ui-ids-overlay');
  expect(overlay.getAttribute('aria-hidden')).toBe('true');
  await vi.waitFor(() => expect(overlay.textContent).toBe('agv.stop'));

  stop.setAttribute('data-ui', 'line.stop');
  await vi.waitFor(() => expect(overlay.textContent).toBe('line.stop'));
});
