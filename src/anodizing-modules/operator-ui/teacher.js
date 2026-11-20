import { range, vector } from './pickPlace';

export const projectPath = project => '/api/teacher-project?' + new URLSearchParams({ project, evidence: '1' });
export const sessionPath = job => '/api/teacher-state?' + new URLSearchParams({ job });
export const filePath = (job, name) => '/api/teacher-file?' + new URLSearchParams({ job, name });
export const snapshotPath = (project, snapshot) => project && snapshot
  ? '/api/teacher-snapshot?' + new URLSearchParams({
    project, kind: snapshot.kind, capture_id: snapshot.id, sha256: snapshot.sha256,
  }) : '';
export const isTeacherMotion = action => ['validate_move', 'return_observation'].includes(action);
export const poseText = (pose, scale = 1000) => pose?.map((value, index) =>
  (index < 3 ? value * scale : value).toFixed(index < 3 && scale === 1000 ? 3 : 6)).join(', ');
export const degrees = joints => joints?.map(value => (value * 180 / Math.PI).toFixed(2)).join(', ');

const setupDefaults = {
  project: '', calibration: '', robot: '', tcp: '', camera: 'realsense', kind: 'charuco',
  board: '', manifest: '', dictionary: 'DICT_5X5_100', marker: 7, size: 80,
  refine: false, model: 'planar', approach: 100, shift: 100, rotation: 180, pixels: 5,
};

export function setupForm(settings = {}) {
  const target = settings.target || {};
  const limits = settings.limits || {};
  return {
    ...setupDefaults, project: settings.project ?? '', calibration: settings.calibration ?? '',
    robot: settings.robot_ip ?? '', tcp: poseText(settings.tcp) ?? '',
    camera: settings.camera_provider ?? 'realsense', model: settings.motion_model ?? 'planar',
    refine: settings.refine_before_pick ?? false, approach: settings.approach_mm ?? 100,
    shift: limits.max_shift_mm ?? 100, rotation: limits.max_rotation_deg ?? 180,
    pixels: settings.min_pixels_per_module ?? 5,
    kind: target.kind ?? 'charuco', board: target.board ?? '', manifest: target.board_manifest ?? '',
    dictionary: target.dictionary ?? 'DICT_5X5_100', marker: target.marker_id ?? 7, size: target.marker_length_mm ?? 80,
  };
}

export function teacherSettings(form, confirmed) {
  if (!form.project.trim() || !form.calibration.trim() || !form.robot.trim()) {
    throw new Error('Enter the project, calibration and robot address.');
  }
  const tcp = vector(form.tcp, 6, 'Active TCP').map((value, index) => index < 3 ? value / 1000 : value);
  if (form.kind === 'charuco' && (!form.board.trim() || !form.manifest.trim())) {
    throw new Error('Select a printed board or enter its name and manifest.');
  }
  if (form.kind === 'aruco' && !form.dictionary.trim()) throw new Error('Enter the marker dictionary.');
  const target = form.kind === 'charuco'
    ? { kind: form.kind, board: form.board.trim(), board_manifest: form.manifest.trim() }
    : { kind: form.kind, dictionary: form.dictionary.trim(), marker_id: range(form.marker, 0, 999, 'marker ID', true),
      marker_length_mm: range(form.size, 1, Infinity, 'marker size in mm') };
  return {
    project: form.project.trim(), calibration: form.calibration.trim(), robot_ip: form.robot.trim(), tcp,
    tcp_confirmed: confirmed, target, camera_provider: form.camera, motion_model: form.model,
    refine_before_pick: form.refine, approach_mm: range(form.approach, 1, 300, 'approach height in mm'),
    min_pixels_per_module: range(form.pixels, 3, 100, 'minimum pixels per module'),
    limits: {
      max_shift_mm: range(form.shift, 1, 500, 'allowed displacement in mm'),
      max_rotation_deg: range(form.rotation, 1, 180, 'allowed rotation in degrees'),
      max_tilt_change_deg: 10, max_height_change_mm: 10, max_tool_tilt_deg: 20,
    },
  };
}

export function captureTank(edit, value) {
  if (edit === 'keep') return {};
  if (edit === 'clear') return { tank: null };
  return { tank: /^Tank(?:0[1-9]|[1-9][0-9])$/.test(value) ? value :
    range(value, 1, 99, 'tank number', true) };
}
