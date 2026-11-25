<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import Checkbox from 'primevue/checkbox';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Select from 'primevue/select';
import OperatorButton from './OperatorButton.vue';
import PickPlaceFields from './PickPlaceFields.vue';
import PickPlaceJob from './PickPlaceJob.vue';
import ProgrammePanel from './ProgrammePanel.vue';
import PickPlaceTools from './PickPlaceTools.vue';
import { request } from './api';
import {
  agvReason, bounds, boundsDefaults, boundsFields, boundsFrom, finishJob, jobPath, ownerReason,
  publicError, range, savedObject, sources, store, stored, terminal, vector,
} from './pickPlace';

const props = defineProps({ owner: Object, agv: Object, stopId: String, tool: String });
const emit = defineEmits(['outcome', 'changed']);
const inspectKey = 'ano-manual-inspect-id';
const lostKey = 'ano-pick-place-unconfirmed';
const sourceKey = 'ano-pick-place-form';
const sceneKey = 'ano-manual-scene';
const handoffKey = 'ano-handoff-pick-place';
const handoff = savedObject(handoffKey);
const saved = savedObject(sourceKey);
store(handoffKey, null);

const form = reactive({
  ...boundsDefaults, scene: stored(sceneKey), verticalSpeed: 10,
  ...Object.fromEntries(sources.map(([key]) => [key,
    typeof handoff[key] === 'string' ? handoff[key] : typeof saved[key] === 'string' ? saved[key] : '',
  ])),
  pickTag: '', placeTag: '', item: '',
});
const recipeFields = [
  ['placeProject', 'Endpoint B teaching project (blank uses A)'],
  ['transfers', 'Transfers (1–100)', 1, 100, 1],
];
const recipeLimits = [
  ['timeout', 'AGV travel timeout (s)', 1, 600],
  ['speedCap', 'AGV set-speed cap (m/h)', 500, 1500, 1],
  ['settle', 'Settling time after each command (s)', 0, 5],
];
const recipeForm = reactive({ placeProject: '', transfers: 1, timeout: 120, speedCap: 1000, settle: 1 });
const robotFields = [
  ['host', 'Robot address'], ['tcp', 'Active TCP (XYZ mm, rotation vector radians)'], ...boundsFields,
];
const robot = reactive({ ...boundsDefaults, host: '', tcp: '' });
const translation = ref(5);
const rotation = ref(1);
const robotAxes = ['x', 'y', 'z', 'rx', 'ry', 'rz'];
const itemOptions = [
  { label: 'Released / no held item', value: 'operator_confirmed_released' },
  { label: 'Held', value: 'operator_confirmed_held' },
];
const endpoints = reactive(['A', 'B'].map(label => ({ label, station: '', project: null, meta: null, loading: false, failed: false, edited: false })));
const setup = ref(false);
const singleOff = ref(false);
const repeatOff = ref(false);
const robotOff = ref(false);
const reviewed = ref(null);
const approved = ref(false);
const travelApproved = ref(false);
const agvApproval = ref(null);
const loadNote = ref(handoff.simulation ? 'The handed-off project was simulated. Choose physically taught inputs for real motion.' : '');
const manualCapability = ref(null);
const robotCapability = ref(null);
const travel = ref(null);
const liveTravel = ref(null);
const motionSample = ref(null);
const busy = ref('');
const outcomes = reactive({});
const rawEvidence = ref('');
const prepareResult = ref(null);
const revision = ref(0);
const clock = ref(Date.now());
const lost = ref(stored(lostKey));
const inspected = ref(lost.value || stored(inspectKey));
const followed = ref(inspected.value);
const job = ref(null);
const jobProblem = ref('');
const mcuBlocked = ref('Wait for MCU status.');
const edited = new Set();
let pageOpen = true, timer, readingMotion = false, readingJob = null, generation = 0, tracking = 0, submittedJob = null;

const motion = computed(() => {
  const sample = motionSample.value;
  if (!sample) return null;
  const age = sample.status_age_s == null ? null : sample.status_age_s + Math.max(0, clock.value - sample.requested) / 1000;
  const changed = sample.connection_id != null && props.agv.state.sequence >= 0 &&
    (!props.agv.state.connected || sample.connection_id !== props.agv.state.connection_id ||
      sample.stop_generation !== props.agv.state.stop_generation);
  if (changed) return null;
  if (age != null && age >= 3) return { ...sample, parked: false, reporting: false, detail: 'AGV status is stale. Check the radio or confirm the AGV is switched off.' };
  return sample;
});
const cross = computed(() => reviewed.value && reviewed.value.stations.A !== reviewed.value.stations.B);
const jobActive = computed(() => job.value && !terminal(job.value));
const fence = computed(() => lost.value ? 'Inspect the unconfirmed request ID in Current operation before sending another command.' : '');
const common = computed(() => (busy.value ? 'Wait for the current request.' : '') || fence.value || ownerReason(props.owner) ||
  (jobActive.value ? 'Follow the active operation.' : ''));
const hardwareReason = computed(() => !manualCapability.value?.hardware_available
  ? 'Manual execution unavailable. Check service access and the selected robot model.' : '');

function clearApprovals() {
  approved.value = false;
  travelApproved.value = false;
  agvApproval.value = null;
}
function invalidateReview() {
  generation++;
  reviewed.value = null;
  clearApprovals();
}
function edit(key, value) {
  if (form[key] === value) return;
  edited.add(key);
  form[key] = value;
  setup.value = false;
  revision.value++;
  invalidateReview();
  if (sources.some(([field]) => field === key)) {
    store(sourceKey, JSON.stringify(Object.fromEntries(sources.map(([field]) => [field, form[field]]))));
  }
  if (key === 'scene') store(sceneKey, value);
}
function editRecipe(key, value) {
  recipeForm[key] = value;
  invalidateReview();
}
function editRobot(key, value) {
  robot[key] = value;
  edited.add('robot.' + key);
  revision.value++;
}
function setStation(endpoint, value) {
  endpoint.station = value;
  endpoint.edited = true;
  invalidateReview();
}
function bindings() {
  return { connection_id: motion.value?.connection_id, stop_generation: motion.value?.stop_generation };
}
function approve() {
  const live = bindings();
  agvApproval.value = approved.value && (!cross.value || travelApproved.value) && motion.value?.parked &&
    Number.isInteger(live.connection_id) && Number.isInteger(live.stop_generation) ? live : null;
}
watch([approved, travelApproved], approve, { flush: 'sync' });
watch([() => props.agv.haltRevision, () => props.agv.state.connected,
  () => props.agv.state.connection_id, () => props.agv.state.stop_generation], () => {
  if (reviewed.value?.agv_motion === 'parked') clearApprovals();
});
watch(repeatOff, invalidateReview, { flush: 'sync' });

async function projectMetadata(endpoint, project) {
  const lookup = Symbol();
  Object.assign(endpoint, { project, lookup, meta: null, loading: Boolean(project), failed: false, edited: false, station: '' });
  if (!project) return;
  try {
    const meta = await request('/api/teacher-project?project=' + encodeURIComponent(project));
    if (!pageOpen || endpoint.lookup !== lookup) return;
    endpoint.meta = meta;
    if (!endpoint.edited) endpoint.station = meta.agv?.station ?? '';
  } catch {
    if (endpoint.lookup === lookup) endpoint.failed = true;
  } finally {
    if (endpoint.lookup === lookup) endpoint.loading = false;
  }
}
watch([() => form.project.trim(), () => recipeForm.placeProject.trim() || form.project.trim()], (projects) => {
  for (const [index, project] of projects.entries()) {
    if (endpoints[index].project !== project) void projectMetadata(endpoints[index], project);
  }
}, { immediate: true });

function stationSource(endpoint) {
  if (endpoint.loading) return 'Reading the teaching project…';
  if (endpoint.failed) return 'Teaching project unavailable. Check its folder.';
  const taught = endpoint.meta?.agv;
  const value = endpoint.station === '' && endpoint.label === 'B' ? endpoints[0].station : endpoint.station;
  if (!taught?.station) return 'No station taught.';
  const source = taught.source === 'agv_status' ? 'AGV status during teaching' : 'operator entry during teaching';
  return 'Taught station ' + taught.station + ' · ' + source + '.' +
    (value !== '' && Number(value) !== taught.station ? ' Warning · selected station differs from teaching.' : '');
}
function inputs(action) {
  return { action, project: form.project.trim(), calibration: form.calibration.trim(), manifest: form.manifest.trim(),
    pick_tag: form.pickTag, place_tag: form.placeTag, reuse_taught_offset: setup.value };
}
function motionInputs(action) {
  return { ...inputs(action), ...bounds(form), speed_mm_s: range(form.verticalSpeed, 1, 20, 'vertical speed in mm/s'),
    tcp_confirmed: setup.value, operator_path_approved: setup.value, held_envelope_confirmed: setup.value,
    initial_item_state: form.item };
}
function recipeInputs(action) {
  const station = (endpoint, optional) => endpoint.station === '' && optional ? null :
    range(endpoint.station, 1, 255, 'station ' + endpoint.label, true);
  const stationA = station(endpoints[0], repeatOff.value);
  const stationB = station(endpoints[1], true);
  if (repeatOff.value && stationA != null && stationB != null && stationA !== stationB) {
    throw new Error('With the AGV switched off, both endpoints must use the same station.');
  }
  return { ...motionInputs(action), agv_off: repeatOff.value, place_project: recipeForm.placeProject.trim(),
    transfers: range(recipeForm.transfers, 1, 100, 'transfers', true),
    settle_s: range(recipeForm.settle, 0, 5, 'settling time in seconds'), station_a: stationA, station_b: stationB,
    agv_timeout_s: range(recipeForm.timeout, 1, 600, 'AGV timeout in seconds'),
    agv_speed_cap_m_h: range(recipeForm.speedCap, 500, 1500, 'AGV speed cap in m/h', true),
    initial_item_state: 'operator_confirmed_released', reuse_taught_offset: true,
    operator_path_approved: true, tcp_confirmed: true, held_envelope_confirmed: true };
}
function robotInputs(axis, direction) {
  if (!robot.host.trim()) throw new Error('Enter the robot address.');
  return { action: 'STEP', axis, increment: direction * (axis.length === 1 ? translation.value : rotation.value),
    robot_ip: robot.host.trim(), tcp: vector(robot.tcp, 6, 'Active TCP').map((value, i) => i < 3 ? value / 1000 : value),
    ...bounds(robot), agv_off: robotOff.value };
}
function invalid(build) {
  try {
    build();
    return '';
  } catch (error) {
    return error.message;
  }
}
const reviewWhy = computed(() => common.value || hardwareReason.value ||
  (!form.project.trim() ? 'Enter endpoint A’s completed teaching project.' : '') ||
  (endpoints.some(endpoint => endpoint.loading) ? 'Wait for the teaching project settings.' : '') ||
  invalid(() => recipeInputs('review_repeat')));
const startWhy = computed(() => reviewWhy.value || agvReason(motion.value, repeatOff.value) ||
  (!reviewed.value ? 'Review the current recipe.' : cross.value && !travel.value?.allow_agv_in_recipe ?
    'Check-only mode needs both endpoints at the same station.' :
    !approved.value ? 'Approve the reviewed bounded test.' :
    cross.value && !travelApproved.value ? 'Confirm Manual mode, a clear line and physical e-stop coverage.' :
    reviewed.value.agv_motion !== 'off' && (!agvApproval.value ||
      agvApproval.value.connection_id !== bindings().connection_id || agvApproval.value.stop_generation !== bindings().stop_generation)
      ? 'Check that the AGV is parked, then tick the approvals again.' : ''));
function singleWhy(action) {
  const state = action === 'PICK' ? 'operator_confirmed_released' : 'operator_confirmed_held';
  return common.value || hardwareReason.value || agvReason(motion.value, singleOff.value) ||
    (!setup.value ? 'Confirm the physical setup and clear path.' : '') ||
    (form.item !== state ? 'Choose ' + (action === 'PICK' ? 'Released / no held item for PICK.' : 'Held for PLACE.') : '') ||
    invalid(() => motionInputs(action));
}
const robotWhy = computed(() => common.value || agvReason(motion.value, robotOff.value) ||
  (!robotCapability.value?.enabled ? 'Robot steps unavailable. Check service access and the selected model.' : '') ||
  invalid(() => robotInputs('x', 1)));
const liveTravelWhy = computed(() => common.value || (!travel.value?.pose ? 'Save a travel pose in Teaching first.' : ''));

async function perform(button, task) {
  if (busy.value) return;
  busy.value = button;
  rawEvidence.value = '';
  const started = Date.now();
  let waiting = 'Waiting for service reply';
  function show(text, tone, select = false) {
    if (!pageOpen) return;
    const outcome = { text, tone, pending: ['running', 'waiting'].includes(tone) };
    outcomes[button] = outcome;
    emit('outcome', outcome, select);
  }
  show('Sending…', 'running', true);
  const pulse = setInterval(() => {
    const elapsed = (Date.now() - started) / 1000;
    if (elapsed >= 1) {
      const left = Math.max(0, Math.ceil(8 - elapsed));
      show(waiting && left ? waiting + '… ' + left + ' s' : 'Waiting for the service job. Follow Current operation.', 'waiting');
    }
  }, 250);
  try {
    const text = await task(() => { waiting = ''; });
    if (text) show(text, 'done');
  } catch (error) {
    rawEvidence.value = error.message;
    show(error.operatorMessage || publicError(error), 'failed');
  } finally {
    clearInterval(pulse);
    busy.value = '';
    if (pageOpen) emit('changed');
  }
}

function selectJob(id, value = null) {
  if (!pageOpen) return;
  tracking++;
  followed.value = inspected.value = id;
  job.value = value;
  jobProblem.value = '';
  store(inspectKey, id);
}
function acceptJob(value) {
  job.value = value;
  jobProblem.value = '';
  if (lost.value === value.id) {
    lost.value = '';
    if (stored(lostKey) === value.id) store(lostKey, null);
    if (value.cancelled_before_acceptance) {
      setup.value = singleOff.value = repeatOff.value = robotOff.value = false;
      form.item = '';
      invalidateReview();
    }
  }
}
function jobOutcome(value) {
  if (value.cancelled_before_acceptance) return { text: 'Request cancelled before acceptance.', tone: 'done' };
  if (value.error?.includes('Automatic recipe or saved inputs changed')) {
    return { text: 'Recipe inputs changed. Review and approve again.', tone: 'failed' };
  }
  if (value.state === 'done') return { text: 'Operation completed. Check the record and physical state.', tone: 'done' };
  const interrupted = value.state === 'interrupted' || value.manual?.stage === 'interrupted';
  return { text: (interrupted ? 'Operation interrupted.' : 'Operation failed.') + ' Inspect the evidence and physical state.', tone: 'failed' };
}
function publishJobOutcome(value = job.value) {
  if (!terminal(value) || value.id !== submittedJob?.id || busy.value === submittedJob.button) return;
  const outcome = jobOutcome(value);
  outcomes[submittedJob.button] = outcome;
  emit('outcome', outcome, false);
  submittedJob = null;
}
watch(job, publishJobOutcome);
async function readJob(id) {
  const value = await request(jobPath(id), null, 3000);
  if (value.id !== id) throw new Error('Wrong job identity');
  return value;
}
async function refreshJob() {
  const id = followed.value;
  const version = tracking;
  if (!id || readingJob === version || (job.value?.id === id && terminal(job.value))) return;
  readingJob = version;
  try {
    const value = await readJob(id);
    if (pageOpen && tracking === version) acceptJob(value);
  } catch (error) {
    if (pageOpen && tracking === version) jobProblem.value = error.status === 404 ?
      'Operation not found. Check the ID and service. An unconfirmed request may still be in flight.' :
      'Connection unavailable. Accepted work may continue. Inspect the status or use the physical e-stop if needed.';
  } finally {
    if (readingJob === version) readingJob = null;
  }
}
function inspect(id) {
  void perform('pick-inspect', async () => {
    selectJob(id);
    const version = tracking;
    readingJob = version;
    try {
      const value = await readJob(id);
      if (pageOpen && tracking === version) acceptJob(value);
      return 'Operation status retrieved.';
    } catch (error) {
      if (pageOpen && tracking === version) jobProblem.value = publicError(error);
      throw error;
    } finally {
      if (readingJob === version) readingJob = null;
    }
  });
}
function cancelUnconfirmed() {
  const id = lost.value;
  if (!id) return;
  void perform('pick-cancel-unconfirmed', async () => {
    const value = await request('/api/manual-control', { action: 'cancel-unconfirmed', job_id: id });
    if (value.id !== id) throw new Error('Wrong operation identity');
    if (pageOpen && lost.value === id) {
      selectJob(id);
      acceptJob(value);
    }
    return value.cancelled_before_acceptance ? 'Request cancelled before acceptance. Review and approve a new operation.' :
      'The service already accepted the request. Follow Current operation.';
  });
}
watch(() => props.owner?.stop_targets?.find(target => target.kind === 'manual')?.job_id, id => {
  if (id && id !== followed.value) {
    selectJob(id);
    void refreshJob();
  }
}, { immediate: true });

async function review(button, body, apply) {
  await perform(button, async acknowledged => {
    const version = generation;
    const id = crypto.randomUUID();
    const accepted = await request('/api/manual-pick-place', { ...body, request_id: id });
    if (accepted.id !== id) throw new Error('Wrong review identity');
    acknowledged();
    const result = await finishJob(accepted, () => pageOpen);
    if (!pageOpen) return;
    if (result.state !== 'done') throw Object.assign(new Error(result.error || result.state), {
      operatorMessage: 'Review failed. Check the saved inputs and operation details, then review again.',
    });
    if (version !== generation) return 'Settings changed during review. Review the updated inputs.';
    apply(result.result);
    return body.action === 'prepare' ? 'Saved role inputs reviewed. A fresh observation is still required.' :
      'Recipe reviewed. Check the setup and tick the approvals.';
  });
}
function reviewRecipe() {
  if (reviewWhy.value) return;
  invalidateReview();
  void review('recipe-review', recipeInputs('review_repeat'), result => { reviewed.value = result; });
}
function prepare() {
  if (!common.value && setup.value) void review('pick-prepare', inputs('prepare'), result => { prepareResult.value = result; });
}

function submitMotion(button, path, body) {
  void perform(button, async () => {
    const id = crypto.randomUUID();
    submittedJob = { id, button };
    selectJob(id);
    lost.value = id;
    store(lostKey, id);
    try {
      const value = await request(path, { ...body, request_id: id });
      if (value.id !== id) throw new Error('Wrong operation identity');
      if (pageOpen && followed.value === id && job.value?.id !== id) acceptJob(value);
      return 'Operation accepted. Follow Current operation.';
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && lost.value === id) {
        lost.value = '';
        if (stored(lostKey) === id) store(lostKey, null);
      }
      if (pageOpen && followed.value === id) jobProblem.value = publicError(error);
      throw error;
    }
  }).then(() => { if (pageOpen) publishJobOutcome(); });
}
function startRecipe() {
  if (startWhy.value) return;
  const body = { ...recipeInputs('REPEAT'), ...agvApproval.value, recipe_sha256: reviewed.value.recipe_sha256,
    automatic_test_approved: true, agv_manual_confirmed: travelApproved.value };
  repeatOff.value = false;
  invalidateReview();
  submitMotion('recipe-start', '/api/manual-pick-place', body);
}
function single(action) {
  if (singleWhy(action)) return;
  const body = { ...motionInputs(action), agv_off: singleOff.value };
  singleOff.value = false;
  setup.value = false;
  submitMotion('single-' + action, '/api/manual-pick-place', body);
}
function step(axis, direction) {
  if (robotWhy.value) return;
  const body = robotInputs(axis, direction);
  robotOff.value = false;
  revision.value++;
  submitMotion('step-' + axis + '-' + direction, '/api/robot-control', body);
}
function confirm(id, prompt) {
  void perform('pick-confirm', async () => {
    await request('/api/manual-control', { job_id: id, action: 'confirm', prompt, value: true });
    void refreshJob();
    return 'Confirmation accepted. Follow Current operation.';
  });
}
function checkLiveTravel() {
  if (liveTravelWhy.value) return;
  void perform('travel-live', async () => {
    liveTravel.value = { ...(await request('/api/agv-travel-pose')), readAt: new Date() };
    if (liveTravel.value.at_pose !== true) {
      throw Object.assign(new Error(liveTravel.value.detail), { operatorMessage: 'Travel pose not confirmed. Read the saved pose and check the robot.' });
    }
    return 'Robot reported at travel pose and still.';
  });
}
async function refreshMotion() {
  if (readingMotion) return;
  readingMotion = true;
  const requested = Date.now();
  try {
    const sample = await request('/api/agv-robot-motion?cached=1', null, 2500);
    motionSample.value = { ...sample, requested };
  } catch {
    motionSample.value = null;
    if (reviewed.value?.agv_motion === 'parked') clearApprovals();
  } finally {
    readingMotion = false;
  }
}
function prefill(target, values, prefix = '') {
  for (const [key, value] of Object.entries(values)) {
    if (value != null && !edited.has(prefix + key)) target[key] = value;
  }
}
async function setupDefaults() {
  const result = await Promise.allSettled([
    request('/api/teacher-defaults'), request('/api/robot-control'), request('/api/manual-pick-place'),
    request('/api/agv-travel-pose?cached=1'),
  ]);
  if (!pageOpen) return;
  const [teacher, stepControl, manual, pose] = result.map(entry => entry.value);
  const settings = teacher?.settings;
  if (settings) {
    const target = settings.target ?? {};
    prefill(form, {
      project: form.project || settings.project, calibration: form.calibration || settings.calibration,
      manifest: form.manifest || target.board_manifest, pickTag: target.board, placeTag: target.board,
    });
  }
  const defaults = stepControl?.defaults ?? {};
  robotCapability.value = stepControl;
  manualCapability.value = manual;
  travel.value = pose;
  prefill(form, { ...boundsFrom(defaults), scene: form.scene || defaults.collision_scene });
  prefill(robot, { ...boundsFrom(defaults), host: defaults.robot_ip,
    tcp: defaults.tcp?.map((value, index) => index < 3 ? value * 1000 : value).join(', ') }, 'robot.');
  if (!loadNote.value) loadNote.value = result.some(entry => entry.status === 'rejected') ?
    'Some saved defaults were unavailable. Check service access and enter reviewed values.' :
    'Review the saved project, calibration and marker provenance before approving motion.';
}
watch(() => props.owner?.active, async (id, previous) => {
  if (id || !previous || robotCapability.value?.enabled) return;
  try {
    robotCapability.value = await request('/api/robot-control', null, 3000);
  } catch {
    robotCapability.value = null;
  }
});
onMounted(() => {
  void setupDefaults();
  void refreshMotion();
  void refreshJob();
  let ticks = 0;
  timer = setInterval(() => {
    clock.value = Date.now();
    void refreshJob();
    if (++ticks % 2 === 0) void refreshMotion();
  }, 300);
});
onUnmounted(() => {
  pageOpen = false;
  clearInterval(timer);
});
</script>

<template>
  <p><a data-ui="pick.legacy.open" href="/pick-place">Legacy Pick/place</a> · <a data-ui="pick.agv.open" href="#/agv">AGV commissioning</a></p>
  <Message v-if="owner?.execution_uncertain || owner?.storage_error" severity="error" role="alert">{{ ownerReason(owner) }}</Message>
  <Message v-if="lost && !busy" severity="warn">
    Submission outcome unconfirmed. Inspect operation {{ lost }} in Current operation before sending another request.
  </Message>
  <div data-ui="pick.page" class="agv-grid pick-grid">
    <div class="agv-main">
      <section data-ui="pick.setup.panel" class="panel" aria-labelledby="pick-setup-heading">
        <h2 id="pick-setup-heading">Teaching inputs</h2>
        <p>Paths refer to the service data directory.</p>
        <PickPlaceFields data-ui="pick.setup.field" :fields="sources" :values="form" prefix="pick" @edit="edit" />
        <p>{{ loadNote }}</p>
        <details data-ui="pick.motion.panel">
          <summary data-ui="pick.motion.toggle">Motion settings</summary>
          <PickPlaceFields data-ui="pick.motion.field" :fields="[...boundsFields, ['verticalSpeed', 'Vertical speed (mm/s)', 1, 20]]"
            :values="form" prefix="pick" @edit="edit" />
        </details>
      </section>

      <ProgrammePanel @changed="emit('changed')" />
      <section data-ui="pick.recipe.panel" class="panel" aria-labelledby="recipe-heading">
        <h2 id="recipe-heading">Automatic rod transfer test</h2>
        <p>The rod starts at A. One transfer is one pickup and placement. Two transfers run A → B → A.</p>
        <PickPlaceFields data-ui="pick.recipe.field" :fields="recipeFields" :values="recipeForm" prefix="recipe" @edit="editRecipe" />
        <div class="field-row">
          <label v-for="endpoint in endpoints" :key="endpoint.label" :for="'recipe-station-' + endpoint.label">
            Endpoint {{ endpoint.label }} AGV station ({{ endpoint.label === 'B' ? 'blank uses A' : '1–255' }})
            <InputText data-ui="pick.recipe.station" :data-ui-key="endpoint.label" :id="'recipe-station-' + endpoint.label" :model-value="endpoint.station" type="number" min="1" max="255" step="1"
              :aria-describedby="'recipe-source-' + endpoint.label" @update:model-value="value => setStation(endpoint, value)" />
            <small :id="'recipe-source-' + endpoint.label">{{ endpoint.meta?.tank || 'Tank not taught' }} · {{ stationSource(endpoint) }}</small>
          </label>
        </div>
        <label class="pick-check" for="repeat-off"><Checkbox :pt="{ input: { 'data-ui': 'pick.recipe.agv.off' } }" v-model="repeatOff" input-id="repeat-off" binary :disabled="Boolean(common)" />
          AGV is switched off for this recipe.</label>
        <p :data-state="motion?.parked ? 'done' : ''">{{ agvReason(motion, repeatOff) || (repeatOff ? 'AGV switched off · operator declaration.' : motion?.detail) }}</p>
        <p>{{ travel?.allow_agv_in_recipe ? 'Approved recipes can call AGV stations.' : 'AGV check-only mode. Use one station for both endpoints.' }}</p>
        <details data-ui="pick.recipe.travel">
          <summary data-ui="pick.recipe.travel.toggle">Travel pose and recipe limits</summary>
          <PickPlaceFields data-ui="pick.recipe.limit" :fields="recipeLimits" :values="recipeForm" prefix="recipe" @edit="editRecipe" />
          <p v-if="travel?.pose">Travel pose saved {{ new Date(travel.pose.saved_at).toLocaleString() }} · joints (degrees)
            {{ travel.pose.joints_rad?.map(value => (value * 180 / Math.PI).toFixed(1)).join(', ') }}.</p>
          <p v-else>No saved travel pose available.</p>
          <p v-if="liveTravel">Live check at {{ liveTravel.readAt.toLocaleTimeString() }} ·
            {{ liveTravel.at_pose === true ? 'At travel pose and still' : 'Travel pose not confirmed' }}.</p>
          <OperatorButton data-ui="pick.recipe.travel.check" id="travel-live" label="Check live travel pose" :why="liveTravelWhy" :outcome="outcomes['travel-live']" @press="checkLiveTravel" />
          <a data-ui="pick.recipe.travel.teaching" href="/teaching#agv-travel-settings">Change travel pose in Teaching</a>
          <p>Settling is a delay. Tight/Open replies provide no proof of hold or release. The test uses no FSR threshold.</p>
        </details>
        <details data-ui="pick.recipe.sequence">
          <summary data-ui="pick.recipe.sequence.toggle">Motion sequence</summary>
          <ol>
            <li>If the station changes, fold to the saved travel pose and wait for AGV arrival.</li>
            <li>Move to the saved observation joints, capture the marker and check the fresh path.</li>
            <li>Move above the endpoint and descend by the saved approach height. PICK opens at hover and closes with command 1.</li>
            <li>PLACE opens with command 3. Lift or retract vertically by 100 mm.</li>
          </ol>
        </details>
        <div class="controls">
          <OperatorButton data-ui="pick.recipe.review" id="recipe-review" label="Review bounded recipe" :why="reviewWhy" :outcome="outcomes['recipe-review']" @press="reviewRecipe" />
        </div>
        <div data-ui="pick.recipe.reviewed" v-if="reviewed" class="pick-readout" aria-label="Reviewed recipe">
          <p>{{ reviewed.transfers }} commanded transfer(s). Final commanded rod endpoint {{ reviewed.A === reviewed.B && !cross ? 'A (same tank)' : reviewed.end }} (unverified).</p>
          <p data-ui="pick.recipe.endpoint.row" :data-ui-key="endpoint.label" v-for="(endpoint, index) in endpoints" :key="endpoint.label">
            {{ endpoint.label }} · {{ reviewed[endpoint.label] }} · {{ endpoints[index].meta?.tank || 'Tank not taught' }} ·
            {{ reviewed.stations[endpoint.label] == null ? 'Station unspecified' : 'Station ' + reviewed.stations[endpoint.label] }} ·
            {{ reviewed.approach_mm[endpoint.label] }} mm approach.
          </p>
          <p>Close 1 · open 3 · settling {{ reviewed.settle_s }} s · withdrawal {{ reviewed.withdrawal_mm ?? 100 }} mm.
            The server checks each fresh path. The automatic test runs through grip and release without individual approvals.</p>
          <p v-if="reviewed.agv_motion === 'off'">AGV switched off for this job.</p>
          <p v-else>Travel timeout {{ reviewed.agv_timeout_s }} s · set-speed cap {{ reviewed.agv_speed_cap_m_h }} m/h
            ({{ reviewed.agv_speed_cap_m_h / 1000 }} km/h).</p>
          <label class="pick-check" for="recipe-approved"><Checkbox :pt="{ input: { 'data-ui': 'pick.recipe.approval' } }" v-model="approved" input-id="recipe-approved" binary
            :disabled="Boolean(common)" />
            I checked the recipe, scene, bounds, TCP and clear path. The rod is at A, the gripper is empty and the tool and rod fit the saved tool box.
            I approve this supervised test, including motion and MCU commands at the stations shown.</label>
          <label v-if="cross && travel?.allow_agv_in_recipe" class="pick-check" for="recipe-travel-approved">
            <Checkbox :pt="{ input: { 'data-ui': 'pick.recipe.travel.approval' } }" v-model="travelApproved" input-id="recipe-travel-approved" binary :disabled="Boolean(common)" />
            {{ reviewed.agv_approval }}</label>
        </div>
        <div class="controls">
          <OperatorButton data-ui="pick.recipe.start" id="recipe-start" label="Start automatic test" :why="startWhy" :outcome="outcomes['recipe-start']" primary @press="startRecipe" />
        </div>
        <p>Keep the physical e-stop accessible. The server continues accepted automatic tests after a page disconnect.
          Completed transfer counts record commands. Check rod position physically.</p>
      </section>

      <details data-ui="pick.robot.panel" class="panel" :open="tool === 'robot' || undefined">
        <summary data-ui="pick.robot.toggle">Robot steps</summary>
        <p>Each click prepares one endpoint. Approve the checked step in Current operation. XYZ uses robot-base axes.
          Rotation uses tool axes and holds the TCP endpoint position. The checked joint path can curve and change height or orientation.</p>
        <PickPlaceFields data-ui="pick.robot.field" :fields="robotFields" :values="robot" prefix="step" @edit="editRobot" />
        <div class="field-row">
          <label for="step-xyz">XYZ step (mm)
            <Select :pt="{ label: { 'data-ui': 'pick.robot.translation' } }" v-model="translation" input-id="step-xyz" :options="[1, 5, 10]" @change="revision++" />
          </label>
          <label for="step-rotation">Tool rotation (degrees)
            <Select :pt="{ label: { 'data-ui': 'pick.robot.rotation' } }" v-model="rotation" input-id="step-rotation" :options="[1, 2, 5]" @change="revision++" />
          </label>
        </div>
        <label class="pick-check" for="step-off"><Checkbox :pt="{ input: { 'data-ui': 'pick.robot.agv.off' } }" v-model="robotOff" input-id="step-off" binary :disabled="Boolean(common)" />
          AGV is switched off for this step.</label>
        <p role="status">{{ robotWhy || 'Ready to prepare a finite step for approval.' }}</p>
        <div class="controls">
          <template v-for="axis in robotAxes" :key="axis">
            <OperatorButton data-ui="pick.robot.step" :data-ui-key="axis + '.' + direction" v-for="direction in [-1, 1]" :key="direction" :id="'step-' + axis + '-' + direction"
              :label="(direction === -1 ? '−' : '+') + (axis.length === 1 ? axis.toUpperCase() : ' tool ' + axis[1].toUpperCase() + ' rotation')"
              :why="robotWhy" :outcome="outcomes['step-' + axis + '-' + direction]" @press="step(axis, direction)" />
          </template>
        </div>
      </details>

      <details data-ui="pick.single.panel" class="panel">
        <summary data-ui="pick.single.toggle">Single PICK / PLACE</summary>
        <PickPlaceFields data-ui="pick.single.field" :fields="[['pickTag', 'Pick tag'], ['placeTag', 'Place tag']]" :values="form" prefix="pick" @edit="edit" />
        <label for="pick-item">Operator-confirmed initial item state</label>
        <Select :pt="{ label: { 'data-ui': 'pick.single.item' } }" :model-value="form.item" input-id="pick-item" :options="itemOptions" option-label="label" option-value="value"
          placeholder="Choose current state" @update:model-value="value => edit('item', value)" />
        <p>Keep the tool, camera, TCP and support height fixed. The tool and item must fit the saved tool box.
          Check the scene, operating bounds and workspace. Review the fresh route and confirm grip or release separately.</p>
        <label class="pick-check" for="single-setup"><Checkbox :pt="{ input: { 'data-ui': 'pick.single.setup' } }" v-model="setup" input-id="single-setup" binary :disabled="Boolean(common)" />
          I checked this operation’s physical setup and clear path.</label>
        <label class="pick-check" for="single-off"><Checkbox :pt="{ input: { 'data-ui': 'pick.single.agv.off' } }" v-model="singleOff" input-id="single-off" binary :disabled="Boolean(common)" />
          AGV is switched off for this operation.</label>
        <p>Fresh observation requires ≥3 pixels/module for pose and ≥4 for the taught reference.
          The robot aligns above the endpoint, descends along base −Z and lifts or retracts along +Z by 100 mm after confirmation.</p>
        <div class="controls">
          <OperatorButton data-ui="pick.single.action" :data-ui-key="action" v-for="action in ['PICK', 'PLACE']" :key="action" :id="'single-' + action" :label="action + ' · fresh observation'"
            :why="singleWhy(action)" :outcome="outcomes['single-' + action]" :primary="action === 'PICK'" @press="single(action)" />
          <OperatorButton data-ui="pick.single.prepare" id="pick-prepare" label="Check saved role inputs (read only)"
            :why="common || (!setup ? 'Confirm the taught-offset setup first.' : '')" :outcome="outcomes['pick-prepare']" @press="prepare" />
        </div>
        <details data-ui="pick.single.evidence" v-if="prepareResult"><summary data-ui="pick.single.evidence.toggle">Saved role evidence</summary><pre>{{ JSON.stringify(prepareResult, null, 2) }}</pre></details>
      </details>

      <PickPlaceTools :owner="owner" :motion="motion" :project="form.project.trim()" :item="form.item" :tool="tool"
        :busy="busy" :fence="fence" :perform="perform" :outcomes="outcomes" @mcu-status="mcuBlocked = $event"
        @mcu-actuation="revision++" @item="value => edit('item', value)" />
    </div>

    <aside data-ui="pick.status.panel" class="operation">
      <PickPlaceJob v-model:id="inspected" :owner="owner" :job="job" :problem="jobProblem" :running="busy"
        :mcu-blocked="mcuBlocked" :revision="revision" :stop-id="stopId" :outcomes="outcomes" :clock="clock" :evidence="rawEvidence"
        @inspect="inspect" @confirm="confirm">
        <template #recovery>
          <div v-if="lost" class="controls">
            <OperatorButton data-ui="pick.request.cancel" id="pick-cancel-unconfirmed" label="Cancel unaccepted request"
              :why="busy ? 'Wait for the current request.' : ''" :outcome="outcomes['pick-cancel-unconfirmed']"
              @press="cancelUnconfirmed" />
          </div>
        </template>
      </PickPlaceJob>
    </aside>
  </div>
</template>
