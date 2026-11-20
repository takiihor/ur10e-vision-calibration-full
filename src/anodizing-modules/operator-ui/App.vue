<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue';
import Button from 'primevue/button';
import StopBar from './StopBar.vue';
import AgvPage from './AgvPage.vue';
import { request } from './api';
import { useAgv } from './useAgv';

const isAgv = ref(location.hash === '#/agv');
const agv = useAgv(isAgv);
const heading = ref();
const safety = ref();
const sidebarOpen = ref(true);
const service = ref(null);
const offline = ref('');
const targetOutcome = ref({});
const targetSending = new Set();
let timer, observer, checking = false, retired = false;

const modules = [
  { name: 'Calibration', url: '/calibration', icon: 'pi-camera' },
  { name: 'Teaching', url: '/teaching', icon: 'pi-book' },
  { name: 'AGV', url: '#/agv', icon: 'pi-truck' },
  { name: 'Pick/place', url: '/pick-place', icon: 'pi-box' },
];
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
  if (window.matchMedia?.('(max-width: 60rem)').matches) sidebarOpen.value = false;
  void nextTick(() => heading.value?.focus());
}

onMounted(() => {
  document.title = isAgv.value ? 'AGV commissioning · Operator app' : 'Home · Operator app';
  window.addEventListener('hashchange', navigate);
  void refreshService();
  timer = setInterval(refreshService, 1500);
  if ('ResizeObserver' in window) {
    observer = new ResizeObserver(() => {
      document.documentElement.style.setProperty('--stop-height', safety.value.offsetHeight + 'px');
    });
    observer.observe(safety.value);
  }
});
onUnmounted(() => {
  retired = true;
  clearInterval(timer);
  observer?.disconnect();
  window.removeEventListener('hashchange', navigate);
});
</script>

<template>
  <div class="layout-wrapper" :class="{ 'layout-static-inactive': !sidebarOpen }">
    <div ref="safety" class="layout-stop">
      <StopBar :agv="agv" :targets="targets" :target-outcomes="targetOutcome" @target-stop="stopTarget" />
    </div>
    <header class="layout-topbar">
      <Button class="menu-button" type="button" icon="pi pi-bars" aria-label="Toggle navigation"
        :aria-expanded="sidebarOpen" aria-controls="operator-nav" @click="sidebarOpen = !sidebarOpen" />
      <a class="brand" href="#/">Operator app</a>
      <span v-if="agv.state.settings?.mode === 'simulator' || !agv.state.connected" class="transport-label">Simulation</span>
      <span v-else class="transport-label">Serial AGV</span>
    </header>
    <div class="layout-body">
      <aside v-show="sidebarOpen" id="operator-nav" class="layout-sidebar">
        <nav class="layout-menu" aria-label="Modules">
          <a href="#/" :aria-current="!isAgv ? 'page' : undefined"><i class="pi pi-home" aria-hidden="true"></i>Home</a>
          <a v-for="module in modules" :key="module.url" :href="module.url"
            :aria-current="module.url === '#/agv' && isAgv ? 'page' : undefined">
            <i class="pi" :class="module.icon" aria-hidden="true"></i>{{ module.name }}
          </a>
        </nav>
      </aside>
      <main class="layout-main-container" id="main">
        <div class="layout-main">
          <h1 ref="heading" tabindex="-1">{{ isAgv ? 'AGV commissioning' : 'Home' }}</h1>
          <AgvPage v-if="isAgv" :agv="agv" />
          <div v-else class="home-grid">
            <section class="panel">
              <h2>Service</h2>
              <p v-if="offline" role="alert" class="alarm">{{ offline }}</p>
              <p v-else-if="service?.app_id === 'anodizing_modules_v1'">Anodizing Modules · Online</p>
              <p v-else-if="service">Service identity unknown. Check the service and reload.</p>
              <p v-else>Checking service…</p>
              <p v-if="service && !offline">{{ active ? `Current operation · ${active.module} · ${active.state}` : 'No active job' }}</p>
              <p v-if="active?.module === 'calibration'"><a href="/calibration">Open current Calibration operation</a></p>
              <dl v-if="service && !offline" class="capabilities">
                <dt>Camera</dt><dd>{{ service.allow_camera ? 'Enabled' : 'Disabled' }}</dd>
                <dt>Teaching hardware</dt><dd>{{ service.allow_teaching ? 'Enabled' : 'Disabled' }}</dd>
                <dt>Manual robot controls</dt><dd>{{ service.allow_manual ? 'Enabled' : 'Disabled' }}</dd>
              </dl>
            </section>
            <section class="panel">
              <h2>AGV status</h2>
              <p v-if="agv.stateError" role="alert" class="alarm">{{ agv.stateError }}</p>
              <p v-else>{{ agv.connectionLabel }}</p>
              <dl class="capabilities">
                <dt>Reported motion</dt><dd>{{ agv.stale || !agv.state.connected || agv.variable('moving') == null ? 'Unknown' : agv.variable('moving') ? 'Running' : 'Stopped' }}</dd>
                <dt>Current station</dt><dd>{{ !agv.stale && agv.state.connected ? agv.variable('current_station') ?? 'Unknown' : 'Unknown' }}</dd>
              </dl>
              <a class="page-link" href="#/agv">Open AGV commissioning</a>
            </section>
          </div>
        </div>
      </main>
    </div>
  </div>
</template>
