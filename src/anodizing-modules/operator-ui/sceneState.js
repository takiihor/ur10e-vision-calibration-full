export function stationPosition(track, station) {
  const distance = track?.stations_m[station];
  if (distance == null) return null;
  const delta = track.end_m.map((value, axis) => value - track.start_m[axis]);
  const fraction = distance / Math.hypot(...delta);
  return track.start_m.map((value, axis) => value + delta[axis] * fraction);
}

function label(id, name, state, sample, anchor = null) {
  return {
    id, name, state, anchor, unknown: state === 'Unknown',
    age: sample?.age_ms == null ? 'Age unavailable' : (sample.age_ms / 1000).toFixed(1) + ' s ago',
  };
}

export function sceneLabels(config, variable, taughtTank) {
  const last = variable('agv.last_confirmed_station')?.value;
  const anchor = stationPosition(config?.track, last);
  const moving = variable('agv.moving');
  const target = variable('agv.target_station')?.value;
  const motion = moving?.value === true ? (target ? `Moving to ${target}, position not reported` : 'Moving, position not reported')
    : moving?.value === false ? 'Stopped' : 'Unknown';
  const joints = variable('robot.joints');
  const robot = joints?.value ? (joints.source === 'robot.simulator' ? 'Simulated joints' : 'Job joint sample') : 'Unknown';
  return [
    { ...label('agv', 'AGV', motion, moving, anchor),
      detail: anchor ? `Last confirmed station ${last}` : 'Unplaced' },
    { ...label('robot', 'Robot', robot, joints),
      detail: anchor && config?.robot_mount ? `Display reference · AGV station ${last}` : 'Unplaced' },
    ...(config?.tanks ?? []).map(tank => {
      const sample = tank.status_id && variable(tank.status_id);
      const state = typeof sample?.value === 'string' ? sample.value : 'Unknown';
      return { ...label(tank.id, tank.label, state, sample, tank.anchor_m), taught: tank.id === taughtTank };
    }),
  ];
}
