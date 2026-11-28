import { DOMWrapper } from '@vue/test-utils';

export async function selectOption(view, selector, label) {
  const target = view.get(selector);
  const input = target.element.matches('[role="combobox"]') ? target : target.get('[role="combobox"]');
  await target.trigger('click');
  const list = document.getElementById(input.attributes('aria-controls'));
  const option = Array.from(list?.querySelectorAll('[role="option"]') ?? [])
    .find(candidate => candidate.textContent.trim() === label);
  if (!option) throw new Error(`No option labelled "${label}" in ${selector}`);
  await new DOMWrapper(option).trigger('mousedown');
}
