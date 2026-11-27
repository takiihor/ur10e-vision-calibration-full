// @vitest-environment jsdom
import { Vector3 } from 'three';
import { positionCallouts, robotFrameToDrawing, robotMount } from './sceneGeometry';
import { mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import { describe, expect, it } from 'vitest';
import ScenePage from './ScenePage.vue';
import { sceneLabels, stationPosition } from './sceneState';

const config = {
  track: { start_m: [2, 0, 1], end_m: [2, 0, 5], stations_m: { 1: 0, 2: 4 } },
  tanks: [{ id: 'Tank01', label: 'Tank 1', anchor_m: [2, 1, 1], status_id: 'line.FixtureStatus' }],
};
function variable(value, age_ms = 80, quality = 'fresh') {
  return { value, age_ms, quality };
}

describe('scene display', () => {
  it('locates by track distance and never uses a reported current station as confirmation', () => {
    const values = {
      'agv.moving': variable(true), 'agv.current_station': variable(2), 'agv.target_station': variable(2),
      'agv.last_confirmed_station': variable(1),
    };
    const [agv] = sceneLabels(config, id => values[id], null);
    expect(stationPosition(config.track, 2)).toEqual([2, 0, 5]);
    expect(agv).toMatchObject({ state: 'Moving to 2, position not reported', anchor: [2, 0, 1] });
    delete values['agv.last_confirmed_station'];
    expect(sceneLabels(config, id => values[id], null)[0].anchor).toBeNull();
  });

  it('retains a historical marker with stale words, and binds the taught marker to the selected tank', () => {
    const values = {
      'agv.moving': variable(null, 5001, 'stale'), 'agv.last_confirmed_station': variable(2),
      'line.FixtureStatus': variable(null, 3100, 'stale'),
    };
    const [agv, robot, tank] = sceneLabels(config, id => values[id], 'Tank01');
    expect(agv).toMatchObject({ state: 'Unknown', age: '5.0 s ago', anchor: [2, 0, 5], unknown: true });
    expect(robot.state).toBe('Unknown');
    expect(tank).toMatchObject({ state: 'Unknown', age: '3.1 s ago', taught: true, unknown: true });
  });

  it('maps vendor Z up to drawing Y up and applies the mount in the entered track frame', () => {
    const mount = robotMount(config.track, [2, 0, 5], { xyz_m: [.25, .5, 0], rpy_rad: [0, 0, 0] });
    const robotFrame = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 1], [0, 0, 0, 1]];
    const [x, y, z] = new Vector3().applyMatrix4(robotFrameToDrawing(mount, robotFrame)).toArray();
    expect(x).toBeCloseTo(2);
    expect(y).toBeCloseTo(1.5);
    expect(z).toBeCloseTo(5.25);
  });

  it('places nearby map labels in separate rows while keeping the anchors fixed', () => {
    const result = positionCallouts([
      { id: 'agv', x: 250, y: 210 }, { id: 'robot', x: 260, y: 205 }, { id: 'tank', x: 270, y: 200 },
    ], 500, 400);
    expect(result.map(({ id, x, y }) => [id, x, y]).sort()).toEqual([
      ['agv', 250, 210], ['robot', 260, 205], ['tank', 270, 200],
    ]);
    result.forEach((label, index) => {
      for (const other of result.slice(index + 1)) {
        expect(Math.abs(label.top - other.top) >= 104 || Math.abs(label.left - other.left) >= 160).toBe(true);
      }
    });
  });

  it('renders all state labels without a 3D view', () => {
    const page = mount(ScenePage, {
      props: { config, variable: id => id === 'line.FixtureStatus' ? variable('Occupied') : null },
      global: { plugins: [PrimeVue] },
    });
    expect(page.text()).toContain('Display only. No collision checking.');
    expect(page.text()).toContain('Tank 1');
    expect(page.text()).toContain('Occupied');
    expect(page.text()).toContain('AGV');
    expect(page.text()).toContain('Robot');
    expect(page.get('a[href="/preview/tool-envelope.html"]').text()).toContain('Legacy');
  });
});

it('labels both off-station anchors as historical when the AGV stops', () => {
  const values = { 'agv.moving': variable(false), 'agv.last_confirmed_station': variable(1) };
  const [agv, robot] = sceneLabels({ ...config, robot_mount: {} }, id => values[id], null);
  expect(agv).toMatchObject({ state: 'Stopped', detail: 'Last confirmed station 1' });
  expect(robot.detail).toBe('Display reference · Last confirmed station 1');
});
