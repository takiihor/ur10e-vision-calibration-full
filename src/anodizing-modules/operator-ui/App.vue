<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Message from 'primevue/message';
import ProgressSpinner from 'primevue/progressspinner';
import Tag from 'primevue/tag';
import AppLayout from './layout/AppLayout.vue';
import StopBar from './StopBar.vue';
import AgvPage from './AgvPage.vue';
import ScenePage from './ScenePage.vue';
import RecordsPage from './RecordsPage.vue';
import LinePage from './LinePage.vue';
import LineTestPanel from './LineTestPanel.vue';
import PickPlacePage from './PickPlacePage.vue';
import TeacherPage from './TeacherPage.vue';
import { request } from './api';
import { useAgv } from './useAgv';

const route = ref(location.hash);
const isAgv = computed(() => route.value === '#/agv');
const isScene = computed(() => ['#/scene', '#/tests/scene'].includes(route.value));
const isRecords = computed(() => ['#/records', '#/tests/records'].includes(route.value));
const lineRoute = computed(() => /^#\/(line|tests\/line-api)(?:\/(tank|signals|alarms|catalog|diagnostics))?$/.exec(route.value));
const lineTest = computed(() => lineRoute.value?.[1] === 'tests/line-api');
const pickRoute = computed(() => /^#\/(?:pick-place|tests\/(pick-place|robot|mcu|tool-power))$/.exec(route.value));
const isTeacher = computed(() => ['#/teaching', '#/tests/teaching'].includes(route.value));
const pickTitles = { robot: 'Robot steps', mcu: 'MCU gripper commands', 'tool-power': 'Tool power' };
const title = computed(() => isAgv.value ? 'AGV commissioning' : isScene.value ? 'Scene' : isRecords.value ? 'Recorder' :
  lineRoute.value ? (lineTest.value ? 'Unified line test' : 'Line reads') :
  pickRoute.value ? (pickTitles[pickRoute.value[1]] ?? 'Pick/place') : isTeacher.value ? 'Teaching' : 'Home');
const agv = useAgv(isAgv, isScene, lineEscape, computed(() => pickRoute.value || isTeacher.value));
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
const heading = ref();
const safety = ref();
const service = ref(null);
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
let timer, observer, checking = false, retired = false;

const transport = computed(() => {
  if (agv.state.sequence === -1 || agv.serviceError) return 'AGV transport · Unknown';
  if (!agv.state.connected) return 'AGV transport · Disconnected';
  return agv.state.settings?.mode === 'serial' ? 'AGV transport · Serial' : 'AGV transport · Simulator';
});
const active = computed(() => service.value?.owner?.jobs?.find(job => job.id === service.value.owner.active));
const targets = computed(() => (service.value?.owner?.stop_targets ?? []).map(({ kind, job_id }) => ({
  kind, job_id,
  path: kind === 'teacher' ? '/api/teacher-control' : '/api/manual-control',
  label: kind === 'teacher' ? 'Teacher Stop (software)' : 'Pick/place Stop (software)',
})));


async function refreshService() {
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
    await request(target.path, { job_id: target.job_id, action: 'stop' });
    show('Stop requested. Check the machine for confirmation.', 'done');
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
  route.value = location.hash;
  if (wasLine && !lineRoute.value) agv.retireOutcome('line-read');
  if (wasLineTest && !lineTest.value) agv.retireOutcome('line-test');
  if (wasRecords && !isRecords.value) agv.retireOutcome('recorder');
  if (wasPick && !pickRoute.value) agv.retireOutcome('pick-place');
  if (wasTeacher && !isTeacher.value) agv.retireOutcome('teacher');
  document.title = title.value + ' · Operator app';
  if (isAgv.value) agv.enterAgv();
  if (isScene.value) void loadScene();
  void nextTick(() => heading.value?.focus());
}

onMounted(() => {
  document.title = title.value + ' · Operator app';
  if (isScene.value) void loadScene();
  window.addEventListener('hashchange', navigate);
  void refreshService();
  timer = setInterval(refreshService, 1500);
  const measureStopBar = () => {
    document.documentElement.style.setProperty('--stop-height', safety.value.offsetHeight + 'px');
  };
  if ('ResizeObserver' in window) {
    measureStopBar();
    observer = new ResizeObserver(measureStopBar);
    observer.observe(safety.value);
  }
});
onUnmounted(() => {
  retired = true;
  clearInterval(timer);
  observer?.disconnect();
  document.documentElement.style.removeProperty('--stop-height');
  window.removeEventListener('hashchange', navigate);
});
</script>

<template>
  <div ref="safety" class="layout-stop">
    <StopBar :agv="agv" :targets="targets" :target-outcomes="targetOutcome" :line-active="lineActive"
      :line-alarm="lineState?.alarm" :line-stop-outcome="lineStopOutcome" :line-escape="lineTest"
      @target-stop="stopTarget" @line-stop="stopLine()" />
  </div>
  <AppLayout :route="route" :transport="transport">
    <h1 ref="heading" tabindex="-1">{{ title }}</h1>
    <AgvPage v-if="isAgv" :agv="agv" />
    <ScenePage v-else-if="isScene" :config="sceneConfig" :config-error="sceneError"
      :state-error="agv.stateError" :variable="agv.variableInfo"
      :line-mode="agv.line?.mode" :line-tanks="agv.line?.mode === 'disabled' ? null : agv.line?.tanks" />
    <RecordsPage v-else-if="isRecords" :variables="agv.variables" :state-error="agv.stateError"
      @outcome="(outcome, choose) => agv.showOutcome('recorder', outcome, choose)" />
    <LinePage v-else-if="lineRoute" :test="lineTest" :state="agv.line" :state-error="agv.stateError"
      :variable="agv.variableInfo" :view="lineRoute[2]"
      @view="selectLineView"
      @outcome="(outcome, choose) => agv.showOutcome('line-read', outcome, choose)">
      <template v-if="lineTest" #default="{ quality, catalog }">
        <LineTestPanel ref="linePanel" :quality="quality" :catalog="catalog" :variable="agv.variableInfo" @changed="refreshService"
          @outcome="(outcome, choose) => agv.showOutcome('line-test', outcome, choose)" />
      </template>
    </LinePage>
    <PickPlacePage v-else-if="pickRoute" :owner="offline ? null : service?.owner" :agv="agv"
      :stop-id="manualStopId" :tool="pickRoute[1]" @changed="refreshService"
      @outcome="(outcome, choose) => agv.showOutcome('pick-place', outcome, choose)" />
    <TeacherPage v-else-if="isTeacher" :service="offline ? null : service" :agv="agv"
      :stop-id="teacherStopId" :manual-stop-id="manualStopId" @changed="refreshService"
      @outcome="(outcome, choose) => agv.showOutcome('teacher', outcome, choose)" />
    <div v-else class="home-grid">
      <Card class="panel home-card" aria-labelledby="service-heading">
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
          <Button v-if="active?.module === 'calibration'" as="a" href="/calibration" class="page-link"
            label="Open current Calibration operation" severity="secondary" />
          <dl v-if="service && !offline" class="capabilities">
            <dt>Camera</dt><dd>{{ service.allow_camera ? 'Enabled' : 'Disabled' }}</dd>
            <dt>Teaching hardware</dt><dd>{{ service.allow_teaching ? 'Enabled' : 'Disabled' }}</dd>
            <dt>Pendant calibration</dt><dd>{{ service.allow_manual ? 'Enabled' : 'Disabled' }}</dd>
            <dt>Manual Pick/place</dt><dd>{{ service.allow_manual_pick_place ? 'Enabled' : 'Disabled' }}</dd>
          </dl>
        </template>
      </Card>
      <Card class="panel home-card" aria-labelledby="agv-heading">
        <template #title><h2 id="agv-heading">AGV status</h2></template>
        <template #content>
          <Message v-if="agv.stateError" severity="error" icon="pi pi-times-circle">{{ agv.stateError }}</Message>
          <p v-else>{{ agv.connectionLabel }}</p>
          <dl class="capabilities">
            <dt>Reported motion</dt><dd>{{ agv.stale || !agv.state.connected || agv.variable('moving') == null ? 'Unknown' : agv.variable('moving') ? 'Running' : 'Stopped' }}</dd>
            <dt>Current station</dt><dd>{{ !agv.stale && agv.state.connected ? agv.variable('current_station') ?? 'Unknown' : 'Unknown' }}</dd>
          </dl>
          <Button as="a" href="#/agv" class="page-link" label="Open AGV commissioning" severity="secondary" />
        </template>
      </Card>
    </div>
  </AppLayout>
</template>
