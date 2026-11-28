import { readFileSync } from 'node:fs'
import { createContext, runInContext } from 'node:vm'
import { JSDOM } from 'jsdom'
import { describe, expect, it } from 'vitest'

function asset(name) {
  return readFileSync(new URL('../app/anodizing_web/kiosk/' + name, import.meta.url), 'utf8')
}

function draw(state, age = 0) {
  const { document } = new JSDOM(asset('index.html')).window
  const context = createContext({
    document, performance, AbortSignal, fetch: () => new Promise(() => {}),
    setInterval() {}, setTimeout() {},
  })
  runInContext(asset('screen.js'), context)
  context.draw(state, age)
  const element = id => document.getElementById(id)
  return { state: document.body.dataset.state, element, text: id => element(id).textContent }
}

function fixture(pattern, changes = {}) {
  return {
    display_pattern: pattern, build: '701f8ff',
    job: null,
    device: {
      connected: true, status_age_s: 0.5,
      status: { current_station: 3, target_station: 4, battery_percent: 18, fault_code: 0 },
    },
    ...changes,
  }
}

describe('IPC status screen', () => {
  it('explains remote motion even when there is no executor job', () => {
    const screen = draw(fixture('moving'))
    expect(screen.state).toBe('moving')
    expect(screen.text('state-word')).toBe('Moving to station 4')
    expect(screen.text('instruction')).toBe('Stay clear')
    expect(screen.text('station')).toBe('3 → 4')
    expect(screen.text('battery-label')).toBe('Battery')
    expect(screen.text('battery')).toBe('18 %')
  })

  it.each([
    ['ready', 'Ready'],
    ['armed', 'Armed'],
    ['alarm', 'Stopped'],
    ['recovery', 'Connection lost'],
  ])('gives %s a plain state word and a symbol', (pattern, message) => {
    const screen = draw(fixture(pattern, { stop_latched: pattern === 'alarm' }))
    expect(screen.state).toBe(pattern)
    expect(screen.text('state-word')).toBe(message)
    expect(screen.text('icon')).not.toBe('')
  })

  it('reports a disconnected AGV without displaying old station or battery values', () => {
    const state = fixture('off')
    state.device.connected = false
    const screen = draw(state)
    expect(screen.state).toBe('off')
    expect(screen.text('state-word')).toBe('Not connected')
    expect(screen.text('station')).toBe('—')
    expect(screen.text('battery')).toBe('—')
  })

  it('replaces old status with a large stale message and greys out the readouts', () => {
    const screen = draw(fixture('moving'), 7.1)
    expect(screen.state).toBe('stale')
    expect(screen.text('state-word')).toBe('Screen not updating')
    expect(screen.text('instruction')).toBe('Ask the operator')
    expect(screen.text('stale-age')).toBe('STALE · 7 s')
    expect(screen.text('station')).toBe('—')
  })

  it('warns when feedback stops even if the screen can still read the executor', () => {
    const state = fixture('off')
    state.device.status_age_s = 5.1
    const screen = draw(state)
    expect(screen.state).toBe('stale')
    expect(screen.text('stale-age')).toBe('STALE · 5 s')
  })

  it.each([
    [25, 'charging', 'Charging', undefined],
    [15, 'in_use', 'In use', 'low'],
    [5, 'steady', 'Steady', 'critical'],
    [20, null, '', undefined],
    [10, null, '', 'low'],
  ])('shows the trend and low-battery warning at %s%%', (battery, trend, label, level) => {
    const state = fixture('ready')
    state.device.status.battery_percent = battery
    state.device.battery_trend = trend
    const screen = draw(state)
    expect(screen.text('battery')).toBe(battery + ' %')
    expect(screen.text('battery-trend')).toBe(label)
    expect(screen.element('charging-icon').hidden).toBe(trend !== 'charging')
    expect(screen.element('battery-warning').dataset.batteryLevel).toBe(level)
    expect(screen.element('battery-warning').hidden).toBe(!level)
    expect(screen.text('battery-warning')).toContain('Battery low. Please charge.')
    expect(screen.text('battery-warning')).toContain('Switch the AGV off, plug in its charger and stay nearby.')
    expect(screen.text('battery-estimate')).toContain('estimated from the battery level')
  })

  it('clears the trend and warning when AGV feedback ages out', () => {
    const state = fixture('ready')
    state.device.battery_trend = 'charging'
    const screen = draw(state, 2.6)
    expect(screen.text('battery-trend')).toBe('')
    expect(screen.element('battery-warning').hidden).toBe(true)
  })

  it('shows job outcomes in plain words without exposing job identifiers', () => {
    const screen = draw(fixture('ready', {
      last_job: { job_id: 'internal-job-id', state: 'done', outcome: 'succeeded' },
    }))
    expect(screen.text('job')).toBe('Last job finished')
  })
})

function luminance(hex) {
  const rgb = hex.match(/[\da-f]{2}/gi).map(value => parseInt(value, 16) / 255)
  return rgb.reduce((total, channel, index) =>
    total + [0.2126, 0.7152, 0.0722][index] *
      (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4), 0)
}

function contrast(first, second) {
  const values = [luminance(first), luminance(second)].sort((a, b) => b - a)
  return (values[0] + 0.05) / (values[1] + 0.05)
}

it('meets the contrast minimums in every state, including the footer and stale view', () => {
  const css = asset('screen.css')
  const color = name => css.match(new RegExp('--' + name + ':\\s*(#[a-f0-9]{6})\\b', 'i'))[1]
  for (const background of ['background', 'surface']) {
    for (const foreground of ['text', 'muted', 'stale', 'charging', 'in-use', 'battery-low', 'battery-critical']) {
      expect(contrast(color(foreground), color(background))).toBeGreaterThanOrEqual(7)
    }
  }
  const palettes = [...css.matchAll(/--banner-bg:\s*(#[\da-f]{6});\s*--banner-text:\s*(#[\da-f]{6})/gi)]
  expect(palettes).toHaveLength(4)
  for (const [, background, foreground] of palettes) {
    expect(contrast(foreground, background)).toBeGreaterThanOrEqual(4.5)
  }
})
