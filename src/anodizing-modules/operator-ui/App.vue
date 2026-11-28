<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Message from 'primevue/message';
import ProgressSpinner from 'primevue/progressspinner';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';
import AppLayout from './layout/AppLayout.vue';
import StopBar from './StopBar.vue';
import AgvPage from './AgvPage.vue';
import ScenePage from './ScenePage.vue';
import RecordsPage from './RecordsPage.vue';
import LinePage from './LinePage.vue';
import LineTestPanel from './LineTestPanel.vue';
import PickPlacePage from './PickPlacePage.vue';
import TeacherPage from './TeacherPage.vue';
import CalibrationPage from './CalibrationPage.vue';
import { operatorKeyRequired, request, simulationEnabled } from './api';
import { useAgv } from './useAgv';

const route = ref(location.hash);
const isAgv = computed(() => ['#/agv', '#/tests/agv'].includes(route.value));
const isScene = computed(() => ['#/scene', '#/tests/scene'].includes(route.value));
const isRecords = computed(() => ['#/records', '#/tests/records'].includes(route.value));
const lineRoute = computed(() => /^#\/(line|tests\/line-api)(?:\/(tank|signals|alarms|catalog|diagnostics))?$/.exec(route.value));
const allowSimulation = simulationEnabled();
const lineTest = computed(() => allowSimulation && lineRoute.value?.[1] === 'tests/line-api');
const pickRoute = computed(() => /^#\/(?:pick-place|tests\/(pick-place|robot|mcu|tool-power))$/.exec(route.value));
const isTeacher = computed(() => ['#/teaching', '#/tests/teaching'].includes(route.value));
const isCalibration = computed(() => ['#/calibration', '#/tests/calibration'].includes(route.value));
const pickTitles = { robot: 'Robot steps', mcu: 'MCU gripper commands', 'tool-power': 'Tool power' };
const title = computed(() => isAgv.value ? 'AGV commissioning' : isScene.value ? 'Scene' : isRecords.value ? 'Recorder' :
  lineRoute.value ? (lineTest.value ? 'Unified line test' : 'Line reads') :
  pickRoute.value ? (pickTitles[pickRoute.value[1]] ?? 'Pick/place') : isTeacher.value ? 'Teaching' :
  isCalibration.value ? 'Calibration' : 'Home');
const service = ref(null);
const requiresSavedKey = operatorKeyRequired();
const operatorReadOnly = computed(() => (service.value?.operator_auth?.required ?? requiresSavedKey)
  && !service.value?.operator_auth?.authorized);
const agv = useAgv(isAgv, isScene, lineEscape, computed(() => pickRoute.value || isTeacher.value || isCalibration.value),
  operatorReadOnly);
const sceneConfig = ref(null);
const sceneError = ref('');
let sceneRead = false;

async function loadScene() {
  if (sceneConfig.value || sceneRead) return;
  sceneRead = true;
  sceneError.value = '';
  try {
    sceneConfig.value = await request('/api/scene', null, 3000);
  } catch {
    sceneError.value = 'Scene setup unavailable. Check the service and reload.';
  } finally {
    sceneRead = false;
  }
}
const linkClock = ref(Date.now());
const lineLink = computed(() => service.value?.line_link);
const linkSince = computed(() => Date.parse(lineLink.value?.since));
const linkAge = computed(() => Math.max(0, linkClock.value - linkSince.value));
const linkRecovered = computed(() => lineLink.value?.state === 'ok' && linkAge.value < 30000);
const linkTime = computed(() => new Date(linkSince.value).toLocaleTimeString([], {
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
}));
const linkElapsed = computed(() => linkAge.value < 60000
  ? `${Math.floor(linkAge.value / 1000)} s` : `${Math.floor(linkAge.value / 60000)} min`);
const offline = ref('');
const targetOutcome = ref({});
const linePanel = ref();
const lineStopOutcome = ref(null);
const lineStopRequest = ref(null);
const lineState = computed(() => service.value?.line_test);
const lineActive = computed(() => lineState.value?.active);
const targetSending = new Set();
const manualStopId = ref('');
const teacherStopId = ref('');
let timer, checking = false, retired = false;

const transport = computed(() => {
  if (agv.state.sequence === -1 || agv.serviceError) return 'AGV transport · Unknown';
  if (!agv.state.connected) return 'AGV transport · Disconnected';
  const labels = { wired: agv.state.executor_link ? 'IPC executor' : 'Wired RS-485', serial: 'Serial', simulator: 'Simulator' };
  return 'AGV transport · ' + (labels[agv.state.settings?.mode] ?? 'Unknown');
});
const active = computed(() => service.value?.owner?.jobs?.find(job => job.id === service.value.owner.active));
const targetControls = {
  teacher: { path: '/api/teacher-control', label: 'Teacher Stop (software)' },
  manual: { path: '/api/manual-control', label: 'Pick/place Stop (software)' },
  calibration: { path: '/api/calibration-control', label: 'Calibration Stop (software)' },
  programme: { path: '/api/programme', label: 'Stop simulated programme' },
};
const targets = computed(() => {
  const current = service.value?.programme;
  const targets = [...(service.value?.owner?.stop_targets ?? [])];
  if (['running', 'held', 'Stop unconfirmed'].includes(current?.state)) {
    targets.push({ kind: 'programme', job_id: current.id });
  }
  return targets.filter(({ kind }) => targetControls[kind]).map(target => ({ ...target, ...targetControls[target.kind] }));
});


async function refreshService() {
  linkClock.value = Date.now();
  if (checking) return;
  checking = true;
  try {
    const response = await request('/api/status', null, 3000);
    if (retired) return;
    service.value = response;
    offline.value = '';
    updateLineStop();
  } catch (error) {
    if (lineStopRequest.value && lineStopOutcome.value?.pending) updateLineStop(true);
    offline.value = error.status === 403 ? 'Page access expired. Reload to reconnect.' : 'Service unavailable. Check the service and reload.';
  } finally {
    checking = false;
  }
}

async function stopTarget(target) {
  const key = target.kind + '/' + target.job_id;
  if (targetSending.has(key)) return;
  targetSending.add(key);
  if (target.kind === 'manual') manualStopId.value = target.job_id;
  if (target.kind === 'teacher') teacherStopId.value = target.job_id;
  const started = Date.now();
  const at = new Date(started).toLocaleTimeString();
  function show(text, tone) {
    const outcome = { text: tone === 'running' ? text : `${at} · ${text}`, tone,
      pending: tone === 'running' || tone === 'waiting' };
    targetOutcome.value[key] = outcome;
    agv.showOutcome(key, outcome, tone === 'running');
  }
  show('Sending…', 'running');
  const wait = setInterval(() => show(`Waiting for Stop reply… ${Math.max(0, Math.ceil((8000 - Date.now() + started) / 1000))} s`, 'waiting'), 1000);
  try {
    const result = await request(target.path, { job_id: target.job_id, action: 'stop' });
    if (target.kind === 'programme') {
      show(result.state === 'stopped' ? 'Simulated Stop confirmed.' : 'Stop unconfirmed',
        result.state === 'stopped' ? 'done' : 'failed');
    } else {
      show('Stop requested. Check the machine for confirmation.', 'done');
    }
  } catch {
    show('Stop unconfirmed. Use the robot’s e-stop if needed and open the module for details.', 'failed');
  } finally {
    clearInterval(wait);
    targetSending.delete(key);
    void refreshService();
  }
}

function lineEscape() {
  if (!lineTest.value) return false;
  if (lineActive.value) {
    void stopLine(true);
    return true;
  }
  return !agv.state.connected && (linePanel.value?.cancelReview() ?? false);
}

function showLineStop(text, tone, select = false) {
  const stamp = lineStopRequest.value.at.toLocaleTimeString();
  const via = lineStopRequest.value.escape ? ' (Esc)' : '';
  const outcome = { text: `${stamp}${via} · ${text}`, tone, pending: tone === 'running' || tone === 'waiting' };
  lineStopOutcome.value = outcome;
  agv.showOutcome('line-stop', outcome, select);
}

function updateLineStop(unavailable = false) {
  if (!lineStopRequest.value || !lineStopOutcome.value?.pending) return;
  const { id, at } = lineStopRequest.value;
  const result = lineState.value?.latest;
  if (result?.id === id && lineActive.value?.id !== id) {
    const cleared = result.state === 'finished';
    showLineStop(result.reason, cleared || result.state === 'refused' ? 'done' : 'failed');
  } else if (unavailable || Date.now() - at.getTime() > 12000) {
    showLineStop('Clear unconfirmed. Check the line test status and use the line’s physical e-stop if needed.', 'failed');
  } else if (Date.now() - at.getTime() >= 1000) {
    showLineStop('Waiting for command zero confirmation… ' + Math.ceil(lineActive.value?.deadline_s ?? 8) + ' s', 'waiting');
  }
}

async function stopLine(escape = false) {
  if (!lineActive.value || lineStopOutcome.value?.pending) return;
  const id = lineActive.value.id;
  lineStopRequest.value = { id, at: new Date(), escape };
  showLineStop('Sending…', 'running', true);
  try {
    await request('/api/tests/line', { action: 'stop', request_id: id }, 3500);
    showLineStop('Clear requested. Waiting for command zero confirmation.', 'waiting');
  } catch {
    showLineStop('Clear request unconfirmed. Check the line test status and use the line’s physical e-stop if needed.', 'failed');
  }
  void refreshService();
}

function selectLineView(view) {
  location.hash = '#/' + lineRoute.value[1] + (view === 'overview' ? '' : '/' + view);
}

function navigate() {
  const wasLine = lineRoute.value;
  const wasRecords = isRecords.value;
  const wasLineTest = lineTest.value;
  const wasPick = pickRoute.value;
  const wasTeacher = isTeacher.value;
  const wasCalibration = isCalibration.value;
  route.value = location.hash;
  if (wasLine && !lineRoute.value) agv.retireOutcome('line-read');
  if (wasLineTest && !lineTest.value) agv.retireOutcome('line-test');
  if (wasRecords && !isRecords.value) agv.retireOutcome('recorder');
  if (wasPick && !pickRoute.value) agv.retireOutcome('pick-place');
  if (wasTeacher && !isTeacher.value) agv.retireOutcome('teacher');
  if (wasCalibration && !isCalibration.value) agv.retireOutcome('calibration');
  document.title = title.value + ' · Dev console';
  if (isAgv.value) agv.enterAgv();
  if (isScene.value) void loadScene();
  void nextTick(() => document.getElementById('operator-page-heading')?.focus());
}

onMounted(() => {
  document.title = title.value + ' · Dev console';
  if (isScene.value) void loadScene();
  window.addEventListener('hashchange', navigate);
  void refreshService();
  timer = setInterval(refreshService, 1500);
});
onUnmounted(() => {
  retired = true;
  clearInterval(timer);
  window.removeEventListener('hashchange', navigate);
});
</script>

<template>
  <AppLayout :route="route">
    <template #urgent>
      <StopBar :agv="agv" :transport="transport" :agv-page="isAgv" :show-ownership="isAgv || isCalibration || Boolean(pickRoute)" :route="route" :link-lost="lineLink?.state === 'lost'" :targets="targets" :target-outcomes="targetOutcome" :line-active="lineActive"
        :line-alarm="lineState?.alarm" :line-stop-outcome="lineStopOutcome" :line-escape="lineTest"
        @target-stop="stopTarget" @line-stop="stopLine()">
        <template #notice>
          <Message v-if="lineLink?.state === 'lost'" severity="error"
            :pt="{ root: { id: 'agv-link-loss', class: 'line-link-alert my-0.5' }, transition: { css: false } }">
            <strong>AGV link lost<template v-if="Number.isFinite(linkSince)"> since {{ linkTime }}</template></strong><span v-if="Number.isFinite(linkSince)" aria-hidden="true">{{ ' (' + linkElapsed + ')' }}</span>.
            The AGV is designed to hold and disarm on link loss.
            {{ lineLink.hint || agv.executorDownReason.replace(/^AGV link lost\.\s*/i, '') }}
          </Message>
          <Message v-else-if="linkRecovered" severity="success" :pt="{ root: { class: 'line-link-alert my-0.5', role: 'status', 'aria-live': 'polite' }, transition: { css: false } }">
            Link back at {{ linkTime }}, <a data-ui="line.link.reconnect" href="#/agv">reconnect on the AGV page</a>.
          </Message>
        </template>
      </StopBar>
    </template>
    <Toolbar class="page-header mb-2 flex-wrap items-center gap-x-3 gap-y-2" aria-labelledby="operator-page-heading">
      <template #start>
        <h1 id="operator-page-heading" tabindex="-1" class="m-0">{{ title }}</h1>
      </template>
      <template #end v-if="isTeacher || pickRoute">
        <div class="flex w-full flex-wrap gap-2 max-md:[&>.p-button]:w-full" role="group" aria-label="Workspace links">
          <Button v-if="isTeacher" data-ui="teaching.legacy.open" as="a" href="/teaching" severity="secondary" variant="outlined" label="Open legacy teaching" />
          <template v-else>
            <Button data-ui="pick.legacy.open" as="a" href="/pick-place" severity="secondary" variant="outlined" label="Open legacy pick/place" />
            <Button data-ui="pick.agv.open" as="a" href="#/agv" severity="secondary" variant="outlined" label="Open AGV commissioning" />
          </template>
        </div>
      </template>
    </Toolbar>
    <AgvPage v-if="isAgv" :agv="agv" :link-lost="lineLink?.state === 'lost'" />
    <ScenePage v-else-if="isScene" :config="sceneConfig" :config-error="sceneError"
      :state-error="agv.stateError" :variable="agv.variableInfo"
      :line-mode="agv.line?.mode" :line-tanks="agv.line?.mode === 'disabled' ? null : agv.line?.tanks" />
    <RecordsPage v-else-if="isRecords" :variables="agv.variables" :state-error="agv.stateError" :write-block="agv.operatorKeyReason"
      @outcome="(outcome, choose) => agv.showOutcome('recorder', outcome, choose)" />
    <LinePage v-else-if="lineRoute" :test="lineTest" :state="agv.line" :state-error="agv.stateError"
      :variable="agv.variableInfo" :view="lineRoute[2]" :write-block="agv.operatorKeyReason"
      @view="selectLineView"
      @outcome="(outcome, choose) => agv.showOutcome('line-read', outcome, choose)">
      <template v-if="lineTest" #default="{ quality, catalog }">
        <LineTestPanel ref="linePanel" :quality="quality" :catalog="catalog" :variable="agv.variableInfo"
          :system-signals="agv.line?.system_signals" :write-block="agv.operatorKeyReason" @changed="refreshService"
          @outcome="(outcome, choose) => agv.showOutcome('line-test', outcome, choose)" />
      </template>
    </LinePage>
    <PickPlacePage v-else-if="pickRoute" :owner="offline ? null : service?.owner" :agv="agv"
      :stop-id="manualStopId" :tool="pickRoute[1]" @changed="refreshService"
      @outcome="(outcome, choose) => agv.showOutcome('pick-place', outcome, choose)" />
    <TeacherPage v-else-if="isTeacher" :service="offline ? null : service" :agv="agv"
      :stop-id="teacherStopId" :manual-stop-id="manualStopId" @changed="refreshService"
      @outcome="(outcome, choose) => agv.showOutcome('teacher', outcome, choose)" />
    <CalibrationPage v-else-if="isCalibration" :service="offline ? null : service" :agv="agv"
      :stop-outcomes="targetOutcome" @changed="refreshService"
      @outcome="(outcome, choose) => agv.showOutcome('calibration', outcome, choose)" />
    <div data-ui="home.page" v-else class="grid items-start gap-4 min-[36rem]:grid-cols-2">
      <Card data-ui="home.service.panel" class="panel home-card" aria-labelledby="service-heading">
        <template #title><h2 id="service-heading">Service</h2></template>
        <template #content>
          <Message v-if="offline" severity="error" icon="pi pi-times-circle">{{ offline }}</Message>
          <p v-else-if="service?.app_id === 'anodizing_modules_v1'">
            Anodizing Modules <Tag severity="success" value="Online" />
          </p>
          <Message v-else-if="service" severity="warn" icon="pi pi-exclamation-triangle">
            Service identity unknown. Check the service and reload.
          </Message>
          <p v-else role="status"><ProgressSpinner class="control-spinner" aria-hidden="true" /> Checking service…</p>
          <p v-if="service && !offline">{{ active ? `Current operation · ${active.module} · ${active.state}` : 'No active job' }}</p>
          <Button data-ui="home.calibration.open" v-if="active?.module === 'calibration'" as="a" href="#/calibration" class="page-link"
            label="Open current Calibration operation" severity="secondary" />
          <dl v-if="service && !offline" class="capabilities">
            <dt>Camera</dt><dd>{{ service.allow_camera ? 'Enabled' : 'Disabled' }}</dd>
            <dt>Teaching hardware</dt><dd>{{ service.allow_teaching ? 'Enabled' : 'Disabled' }}</dd>
            <dt>Pendant calibration</dt><dd>{{ service.allow_manual ? 'Enabled' : 'Disabled' }}</dd>
            <dt>Manual Pick/place</dt><dd>{{ service.allow_manual_pick_place ? 'Enabled' : 'Disabled' }}</dd>
          </dl>
        </template>
      </Card>
      <Card data-ui="home.agv.panel" class="panel home-card" aria-labelledby="agv-heading">
        <template #title><h2 id="agv-heading">AGV status</h2></template>
        <template #content>
          <Message v-if="agv.stateError" severity="error" icon="pi pi-times-circle">{{ agv.stateError }}</Message>
          <p v-else>{{ agv.connectionLabel }}</p>
          <dl class="capabilities">
            <dt>Reported motion</dt><dd>{{ agv.stale || !agv.state.connected || agv.variable('moving') == null ? 'Unknown' : agv.variable('moving') ? 'Running' : 'Stopped' }}</dd>
            <dt>Current station</dt><dd>{{ !agv.stale && agv.state.connected ? agv.variable('current_station') ?? 'Unknown' : 'Unknown' }}</dd>
          </dl>
          <Button data-ui="home.agv.open" as="a" href="#/agv" class="page-link" label="Open AGV commissioning" severity="secondary" />
        </template>
      </Card>
    </div>
  </AppLayout>
</template>
