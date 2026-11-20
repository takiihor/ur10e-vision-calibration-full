<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import Message from 'primevue/message';
import {
  AmbientLight, Box3, BufferGeometry, Color, DirectionalLight, DoubleSide, Float32BufferAttribute,
  Group, Line, LineBasicMaterial, Mesh, MeshStandardMaterial, PerspectiveCamera, Scene, Vector3, WebGLRenderer,
} from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { requestBytes } from './api';
import { labelPosition, loadKinematics, positionCallouts, robotFrameToDrawing, robotMount } from './sceneGeometry';

const props = defineProps({ config: Object, labels: Array, joints: Object, model: Object });
const canvas = ref();
const viewport = ref();
const visibleLabels = ref([]);
const frameMs = ref(null);
const message = ref('');
const robotMessage = ref('');

let camera, world, renderer, controls, observer, drawing, robot, trackLine;
let retired = false;
let modelRevision = 0;
let loadedModel, frames;

function tone(token) {
  return getComputedStyle(canvas.value).getPropertyValue('--p-operator-' + token).trim();
}

function dispose(object) {
  object?.traverse(item => {
    item.geometry?.dispose();
    const materials = Array.isArray(item.material) ? item.material : [item.material];
    for (const material of materials) {
      if (!material) continue;
      for (const value of Object.values(material)) if (value?.isTexture) value.dispose();
      material.dispose();
    }
  });
}

function draw() {
  if (!drawing || retired || message.value) return;
  const station = props.labels[0].anchor;
  const mount = station && props.config.robot_mount && robotMount(props.config.track, station, props.config.robot_mount);
  const sample = props.joints;
  robot.visible = Boolean(mount && loadedModel && sample?.value &&
    sample.model_sha256 === loadedModel.sha256 && props.model?.sha256 === loadedModel.sha256);
  if (robot.visible) {
    const joints = frames(sample.value, loadedModel.model);
    robot.children.forEach(part => {
      part.matrix.copy(robotFrameToDrawing(mount, joints[part.userData.depth]));
      part.matrixWorldNeedsUpdate = true;
    });
  }
  renderer.render(world, camera);
  const robotAnchor = mount && new Vector3().setFromMatrixPosition(mount).toArray();
  const { clientWidth: width, clientHeight: height } = viewport.value;
  const projected = props.labels.flatMap(label => {
    const anchor = label.id === 'robot' ? robotAnchor : label.anchor;
    const position = anchor && labelPosition(anchor, camera, width, height);
    return position ? [{ ...label, ...position }] : [];
  });
  visibleLabels.value = positionCallouts(projected, width, height);
}

function leaderStyle(item) {
  const dx = item.left - item.x;
  const dy = item.top - item.y;
  return {
    left: `${item.x}px`, top: `${item.y}px`, width: `${Math.hypot(dx, dy)}px`,
    transform: `rotate(${Math.atan2(dy, dx)}rad)`,
  };
}

function fit() {
  const bounds = new Box3().setFromObject(drawing);
  if (trackLine) bounds.expandByObject(trackLine);
  const radius = Math.max(bounds.getSize(new Vector3()).length() / 2, .1);
  bounds.getCenter(controls.target);
  const fov = Math.min(camera.fov * Math.PI / 180, 2 * Math.atan(Math.tan(camera.fov * Math.PI / 360) * camera.aspect));
  const distance = radius / Math.sin(fov / 2) * 1.25;
  camera.position.copy(controls.target).addScaledVector(new Vector3(1, .9, 1.35).normalize(), distance);
  camera.near = Math.max(distance / 10000, .001);
  camera.far = distance * 30;
  camera.updateProjectionMatrix();
  controls.update();
  draw();
}

function resize() {
  if (retired || message.value) return;
  const { clientWidth, clientHeight } = viewport.value;
  if (!clientWidth || !clientHeight) return;
  renderer.setSize(clientWidth, clientHeight, false);
  camera.aspect = clientWidth / clientHeight;
  camera.updateProjectionMatrix();
  draw();
}

function zoom(event) {
  if (!['+', '=', '-'].includes(event.key)) return;
  event.preventDefault();
  const factor = event.key === '-' ? 1.15 : 1 / 1.15;
  camera.position.sub(controls.target).multiplyScalar(factor).add(controls.target);
  controls.update();
  draw();
}

async function showModel(hash) {
  if (!drawing || retired) return;
  const revision = ++modelRevision;
  dispose(robot);
  robot.clear();
  loadedModel = null;
  robotMessage.value = '';
  if (!hash) {
    draw();
    return;
  }
  try {
    const [bytes, kinematics] = await Promise.all([requestBytes('/preview/ur10e-model.json'), loadKinematics()]);
    const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
      .map(byte => byte.toString(16).padStart(2, '0')).join('');
    if (digest !== hash || typeof kinematics !== 'function') throw new Error('Model identity changed.');
    const model = JSON.parse(new TextDecoder().decode(bytes));
    if (retired || revision !== modelRevision) return;
    model.bodies.forEach((body, index) => {
      const geometry = new BufferGeometry();
      geometry.setAttribute('position', new Float32BufferAttribute(body.vertices.flat(), 3));
      geometry.setIndex(body.faces.flat());
      geometry.computeVertexNormals();
      const material = new MeshStandardMaterial({ color: tone(index % 2 ? 'muted' : 'text'), side: DoubleSide });
      const mesh = new Mesh(geometry, material);
      mesh.userData.depth = body.depth;
      mesh.matrixAutoUpdate = false;
      robot.add(mesh);
    });
    loadedModel = { model, sha256: hash };
    frames = kinematics;
    draw();
  } catch {
    if (!retired && revision === modelRevision) {
      robotMessage.value = 'Robot model unavailable. Check the selected model and reload.';
    }
  }
}

function lostContext() {
  message.value = '3D view unavailable. Use the Scene labels list.';
}

onMounted(async () => {
  const start = performance.now();
  try {
    renderer = new WebGLRenderer({ canvas: canvas.value, antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    world = new Scene();
    world.background = new Color(tone('canvas'));
    world.add(new AmbientLight(0xffffff, 1.5));
    const light = new DirectionalLight(0xffffff, 2);
    light.position.set(1, 3, 2);
    world.add(light);
    camera = new PerspectiveCamera(45, 1, .01, 1000);
    controls = new OrbitControls(camera, canvas.value);
    controls.addEventListener('change', draw);
    controls.listenToKeyEvents(canvas.value);
    robot = new Group();
    world.add(robot);
    if (props.config.track) {
      const { start_m, end_m } = props.config.track;
      const geometry = new BufferGeometry().setFromPoints([new Vector3(...start_m), new Vector3(...end_m)]);
      trackLine = new Line(geometry, new LineBasicMaterial({ color: tone('action') }));
      world.add(trackLine);
    }
    observer = new ResizeObserver(resize);
    observer.observe(viewport.value);
    resize();
    const bytes = await requestBytes(props.config.drawing.url, 'default');
    const gltf = await new GLTFLoader().parseAsync(bytes, '');
    if (retired) {
      dispose(gltf.scene);
      return;
    }
    drawing = gltf.scene;
    if (new Box3().setFromObject(drawing).isEmpty()) throw new Error('Empty drawing.');
    world.add(drawing);
    fit();
    frameMs.value = Math.round(performance.now() - start);
    void showModel(props.model?.sha256);
  } catch {
    if (!retired) lostContext();
  }
});
watch(() => props.model?.sha256, showModel);
watch(() => [props.labels, props.joints], draw);
onUnmounted(() => {
  retired = true;
  observer?.disconnect();
  controls?.dispose();
  dispose(drawing);
  dispose(robot);
  dispose(trackLine);
  renderer?.dispose();
});
</script>

<template>
  <figure class="scene-figure" :data-frame-ms="frameMs">
    <Message v-if="message" severity="secondary">{{ message }}</Message>
    <template v-else>
      <div v-if="frameMs !== null" class="scene-view-controls">
        <Button type="button" label="Fit view" severity="secondary" @click="fit" />
        <small id="scene-keys">Drag to orbit. Right-drag or arrow keys to pan. Use the wheel or + and − to zoom.</small>
      </div>
      <p v-else role="status">Loading 3D drawing…</p>
    </template>
    <div v-show="!message" ref="viewport" class="scene-viewport">
      <canvas ref="canvas" aria-label="3D display of the line" :tabindex="frameMs === null ? -1 : 0"
        :aria-describedby="frameMs === null ? undefined : 'scene-keys'"
        @keydown="zoom" @webglcontextlost="lostContext" />
      <template v-for="item in visibleLabels" :key="item.id">
        <div class="scene-map-leader" :style="leaderStyle(item)" aria-hidden="true" />
        <div class="scene-map-anchor" :style="{ left: item.x + 'px', top: item.y + 'px' }" aria-hidden="true" />
      </template>
      <div v-for="item in visibleLabels" :key="item.id" class="scene-map-label" :class="{ 'scene-unknown': item.unknown }"
        :style="{ left: item.left + 'px', top: item.top + 'px' }" aria-hidden="true">
        <strong>{{ item.name }}</strong><template v-if="item.taught"> · Taught tank</template>
        <div>{{ item.state }}</div>
        <div>{{ item.age }}</div>
        <div v-if="item.detail">{{ item.detail }}</div>
      </div>
    </div>
    <Message v-if="robotMessage" severity="secondary">{{ robotMessage }}</Message>
  </figure>
</template>
