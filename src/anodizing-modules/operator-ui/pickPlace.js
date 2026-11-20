import { request } from './api';

export const terminal = job => ['done', 'failed', 'interrupted'].includes(job?.state);
export const jobPath = id => '/api/jobs/' + encodeURIComponent(id);

export function stored(key, fallback = '') {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}

export function store(key, value) {
  try {
    if (value == null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // The server keeps the operation record even when browser storage is blocked.
  }
}

export function savedObject(key) {
  try {
    return JSON.parse(stored(key, '{}')) ?? {};
  } catch {
    return {};
  }
}

export const sources = [
  ['project', 'Endpoint A teaching project folder'],
  ['calibration', 'Calibration JSON'],
  ['manifest', 'Marker manifest override'],
];
export const boundsFields = [
  ['scene', 'Reviewed collision scene path'],
  ['jointMin', 'Joint operating minimum (six degrees)'],
  ['jointMax', 'Joint operating maximum (six degrees)'],
  ['jointSource', 'Operating bounds source'],
  ['workMin', 'Workspace minimum XYZ (mm)'],
  ['workMax', 'Workspace maximum XYZ (mm)'],
  ['jointSpeed', 'Joint transit speed (degrees/s)', 1, 6],
];
export const boundsDefaults = {
  scene: '', jointMin: '', jointMax: '', jointSource: '',
  workMin: '-900, -900, 0', workMax: '900, 900, 1200', jointSpeed: 3,
};

export function boundsFrom(defaults) {
  return {
    scene: defaults.collision_scene,
    jointMin: defaults.joint_bounds_rad?.map(pair => pair[0] * 180 / Math.PI).join(', '),
    jointMax: defaults.joint_bounds_rad?.map(pair => pair[1] * 180 / Math.PI).join(', '),
    jointSource: defaults.joint_bounds_source,
    workMin: defaults.workspace_min_m?.map(value => value * 1000).join(', '),
    workMax: defaults.workspace_max_m?.map(value => value * 1000).join(', '),
    jointSpeed: defaults.joint_speed_deg_s,
  };
}

export function vector(text, size, name, scale = 1) {
  const value = String(text ?? '').trim();
  const numbers = value ? value.split(/[\s,]+/).map(item => Number(item) * scale) : [];
  if (numbers.length !== size || !numbers.every(Number.isFinite)) throw new Error(name + ' needs ' + size + ' finite values.');
  return numbers;
}

export function range(value, min, max, name, integer = false) {
  const number = value === '' || value == null ? NaN : Number(value);
  if (!Number.isFinite(number) || number < min || number > max || (integer && !Number.isInteger(number))) {
    throw new Error('Enter ' + name + ' from ' + min + ' to ' + max + (integer ? ' as a whole number.' : '.'));
  }
  return number;
}

export function bounds(settings) {
  const scene = settings.scene.trim();
  const source = settings.jointSource.trim();
  if (!scene || !source) throw new Error('Enter the reviewed collision scene and operating bounds source.');
  const low = vector(settings.jointMin, 6, 'Joint minimum', Math.PI / 180);
  const high = vector(settings.jointMax, 6, 'Joint maximum', Math.PI / 180);
  const minimum = vector(settings.workMin, 3, 'Workspace minimum', .001);
  const maximum = vector(settings.workMax, 3, 'Workspace maximum', .001);
  if (low.some((value, index) => value >= high[index]) || minimum.some((value, index) => value >= maximum[index])) {
    throw new Error('Each joint and workspace minimum must be below its maximum.');
  }
  return {
    collision_scene: scene, joint_bounds_source: source,
    joint_bounds_rad: low.map((value, index) => [value, high[index]]),
    workspace_min_m: minimum, workspace_max_m: maximum,
    joint_speed_deg_s: range(settings.jointSpeed, 1, 6, 'joint speed in degrees/s'),
  };
}

export function ownerReason(owner, active = true) {
  if (!owner) return 'Wait for service status or reload to reconnect.';
  if (owner.execution_uncertain) return 'Execution is uncertain. Reconcile the physical state and review the saved operation before a service restart.';
  if (owner.storage_error) return 'Job storage failed. Check service storage before starting another operation.';
  if (owner.closing) return 'The service is closing. Reconnect after it restarts.';
  if (owner.mcu_pending) return 'An MCU request is pending. Wait for its reply.';
  if (active && owner.active) return 'Another operation owns the service. Follow Current operation.';
  return '';
}

export function agvReason(motion, off) {
  if (!motion) return 'Wait for AGV status or reconnect on the AGV page.';
  if (off) return motion.reporting ? 'The AGV is reporting. Clear the switched-off declaration and check that it is parked.' : '';
  return motion.parked ? '' : motion.detail || 'Check that the AGV is parked.';
}

export function publicError(error) {
  if (error.status === 403) return 'Access refused. Check service permissions or reload to reconnect.';
  if (error.status === 404) return 'Request not found. Check the operation ID and service.';
  if (error.status >= 400 && error.status < 500) return 'Request refused. Check settings and saved evidence before a new request.';
  return 'Request outcome unconfirmed. Inspect its ID before sending another request.';
}

export async function finishJob(job, isOpen) {
  while (!terminal(job) && isOpen()) {
    const latest = await request(jobPath(job.id), null, 3000);
    if (latest.id !== job.id) throw new Error('Job identity changed');
    job = latest;
    if (!terminal(job)) await new Promise(resolve => setTimeout(resolve, 300));
  }
  return job;
}
