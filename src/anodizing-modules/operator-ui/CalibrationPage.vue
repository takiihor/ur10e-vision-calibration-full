<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import OperatorButton from './OperatorButton.vue';
import SnapshotViewer from './SnapshotViewer.vue';
import { request } from './api';
import { agvReason, jobPath, ownerReason, publicError, savedObject, store, stored, terminal } from './pickPlace';

const props = defineProps({ service: Object, agv: Object, stopOutcomes: Object });
const emit = defineEmits(['outcome', 'changed']);

const steps = [
  ['setup', 'Setup'], ['tool', 'Tool box'], ['plan', 'Plan'], ['capture', 'Capture'],
  ['results', 'Results'], ['verify', 'Verify'],
];
const actions = {
  camera: 'Capture board view', 'camera-preview': 'Preview camera (no pose)', plan: 'Inspect automatic plan',
  manual: 'Collect pendant-positioned views', collect: 'Start automatic collection',
  calibrate: 'Fit both', solve: 'Solve session', intrinsics: 'Fit intrinsics', simulate: 'Simulate calibration',
  'verify-capture': 'Collect fresh verification views', replay: 'Replay views for verification', verify: 'Verify fresh session',
};
const setupChecks = [
  'The camera mount is rigid. The board is fixed, flat and measured.',
  'The board is fully visible. The robot is stationary.',
  'The work area is clear. The emergency stop is accessible.',
];
const defaults = {
  session: 'sessions/sim-001', manifest: 'boards/calibration_stage2/board_manifest.json',
  board: 'calibration_stage2', serial: '', robot_ip: '', camera_provider: 'realsense',
  camera_fps: '30', camera_exposure_us: '5000', camera_gain_db: '0', intrinsics: '',
  calibration: '', verification: '', collision_scene: '', views: '14', tilt_deg: '25', roll_deg: '30',
};
const cameraFields = [
  ['serial', 'Camera serial'], ['robot_ip', 'Robot IP or hostname'],
  ['camera_fps', 'Camera frame rate (FPS)', 1, 120, 1],
];
const baslerFields = [
  ['camera_exposure_us', 'Exposure (µs)', 1, 1000000, 1], ['camera_gain_db', 'Gain (dB)', 0, 48, 0.1],
];
const planFields = [['views', 'Views', 6, 14, 1], ['tilt_deg', 'Tilt (degrees)', 5, 40, 1], ['roll_deg', 'Roll (degrees)', 0, 60, 1]];
const fields = {
  setup: [['session', 'Session folder'], ['manifest', 'Board manifest'], ['board', 'Board name']],
  plan: [['collision_scene', 'Collision scene JSON'], ...planFields],
  verify: [['calibration', 'Calibration JSON'], ['verification', 'Fresh verification session']],
};
const formKey = 'ano-calibration-form';
const jobKey = 'ano-job-calibration';
const pendingKey = 'ano-vue-calibration-pending';
const fitKey = 'ano-vue-calibration-fit';
const checks = ref([]);
const step = ref('setup');
const form = reactive({ ...defaults });
const saved = savedObject(formKey);
const profile = ref({});
const notice = ref('');
const tracking = ref(stored(pendingKey) || stored(jobKey));
const inspection = ref(tracking.value);
const unconfirmed = ref(stored(pendingKey));
const job = ref(null);
const fitted = ref(null);
const resultsApplied = new Set();
const outcomes = reactive({});
const control = ref('');
const clock = ref(Date.now());
const agvSample = ref(null);
const off = ref(false);
const editor = ref();
const editorMounted = ref(false);
const editorReady = ref(false);
const sceneScope = ref('');
const sceneDirty = ref(false);
let alive = true, polling = false, timer, revision = 0, edited = false, agvRevision = 0;
const owner = computed(() => props.service?.owner);
const editorActions = ['scene', 'scene-read', 'preview-calibration', 'pose'];
const active = computed(() => owner.value?.jobs?.find(item => item.id === owner.value.active && item.module === 'calibration' &&
  !editorActions.includes(item.calibration_action)));
const current = computed(() => Boolean(job.value && owner.value?.active === job.value.id && !terminal(job.value)));
const locked = computed(() => Boolean(control.value || active.value));
const basler = computed(() => form.camera_provider === 'basler');
const available = computed(() => props.service?.allow_auto_calibration && props.service.robot_model?.valid !== false);
const motion = computed(() => {
  const sample = agvSample.value;
  const link = props.agv?.state;
  if (!sample) return null;
  if (sample.connection_id != null && link?.sequence >= 0 &&
      (!link.connected || link.connection_id !== sample.connection_id || link.stop_generation !== sample.stop_generation)) return null;
  if (sample.status_age_s != null && sample.status_age_s + (clock.value - sample.requested) / 1000 >= 3) {
    return { ...sample, parked: false, reporting: false,
      detail: 'AGV status is stale. Check the radio or confirm the AGV is switched off.' };
  }
  return sample;
});

const events = computed(() => job.value?.events ?? []);
const observations = computed(() => events.value.filter(event => event.type === 'observation'));
const latestObservation = computed(() => observations.value.at(-1));
const imageEvidence = computed(() => {
  const result = job.value?.result;
  if (result?.image_jpeg_base64) return { image: result.image_jpeg_base64, snapshot: result.snapshot };
  return observations.value.findLast(event => event.image);
});
const fit = computed(() => job.value?.result?.result ?? events.value.findLast(event => event.type === 'result')?.result);
const intrinsic = computed(() => events.value.findLast(event => event.type === 'intrinsics'));
const verification = computed(() => events.value.findLast(event => event.type === 'verification'));
const preview = computed(() => job.value?.result?.image_jpeg_base64 ? job.value.result : null);
const pending = computed(() => current.value ? job.value.pending : null);
const age = computed(() => job.value?.created == null ? null :
  Math.max(0, Math.floor((job.value.finished ?? clock.value / 1000) - job.value.created)));
const logs = computed(() => events.value.flatMap(event => {
  if (event.type === 'log' || event.type === 'error') return (event.message || '').split(/\r?\n/);
  if (event.type === 'observation') return [`${event.label} · ${event.status} · ${event.corners} corners · ${event.reasons?.join(', ')}`];
  return [];
}).filter(line => line.trim()).slice(-8).join('\n'));
const nativeError = computed(() => {
  for (const event of [...events.value].reverse()) {
    if (['request', 'result', 'verification'].includes(event.type) || event.type === 'observation' && event.status === 'ok') break;
    if (event.type === 'error' || /^\s*(?:error:|RTDEControlInterface exception:|RTDEReceiveInterface boost system exception:|RTDEControlInterface: could not receive data)/im.test(event.message || '')) return true;
  }
  return false;
});
const details = computed(() => {
  if (!job.value) return '';
  const clean = { ...job.value, events: events.value.map(event => event.image ? { ...event, image: '[board view]' } : event) };
  if (preview.value) clean.result = { ...clean.result, image_jpeg_base64: '[board view]' };
  return JSON.stringify(clean, null, 2);
});
const fitResult = computed(() => fitted.value?.result);
const handoffReason = computed(() => control.value ? 'Wait for the current request.' : current.value
  ? 'Wait for the capture to finish.' : !fitResult.value || form.calibration.trim() !== fitResult.value.path
  ? 'Select the completed usable hand-eye output before continuing.' : '');
const simFit = r => Boolean(r?.simulation_truth || r?.camera?.simulated || r?.robot?.host === 'simulated');
const number = value => Number.isFinite(value) ? value.toFixed(3) : '—';

function edit(key, value) {
  edited = true;
  form[key] = String(value ?? '');
  if (key === 'camera_provider') form.camera_fps = basler.value ? '20' : '30';
  if (['board', 'manifest', 'robot_ip', 'serial', 'camera_provider', 'intrinsics'].includes(key)) checks.value = [];
  if (key === 'collision_scene') {
    sceneScope.value = '';
    sceneDirty.value = false;
  }
  store(formKey, JSON.stringify(form));
}

function loadProfile() {
  for (const key of Object.keys(defaults)) {
    const value = profile.value[key] ?? defaults[key];
    form[key] = String(value);
  }
  if (!['realsense', 'basler'].includes(form.camera_provider)) form.camera_provider = 'realsense';
  if (basler.value && profile.value.camera_fps == null) form.camera_fps = '20';
}

function resetProfile() {
  loadProfile();
  edit('collision_scene', form.collision_scene);
  checks.value = [];
  off.value = false;
  notice.value = 'Profile loaded. Check the board, camera and setup.';
}

const wantsMotion = action => ['collect', 'replay'].includes(action);
const wantsAuto = action => wantsMotion(action) || action === 'plan';
const wantsCapture = action => action === 'manual' || action === 'verify-capture';
const wantsCamera = action => ['camera', 'camera-preview'].includes(action);
const offline = action => ['simulate', 'solve', 'calibrate', 'intrinsics', 'verify'].includes(action);

function submitReason(action) {
  if (control.value) return 'Wait for the current request.';
  if (unconfirmed.value) return 'Inspect the unconfirmed job before starting again.';
  if (wantsCamera(action) && props.service && !props.service.allow_camera) return 'Camera access is disabled.';
  if (wantsCapture(action) && props.service && !props.service.allow_manual) return 'Pendant capture is disabled.';
  if (wantsAuto(action) && props.service && !props.service.allow_auto_calibration) return 'Automatic calibration is disabled.';
  if (wantsAuto(action) && props.service && !available.value) return 'The selected robot model is unavailable.';
  const reason = ownerReason(owner.value);
  if (reason && !(offline(action) && owner.value?.execution_uncertain && !owner.value.active && !owner.value.storage_error &&
      !owner.value.closing && !owner.value.mcu_pending)) return reason;
  if (!form.manifest.trim() || !form.board.trim()) return 'Enter the board manifest and board name in Setup.';
  if (!wantsCamera(action) && !form.session.trim()) return 'Enter the session folder in Setup.';
  if (wantsAuto(action) || wantsCapture(action)) {
    if (checks.value.length !== setupChecks.length) return 'Complete the Setup checks.';
    if (!form.robot_ip.trim() || /\s/.test(form.robot_ip.trim())) return 'Enter the robot IP or hostname in Setup.';
  }
  if (wantsCamera(action) || wantsAuto(action) || wantsCapture(action)) {
    if (!form.serial.trim()) return 'Enter the camera serial in Setup.';
    if (!validNumber(cameraFields[2])) return 'Enter a camera frame rate from 1 to 120 FPS.';
    if (basler.value && baslerFields.some(field => !validNumber(field))) return 'Check the Basler exposure and gain ranges.';
    if (basler.value && action !== 'camera-preview' && !form.intrinsics.trim() &&
        !['replay', 'verify-capture'].includes(action)) return 'Select measured Basler lens intrinsics in Setup.';
  }
  if ((wantsAuto(action) || wantsCapture(action) || action === 'simulate') && planFields.some(field => !validNumber(field))) {
    return 'Check the view count and angles in Plan.';
  }
  if (['verify', 'verify-capture', 'replay'].includes(action)) {
    if (!form.calibration.trim() || !form.verification.trim()) return 'Enter the calibration JSON and fresh verification folder.';
    if (form.session.trim() === form.verification.trim()) return 'Choose a verification folder separate from the fitting session.';
  }
  if (wantsAuto(action)) {
    if (!form.collision_scene.trim()) return 'Select a reviewed collision scene in Plan.';
    if (sceneDirty.value) return 'Review and save the edited collision scene before continuing.';
  }
  return wantsMotion(action) ? agvReason(motion.value, off.value) : '';
}

function validNumber([key, , min, max, increment]) {
  const value = Number(form[key]);
  return form[key].trim() && value >= min && value <= max && (increment !== 1 || Number.isInteger(value));
}

function payload(action, id) {
  const data = {
    action: action === 'verify-capture' ? 'manual' : action, request_id: id,
    session: (action === 'verify-capture' ? form.verification : form.session).trim(),
    manifest: form.manifest.trim(), board: form.board.trim(), serial: form.serial.trim(), robot_ip: form.robot_ip.trim(),
    camera_provider: form.camera_provider, camera_fps: action === 'camera' && !basler.value ? 15 : Number(form.camera_fps),
    intrinsics: form.intrinsics.trim(), views: Number(form.views), tilt_deg: Number(form.tilt_deg), roll_deg: Number(form.roll_deg),
  };
  if (action === 'verify-capture') data.fit_session = form.session.trim();
  if (['verify', 'verify-capture', 'replay'].includes(action)) {
    data.calibration = form.calibration.trim();
    data.verification = form.verification.trim();
  }
  if (wantsAuto(action)) data.collision_scene = form.collision_scene.trim();
  if (wantsMotion(action) && off.value) data.agv_off = true;
  if (basler.value && (wantsCamera(action) || wantsCapture(action) || wantsAuto(action))) {
    data.camera_exposure_us = Number(form.camera_exposure_us);
    data.camera_gain_db = Number(form.camera_gain_db);
  }
  return data;
}

function display(id, text, tone, choose = false) {
  const outcome = { text, tone, pending: tone === 'running' || tone === 'waiting' };
  outcomes[id] = outcome;
  if (alive) emit('outcome', outcome, choose);
}

async function send(id, run) {
  if (control.value) return;
  control.value = id;
  const started = Date.now();
  display(id, 'Sending…', 'running', true);
  const wait = setInterval(() => {
    display(id, `Waiting for service reply… ${Math.max(0, Math.ceil((8000 - Date.now() + started) / 1000))} s`, 'waiting');
  }, 1000);
  try {
    const outcome = await run();
    display(id, outcome, 'done');
  } catch (error) {
    notice.value = error.message;
    display(id, error.operatorMessage || publicError(error), 'failed');
  } finally {
    clearInterval(wait);
    control.value = '';
    if (alive) emit('changed');
  }
}

function setJob(found, apply = false) {
  if (!alive) return;
  job.value = found;
  notice.value = '';
  if (found.id === unconfirmed.value) {
    unconfirmed.value = '';
    store(pendingKey, null);
  }
  if (!apply || !terminal(found) || resultsApplied.has(found.id)) return;
  resultsApplied.add(found.id);
  const intrinsicResult = found.events?.findLast(event => event.type === 'intrinsics');
  if (intrinsicResult?.path && stored('ano-applied-intrinsics') !== found.id + ':' + intrinsicResult.path) {
    edit('intrinsics', intrinsicResult.path);
    store('ano-applied-intrinsics', found.id + ':' + intrinsicResult.path);
  }
  if (found.state !== 'done' || !found.result?.result || !found.result.path) return;
  const identity = found.id + ':' + found.result.path;
  if (stored('ano-applied-calibration') !== identity) {
    edit('calibration', found.result.path);
    store('ano-applied-calibration', identity);
  }
  updateFit(found);
}

function updateFit(found) {
  const candidate = found.result?.result;
  if (found.state === 'done' && candidate?.type === 'eye_in_hand' && ['good', 'acceptable'].includes(candidate.verdict)) {
    fitted.value = found;
    store(fitKey, found.id);
  }
}

function track(id) {
  revision++;
  tracking.value = inspection.value = id;
  store(jobKey, id);
}

function submit(action) {
  if (submitReason(action)) return;
  void send(action, async () => {
    const id = crypto.randomUUID();
    const body = payload(action, id);
    if (wantsMotion(action)) off.value = false;
    track(id);
    store(pendingKey, id);
    unconfirmed.value = id;
    try {
      const found = await request('/api/calibration', body);
      setJob(found, true);
      return 'Job accepted. Follow Current operation.';
    } catch (error) {
      if (error.status >= 400 && error.status < 500) {
        unconfirmed.value = '';
        store(pendingKey, null);
        tracking.value = job.value?.id ?? '';
        store(jobKey, tracking.value || null);
      }
      throw error;
    }
  });
}

async function readJob(id, apply) {
  const sequence = revision;
  const found = await request(jobPath(id), null, 3000);
  if (found.id !== id || found.module !== 'calibration') throw new Error('Choose a Calibration job.');
  if (sequence === revision && alive) {
    setJob(found, apply);
    if (!apply && found.result?.path === form.calibration.trim()) updateFit(found);
  }
}

function inspect() {
  void send('inspect', async () => {
    const id = inspection.value.trim();
    track(id);
    try {
      await readJob(id, false);
    } catch (error) {
      if (id === unconfirmed.value && error.status === 404 && owner.value && !owner.value.active) {
        unconfirmed.value = '';
        store(pendingKey, null);
        if (!job.value) track('');
        return 'The service has no record of this request. Review the setup before starting again.';
      }
      throw error;
    }
    return 'Saved operation loaded.';
  });
}

function answer(id, value) {
  if (!pending.value || control.value || !current.value) return;
  const binding = { job_id: job.value.id, request_id: pending.value.id, value };
  if (value === true && (!available.value || !wantsMotion(job.value.auto_action))) return;
  revision++;
  void send(id, async () => {
    const found = await request('/api/answer', binding);
    if (job.value?.id === binding.job_id) setJob(found);
    return 'Answer accepted. Follow Current operation.';
  });
}

function handoff() {
  if (handoffReason.value) return;
  void send('teach', async () => {
    const query = new URLSearchParams({ job: fitted.value.id, selection: form.calibration.trim() });
    const value = await request('/api/calibration-handoff?' + query);
    if (alive) {
      if (!store('ano-handoff-teaching', JSON.stringify(value))) {
        const message = 'Browser storage is unavailable. Enable it and select Continue to Teaching again.';
        throw Object.assign(new Error(message), { operatorMessage: message });
      }
      location.hash = '#/teaching';
    }
    return 'Calibration selected for Teaching.';
  });
}

async function tick() {
  clock.value = Date.now();
  if (polling || !owner.value || control.value) return;
  polling = true;
  try {
    if (active.value && tracking.value !== active.value.id) track(active.value.id);
    if (tracking.value && (!terminal(job.value) || tracking.value !== job.value?.id)) {
      await readJob(tracking.value, !unconfirmed.value || tracking.value === unconfirmed.value);
    }
  } catch (error) {
    if (alive) notice.value = error.status === 404 ? 'Job not found. Inspect its ID.' : 'Job updates unavailable. Check the service and reload.';
  } finally {
    polling = false;
  }
}

async function loadAgv() {
  const requested = Date.now();
  const turn = ++agvRevision;
  try {
    const result = await request('/api/agv-robot-motion?cached=1', null, 3000);
    if (turn !== agvRevision || !alive) return;
    agvSample.value = { ...result, requested };
  } catch {
    if (turn === agvRevision) agvSample.value = null;
  }
}

function postEditor(type, values) {
  editor.value?.contentWindow?.postMessage({ type, ...values }, location.origin);
}

function editorMessage({ origin, source, data }) {
  if (origin !== location.origin || source !== editor.value?.contentWindow || !data) return;
  if (data.type === 'calibration-scene-ready') {
    editorReady.value = true;
    return;
  }
  if (data.type === 'calibration-escape') window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', cancelable: true }));
  if (data.type === 'calibration-scene-dirty') sceneDirty.value = true;
  if (data.type === 'calibration-scene-loaded' && data.selection === form.collision_scene.trim()) {
    sceneScope.value = data.scope;
    sceneDirty.value = data.dirty === true;
  }
  if (data.type === 'calibration-scene-saved' && typeof data.path === 'string' && data.path.startsWith('/')) {
    edit('collision_scene', data.path);
    sceneScope.value = data.scope;
  }
}

function selectStep(id) {
  step.value = id;
  if (id === 'tool') editorMounted.value = true;
}

watch(() => [editorReady.value, form.collision_scene], ([ready, path]) => {
  if (ready) postEditor('calibration-scene-select', { path: path.trim() });
});
watch(() => [editorReady.value, form.calibration], ([ready, path]) => {
  if (ready) postEditor('calibration-preview-select', { path: path.trim() });
});
watch(() => [editorReady.value, form.robot_ip], ([ready, robot]) => {
  if (ready) postEditor('calibration-robot-select', { robot: robot.trim() });
});
watch([() => props.agv?.state.connected, () => props.agv?.state.connection_id, () => props.agv?.state.stop_generation], () => {
  off.value = false;
  void loadAgv();
});
watch(() => owner.value?.active, () => void tick());

onMounted(async () => {
  for (const key of Object.keys(defaults)) if (typeof saved[key] === 'string') form[key] = saved[key];
  window.addEventListener('message', editorMessage);
  void tick();
  void loadAgv();
  timer = setInterval(() => {
    void tick();
    if (['plan', 'capture', 'verify'].includes(step.value)) void loadAgv();
  }, 1000);
  void request('/api/calibration-defaults').then(value => {
    if (!alive) return;
    profile.value = value;
    if (!edited && !Object.keys(saved).length) loadProfile();
  }).catch(() => { if (alive) notice.value = 'Calibration profile unavailable. Enter the setup fields or reload.'; });
  const id = stored(fitKey);
  if (id && id !== tracking.value) {
    try {
      const found = await request(jobPath(id));
      if (alive && !fitted.value && found.id === id && found.module === 'calibration') updateFit(found);
    } catch { /* The operator can reopen the saved fit by job ID. */ }
  }
});
onUnmounted(() => {
  alive = false;
  clearInterval(timer);
  window.removeEventListener('message', editorMessage);
});
</script>

<template>
  <div class="calibration-workspace">
    <div class="calibration-workflow">
      <nav class="calibration-steps actions" aria-label="Calibration steps">
        <OperatorButton v-for="[id, label] in steps" :key="id" :id="'cal-step-' + id" :label="label"
          :current="step === id" :primary="step === id" @press="selectStep(id)" />
      </nav>
      <section class="panel calibration-step" :aria-label="steps.find(([id]) => id === step)[1]">
        <p v-if="step === 'setup'">Paths are relative to the IPC data directory. Use a fixed, measured board and confirm the active TCP.</p>
        <p v-if="step === 'plan'">Plan reads controller feedback and checks the selected collision scene. Automatic collection asks for approval before each phase.</p>
        <p v-if="step === 'capture'">Stop the robot before capturing each pendant-positioned view. Select Finish collection when the views are ready to fit.</p>
        <p v-if="step === 'results'">Fit the recorded session or render a simulated calibration. Simulation does not qualify hardware.</p>
        <p v-if="step === 'verify'">Collect a separate session with the board and camera fixed. Review replay requests in Current operation.</p>
        <div v-if="fields[step]" class="calibration-fields">
          <label v-for="[key, label, min, max, increment] in fields[step]" :key="key" :for="'cal-' + key">{{ label }}
            <InputText :id="'cal-' + key" :model-value="form[key]" :type="min == null ? 'text' : 'number'"
              :min="min" :max="max" :step="increment" :readonly="locked" autocomplete="off" @update:model-value="edit(key, $event)" />
          </label>
        </div>
        <template v-if="step === 'setup'">
          <div class="calibration-fields">
            <label for="cal-camera_provider">Camera
              <select id="cal-camera_provider" :value="form.camera_provider" :disabled="locked" @change="edit('camera_provider', $event.target.value)">
                <option value="realsense">RealSense</option><option value="basler">Basler a2A2448-23gmBAS</option>
              </select>
            </label>
            <label v-for="[key, label, min, max, increment] in [...cameraFields, ...(basler ? baslerFields : [])]"
              :key="key" :for="'cal-' + key">{{ label }}
              <InputText :id="'cal-' + key" :model-value="form[key]" :type="min == null ? 'text' : 'number'"
                :min="min" :max="max" :step="increment" :readonly="locked" autocomplete="off" @update:model-value="edit(key, $event)" />
            </label>
            <label for="cal-intrinsics">Intrinsics JSON
              <InputText id="cal-intrinsics" :model-value="form.intrinsics" :readonly="locked" autocomplete="off" @update:model-value="edit('intrinsics', $event)" />
            </label>
          </div>
          <p v-if="basler">Use the mounted Basler lens intrinsics. Check focus, exposure and gain in camera preview.</p>
          <div class="calibration-checks">
            <label v-for="(label, index) in setupChecks" :key="label" class="check">
              <input :id="'cal-ready-' + index" v-model="checks" type="checkbox" :value="label"> {{ label }}
            </label>
          </div>
          <OperatorButton v-if="Object.keys(profile).length" id="cal-profile" label="Load IPC calibration profile"
            :why="locked ? 'Wait for the current operation before loading a profile.' : ''" @press="resetProfile" />
          <p>Board capture checks for a usable pose. Camera preview checks image quality and marker visibility without intrinsics.</p>
        </template>
        <div v-if="editorMounted" v-show="step === 'tool'" class="calibration-tool">
          <p>The tool box is saved in the collision scene. The camera graphic is schematic.</p>
          <iframe ref="editor" title="Tool box and collision scene editor" src="/preview/tool-envelope.html?view=calibration" />
          <a href="/preview/tool-envelope.html" target="_blank" rel="noopener">Open full collision editor</a>
        </div>
        <template v-if="step === 'plan'">
          <p>{{ props.service?.robot_model?.valid ? (props.service.robot_model.nominal ? 'Nominal robot model' : 'Selected robot model') :
            'Robot model status unavailable.' }}</p>
          <p v-if="props.service?.robot_model?.sha256" class="calibration-path">Model SHA-256 {{ props.service.robot_model.sha256 }}</p>
          <p>{{ sceneDirty ? 'Tool editor has unsaved changes.' : sceneScope === 'supervised_self' ?
            'Selected scene covers the arm and attached tools only. External objects are outside this scope.' : sceneScope === 'cell' ?
            'Selected scene uses cell scope. Check external objects and coverage.' : 'Open Tool box to review the selected scene scope.' }}</p>
        </template>
        <template v-if="step === 'capture' || step === 'verify'">
          <label class="check"><input id="cal-agv-off" v-model="off" type="checkbox">
            AGV is switched off (operator declaration for the next automatic start).</label>
          <p role="status">{{ agvReason(motion, off) || (off ? 'AGV switched off · operator declaration.' : motion?.detail) }}</p>
        </template>
        <div v-if="step !== 'tool'" class="actions calibration-actions">
          <OperatorButton v-for="action in { setup: ['camera', 'camera-preview'], plan: ['plan'], capture: ['manual', 'collect'],
            results: ['calibrate', 'solve', 'intrinsics', 'simulate'], verify: ['verify-capture', 'replay', 'verify'] }[step]"
            :key="action" :id="'cal-' + action" :label="actions[action]" :why="submitReason(action)"
            :outcome="outcomes[action]" @press="submit(action)" />
          <OperatorButton v-if="step === 'results'" id="cal-teach" label="Continue to Teaching" :why="handoffReason"
            :outcome="outcomes.teach" @press="handoff" />
        </div>
        <p v-if="step === 'results' && simFit(fitResult?.result)">Simulation · use the selected file for offline Teaching.</p>
      </section>
      <p class="actions">
        <a class="page-link" href="#/tests/robot">Open Robot steps</a>
        <a class="page-link" href="/calibration">Legacy Calibration</a>
      </p>
      <details class="panel calibration-details">
        <summary>Saved job details</summary>
        <label for="cal-job-id">Calibration job ID
          <InputText id="cal-job-id" v-model="inspection" autocomplete="off" />
        </label>
        <OperatorButton id="cal-inspect" label="Show job" :why="control ? 'Wait for the current request.' :
          active && inspection.trim() !== active.id ? 'Follow the active Calibration job first.' :
          !inspection.trim() ? 'Enter a calibration job ID.' : ''" :outcome="outcomes.inspect" @press="inspect" />
        <pre v-if="details">{{ details }}</pre>
        <p v-else>No job selected.</p>
      </details>
    </div>
    <aside class="panel calibration-operation" aria-labelledby="cal-operation-title">
      <h2 id="cal-operation-title">Current operation</h2>
      <p v-if="!service" role="status">Service updates unavailable. Check the connection and reload.</p>
      <p v-else-if="!job" role="status">No job selected.</p>
      <template v-if="job">
        <p id="cal-job-state" role="status" :data-state="job.state === 'done' ? 'done' : terminal(job) ? 'failed' : 'running'">
          {{ ({done: 'Finished', failed: 'Failed', interrupted: 'Interrupted', running: 'Running', waiting: 'Waiting for operator'})[job.state] || job.state }}
          <template v-if="age != null"> · {{ Math.floor(age / 60) }} min {{ age % 60 }} s</template>
        </p>
        <p class="calibration-path">{{ actions[job.calibration_action] || 'Calibration job' }} · {{ job.id }}</p>
        <Message v-if="job.stop_requested" severity="warn">Stop requested. Check the machine for confirmation.</Message>
        <Message v-if="(job.error || nativeError) && job.state !== 'done'" severity="error">
          Review the saved job and recent messages before another attempt.
        </Message>
        <Message v-if="job.agv_interruption?.alarm" severity="error">{{ job.agv_interruption.alarm }}</Message>
        <p v-if="job.auto_scene">{{ job.auto_scene.collision_scope === 'supervised_self' ?
          'Accepted scene covers the arm and attached tools only. External objects are outside this scope.' :
          'Accepted scene uses cell collision scope.' }}</p>
        <template v-if="pending">
          <h3>Operator request</h3>
          <p class="calibration-prompt">{{ pending.summary }}</p>
          <div v-if="pending.kind === 'manual' && !job.auto_action" :key="pending.id" class="actions">
            <OperatorButton id="cal-capture-view" label="Capture view" :why="control ? 'Wait for the current request.' : ''"
              :outcome="outcomes['capture-view']" @press="answer('capture-view', '')" />
            <OperatorButton id="cal-finish" label="Finish collection" :why="control ? 'Wait for the current request.' : ''"
              :outcome="outcomes.finish" @press="answer('finish', 'q')" />
          </div>
          <div v-if="pending.kind === 'motion'" :key="pending.id" class="actions">
            <OperatorButton id="cal-approve" label="Approve phase" :why="control ? 'Wait for the current request.' :
              !available || !wantsMotion(job.auto_action) ? 'This job is not authorized for automatic motion.' : ''"
              :outcome="outcomes.approve" @press="answer('approve', true)" />
            <OperatorButton id="cal-cancel" label="Cancel phase" :why="control ? 'Wait for the current request.' : ''"
              :outcome="outcomes.cancel" @press="answer('cancel', false)" />
          </div>
        </template>
        <p v-if="latestObservation" role="status">
          {{ latestObservation.status === 'ok' ? 'View accepted' : 'View rejected' }} · {{ latestObservation.corners }} corners ·
          {{ observations.filter(event => event.status === 'ok').length }} accepted /
          {{ observations.filter(event => event.status !== 'ok').length }} rejected
          <template v-if="latestObservation.rmse_px != null"> · RMSE {{ number(latestObservation.rmse_px) }} px</template>
        </p>
        <p v-if="preview?.preview_only">No pose · Camera preview · {{ preview.charuco_count }} corners</p>
        <p v-else-if="preview">{{ preview.usable ? 'Board view usable' : 'Board view rejected' }} · {{ preview.observation?.charuco_ids?.length ?? 0 }} corners</p>
        <p v-if="preview?.camera?.exposure_time_us != null">{{ number(preview.camera.exposure_time_us) }} µs · {{ number(preview.camera.gain_db) }} dB</p>
        <SnapshotViewer v-if="imageEvidence" label="Board observation" :image="imageEvidence.image" :snapshot="imageEvidence.snapshot" />
        <template v-if="fit">
          <h3>Hand-eye result · {{ fit.verdict }} <template v-if="simFit(fit)">· Simulation</template></h3>
          <p>{{ fit.metrics?.sample_count ?? '—' }} samples · Reprojection RMSE {{ number(fit.metrics?.reprojection_rmse_px) }} px</p>
          <p>Maximum consistency error {{ number(fit.metrics?.consistency?.position_max_mm) }} mm /
            {{ number(fit.metrics?.consistency?.rotation_max_deg) }} degrees</p>
          <p v-for="warning in fit.warnings" :key="warning">{{ warning }}</p>
          <p v-if="job.result?.path" class="calibration-path">Hand-eye JSON · {{ job.result.path }}</p>
          <p v-if="job.result?.calibration_sha256" class="calibration-path">SHA-256 {{ job.result.calibration_sha256 }}</p>
        </template>
        <p v-if="intrinsic?.path" class="calibration-path">Intrinsics JSON · {{ intrinsic.path }}</p>
        <p v-if="verification?.report" role="status">
          Verification {{ verification.report.passed ? 'PASS' : 'FAIL' }}{{ verification.report.simulated ? ' · Simulation' : '' }}
        </p>
        <p v-if="verification?.path" class="calibration-path">Verification JSON · {{ verification.path }}</p>
        <p v-if="job.result?.session" class="calibration-path">Session · {{ job.result.session }}</p>
        <p v-if="terminal(job) && stopOutcomes?.['calibration/' + job.id]" role="status">{{ stopOutcomes['calibration/' + job.id].text }}</p>
      </template>
      <p v-if="unconfirmed" role="status">Request outcome unconfirmed. Inspect job {{ unconfirmed }} before starting again.</p>
      <details v-if="notice || logs || job?.error"><summary>Recent messages</summary>
        <pre>{{ [notice, job?.error, logs].filter(Boolean).join('\n') }}</pre>
      </details>
    </aside>
  </div>
</template>
