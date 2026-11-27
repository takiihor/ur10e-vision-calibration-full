import { readFileSync } from 'node:fs';
import { Theme } from '@primeuix/themes';
import { afterAll, expect, it } from 'vitest';
import Alucard from './alucard.js';

it('uses the Alucard canvas, foreground and selection on both interfaces', () => {
  const css = readFileSync(new URL('./operator.css', import.meta.url), 'utf8');
  const legacy = readFileSync(new URL('../app/anodizing_web/web/teacher-style.css', import.meta.url), 'utf8');
  expect(css.match(/color-scheme:\s*(\w+)/g)).toEqual(['color-scheme: light']);
  expect(legacy).toMatch(/color-scheme:\s*light\b/);
  expect(Alucard.extend.operator).toMatchObject({ canvas: '#FFFBEB', text: '#1F1F1F', pressed: '#CFCFDE' });
  expect(legacy).toContain('#FFFBEB');
  expect(legacy).toContain('#1F1F1F');
  expect(legacy).toContain('#CFCFDE');
});

function luminance(color) {
  expect(color).toMatch(/^#[a-f\d]{6}$/i);
  const linear = color.slice(1).match(/../g).map(byte => {
    const channel = parseInt(byte, 16) / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
}

function contrast(first, second) {
  const [low, high] = [luminance(first), luminance(second)].sort((a, b) => a - b);
  return (high + 0.05) / (low + 0.05);
}

const report = [];

function checkRatio(scope, foreground, ink, background, surface, minimum) {
  const ratio = contrast(ink, surface);
  if (process.env.THEME_CONTRAST_REPORT) {
    report.push([scope, foreground, ink, background, surface, minimum, ratio.toFixed(2)].join(','));
  }
  expect(ratio, `${foreground} on ${background}`).toBeGreaterThanOrEqual(minimum);
}

afterAll(() => {
  if (report.length) console.log(['interface,foreground,color,background,color,minimum,ratio', ...report].join('\n'));
});

it('keeps legacy text, states and controls legible across the Alucard surfaces', () => {
  const css = readFileSync(new URL('../app/anodizing_web/web/teacher-style.css', import.meta.url), 'utf8');
  const colors = Object.fromEntries([...css.matchAll(/--alucard-([\w-]+):\s*(#[\da-f]{6});/gi)]
    .map(([, name, color]) => [name, color]));
  const surfaces = ['canvas', 'panel', 'raised'];
  const textPairs = {
    text: [...surfaces, 'selection'],
    muted: surfaces,
    action: surfaces,
    danger: [...surfaces, 'danger-soft'],
    warning: surfaces,
    good: [...surfaces, 'good-soft'],
  };
  for (const [ink, backgrounds] of Object.entries(textPairs)) {
    for (const background of backgrounds) {
      checkRatio('Legacy', ink, colors[ink], background, colors[background], 7);
    }
  }
  for (const fill of ['action', 'action-hover', 'action-pressed', 'danger', 'danger-hover', 'danger-pressed']) {
    checkRatio('Legacy button label', 'panel', colors.panel, fill, colors[fill], 7);
    for (const adjacent of surfaces) {
      checkRatio('Legacy button fill', fill, colors[fill], adjacent, colors[adjacent], 4.5);
    }
  }
  for (const edge of ['focus', 'control', 'warning', 'action', 'danger']) {
    for (const background of [...surfaces, 'selection']) {
      checkRatio('Legacy control boundary', edge, colors[edge], background, colors[background], 3);
    }
  }
  checkRatio('Legacy alarm boundary', 'danger', colors.danger, 'danger-soft', colors['danger-soft'], 3);
  checkRatio('Legacy current step', 'focus', colors.focus, 'raised', colors.raised, 3);
});

it('meets operator HCI contrast for text, control boundaries and focus in all used states', () => {
  Theme.setTheme({ preset: Alucard, options: { darkModeSelector: false } });
  const { primitive, semantic, global } = Theme.getCommon();
  const components = ['accordion', 'button', 'card', 'checkbox', 'datatable', 'dialog', 'inputtext',
    'message', 'panel', 'progressspinner', 'select', 'tag', 'textarea', 'timeline', 'toast', 'toggleswitch'];
  const css = [primitive.css, semantic.css, global.css, ...components.map(name => Theme.getComponent(name).css)].join('');
  const tokens = Object.fromEntries([...css.matchAll(/--p-([\w-]+):([^;{}]+)/g)].map(([, name, value]) => [name, value]));

  function resolve(name) {
    const value = tokens[name];
    expect(value, name).toBeDefined();
    const alias = value.match(/^var\(--p-([\w-]+)\)$/);
    return alias ? resolve(alias[1]) : value;
  }

  function check(foreground, backgrounds, minimum) {
    for (const background of backgrounds) {
      checkRatio('Vue', foreground, resolve(foreground), background, resolve(background), minimum);
    }
  }

  const canvas = 'operator-canvas';
  const panel = 'operator-panel';
  const raised = 'operator-raised';
  const pressed = 'operator-pressed';
  const surfaces = [canvas, panel, raised];
  check('operator-text', [...surfaces, pressed], 7);
  check('operator-muted', surfaces, 7);
  check('operator-action', surfaces, 7); // Links
  for (const tone of ['running', 'critical', 'warning', 'good', 'serious']) check('operator-' + tone, surfaces, 7);
  check('operator-serious', [panel], 3); // alarm edge
  check('operator-warning', [canvas, panel], 3); // Armed outline
  check('operator-action', [canvas, raised], 3); // current page indicator
  const sakai = readFileSync(new URL('./layout/sakai.css', import.meta.url), 'utf8');
  expect(sakai).toContain('--surface-overlay: var(--p-overlay-popover-background)');
  check('text-color', ['overlay-popover-background'], 7); // Sakai sidebar
  check('text-muted-color', ['overlay-popover-background'], 7);

  const buttonParents = { primary: surfaces, secondary: surfaces, danger: surfaces };
  for (const [variant, parents] of Object.entries(buttonParents)) {
    for (const state of ['', 'hover-', 'active-']) {
      const prefix = 'button-' + variant + '-' + state;
      check(prefix + 'color', [prefix + 'background'], 7);
      check(prefix + 'border-color', parents, variant === 'secondary' ? 3 : 4.5);
      if (variant !== 'secondary') check(prefix + 'background', parents, 4.5);
      if (variant === 'secondary') check(prefix + 'border-color', [prefix + 'background'], 3);
    }
  }
  check('operator-border', [...surfaces, pressed], 3); // native fields and disabled buttons
  check('operator-critical', [raised], 7); // disabled Stop label
  check('operator-critical', [panel, raised], 3); // disabled Stop outline
  check('operator-focus', [...surfaces, pressed], 3); // 3 px gap puts the ring against the surrounding surface
  check('button-text-secondary-color', [panel, 'button-text-secondary-hover-background'], 3); // dialog close icon
  check('button-link-color', surfaces, 7);

  check('checkbox-border-color', [canvas, panel, raised], 3);
  check('checkbox-hover-border-color', [canvas, panel, raised], 3);
  check('checkbox-focus-border-color', [canvas, panel, raised], 3);
  check('checkbox-focus-ring-color', [panel, raised], 3);
  check('checkbox-checked-border-color', [panel, raised], 3);
  check('checkbox-checked-hover-border-color', [panel, raised], 3);
  check('checkbox-checked-focus-border-color', [panel, raised], 3);
  check('checkbox-invalid-border-color', surfaces, 3);
  check('checkbox-icon-checked-color', ['checkbox-checked-background'], 3);
  check('checkbox-icon-checked-hover-color', ['checkbox-checked-hover-background'], 3);
  check('checkbox-checked-disabled-border-color', [panel, raised, 'checkbox-disabled-background'], 3);
  check('checkbox-icon-disabled-color', ['checkbox-disabled-background'], 3);

  check('card-color', ['card-background'], 7);
  check('card-subtitle-color', ['card-background'], 7);
  check('panel-color', ['panel-background'], 7);
  check('panel-header-color', ['panel-background'], 7);
  check('panel-border-color', ['panel-background'], 3);

  for (const name of ['inputtext', 'select', 'textarea']) {
    check(name + '-color', [name + '-background'], 7);
    check(name + '-placeholder-color', [name + '-background'], 7);
    check(name + '-disabled-color', [name + '-disabled-background'], 7);
    check(name + '-border-color', [name + '-background', panel], 3);
    check(name + '-hover-border-color', [name + '-background', panel], 3);
    check(name + '-focus-border-color', [name + '-background', panel], 3);
    check(name + '-invalid-border-color', [name + '-background', panel], 3);
    check(name + '-focus-ring-color', surfaces, 3);
  }
  check('select-dropdown-color', ['select-background'], 7);
  check('select-clear-icon-color', ['select-background'], 3);
  check('select-overlay-color', ['select-overlay-background'], 7);
  check('select-overlay-border-color', ['select-overlay-background'], 3);
  check('select-option-color', ['select-overlay-background'], 7);
  for (const state of ['focus', 'selected', 'selected-focus']) {
    check('select-option-' + state + '-color', ['select-option-' + state + '-background'], 7);
  }
  check('select-option-selected-background', ['select-overlay-background'], 3);
  check('select-option-selected-focus-background', ['select-overlay-background'], 3);

  for (const state of ['', 'hover-', 'disabled-', 'checked-', 'checked-hover-']) {
    check('toggleswitch-handle-' + state + 'background', ['toggleswitch-' + state + 'background'], 3);
    if (state !== 'disabled-') check('toggleswitch-' + state + 'border-color', surfaces, 3);
  }
  check('toggleswitch-focus-ring-color', surfaces, 3);
  for (const alternate of ['one', 'two', 'three', 'four']) {
    check('progressspinner-color-' + alternate, surfaces, 3);
  }

  for (const state of ['primary', 'secondary', 'success', 'info', 'warn', 'danger']) {
    check('tag-' + state + '-color', ['tag-' + state + '-background'], 7);
    check('tag-' + state + '-background', surfaces, 3);
  }
  for (const name of ['message', 'toast']) {
    for (const state of ['secondary', 'success', 'info', 'warn', 'error']) {
      const prefix = name + '-' + state + '-';
      check(prefix + 'color', [prefix + 'background'], 7);
      check(prefix + 'border-color', [prefix + 'background'], 3);
      check(prefix + 'close-button-focus-ring-color', [prefix + 'background', prefix + 'close-button-hover-background'], 3);
      if (name === 'message') check(prefix + 'simple-color', surfaces, 7);
      if (name === 'toast') check(prefix + 'detail-color', [prefix + 'background'], 7);
    }
  }

  for (const state of ['', 'hover-', 'active-', 'active-hover-']) {
    const prefix = 'accordion-header-';
    check(prefix + state + 'color', [prefix + state + 'background'], 7);
  }
  check('accordion-header-focus-ring-color', ['accordion-header-background'], 3);
  for (const state of ['', 'hover-', 'active-', 'active-hover-']) {
    check('accordion-header-toggle-icon-' + state + 'color', ['accordion-header-' + state + 'background'], 3);
  }
  check('timeline-event-marker-border-color', ['timeline-event-marker-background'], 3);
  check('timeline-event-marker-content-background', ['timeline-event-marker-background'], 3);
  check('datatable-header-cell-color', ['datatable-header-cell-background'], 7);
  check('datatable-header-cell-hover-color', ['datatable-header-cell-hover-background'], 7);
  check('datatable-sort-icon-color', ['datatable-header-cell-background'], 3);
  check('datatable-sort-icon-hover-color', ['datatable-header-cell-hover-background'], 3);
  check('datatable-row-color', ['datatable-row-background', 'datatable-row-striped-background'], 7);
  check('datatable-body-cell-border-color', ['datatable-row-background'], 3);

  check('dialog-color', ['dialog-background'], 7);
  check('dialog-border-color', ['dialog-background'], 3);
  const [, opacity] = resolve('mask-background').match(/^rgba\(0, 0, 0, ([\d.]+)\)$/);
  const brightestBehindMask = '#' + Math.round((1 - Number(opacity)) * 255).toString(16).padStart(2, '0').repeat(3);
  checkRatio('Vue', 'dialog-background', resolve('dialog-background'), 'lightest masked background', brightestBehindMask, 3);
});
