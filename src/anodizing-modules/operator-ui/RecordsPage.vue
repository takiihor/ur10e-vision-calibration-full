<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Checkbox from 'primevue/checkbox';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Textarea from 'primevue/textarea';
import { downloadBlob, request, requestDownload } from './api';

const props = defineProps({ variables: Array, stateError: String });
const emit = defineEmits(['outcome']);
const rates = [0.2, 1, 5];
const formats = [
  { id: 'summary', label: 'Download summary', suffix: 'md' },
  { id: 'csv', label: 'Download CSV', suffix: 'csv' },
  { id: 'json', label: 'Download JSON', suffix: 'json' },
];
const reasons = {
  stopped: 'Stopped by operator',
  shutdown: 'Service shut down',
  interrupted: 'Interrupted by service restart',
  time_limit: '2 hour limit reached',
  size_limit: '50 MiB limit reached',
  storage_error: 'Recording ended with a storage error. Check space and permissions before starting again.',
  event_overflow: 'Recording ended because the event buffer filled. Check service load or choose a slower sample rate.',
  source_error: 'Recording ended because state or event data could not be saved. Check the service before starting again.',
};

const catalog = ref(null);
const error = ref('');
const operation = ref('');
const feedback = ref('');
const exporting = ref(null);
const exportFeedback = ref('');
const selected = ref([]);
const rate = ref(1);
const operator = ref('');
const site = ref('');
const devices = ref('');
const notes = ref('');
const active = computed(() => catalog.value?.active);
const finished = computed(() => (catalog.value?.records ?? []).filter(record => record.outcome));
const ended = computed(() => finished.value[0]?.outcome?.reason);
const endedWithError = computed(() => ['storage_error', 'event_overflow', 'source_error'].includes(ended.value));
const selectedIds = computed(() => props.variables.filter(item => selected.value.includes(item.id)).map(item => item.id));
const blocked = computed(() => {
  if (operation.value) return 'Wait for the recorder response.';
  if (error.value) return 'Reconnect to the service before changing the recording.';
  if (!catalog.value) return 'Checking the recorder…';
  if (active.value) return 'Finish the active recording before starting another.';
  if (props.stateError || !props.variables.length) return 'Wait for the available variables.';
  return '';
});
const startReason = computed(() => blocked.value || (!selectedIds.value.length ? 'Choose at least one variable.' : ''));
const stopReason = computed(() => operation.value ? 'Wait for the recorder response.' :
  error.value ? 'Reconnect to the service to stop recording.' :
  active.value?.status === 'stopping' ? 'The server is finishing the recording.' : '');
let version = 0;
let reading = false;
let retired = false;
let poll;

async function refresh() {
  if (reading || operation.value) return;
  reading = true;
  const before = version;
  try {
    const response = await request('/api/records', null, 3000);
    if (before === version && !retired) {
      catalog.value = response;
      error.value = '';
    }
  } catch (failure) {
    if (before === version && !retired) {
      error.value = failure.status === 403 ? 'Page access expired. Reload to reconnect to the recorder.' :
        'Recorder unavailable. Check the service and reload.';
    }
  } finally {
    reading = false;
  }
}

function outcome(text, tone, choose = false) {
  feedback.value = text;
  if (!retired) emit('outcome', { text, tone }, choose);
}

async function update(action) {
  if (action === 'start' ? startReason.value : stopReason.value || !active.value) return;
  version++;
  operation.value = action;
  outcome('Sending…', 'running', true);
  const deadline = performance.now() + 8000;
  const wait = setInterval(() => {
    const seconds = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
    outcome(`Waiting for the recorder… ${seconds} s`, 'waiting');
  }, 1000);
  try {
    const record = await request('/api/records', action === 'start' ?
      { action, variables: selectedIds.value, rate_hz: rate.value, operator: operator.value,
        site: site.value, devices: devices.value, notes: notes.value } :
      { action, id: active.value.id });
    catalog.value = {
      ...catalog.value,
      active: record.outcome ? null : record,
      records: [record, ...catalog.value.records.filter(item => item.id !== record.id)],
    };
    outcome(action === 'start' ? 'Recording started.' : 'Stop accepted. Check the recording status.', 'done');
  } catch (failure) {
    outcome(failure.status === 403 ? 'Page access expired. Reload to reconnect to the recorder.' :
      failure.status === 400 ? failure.message :
      'Recorder response unavailable. Check the recording status before trying again.', 'failed');
  } finally {
    clearInterval(wait);
    operation.value = '';
    if (!retired) void refresh();
  }
}

async function download(record, format) {
  if (exporting.value) return;
  exporting.value = record.id;
  exportFeedback.value = 'Preparing download…';
  try {
    const blob = await requestDownload(`/api/records/${record.id}/${format.id}`);
    downloadBlob(blob, `record-${record.id}.${format.suffix}`);
    exportFeedback.value = 'Download ready.';
  } catch (failure) {
    exportFeedback.value = failure.status === 403 ? 'Page access expired. Reload and download again.' :
      'Download unavailable. Check recording storage and try again.';
  } finally {
    exporting.value = null;
  }
}

function localTime(value) {
  return new Date(value).toLocaleString();
}

function transport(value) {
  return value === 'simulator' ? 'Simulator' : value === 'serial' ? 'Serial' : 'Disconnected';
}

onMounted(() => {
  void refresh();
  poll = setInterval(refresh, 1000);
});
onUnmounted(() => {
  retired = true;
  clearInterval(poll);
});
</script>

<template>
  <Message v-if="error" severity="error" role="alert">{{ error }}</Message>
  <Message v-if="catalog?.error" severity="warn" role="alert">{{ catalog.error }}</Message>
  <div class="home-grid recorder-grid">
    <Card class="panel home-card" aria-labelledby="record-current">
      <template #title><h2 id="record-current">Recording status</h2></template>
      <template #content>
        <div v-if="active">
          <p id="record-state" role="status" :data-state="active.status === 'stopping' ? 'waiting' : 'running'">
            {{ active.status === 'stopping' ? 'Finishing recording' : 'Recording' }}
          </p>
          <p>{{ active.variable_count ?? active.variables?.length }} variables · {{ active.rate_hz }} Hz · Started {{ localTime(active.started_at) }}</p>
          <p>AGV transport at start: {{ transport(active.agv_transport) }}</p>
          <p v-if="active.scene_simulation">Scene simulation enabled</p>
          <p v-if="active.line_sources?.length">Line sources · {{ active.line_sources.join(', ') }}</p>
          <div class="control controls">
            <Button id="record-stop" label="Stop recording" severity="secondary" :disabled="Boolean(stopReason)"
              :aria-describedby="stopReason ? 'record-stop-why' : undefined" @click="update('stop')" />
            <small id="record-stop-why">{{ stopReason }}</small>
          </div>
          <p>Recording continues when this page closes. Use Stop recording to finish it.</p>
        </div>
        <p v-else-if="catalog" role="status">No recording active.</p>
        <p v-else-if="!error" role="status">Checking recorder…</p>
        <Message v-if="!active && ended && ended !== 'stopped'" :severity="endedWithError ? 'error' : 'warn'"
          :role="endedWithError ? 'alert' : 'status'">{{ reasons[ended] }}</Message>
        <p v-if="feedback" role="status">{{ feedback }}</p>
        <Button as="a" href="#/agv" class="page-link" severity="secondary" label="AGV checklist and test exports" />
        <Button as="a" href="/agv" class="page-link legacy-record-link" severity="secondary" label="Open legacy AGV page" />
      </template>
    </Card>
    <Card class="panel home-card" aria-labelledby="record-new">
      <template #title><h2 id="record-new">New recording</h2></template>
      <template #content>
        <form @submit.prevent="update('start')">
          <p>Record cached state and events for up to 2 hours or 50 MiB.</p>
          <label class="record-rate">Sample rate
            <select v-model="rate" :disabled="Boolean(blocked)">
              <option v-for="option in rates" :key="option" :value="option">{{ option }} Hz</option>
            </select>
          </label>
          <fieldset class="record-variables" :disabled="Boolean(blocked)">
            <legend>Variables to sample</legend>
            <p v-if="!variables.length">Waiting for variables from the service.</p>
            <div class="record-choices">
              <label v-for="item in variables" :key="item.id" class="check">
                <Checkbox v-model="selected" :value="item.id" :disabled="Boolean(blocked)" />
                <span>{{ item.name }}<template v-if="item.unit"> ({{ item.unit }})</template><small>{{ item.id }}<template v-if="item.source === 'line.simulator'"> · Simulation</template></small></span>
              </label>
            </div>
          </fieldset>
          <details>
            <summary>Session details and notes</summary>
            <label>Operator <InputText v-model="operator" maxlength="100" :disabled="Boolean(blocked)" /></label>
            <label>Site <InputText v-model="site" maxlength="100" :disabled="Boolean(blocked)" /></label>
            <label>Devices <InputText v-model="devices" maxlength="200" :disabled="Boolean(blocked)" /></label>
            <label>Test notes <Textarea v-model="notes" maxlength="2000" rows="3" :disabled="Boolean(blocked)" /></label>
          </details>
          <div class="control controls">
            <Button id="record-start" type="submit" label="Start recording" :disabled="Boolean(startReason)"
              :aria-describedby="startReason ? 'record-start-why' : undefined" />
            <small id="record-start-why">{{ startReason }}</small>
          </div>
        </form>
      </template>
    </Card>
    <Card class="panel home-card records-history" aria-labelledby="record-history">
      <template #title><h2 id="record-history">Finished recordings</h2></template>
      <template #content>
        <p v-if="catalog && !finished.length">No finished recordings.</p>
        <p v-if="exportFeedback" role="status">{{ exportFeedback }}</p>
        <ul v-if="finished.length">
          <li v-for="record in finished" :key="record.id" class="record-history-item">
            <div>
              <strong>{{ localTime(record.started_at) }} · {{ record.rate_hz }} Hz</strong>
              <p>{{ reasons[record.outcome.reason] }} · AGV at start: {{ transport(record.agv_transport) }}</p>
              <p v-if="record.scene_simulation">Scene simulation enabled</p>
              <p v-if="record.line_sources?.length">Line sources · {{ record.line_sources.join(', ') }}</p>
            </div>
            <div class="controls">
              <Button v-for="format in formats" :key="format.id" :label="format.label" severity="secondary"
                :aria-label="`${format.label} for recording started ${localTime(record.started_at)}`"
                :disabled="Boolean(exporting)" @click="download(record, format)" />
            </div>
          </li>
        </ul>
      </template>
    </Card>
  </div>
</template>

<style scoped>
.record-rate, .record-variables { margin: .7rem 0; }
.record-rate { max-width: 14rem; }
.record-variables { min-width: 0; border: 1px solid var(--p-operator-pressed); border-radius: 6px; padding: .4rem .65rem; }
.record-choices { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 11rem), 1fr)); gap: .35rem; }
.record-choices .check { width: 100%; align-items: flex-start; }
.record-choices .p-checkbox { margin-top: .25rem; }
.legacy-record-link { margin-inline-start: .5rem; }
.records-history { grid-column: 1 / -1; }
.records-history ul { padding: 0; list-style: none; margin: 0; }
.record-history-item { display: flex; flex-wrap: wrap; gap: .4rem 1rem; align-items: center; justify-content: space-between; padding: .7rem 0; }
.record-history-item + .record-history-item { border-top: 1px solid var(--p-operator-pressed); }
@media (max-width: 62rem) {
  .recorder-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
