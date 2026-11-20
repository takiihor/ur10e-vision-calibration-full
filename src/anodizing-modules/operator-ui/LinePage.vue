<script setup>
import { computed, onUnmounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import Card from 'primevue/card';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Tag from 'primevue/tag';
import { downloadBlob, request } from './api';
import { sampleAge } from './sceneState';

const props = defineProps({ test: Boolean, state: Object, stateError: String, variable: Function, view: String });
const emit = defineEmits(['outcome', 'view']);
const serviceUnavailable = 'Line status unavailable. Check the service and reload.';
const refreshMessages = {
  pending: 'Catalog refresh pending.',
  done: 'Catalog replied.',
  failed: 'Catalog unavailable. Check the line API connection and retry.',
};
const views = [
  ['overview', 'Overview'], ['tank', 'Tank monitor'], ['signals', 'Signal browser'],
  ['alarms', 'Alarm signals'], ['catalog', 'Tag catalog'], ['diagnostics', 'Communication log'],
];
const view = ref(props.view ?? 'overview');
watch(() => props.view, value => { view.value = value ?? 'overview'; });

const catalog = ref(null);
const catalogError = ref('');
const query = ref('');
const access = ref('R');
const type = ref('');
const group = ref('');
const tank = ref('');
const advanced = ref(false);
const diagnostics = ref(null);
const diagnosticsError = ref('');
const loadingLog = ref(false);
const outcome = ref(null);
const remaining = ref(0);
let pending = null;
let timer;
let catalogRequest = 0;

const sample = computed(() => props.variable('line.poll_age'));
const reachable = computed(() => props.variable('line.api_reachable')?.value);
const quality = computed(() => sample.value?.quality === 'stale' ? 'stale' : props.state?.quality);
const currentCatalog = computed(() => catalog.value?.revision === props.state?.catalog_revision ? catalog.value : null);
const tags = computed(() => (currentCatalog.value?.tags ?? []).filter(tag => props.test || tag.read_write === 'R'));
const tankIds = computed(() => Object.keys(currentCatalog.value?.tanks ?? {}).sort());
const groups = computed(() => [...new Set(tags.value.map(tag => tag.group))].sort());
const types = computed(() => [...new Set(tags.value.map(tag => tag.data_type).filter(Boolean))].sort());
const byName = computed(() => new Map(tags.value.map(tag => [tag.tag_name, tag])));
const shown = computed(() => {
  if (view.value === 'overview') return ['Remote', 'Local', 'MainInletStatus'].map(name => ({
    name: {Remote: 'Remote mode', Local: 'Local mode', MainInletStatus: 'Main inlet'}[name], tag: byName.value.get(name),
  }));
  if (view.value === 'tank') return Object.entries(currentCatalog.value?.tanks[tank.value] ?? {}).map(([role, name]) => ({
    name: role.replaceAll('_', ' '), role, tag: byName.value.get(name),
  }));
  if (view.value === 'alarms') return tags.value.filter(tag => tag.group === 'Alarms')
    .map(tag => ({ name: tag.tag_name, tag }));
  const needle = query.value.trim().toLowerCase();
  return tags.value.filter(tag => (!props.test || !access.value || tag.read_write === access.value) &&
    (!group.value || tag.group === group.value) && (!type.value || tag.data_type === type.value) &&
    [tag.tag_name, tag.description, tag.device_address].some(text => text.toLowerCase().includes(needle)))
    .map(tag => ({ name: tag.tag_name, tag }));
});
const visibleRows = computed(() => shown.value.map(row => ({
  ...row, sample: row.tag && props.variable('line.' + row.tag.tag_name),
})));
const refreshFeedback = computed(() => outcome.value?.text || refreshMessages[props.state?.refresh?.outcome]);
const refreshReason = computed(() => props.stateError ? serviceUnavailable :
  remaining.value || props.state?.refresh?.outcome === 'pending' ? 'Wait for the current refresh.' :
  !props.state?.refresh_available ? props.state?.reason || 'Waiting for line setup.' : '');

watch(() => props.state?.catalog_revision, loadCatalog, { immediate: true });
watch(tankIds, ids => { if (!ids.includes(tank.value)) tank.value = ids[0] ?? ''; }, { immediate: true });
watch(groups, values => { if (!values.includes(group.value)) group.value = ''; });
watch(types, values => { if (!values.includes(type.value)) type.value = ''; });
watch(view, value => { if (value === 'diagnostics') void loadLog(); }, { immediate: true });
watch(() => props.state?.refresh, checkRefresh);

async function loadCatalog() {
  const id = ++catalogRequest;
  try {
    const response = await request('/api/line/catalog', null, 3000);
    if (id !== catalogRequest) return;
    catalog.value = response;
    catalogError.value = '';
  } catch {
    if (id === catalogRequest) catalogError.value = 'Catalog unavailable. Check the service and refresh the catalog.';
  }
}

function showOutcome(text, tone, running = false) {
  outcome.value = { text, tone };
  emit('outcome', outcome.value, running);
}

function endRefresh(text, tone) {
  pending = null;
  clearInterval(timer);
  remaining.value = 0;
  showOutcome(text, tone);
}

function checkRefresh(result) {
  if (pending === null || result?.id !== pending || result.outcome === 'pending') return;
  if (result.outcome === 'done') {
    if (!currentCatalog.value) void loadCatalog();
    endRefresh(refreshMessages.done, 'done');
  } else {
    endRefresh(refreshMessages.failed, 'failed');
  }
  if (view.value === 'diagnostics') void loadLog();
}

async function refresh() {
  if (refreshReason.value) return;
  const deadline = performance.now() + 9000;
  remaining.value = 9;
  showOutcome('Sending…', 'running', true);
  timer = setInterval(() => {
    const seconds = Math.ceil((deadline - performance.now()) / 1000);
    if (seconds <= 0) {
      endRefresh('Catalog refresh unconfirmed. Check the service and retry.', 'failed');
    } else if (seconds !== remaining.value) {
      remaining.value = seconds;
      showOutcome('Waiting for the catalog… ' + seconds + ' s', 'waiting');
    }
  }, 200);
  try {
    const confirmation = await request('/api/line/refresh', { action: 'refresh' }, 3000);
    if (!remaining.value) return;
    pending = confirmation.id;
    checkRefresh(props.state?.refresh);
  } catch (error) {
    if (!remaining.value) return;
    endRefresh(error.status === 403 ? 'Page access expired. Reload to reconnect.' :
      'Catalog refresh unavailable. Check the service and retry.', 'failed');
  }
}

async function loadLog() {
  if (loadingLog.value) return;
  loadingLog.value = true;
  diagnosticsError.value = '';
  try {
    diagnostics.value = (await request('/api/line/diagnostics', null, 3000)).events;
  } catch {
    diagnosticsError.value = 'Communication log unavailable. Check the service and retry.';
  } finally {
    loadingLog.value = false;
  }
}

function exportLog(format) {
  const mode = props.state?.mode;
  if (format === 'json') {
    const payload = JSON.stringify({ mode, events: diagnostics.value }, null, 2);
    downloadBlob(new Blob([payload], { type: 'application/json' }), 'line-read-log.json');
    return;
  }
  const fields = ['at', 'operation', 'outcome', 'http_status', 'row_id', 'source_at', 'tags_ok', 'tags_failed'];
  const rows = diagnostics.value.map(event => [mode, ...fields.map(field => event[field])]);
  const csv = [['mode', ...fields], ...rows].map(row => row.map(value =>
    '"' + String(value ?? '').replaceAll('"', '""') + '"').join(',')).join('\r\n');
  downloadBlob(new Blob([csv + '\r\n'], { type: 'text/csv' }), 'line-read-log.csv');
}

function formatValue(item) {
  if (item.sample?.value == null) return 'Unknown';
  if (item.role === 'basket') return item.sample.value === 1 ? 'Occupied' : 'Empty';
  return item.sample.value + (item.sample.unit ? ' ' + item.sample.unit : '');
}

function localTime(stamp) {
  return stamp ? new Date(stamp).toLocaleTimeString() : 'Unknown';
}

onUnmounted(() => {
  clearInterval(timer);
  remaining.value = 0;
  ++catalogRequest;
});
</script>

<template>
  <Card class="panel">
    <template #content>
      <div class="line-health">
        <Tag v-if="state?.mode === 'simulation'" severity="warn" value="Simulation" />
        <Tag :value="'API · ' + (reachable === true ? 'Reachable' : reachable === false ? 'Unavailable' : 'Unknown')"
          :severity="reachable === false ? 'danger' : 'secondary'" />
        <Tag :value="'PLC sample · ' + ({fresh: 'Fresh', partial: 'Partial', stale: 'Stale'}[quality] ?? 'Unknown')"
          :severity="quality === 'stale' ? 'danger' : quality === 'partial' ? 'warn' : 'secondary'" />
        <span id="line-age">{{ sampleAge(sample) }}</span>
        <template v-if="test">
          <Tag severity="secondary" :value="'Remote · ' + (variable('line.Remote')?.value ?? 'Unknown')" />
          <Tag severity="secondary" :value="'Local · ' + (variable('line.Local')?.value ?? 'Unknown')" />
        </template>
      </div>
      <Message v-if="stateError || quality === 'stale' || quality === 'unknown'" role="alert"
        :severity="quality === 'stale' || stateError ? 'error' : 'warn'">
        {{ stateError ? serviceUnavailable :
          quality === 'stale' ? 'PLC sample expired. Check the supplier polling feed.' : state?.reason }}
      </Message>
      <Message v-else-if="quality === 'partial'" severity="warn" role="status">
        Partial PLC row. Check the unavailable signals and supplier counts.
      </Message>
      <dl class="capabilities">
        <dt>Supplier timestamp</dt><dd id="line-source-at">{{ state?.source_at ?? 'Unknown' }}</dd>
        <template v-if="state?.clock_offset_ms != null">
          <dt>Supplier clock correction</dt><dd id="line-clock-offset">{{ state.clock_offset_ms }} ms</dd>
          <dt>Corrected supplier timestamp</dt><dd id="line-corrected-source-at">{{ state.corrected_source_at ?? 'Unknown' }}</dd>
        </template>
        <dt>PLC row</dt><dd>{{ state?.row_id ?? 'Unknown' }}</dd>
        <dt>Reported tag reads</dt><dd>{{ state?.tags_ok ?? 'Unknown' }} available · {{ state?.tags_failed ?? 'Unknown' }} unavailable</dd>
      </dl>
      <div class="line-actions">
        <Button id="line-refresh" label="Refresh catalog" :disabled="Boolean(refreshReason)" @click="refresh" />
        <span v-if="refreshReason">{{ refreshReason }}</span>
        <span v-if="refreshFeedback" role="status">{{ refreshFeedback }}</span>
        <Button as="a" :href="test ? '#/line' : '#/tests/line-api'" severity="secondary"
          :label="test ? 'Operator line view' : 'Unified line test'" />
      </div>
    </template>
  </Card>

  <slot :quality="quality" :catalog="currentCatalog" />

  <Card class="panel line-content">
    <template #content>
      <div class="line-controls">
        <label for="line-view">Line view
          <select id="line-view" v-model="view" @change="emit('view', view)">
            <option v-for="[id, label] in views" :key="id" :value="id">{{ label }}</option>
          </select>
        </label>
        <label v-if="view === 'tank' && tankIds.length" for="line-tank">Physical tank
          <select id="line-tank" v-model="tank">
            <option v-for="id in tankIds" :key="id">{{ id }}</option>
          </select>
        </label>
        <template v-if="view === 'signals' || view === 'catalog'">
          <label for="line-query">Search name, description or address
            <InputText id="line-query" v-model="query" type="search" />
          </label>
          <label v-if="test" for="line-access">Permission
            <select id="line-access" v-model="access">
              <option value="R">Read-only</option><option value="">All signals</option><option value="W">Writable readback</option>
            </select>
          </label>
          <label for="line-group">Group
            <select id="line-group" v-model="group">
              <option value="">All groups</option><option v-for="name in groups" :key="name">{{ name }}</option>
            </select>
          </label>
          <label for="line-type">Type
            <select id="line-type" v-model="type">
              <option value="">All types</option><option v-for="name in types" :key="name">{{ name }}</option>
            </select>
          </label>
          <label v-if="view === 'catalog'" class="line-check" for="line-advanced">
            <input id="line-advanced" v-model="advanced" type="checkbox" /> Advanced metadata
          </label>
        </template>
      </div>

      <details v-if="currentCatalog?.warnings.length" class="line-warnings">
        <summary>Catalog and mapping warnings ({{ currentCatalog.warnings.length }})</summary>
        <ul><li v-for="warning in currentCatalog.warnings" :key="warning.message">{{ warning.message }}</li></ul>
      </details>
      <Message v-if="catalogError" severity="error">{{ catalogError }}</Message>

      <template v-if="view === 'diagnostics'">
        <div class="line-actions">
          <Button label="Refresh log" :disabled="loadingLog" @click="loadLog" />
          <Button label="Download log JSON" severity="secondary" :disabled="!diagnostics?.length" @click="exportLog('json')" />
          <Button label="Download log CSV" severity="secondary" :disabled="!diagnostics?.length" @click="exportLog('csv')" />
          <span v-if="loadingLog" role="status">Reading communication log…</span>
          <span v-else-if="!diagnostics?.length">Download available after the first read event.</span>
        </div>
        <Message v-if="diagnosticsError" severity="error">{{ diagnosticsError }}</Message>
        <ul v-else-if="diagnostics?.length" class="line-readings">
          <li v-for="(event, index) in [...diagnostics].reverse()" :key="index">
            {{ localTime(event.at) }} · {{ event.operation }} · {{ event.outcome }}
            <small>HTTP {{ event.http_status ?? 'no reply' }} · PLC row {{ event.row_id ?? 'unknown' }}</small>
          </li>
        </ul>
      </template>
      <template v-else>
        <p v-if="view === 'alarms'">Raw alarm bits. Confirm their polarity with the supplier.</p>
        <p v-if="!visibleRows.length">
          {{ view === 'tank' ? 'No confirmed signals for this physical tank. Check the local mapping.' : 'No matching signals.' }}
        </p>
        <ul v-else class="line-readings">
          <li v-for="row in visibleRows" :key="row.name" :data-tag="row.tag?.tag_name">
            <div class="line-value">
              <strong>{{ row.name }}</strong>
              <Tag v-if="row.tag" :value="row.tag.read_write === 'W' ? 'W · readback' : 'R · read-only'" severity="secondary" />
              <span :class="{ 'line-primary': view === 'overview' }">{{ formatValue(row) }}</span>
              <small>{{ sampleAge(row.sample) }}</small>
            </div>
            <p v-if="row.tag?.description">{{ row.tag.description }}</p>
            <small v-if="row.role">{{ row.tag?.tag_name }}</small>
            <small v-if="row.sample?.value == null">
              {{ row.sample?.quality === 'stale' ? 'PLC sample expired.' : row.sample?.reason ?? 'Signal unavailable.' }}
            </small>
            <details v-if="row.sample?.last_read">
              <summary>Last read</summary>
              {{ row.sample.last_read.value }} {{ row.sample.unit }} · {{ localTime(row.sample.last_read.read_at) }}
            </details>
            <small v-if="view === 'catalog' && advanced && row.tag">
              {{ row.tag.device_address }} · {{ row.tag.data_type ?? 'Type not published' }} · Multiplier {{ row.tag.multiplier }}
              · Minimum {{ row.tag.minimum ?? 'not published' }} · Maximum {{ row.tag.maximum ?? 'not published' }}
            </small>
          </li>
        </ul>
      </template>
    </template>
  </Card>
</template>
