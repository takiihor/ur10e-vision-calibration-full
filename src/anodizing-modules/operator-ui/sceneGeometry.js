import { Euler, Matrix4, Vector3 } from 'three';

export function robotMount(track, station, mount) {
  const heading = -Math.atan2(track.end_m[2] - track.start_m[2], track.end_m[0] - track.start_m[0]);
  const base = new Matrix4().makeRotationY(heading).setPosition(...station);
  const rotation = new Euler(...mount.rpy_rad, 'ZYX');
  return base.multiply(new Matrix4().makeRotationFromEuler(rotation).setPosition(...mount.xyz_m));
}

export function robotFrameToDrawing(mount, frame) {
  return mount.clone().multiply(new Matrix4().makeRotationX(-Math.PI / 2)).multiply(new Matrix4().set(...frame.flat()));
}

export function labelPosition(anchor, camera, width, height) {
  const point = new Vector3(...anchor).project(camera);
  if (Math.abs(point.x) > 1 || Math.abs(point.y) > 1 || Math.abs(point.z) > 1) return null;
  return { x: width * (1 + point.x) / 2, y: height * (1 - point.y) / 2 };
}

// shortcut: dense or small views can run out of callout slots; the complete label list stays available.
export function positionCallouts(anchors, width, height) {
  const columns = Math.max(1, Math.floor(width / 160));
  const rows = Math.max(1, Math.floor(height / 104));
  const slots = Array.from({ length: columns * rows }, (_, index) => ({
    left: (index % columns + .5) * width / columns,
    top: (Math.floor(index / columns) + .5) * height / rows,
  }));
  return anchors.slice(0, slots.length).map(anchor => {
    slots.sort((a, b) => distance(a, anchor) - distance(b, anchor));
    return { ...anchor, ...slots.shift() };
  });
}

function distance(slot, anchor) {
  return Math.hypot(slot.left - anchor.x, slot.top - anchor.y + 32);
}

let kinematics;
export function loadKinematics() {
  if (typeof window.robotFrames === 'function') return Promise.resolve(window.robotFrames);
  kinematics ??= new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = '/preview/robot-preview.js';
    script.onload = () => resolve(window.robotFrames);
    script.onerror = () => reject(new Error('Robot kinematics unavailable.'));
    document.head.append(script);
  }).catch(error => {
    kinematics = null;
    throw error;
  });
  return kinematics;
}
