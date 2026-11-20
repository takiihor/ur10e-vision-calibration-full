import { readFileSync } from 'node:fs';
import { Theme } from '@primeuix/themes';
import { expect, it } from 'vitest';
import Nord from './nord.js';

it('uses a single dark color scheme for the operator pages and native form controls', () => {
  const css = readFileSync(new URL('./operator.css', import.meta.url), 'utf8');
  expect(css.match(/color-scheme:\s*(\w+)/g)).toEqual(['color-scheme: dark']);
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

it('meets operator HCI contrast for text, control boundaries and focus in all used states', () => {
  Theme.setTheme({ preset: Nord, options: { darkModeSelector: false } });
  const { semantic, global } = Theme.getCommon();
  const css = [semantic.css, global.css, ...['button', 'checkbox', 'dialog'].map(name => Theme.getComponent(name).css)].join('');
  const tokens = Object.fromEntries([...css.matchAll(/--p-([\w-]+):([^;{}]+)/g)].map(([, name, value]) => [name, value]));

  function resolve(name) {
    const value = tokens[name];
    expect(value, name).toBeDefined();
    const alias = value.match(/^var\(--p-([\w-]+)\)$/);
    return alias ? resolve(alias[1]) : value;
  }

  function check(foreground, backgrounds, minimum) {
    for (const background of backgrounds) {
      expect(contrast(resolve(foreground), resolve(background)), `${foreground} on ${background}`)
        .toBeGreaterThanOrEqual(minimum);
    }
  }

  const canvas = 'operator-canvas';
  const panel = 'operator-panel';
  const raised = 'operator-raised';
  const pressed = 'operator-pressed';
  const surfaces = [canvas, panel, raised];
  check('operator-text', surfaces, 4.5);
  check('operator-muted', surfaces, 4.5);
  check('operator-action', [panel], 4.5); // Home links
  check(canvas, ['operator-action'], 4.5); // text selection
  for (const tone of ['running', 'critical', 'warning', 'good']) check('operator-' + tone, surfaces, 4.5);
  check('operator-serious', [canvas], 4.5);
  check('operator-serious', [panel], 3); // alarm edge
  check('operator-warning', [canvas, panel], 3); // Armed outline
  check('operator-action', [canvas, raised], 3); // current page indicator

  const buttonParents = { primary: [panel], secondary: surfaces, danger: [panel] };
  for (const [variant, parents] of Object.entries(buttonParents)) {
    for (const state of ['', 'hover-', 'active-']) {
      const prefix = 'button-' + variant + '-' + state;
      check(prefix + 'color', [prefix + 'background'], 4.5);
      check(prefix + 'border-color', parents, 3);
      if (variant === 'secondary') check(prefix + 'border-color', [prefix + 'background'], 3);
    }
  }
  check('operator-border', [...surfaces, pressed], 3); // native fields and disabled buttons
  check('operator-critical', [panel, raised], 3); // disabled Stop outline
  check('operator-focus', surfaces, 3); // 3 px gap puts the ring against the surrounding surface

  check('checkbox-border-color', [canvas, panel, raised], 3);
  check('checkbox-hover-border-color', [canvas, panel, raised], 3);
  check('checkbox-focus-border-color', [canvas, panel, raised], 3);
  check('checkbox-focus-ring-color', [panel, raised], 3);
  check('checkbox-checked-border-color', [panel, raised], 3);
  check('checkbox-checked-hover-border-color', [panel, raised], 3);
  check('checkbox-checked-focus-border-color', [panel, raised], 3);
  check('checkbox-icon-checked-color', ['checkbox-checked-background'], 3);
  check('checkbox-icon-checked-hover-color', ['checkbox-checked-hover-background'], 3);
  check('checkbox-checked-disabled-border-color', [panel, raised, 'checkbox-disabled-background'], 3);
  check('checkbox-icon-disabled-color', ['checkbox-disabled-background'], 3);

  check('dialog-color', ['dialog-background'], 4.5);
  check('dialog-border-color', ['dialog-background'], 3);
  const [, opacity] = resolve('mask-background').match(/^rgba\(0, 0, 0, ([\d.]+)\)$/);
  const brightestBehindMask = '#' + Math.round((1 - Number(opacity)) * 255).toString(16).padStart(2, '0').repeat(3);
  expect(contrast(resolve('dialog-border-color'), brightestBehindMask)).toBeGreaterThanOrEqual(3);
});
