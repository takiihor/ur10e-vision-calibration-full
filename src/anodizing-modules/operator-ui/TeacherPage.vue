<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import Checkbox from 'primevue/checkbox';
import InputText from 'primevue/inputtext';
import Select from 'primevue/select';
import Message from 'primevue/message';
import OperatorButton from './OperatorButton.vue';
import PickPlaceFields from './PickPlaceFields.vue';
import PickPlaceJob from './PickPlaceJob.vue';
import SnapshotViewer from './SnapshotViewer.vue';
import { downloadBlob, request, requestDownload } from './api';
import {
  agvReason, bounds, boundsDefaults, boundsFields, boundsFrom, finishJob, jobPath, ownerReason, publicError,
  range, savedObject, store, stored, terminal, vector,
} from './pickPlace';
import {
  captureTank, degrees, filePath, isTeacherMotion, poseText, projectPath, sessionPath, setupForm, snapshotPath, teacherSettings,
} from './teacher';

const props = defineProps({ service: Object, agv: Object, stopId: String, manualStopId: String });
const emit = defineEmits(['outcome', 'changed']);
const owner = computed(() => props.service?.owner);
const projectKey = 'ano-vue-teacher-project';
const jobKey = 'ano-vue-teacher-request';
const last = savedObject(jobKey);
const form = reactive(setupForm({ project: stored(projectKey) }));
const motionEdits = new Set();
const historyImages = reactive(new Set());
const motionForm = reactive({ ...boundsDefaults, scene: stored('ano-vue-teacher-scene'), returnSpeed: 3, verticalSpeed: 10 });
const advancedFields = [
  ['approach', 'Approach height above pick (mm)', 1, 300], ['shift', 'Allowed displacement (mm)', 1, 500],
  ['rotation', 'Allowed rotation (degrees)', 1, 180], ['pixels', 'Minimum pixels per module', 3, 100],
];
const defaults = ref(null);
const details = ref('');
const setupNotice = ref('');
const saved = ref(null);
const native = ref(null);
const job = ref(null);
const tracked = ref(last.id || '');
const inspectionId = ref(last.id || '');
const pendingId = ref('');
const manual = ref(null);
const manualId = ref(stored('ano-manual-inspect-id'));
const manualProblem = ref('');
const agvMotion = ref(null);
const travel = ref(null);
const live = ref(null);
const capability = ref(null);
const clock = ref(Date.now());
const sessionId = ref('');
const exportId = ref('');
const editing = ref(true);
const running = ref('');
const confirmed = ref(false);
const markerFixed = ref(false);
const folded = ref(false);
const station = ref('');
const tankMode = ref('keep');
const tankValue = ref('');
const imageReady = ref(null);
const measured = ref('');
const outcomes = reactive({});
const approvals = reactive({ validate_move: false, return_observation: false, vertical_descent: false });
const switchedOff = reactive({ validate_move: false, return_observation: false, vertical_descent: false });
const cameras = [{ label: 'RealSense', value: 'realsense' }, { label: 'Basler', value: 'basler' }];
const targets = [{ label: 'ChArUco board', value: 'charuco' }, { label: 'Single ArUco marker', value: 'aruco' }];
const models = [{ label: 'XY + yaw · fixed support height', value: 'planar' }, { label: 'Full 3D pose', value: 'full' }];
const tankOptions = [{ label: 'Keep saved tank', value: 'keep' }, { label: 'Record a tank', value: 'set' },
  { label: 'Clear saved tank', value: 'clear' }];
const downloads = [
  ['grasp_pick.json', 'Marker-to-pick relationship'], ['vision.json', 'Vision configuration'],
  ['target.json', 'Pick pose JSON'], ['target.script', 'URScript variables'], ['pick_target.json', 'Full report'],
];
let alive = true, timer, polling = false, supervisedId = '';
const formRevision = ref(0);

const project = computed(() => native.value?.project || saved.value);
const flow = computed(() => project.value?.flow || {});
const validation = computed(() => native.value?.project?.validation);
const history = computed(() => project.value?.validation_history || []);
const alignment = computed(() => native.value?.descent_alignment);
const ownerBlock = computed(() => ownerReason(owner.value));
const busy = computed(() => running.value ? 'Wait for the current request.' : pendingId.value
  ? 'Inspect the unconfirmed request before sending another.' : ownerBlock.value);
const allow = computed(() => props.service?.allow_teaching ? '' : 'Teaching is disabled. Start the service with reviewed Teaching access.');
const ready = computed(() => busy.value || allow.value || (editing.value || !sessionId.value
  ? 'Confirm the TCP and check setup for this project.' : ''));
const motion = computed(() => {
  const sample = agvMotion.value;
  if (!sample) return null;
  const age = sample.status_age_s == null ? null : sample.status_age_s + (clock.value - sample.requested) / 1000;
  const link = props.agv.state;
  if (sample.connection_id != null && link.sequence >= 0 &&
      (!link.connected || link.connection_id !== sample.connection_id || link.stop_generation !== sample.stop_generation)) return null;
  return age != null && age >= 3 ? { ...sample, parked: false, reporting: false, teaching_station: null,
    detail: 'AGV status is stale. Check the radio or confirm the AGV is switched off.' } : sample;
});
const observedStation = computed(() => motion.value?.teaching_station);
const validationSeconds = computed(() => Math.max(0, Math.floor(60 - (clock.value / 1000 - (validation.value?.timestamp || 0)))));
const stationLabel = computed(() => project.value?.agv ? 'Saved station ' + project.value.agv.station +
  (project.value.agv.source === 'agv_status' ? (project.value.agv.after_image ? ' · AGV report after image acquisition' : ' · historical AGV report') : ' · operator entry') : 'No station saved.');
const boardOptions = computed(() => (defaults.value?.picking_boards || []).map(({ target, dimensions_mm }, value) =>
  ({ value, label: target.board + ' · ' + dimensions_mm.join(' × ') + ' mm' })));
const calibrationOptions = computed(() => defaults.value?.calibrations || []);
const tankCatalog = computed(() => defaults.value?.tanks || []);
const validationIdentity = computed(() => validation.value?.snapshot?.id);
const referenceImage = computed(() => snapshotPath(project.value?.project, project.value?.snapshots?.reference) ||
  (flow.value.captured && !project.value?.simulation ? '/api/teaching-image?' +
    new URLSearchParams({ project: project.value.project }) : ''));
const reviewReason = computed(() => ready.value || (validation.value?.status !== 'completed'
  ? 'Complete the checked joint test first.' : problem(measuredError)));
const travelReason = computed(() => busy.value || (!travel.value?.robot_configured ? 'Reviewed robot access is required.' : '') ||
  (!project.value?.project || project.value.simulation || editing.value && form.project !== project.value.project
    ? 'Open a saved project for this robot.' : '') || (!folded.value ? 'Confirm the folded pose first.' : ''));

function problem(check) {
  try {
    check();
    return '';
  } catch (error) {
    return error.message;
  }
}
function edit(key, value) {
  formRevision.value++;
  form[key] = value;
  editing.value = true;
  confirmed.value = markerFixed.value = folded.value = false;
  clearApprovals(true);
  if (key === 'project') {
    store(projectKey, value);
    saved.value = native.value = null;
    sessionId.value = exportId.value = '';
  }
}
function chooseCalibration(path) {
  const selected = calibrationOptions.value.find(item => item.path === path);
  if (!selected) return;
  edit('calibration', path);
  form.tcp = poseText(selected.tcp) || '';
  form.robot = selected.robot_ip;
}
function chooseBoard(index) {
  const target = defaults.value.picking_boards[index].target;
  edit('board', target.board);
  form.manifest = target.board_manifest;
}
function showProject(value) {
  if (!value) return;
  Object.assign(form, setupForm({ ...value.settings || value, project: value.project }));
  store(projectKey, value.project);
}
function clearApprovals(clearOff = false) {
  for (const action in approvals) {
    approvals[action] = false;
    if (clearOff) switchedOff[action] = false;
  }
}
watch([() => props.stopId, () => props.agv.haltRevision, () => props.agv.state.connected,
  () => props.agv.state.connection_id, () => props.agv.state.stop_generation], () => clearApprovals(true));
watch(() => validation.value?.id, () => { approvals.validate_move = approvals.vertical_descent = false; measured.value = ''; });
function editMotion(key, value) {
  motionEdits.add(key);
  motionForm[key] = value;
  clearApprovals();
  if (key === 'scene') store('ano-vue-teacher-scene', value);
}
function workspace() {
  if (!motionForm.scene.trim()) throw new Error('Select the reviewed collision scene.');
  const minimum = vector(motionForm.workMin, 3, 'Workspace minimum XYZ', .001);
  const maximum = vector(motionForm.workMax, 3, 'Workspace maximum XYZ', .001);
  if (minimum.some((value, index) => value >= maximum[index])) throw new Error('Each workspace minimum must be below its maximum.');
  return { collision_scene: motionForm.scene.trim(), workspace_min_m: minimum, workspace_max_m: maximum };
}
function motionPayload(action) {
  const agv_off = switchedOff[action];
  if (action !== 'vertical_descent') return {
    ...workspace(), agv_off, path_confirmed: approvals[action],
    joint_speed_deg_s: range(action === 'validate_move' ? motionForm.jointSpeed : motionForm.returnSpeed, 1, 6, 'joint speed in degrees/s'),
    ...(action === 'validate_move' ? { capture_id: validation.value.id } : {}),
  };
  const limits = bounds(motionForm);
  const z = validation.value.pick_tcp_pose_m_rad[2];
  if (z < limits.workspace_min_m[2] || z > limits.workspace_max_m[2]) {
    throw new Error('The taught height is outside the reviewed workspace. Check the bounds.');
  }
  return {
    ...limits, agv_off, calibration: project.value.settings.calibration, manifest: project.value.settings.target.board_manifest || '',
    alignment_job_id: alignment.value.job_id, alignment_capture_id: validation.value.id,
    speed_mm_s: range(motionForm.verticalSpeed, 1, 20, 'vertical speed in mm/s'),
    marker_unchanged: true, operator_vertical_approved: true, operator_path_approved: true, tcp_confirmed: true,
  };
}
function motionReason(action) {
  if (ready.value) return ready.value;
  if (action === 'vertical_descent') {
    if (!capability.value?.hardware_available) return 'Reviewed Cartesian movement is unavailable. Check the service and model.';
    if (validation.value?.status !== 'completed' || validation.value.validation_mode !== 'fixed_z' ||
        alignment.value?.job_id !== sessionId.value || alignment.value.capture_id !== validation.value.id ||
        alignment.value.grasp_sha256 !== validation.value.grasp_sha256) {
      return 'Complete a new checked planar alignment for the current capture before requesting descent.';
    }
  } else {
    if (!native.value?.motion_available) return 'Checked Teacher motion needs the reviewed opt-in and factory model.';
    if (action === 'return_observation' && !flow.value.captured) return 'Capture the reference first.';
    if (action === 'validate_move' && (validation.value?.status !== 'captured' || !validationSeconds.value ||
        !validationIdentity.value || imageReady.value !== validationIdentity.value)) {
      return 'Acquire and review a fresh validation image before moving.';
    }
  }
  return agvReason(motion.value, switchedOff[action]) || problem(() => motionPayload(action)) ||
    (!approvals[action] ? 'Review the target and tick the path approval.' : '');
}
function capturePayload() {
  return {
    ...captureTank(tankMode.value, tankValue.value),
    ...(observedStation.value == null ? { agv_station: range(station.value, 1, 255, 'AGV station', true) } : {}),
  };
}
function measuredError() {
  return measured.value.trim() ? vector(measured.value, 3, 'Measured TCP error') : null;
}
function show(id, text, tone, choose = false) {
  if (!alive) return;
  const outcome = { text, tone, pending: tone === 'running' || tone === 'waiting' };
  outcomes[id] = outcome;
  emit('outcome', outcome, choose);
}
async function feedback(id, seconds, operation) {
  if (running.value) return;
  running.value = id;
  details.value = '';
  const start = Date.now();
  show(id, 'Sending…', 'running', true);
  const waiting = setInterval(() => show(id, 'Waiting for service · ' +
    Math.max(0, Math.ceil(seconds - (Date.now() - start) / 1000)) + ' s', 'waiting'), 1000);
  try {
    const message = await operation();
    show(id, message, 'done');
  } catch (error) {
    details.value = error.message;
    show(id, Number.isInteger(error.status) ? publicError(error) :
      error.operatorMessage || 'Outcome unconfirmed. Check Current operation and saved evidence.', 'failed');
  } finally {
    clearInterval(waiting);
    running.value = '';
    if (alive) emit('changed');
  }
}
async function readProject(name, fill = false) {
  const revision = formRevision.value;
  const value = await request(projectPath(name));
  if (!alive || revision !== formRevision.value) return;
  saved.value = value;
  if (fill) {
    showProject(value);
    editing.value = true;
    native.value = null;
    sessionId.value = exportId.value = '';
    confirmed.value = markerFixed.value = folded.value = false;
    clearApprovals(true);
  }
}
function openProject() {
  void feedback('teacher-open', 8, async () => {
    await readProject(form.project.trim(), true);
    return 'Saved project opened. Confirm the TCP and check setup to continue.';
  });
}
async function syncSession(id) {
  try {
    const value = await request(sessionPath(id), null, 2500);
    if (!alive || tracked.value !== id || !value?.project) return false;
    sessionId.value = id;
    native.value = value;
    return true;
  } catch (error) {
    if (error.status !== 404) throw error;
    if (sessionId.value === id) {
      saved.value = native.value?.project || saved.value;
      native.value = null;
      sessionId.value = '';
      editing.value = true;
    }
    return false;
  }
}
async function syncJob(id, restore = false) {
  const found = await request(jobPath(id), null, 3000);
  if (!alive || tracked.value !== id || found.module !== 'teaching') return;
  if (found.auto_action === 'vertical_descent') {
    manualId.value = id;
    manual.value = found;
    return;
  }
  job.value = found;
  const active = await syncSession(id);
  if (restore && active) {
    editing.value = false;
    showProject(native.value.project);
    if (found.state === 'done') exportId.value = id;
  }
  if (restore && !active && terminal(found)) {
    const path = typeof found.result?.project === 'string' ? found.result.project :
      found.result?.project?.project || form.project;
    if (path) await readProject(path, true);
    if (found.state === 'done' && found.result?.project?.flow?.taught) exportId.value = id;
  }
}
async function inspectTeacher() {
  void feedback('teacher-inspect', 8, async () => {
    tracked.value = inspectionId.value.trim();
    try {
      await syncJob(tracked.value, true);
    } catch (error) {
      if (error.status === 404 && pendingId.value === tracked.value && owner.value && !owner.value.active) pendingId.value = '';
      throw error;
    }
    if (tracked.value === pendingId.value) pendingId.value = '';
    return 'Operation inspected.';
  });
}
function heartbeat(id) {
  return request('/api/teacher-control', { job_id: id, action: 'heartbeat' }, 1400);
}
function stopSupervision() {
  if (supervisedId) {
    const id = supervisedId;
    supervisedId = '';
    void request('/api/teacher-control', { job_id: id, action: 'stop' }, 2000).catch(() => {});
  }
}
function hidden() {
  if (document.hidden) stopSupervision();
}
const outcomesByAction = {
  configure: 'Setup checked.', capture: 'Reference capture saved.', teach: 'Picking position saved.',
  locate: 'Pick and approach poses exported.', capture_validation: 'Validation capture saved.',
  validate_move: 'Joint test ended. Check the reported result and physical alignment.',
  return_observation: 'Return ended. Check the reported position.', review_validation: 'Alignment observation saved.',
  vertical_descent: 'Descent ended. Inspect its result.', simulate: 'Simulated project saved. Use it for offline review.',
};
function submit(action, body = {}) {
  const id = crypto.randomUUID();
  void feedback('teacher-' + action, isTeacherMotion(action) ? 130 : 100, async () => {
    const requestBody = { action, request_id: id, ...body };
    const manualAction = action === 'vertical_descent';
    if (action !== 'configure' && action !== 'simulate') requestBody.project = project.value.project;
    if (isTeacherMotion(action)) {
      const stop = props.stopId;
      if (document.hidden) throw Object.assign(new Error('Keep Teaching visible while supervising motion.'), { status: 400 });
      await heartbeat(sessionId.value);
      if (!alive || document.hidden || props.stopId !== stop) {
        throw Object.assign(new Error('Teacher supervision ended before submission.'), {
          operatorMessage: 'Motion cancelled. Review the path and approve again.',
        });
      }
    }
    if (!manualAction) {
      store(jobKey, JSON.stringify({ id }));
      tracked.value = inspectionId.value = id;
    } else {
      manualId.value = id;
      store('ano-manual-inspect-id', id);
    }
    pendingId.value = id;
    let response;
    try {
      response = await request('/api/teaching', requestBody);
    } catch (error) {
      if (error.status >= 400 && error.status < 500) pendingId.value = '';
      throw error;
    }
    pendingId.value = '';
    if (!alive) return 'Request accepted.';
    if (manualAction) {
      manual.value = response;
      emit('changed');
      return 'Separate descent requested. Inspect and approve the checked plan below.';
    }
    job.value = response;
    if (isTeacherMotion(action) && !document.hidden) supervisedId = id;
    emit('changed');
    const finished = await finishJob(response, () => alive);
    if (supervisedId === id) supervisedId = '';
    if (!alive) return 'Request accepted.';
    job.value = finished;
    const current = await syncSession(id);
    if (action === 'configure' && current && finished.state === 'done') {
      editing.value = false;
      showProject(native.value.project);
    }
    if (finished.state !== 'done') {
      if (action !== 'configure' && !current) {
        editing.value = true;
        sessionId.value = '';
        native.value = null;
        if (form.project) await readProject(form.project.trim()).catch(() => {});
      }
      const message = action === 'capture' && /^AGV connection or station changed|^No fresh AGV report/.test(finished.error)
        ? finished.error : 'The operation failed. Check Details and the physical setup before trying again.';
      throw Object.assign(new Error(finished.error), { operatorMessage: message });
    }
    if (action === 'simulate') await readProject(finished.result.project, true);
    if (finished.result?.project?.flow?.taught) exportId.value = id;
    if (action === 'capture') { tankMode.value = 'keep'; tankValue.value = ''; }
    return outcomesByAction[action];
  });
}
function configure() {
  submit('configure', { settings: teacherSettings(form, confirmed.value) });
  confirmed.value = false;
}
function startMotion(action) {
  const body = motionPayload(action);
  clearApprovals(true);
  submit(action, body);
}
function simulate() {
  const settings = teacherSettings({ ...form, robot: 'simulation', tcp: form.tcp || '0,0,0,0,0,0' }, false);
  submit('simulate', { project: settings.project, calibration: settings.calibration, target: settings.target });
}
function handoff() {
  const value = project.value;
  store('ano-handoff-pick-place', JSON.stringify({
    project: value.project, calibration: value.settings?.calibration || value.calibration,
    manifest: value.settings?.target?.board_manifest || value.target?.board_manifest || '', simulation: value.simulation === true,
  }));
  location.hash = '#/pick-place';
}
function download(name) {
  void feedback('teacher-file-' + name, 60, async () => {
    downloadBlob(await requestDownload(filePath(exportId.value, name)), name);
    return 'Saved file downloaded.';
  });
}
function saveTravel() {
  folded.value = false;
  void feedback('teacher-travel-save', 8, async () => {
    const pose = await request('/api/agv-travel-pose', { project: project.value.project, confirmed: true });
    travel.value = { ...travel.value, pose };
    live.value = null;
    return 'Travel joints saved.';
  });
}
function checkTravel() {
  void feedback('teacher-travel-check', 8, async () => {
    live.value = await request('/api/agv-travel-pose');
    return live.value.at_pose ? 'Robot reported still at the saved pose.' : 'Travel pose unconfirmed. Check Current travel pose.';
  });
}
async function inspectManual(id) {
  try {
    const value = await request(jobPath(id), null, 3000);
    if (!value.manual && !value.result?.manual && value.auto_action !== 'vertical_descent' && value.module !== 'manual-pick-place') {
      throw new Error('This operation has no manual controls.');
    }
    if (manual.value?.id === id && !terminal(manual.value) && terminal(value) && value.auto_action === 'vertical_descent') {
      show('teacher-vertical_descent', value.state === 'done' ? 'Descent ended. Inspect its result.' :
        'Descent ended without a confirmed result. Inspect the operation.', value.state === 'done' ? 'done' : 'failed');
    }
    manual.value = value;
    manualId.value = id;
    store('ano-manual-inspect-id', id);
    if (pendingId.value === id) pendingId.value = '';
    manualProblem.value = '';
  } catch (error) {
    manualProblem.value = publicError(error);
    if (error.status === 404 && pendingId.value === id && owner.value && !owner.value.active) pendingId.value = '';
  }
}
function confirmManual(id, prompt) {
  void feedback('pick-confirm', 8, async () => {
    await request('/api/manual-control', { action: 'confirm', job_id: id, prompt, value: true });
    await inspectManual(id);
    return 'Plan confirmation sent. Check the operation outcome.';
  });
}
async function tick() {
  clock.value = Date.now();
  if (polling) return;
  polling = true;
  try {
    const requested = Date.now();
    const link = request('/api/agv-robot-motion?cached=1', null, 2500).then(value => { agvMotion.value = { ...value, requested }; });
    const travels = request('/api/agv-travel-pose?cached=1', null, 2500).then(value => { travel.value = value; });
    const updates = [link, travels];
    if (supervisedId && !document.hidden) updates.push(heartbeat(supervisedId));
    if (tracked.value && (!terminal(job.value) || !editing.value)) updates.push(syncJob(tracked.value));
    const activeManual = owner.value?.stop_targets?.find(target => target.kind === 'manual')?.job_id;
    if (activeManual || manual.value && !terminal(manual.value)) updates.push(inspectManual(activeManual || manualId.value));
    await Promise.allSettled(updates);
  } finally {
    polling = false;
  }
}
onMounted(async () => {
  timer = setInterval(tick, 600);
  document.addEventListener('visibilitychange', hidden);
  void tick();
  const revision = formRevision.value;
  const handoffValue = savedObject('ano-handoff-teaching');
  store('ano-handoff-teaching', null);
  await Promise.allSettled([
    request('/api/teacher-defaults').then(value => {
      defaults.value = value;
      if (!alive || formRevision.value !== revision || !editing.value) return;
      if (!form.project) Object.assign(form, setupForm(value.settings));
      if (typeof handoffValue.calibration === 'string') {
        edit('calibration', handoffValue.calibration);
        form.camera = handoffValue.camera_provider || form.camera;
        form.robot = handoffValue.robot_ip || form.robot;
        form.tcp = poseText(handoffValue.tcp) || form.tcp;
        setupNotice.value = handoffValue.simulation ? 'Simulated calibration selected. Use Create simulated project for offline evidence.' :
          'Calibration selected. Review the camera, printed marker and active TCP.';
      }
    }).catch(() => { setupNotice.value = 'Setup defaults unavailable. Check settings or reload.'; }),
    request('/api/manual-pick-place').then(value => { capability.value = value; }),
    request('/api/robot-control').then(value => {
      if (!alive || !value.defaults) return;
      for (const [key, input] of Object.entries(boundsFrom(value.defaults))) {
        if (input != null && !motionEdits.has(key) && (key !== 'scene' || !motionForm.scene)) motionForm[key] = input;
      }
    }),
    (async () => {
      if (typeof handoffValue.calibration === 'string') return;
      if (tracked.value) {
        try { await syncJob(tracked.value, true); return; } catch { /* Try the explicit project below. */ }
      }
      if (form.project) await readProject(form.project, true);
    })(),
    manualId.value ? inspectManual(manualId.value) : Promise.resolve(),
  ]);
});
onUnmounted(() => {
  alive = false;
  clearInterval(timer);
  document.removeEventListener('visibilitychange', hidden);
  stopSupervision();
});
</script>

<template>
  <div class="agv-grid pick-grid teacher-layout">
    <div class="agv-main">
      <section class="panel" aria-labelledby="teacher-setup-heading">
        <div class="teacher-heading">
          <h2 id="teacher-setup-heading">Set up the camera and marker</h2>
          <a href="/teaching">Legacy Teaching</a>
        </div>
        <p v-if="setupNotice" role="status">{{ setupNotice }}</p>
        <Message v-if="allow" severity="warn" :closable="false">{{ allow }}</Message>
        <div class="field-row">
          <label for="teacher-project">Project folder
            <InputText id="teacher-project" :model-value="form.project" autocomplete="off" :readonly="Boolean(running)"
              @update:model-value="edit('project', $event)" />
          </label>
          <label for="teacher-calibration">Hand-eye calibration JSON
            <InputText id="teacher-calibration" :model-value="form.calibration" list="teacher-calibrations"
              autocomplete="off" :readonly="Boolean(running)" @update:model-value="edit('calibration', $event)"
              @change="chooseCalibration($event.target.value)" />
            <datalist id="teacher-calibrations"><option v-for="entry in calibrationOptions" :key="entry.path" :value="entry.path" /></datalist>
          </label>
          <label for="teacher-camera">Camera
            <Select input-id="teacher-camera" :model-value="form.camera" :options="cameras" option-label="label"
              option-value="value" :disabled="Boolean(running)" @update:model-value="edit('camera', $event)" />
          </label>
          <label for="teacher-robot">Robot controller address
            <InputText id="teacher-robot" :model-value="form.robot" autocomplete="off" :readonly="Boolean(running)" @update:model-value="edit('robot', $event)" />
          </label>
          <label for="teacher-kind">Marker type
            <Select input-id="teacher-kind" :model-value="form.kind" :options="targets" option-label="label"
              option-value="value" :disabled="Boolean(running)" @update:model-value="edit('kind', $event)" />
          </label>
          <label for="teacher-tcp">Active TCP (XYZ mm, rotation-vector radians)
            <InputText id="teacher-tcp" :model-value="form.tcp" autocomplete="off" :readonly="Boolean(running)" @update:model-value="edit('tcp', $event)" />
          </label>
        </div>
        <p v-if="calibrationOptions.find(entry => entry.path === form.calibration)">
          Calibration TCP · {{ poseText(calibrationOptions.find(entry => entry.path === form.calibration).tcp) }}
        </p>
        <div v-if="form.kind === 'charuco'" class="field-row">
          <label v-if="boardOptions.length" for="teacher-printed-board">Printed picking board
            <Select input-id="teacher-printed-board" :options="boardOptions" option-label="label" option-value="value"
              :disabled="Boolean(running)" placeholder="Choose a board" @update:model-value="chooseBoard" />
          </label>
          <label for="teacher-board">Board name
            <InputText id="teacher-board" :model-value="form.board" :readonly="Boolean(running)" @update:model-value="edit('board', $event)" />
          </label>
          <label for="teacher-manifest">Board manifest JSON
            <InputText id="teacher-manifest" :model-value="form.manifest" list="teacher-manifests" autocomplete="off"
              :readonly="Boolean(running)" @update:model-value="edit('manifest', $event)" />
            <datalist id="teacher-manifests"><option v-for="name in defaults?.manifests" :key="name" :value="name" /></datalist>
          </label>
        </div>
        <PickPlaceFields v-else prefix="teacher-marker" :fields="[['dictionary', 'Dictionary'], ['marker', 'Marker ID', 0, 999, 1],
          ['size', 'Outer black square (mm)', 1]]" :values="form" @edit="edit" />
        <label class="pick-check" for="teacher-tcp-check">
          <Checkbox v-model="confirmed" input-id="teacher-tcp-check" binary />
          These values match the active gripper TCP on the pendant.
        </label>
        <div class="controls">
          <OperatorButton id="teacher-configure" label="Check setup" primary
            :why="busy || allow || problem(() => teacherSettings(form, confirmed)) || (!confirmed ? 'Confirm the active TCP.' : '')"
            :outcome="outcomes['teacher-configure']" @press="configure" />
          <OperatorButton id="teacher-open" label="Open saved project" :why="running ? 'Wait for the current request.' : !form.project.trim() ? 'Enter the project folder.' : ''"
            :outcome="outcomes['teacher-open']" @press="openProject" />
        </div>
        <details>
          <summary>Advanced setup and simulation</summary>
          <div class="field-row">
            <label for="teacher-model">Follow marker
              <Select input-id="teacher-model" :model-value="form.model" :options="models" option-label="label" option-value="value"
                :disabled="Boolean(running)" @update:model-value="edit('model', $event)" />
            </label>
          </div>
          <p>Full 3D includes height and tilt. Checked validation in this service uses planar projects.</p>
          <PickPlaceFields prefix="teacher-advanced" :fields="advancedFields" :values="form" @edit="edit" />
          <label class="pick-check" for="teacher-refine">
            <Checkbox :model-value="form.refine" input-id="teacher-refine" binary @update:model-value="edit('refine', $event)" />
            Refine before picking in Robot Bench using the taught camera view.
          </label>
          <p>Use an empty folder and a simulated calibration to make an offline project.</p>
          <OperatorButton id="teacher-simulate" label="Create simulated project" :outcome="outcomes['teacher-simulate']"
            :why="busy || problem(() => teacherSettings({ ...form, robot: 'simulation', tcp: form.tcp || '0,0,0,0,0,0' }, false))" @press="simulate" />
        </details>
        <p><a href="/preview/tool-envelope.html">Open tool envelope preview</a></p>
      </section>

      <section class="panel" aria-labelledby="teacher-reference-heading">
        <h2 id="teacher-reference-heading">Capture the reference</h2>
        <p>Use the pendant to show the whole marker and stop the arm before capture. Capturing again replaces the reference and requires teaching the pick again.</p>
        <p class="teacher-state">{{ project?.tank || 'Tank not taught' }} · {{ stationLabel }}</p>
        <p v-if="observedStation != null" role="status">AGV reports station {{ observedStation }}. The service checks the station again after the image.</p>
        <div class="field-row">
          <label v-if="observedStation == null" for="teacher-station">AGV station (1–255)
            <InputText id="teacher-station" v-model="station" type="number" min="1" max="255" step="1" />
          </label>
          <label for="teacher-tank-mode">Tank for this capture
            <Select input-id="teacher-tank-mode" v-model="tankMode" :options="tankOptions" option-label="label"
              option-value="value" @update:model-value="tankValue = ''" />
          </label>
          <label v-if="tankMode === 'set'" for="teacher-tank-number">Tank number (1–99)
            <InputText id="teacher-tank-number" :model-value="String(tankValue).replace(/^Tank0?/, '')"
              type="number" min="1" max="99" step="1" @update:model-value="tankValue = $event" />
          </label>
          <label v-if="tankMode === 'set' && tankCatalog.length" for="teacher-tank-catalog">Catalog tank
            <Select input-id="teacher-tank-catalog" :model-value="tankValue" :options="tankCatalog" placeholder="Choose a tank"
              @update:model-value="tankValue = $event" />
          </label>
        </div>
        <OperatorButton id="teacher-capture" label="Capture marker" primary :why="ready || problem(capturePayload)"
          :outcome="outcomes['teacher-capture']" @press="markerFixed = false; clearApprovals(true); submit('capture', capturePayload())" />
        <SnapshotViewer label="Reference image" :snapshot="project?.snapshots?.reference" :path="referenceImage" />
        <p v-if="project?.observation">Observation joints · {{ degrees(project.observe_joints) }} degrees.
          {{ project.observation.quality?.frames_accepted ?? 'Unknown' }} accepted frames.</p>
      </section>

      <section class="panel" aria-labelledby="teacher-pick-heading">
        <h2 id="teacher-pick-heading">Teach and export the picking position</h2>
        <p>Position the gripper at the lower pick endpoint with the pendant. Stop the arm before saving the current TCP.</p>
        <label class="pick-check" for="teacher-marker-fixed">
          <Checkbox v-model="markerFixed" input-id="teacher-marker-fixed" binary />
          The marker and object stayed fixed since capture.
        </label>
        <div class="controls">
          <OperatorButton id="teacher-teach" label="Read TCP and save pick" primary
            :why="ready || (!flow.captured ? 'Capture the reference first.' : !markerFixed ? 'Confirm the marker stayed fixed.' : '')"
            :outcome="outcomes['teacher-teach']" @press="submit('teach', { marker_unchanged: markerFixed }); markerFixed = false" />
          <OperatorButton id="teacher-locate" label="Read marker and export poses" :why="ready || (!flow.taught ? 'Save the picking position first.' : '')"
            :outcome="outcomes['teacher-locate']" @press="submit('locate')" />
          <OperatorButton id="teacher-handoff" label="Continue to Pick/place" :why="running || (!flow.taught ? 'Open or teach a saved picking project.' : '')"
            @press="handoff" />
        </div>
        <p v-if="project?.simulation" data-state="waiting">Simulated project · Offline evidence only.</p>
        <p v-if="project?.taught?.taught_tcp_in_base">Saved TCP · {{ poseText(project.taught.taught_tcp_in_base) }} · XYZ mm, rotation-vector radians.</p>
        <div v-if="native?.project?.exported" class="pick-readout">
          <p>Pick · {{ poseText(native.project.exported.pick_tcp_pose_m_rad, 1) }}</p>
          <p>Approach · {{ poseText(native.project.exported.approach_tcp_pose_m_rad, 1) }}</p>
          <p>Robot base · XYZ metres, rotation-vector radians. Read the marker again for every pick.
            {{ native.project.exported.refinement_required ? 'Preview only until Robot Bench refines the view.' : '' }}</p>
        </div>
        <div v-if="flow.taught" class="controls">
          <OperatorButton v-for="[file, label] in (flow.exported ? downloads : downloads.slice(0, 2))" :key="file"
            :id="'teacher-file-' + file" :label="label" :why="busy || (!exportId ? 'Check setup to verify the saved files.' : '')"
            :outcome="outcomes['teacher-file-' + file]" @press="download(file)" />
        </div>
        <SnapshotViewer v-if="project?.snapshots?.locate" label="Export image" :snapshot="project.snapshots.locate"
          :path="snapshotPath(project.project, project.snapshots.locate)" />
      </section>

      <section class="panel" aria-labelledby="teacher-validation-heading">
        <h2 id="teacher-validation-heading">Validate the marker and checked joint path</h2>
        <p>Stop the AGV and arm. The planar test uses capture height at the destination. TCP height can change along the joint path.</p>
        <OperatorButton id="teacher-capture_validation" label="Capture for validation"
          :why="ready || (!flow.taught ? 'Save the picking position first.' : form.model !== 'planar' ? 'Use a planar project for this test.' : '')"
          :outcome="outcomes['teacher-capture_validation']" @press="clearApprovals(true); submit('capture_validation')" />
        <SnapshotViewer label="Validation image" :snapshot="validation?.snapshot || project?.snapshots?.validation"
          :path="snapshotPath(project?.project, validation?.snapshot || project?.snapshots?.validation)" @loaded="imageReady = $event" />
        <div v-if="validation" class="pick-readout">
          <p>Capture {{ validation.id }} · {{ validation.status }} ·
            {{ validation.status === 'captured' ? validationSeconds + ' s left for one attempt' : 'Saved result' }}.</p>
          <p>Marker · {{ poseText(validation.marker_tcp_pose_m_rad) }}<br>
            Pick endpoint · {{ poseText(validation.pick_tcp_pose_m_rad) }}<br>
            Joint-test destination · {{ poseText(validation.validation_tcp_pose_m_rad) }}<br>
            Robot base · XYZ mm, rotation-vector radians.</p>
          <p v-if="validation.validation">Pick shift {{ validation.validation.shift_from_taught_mm?.toFixed(2) }} mm ·
            rotation {{ validation.validation.rotation_from_taught_deg?.toFixed(2) }} degrees.</p>
          <p v-if="validation.actual_tcp_pose_m_rad">Measured TCP · {{ poseText(validation.actual_tcp_pose_m_rad) }}</p>
          <p v-if="validation.visual_result">Visual alignment · {{ validation.visual_result }}.</p>
        </div>
        <div v-if="validation?.status === 'completed'">
          <p>Check alignment physically. Record an independent TCP measurement when verifying ±2 mm accuracy.</p>
          <label for="teacher-measured">Independent TCP error XYZ (mm, optional)
            <InputText id="teacher-measured" v-model="measured" />
          </label>
          <div class="controls">
            <OperatorButton id="teacher-review-aligned" label="Alignment looks correct" :why="reviewReason"
              :outcome="outcomes['teacher-review_validation']"
              @press="submit('review_validation', { capture_id: validation.id, result: 'aligned', measured_error_mm: measuredError() })" />
            <OperatorButton id="teacher-review-misaligned" label="Alignment needs adjustment" :why="reviewReason"
              :outcome="outcomes['teacher-review_validation']"
              @press="submit('review_validation', { capture_id: validation.id, result: 'misaligned', measured_error_mm: measuredError() })" />
          </div>
        </div>
        <details>
          <summary>Saved validation history ({{ history.length }})</summary>
          <ul v-if="history.length" class="teacher-history">
            <li v-for="entry in [...history].reverse()" :key="entry.id">
              <strong>{{ new Date(entry.timestamp * 1000).toLocaleString() }} · {{ entry.status }}</strong>
              <p>Visual alignment · {{ entry.visual_result || 'Unreviewed' }}.
                <template v-if="entry.measured_position_error_mm != null">
                  Independently measured {{ entry.measured_position_error_mm.toFixed(2) }} mm ({{ entry.within_2mm ? 'within' : 'outside' }} 2 mm).
                </template>
              </p>
              <details v-if="entry.snapshot" @toggle="$event.target.open ? historyImages.add(entry.id) : historyImages.delete(entry.id)">
                <summary>View saved image</summary>
                <SnapshotViewer v-if="historyImages.has(entry.id)" label="Past validation image"
                  :snapshot="entry.snapshot" :path="snapshotPath(project.project, entry.snapshot)" />
              </details>
            </li>
          </ul>
          <p v-else>No completed motion tests saved.</p>
        </details>
      </section>

      <section class="panel" aria-labelledby="teacher-travel-heading">
        <h2 id="teacher-travel-heading">AGV travel pose</h2>
        <p v-if="travel?.pose">Saved joints · {{ degrees(travel.pose.joints_rad) }} degrees · {{ new Date(travel.pose.saved_at).toLocaleString() }}.</p>
        <p v-else>No travel pose available.</p>
        <p v-if="live" role="status">{{ live.at_pose ? 'Live check · Robot reported still at the travel pose.' :
          'Live check unconfirmed. Fold the arm, stop it and check the robot connection.' }}</p>
        <label for="teacher-folded" class="pick-check"><Checkbox v-model="folded" input-id="teacher-folded" binary />
          The arm is folded inside the AGV footprint, with and without a rod.</label>
        <div class="controls">
          <OperatorButton id="teacher-travel-save" label="Save current joints as travel pose" :why="travelReason"
            :outcome="outcomes['teacher-travel-save']" @press="saveTravel" />
          <OperatorButton id="teacher-travel-check" label="Check live travel pose"
            :why="busy || (!travel?.pose ? 'Save a travel pose first.' : '')" :outcome="outcomes['teacher-travel-check']" @press="checkTravel" />
        </div>
      </section>
    </div>

    <aside class="operation">
      <section class="panel pick-operation" aria-labelledby="teacher-operation-heading">
        <h2 id="teacher-operation-heading">Current Teacher operation</h2>
        <p role="status">{{ native?.busy ? 'Running · ' + native.busy.replaceAll('_', ' ') :
          job ? 'Request ' + job.id + ' · ' + job.state : 'No request selected.' }}</p>
        <p v-if="pendingId" data-state="waiting">Request unconfirmed. Inspect {{ pendingId }} before sending again.</p>
        <Message v-if="owner?.execution_uncertain" severity="error" role="alert">Execution is uncertain.
          Check the robot and saved evidence before a reviewed service restart.</Message>
        <p>{{ motion?.detail || 'Checking AGV state…' }}</p>
        <p v-if="native?.collision_scope">Collision scope · {{ native.collision_scope }}.</p>
        <p v-if="native?.return_result?.actual_tcp_pose_m_rad">Return measured TCP · {{ poseText(native.return_result.actual_tcp_pose_m_rad) }}.</p>
        <details>
          <summary>Inspect Teacher request and details</summary>
          <label for="teacher-inspection-id">Request ID
            <InputText id="teacher-inspection-id" v-model="inspectionId" autocomplete="off" />
          </label>
          <OperatorButton id="teacher-inspect" label="Inspect without resuming" :why="running || (!inspectionId.trim() ? 'Enter the request ID.' : '')"
            :outcome="outcomes['teacher-inspect']" @press="inspectTeacher" />
          <pre v-if="details || job?.error">{{ details || job.error }}</pre>
          <pre v-if="native?.motion">{{ JSON.stringify(native.motion, null, 2) }}</pre>
          <pre v-if="native?.return_result">{{ JSON.stringify(native.return_result, null, 2) }}</pre>
        </details>
        <details>
          <summary>Reviewed motion settings</summary>
          <PickPlaceFields prefix="teacher-motion" :fields="[...boundsFields, ['returnSpeed', 'Return joint speed (degrees/s)', 1, 6],
            ['verticalSpeed', 'Vertical descent speed (mm/s)', 1, 20]]" :values="motionForm" @edit="editMotion" />
          <p>Use the reviewed collision scene and workspace for both joint movements. A supervised self-only scene leaves external objects to the operator.</p>
        </details>
        <details v-for="[action, label, declaration] in [
          ['return_observation', 'Return to observation pose', 'I reviewed the saved observation joints, workspace and scene, and cleared the path. TCP height can change.'],
          ['validate_move', 'Run checked joint test', 'The AGV, marker and object stayed fixed since capture. I reviewed the destination, workspace and scene, and cleared the path.'],
          ['vertical_descent', 'Review separate vertical descent', 'The TCP and tool fit the scene. The marker, object and support stayed fixed. I reviewed the vertical path and cleared the area.'],
        ]" :key="action">
          <summary>{{ label }}</summary>
          <p v-if="action === 'return_observation' && flow.captured">Saved joints · {{ degrees(project.observe_joints) }} degrees.</p>
          <p v-if="action === 'vertical_descent' && validation?.pick_tcp_pose_m_rad">
            Taught Z {{ (validation.pick_tcp_pose_m_rad[2] * 1000).toFixed(3) }} mm ·
            alignment Z {{ (validation.validation_tcp_pose_m_rad[2] * 1000).toFixed(3) }} mm. Separate plan approval is required.
          </p>
          <label class="pick-check" :for="'teacher-off-' + action">
            <Checkbox v-model="switchedOff[action]" :input-id="'teacher-off-' + action" binary
              @update:model-value="approvals[action] = false" />I confirm the AGV is switched off.
          </label>
          <label class="pick-check" :for="'teacher-approve-' + action">
            <Checkbox v-model="approvals[action]" :input-id="'teacher-approve-' + action" binary />{{ declaration }}
          </label>
          <OperatorButton :id="'teacher-' + action" :label="label" :why="motionReason(action)"
            :outcome="outcomes['teacher-' + action]" @press="startMotion(action)" />
        </details>
      </section>
      <PickPlaceJob v-model:id="manualId" :owner="owner" :job="manual" :problem="manualProblem" :running="running"
        :revision="formRevision" :stop-id="manualStopId" :outcomes="outcomes" :clock="clock"
        @inspect="inspectManual" @confirm="confirmManual" />
    </aside>
  </div>
</template>
