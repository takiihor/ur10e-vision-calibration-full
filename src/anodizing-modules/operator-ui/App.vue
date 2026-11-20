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
import { request } from './api';
import { useAgv } from './useAgv';

const isAgv = ref(location.hash === '#/agv');
const agv = useAgv(isAgv);
const heading = ref();
const safety = ref();
const service = ref(null);
const offline = ref('');
const targetOutcome = ref({});
const targetSending = new Set();
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
  } catch (error) {
    offline.value = error.status === 403 ? 'Page access expired. Reload to reconnect.' : 'Service unavailable. Check the service and reload.';
  } finally {
    checking = false;
  }
}

async function stopTarget(target) {
  const key = target.kind + '/' + target.job_id;
  if (targetSending.has(key)) return;
  targetSending.add(key);
  function show(text, tone) {
    const outcome = { text, tone };
    targetOutcome.value[key] = outcome;
    agv.showOutcome(key, outcome, tone === 'running');
  }
  show('Sending…', 'running');
  const wait = setTimeout(() => show('Stopping… Check the machine.', 'waiting'), 1000);
  try {
    await request(target.path, { job_id: target.job_id, action: 'stop' });
    show('Stop requested. Check the machine for confirmation.', 'done');
  } catch {
    show('Stop unconfirmed. Use the robot’s e-stop if needed and open the module for details.', 'failed');
  } finally {
    clearTimeout(wait);
    targetSending.delete(key);
    void refreshService();
  }
}

function navigate() {
  isAgv.value = location.hash === '#/agv';
  document.title = isAgv.value ? 'AGV commissioning · Operator app' : 'Home · Operator app';
  if (isAgv.value) agv.enterAgv();
  void nextTick(() => heading.value?.focus());
}

onMounted(() => {
  document.title = isAgv.value ? 'AGV commissioning · Operator app' : 'Home · Operator app';
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
    <StopBar :agv="agv" :targets="targets" :target-outcomes="targetOutcome" @target-stop="stopTarget" />
  </div>
  <AppLayout :is-agv="isAgv" :transport="transport">
    <h1 ref="heading" tabindex="-1">{{ isAgv ? 'AGV commissioning' : 'Home' }}</h1>
    <AgvPage v-if="isAgv" :agv="agv" />
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
