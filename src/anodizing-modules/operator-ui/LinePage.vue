<script setup>
import Disclosure from './Disclosure.vue';
import ToggleSwitch from 'primevue/toggleswitch';
import Skeleton from 'primevue/skeleton';
import Select from 'primevue/select';
import { computed, onUnmounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import Card from 'primevue/card';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Tag from 'primevue/tag';
import { downloadBlob, request, simulationEnabled } from './api';
import { sampleAge } from './sceneState';

const props = defineProps({ test: Boolean, state: Object, stateError: String, variable: Function, view: String, writeBlock: String });
const emit = defineEmits(['outcome', 'view']);
const allowSimulation = simulationEnabled();
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
const filtersActive = computed(() => ['signals', 'catalog'].includes(view.value) &&
  Boolean(query.value.trim() || group.value || type.value || props.test && access.value));
function clearFilters() {
  query.value = group.value = type.value = access.value = '';
}
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
const systemTag = role => props.state?.system_signals?.[role];
const systemValue = role => {
  const name = systemTag(role);
  return name ? props.variable('line.' + name)?.value : null;
};
const shown = computed(() => {
  if (view.value === 'overview') return Object.entries({
    remote_mode: 'Remote mode', local_mode: 'Local mode', main_inlet: 'Main inlet',
  }).map(([role, name]) => ({
    name, tag: byName.value.get(systemTag(role)),
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
const refreshReason = computed(() => props.writeBlock || (props.stateError ? serviceUnavailable :
  remaining.value || props.state?.refresh?.outcome === 'pending' ? 'Wait for the current refresh.' :
  !props.state?.refresh_available ? props.state?.reason || 'Waiting for line setup.' : ''));

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
  if (item.role === 'water_level_blocked') {
    if (props.stateError || item.sample.quality !== 'fresh') return 'Unknown';
    if (item.sample.value === 1) return 'Blocked: water level not met';
    return item.sample.value === 0 ? 'Water-level block inactive' : 'Unknown';
  }
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
  <Card data-ui="line.health.panel" class="panel">
    <template #content>
      <div class="line-health">
        <Tag v-if="state?.mode === 'simulation'" severity="warn" value="Simulation" />
        <Tag :value="'API · ' + (reachable === true ? 'Reachable' : reachable === false ? 'Unavailable' : 'Unknown')"
          :severity="reachable === false ? 'danger' : 'secondary'" />
        <Tag :value="'PLC sample · ' + ({fresh: 'Fresh', partial: 'Partial', stale: 'Stale'}[quality] ?? 'Unknown')"
          :severity="quality === 'stale' ? 'danger' : quality === 'partial' ? 'warn' : 'secondary'" />
        <span id="line-age">{{ sampleAge(sample) }}</span>
        <template v-if="test">
          <Tag severity="secondary" :value="'Remote · ' + (systemValue('remote_mode') ?? 'Unknown')" />
          <Tag severity="secondary" :value="'Local · ' + (systemValue('local_mode') ?? 'Unknown')" />
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
      <div class="line-actions" role="group" aria-label="Catalog actions">
        <Button data-ui="line.catalog.refresh" id="line-refresh" label="Refresh catalog" :disabled="Boolean(refreshReason)" @click="refresh" />
        <span v-if="refreshReason">{{ refreshReason }}</span>
        <span v-if="refreshFeedback" role="status">{{ refreshFeedback }}</span>
        <Button v-if="allowSimulation" data-ui="line.mode.switch" as="a" :href="test ? '#/line' : '#/tests/line-api'" severity="secondary"
          :label="test ? 'Operator line view' : 'Unified line test'" />
      </div>
    </template>
  </Card>

  <slot :quality="quality" :catalog="currentCatalog" />

  <Card data-ui="line.readings.panel" class="panel line-content mt-4">
    <template #content>
      <div class="line-controls my-[.6rem] flex flex-wrap items-end gap-3" role="group" aria-label="Line filters">
        <label for="line-view">Line view
          <Select :pt="{ label: { 'data-ui': 'line.filter.view' } }" input-id="line-view" aria-label="Line view" v-model="view" :options="views" :option-label="option => option[1]" :option-value="option => option[0]" @update:model-value="emit('view', $event)" />
        </label>
        <label v-if="view === 'tank' && tankIds.length" for="line-tank">Physical tank
          <Select :pt="{ label: { 'data-ui': 'line.filter.tank' } }" input-id="line-tank" aria-label="Physical tank" v-model="tank" :options="tankIds" />
        </label>
        <template v-if="view === 'signals' || view === 'catalog'">
          <label for="line-query">Search name, description or address
            <InputText data-ui="line.filter.search" id="line-query" v-model="query" type="search" />
          </label>
          <label v-if="test" for="line-access">Permission
            <Select :pt="{ label: { 'data-ui': 'line.filter.access' } }" input-id="line-access" aria-label="Permission" v-model="access" :options="[['R', 'Read-only'], ['', 'All signals'], ['W', 'Writable readback']]" :option-label="option => option[1]" :option-value="option => option[0]" />
          </label>
          <label for="line-group">Group
            <Select :pt="{ label: { 'data-ui': 'line.filter.group' } }" input-id="line-group" aria-label="Group" v-model="group" :options="['', ...groups]" :option-label="option => option || 'All groups'" />
          </label>
          <label for="line-type">Type
            <Select :pt="{ label: { 'data-ui': 'line.filter.type' } }" input-id="line-type" aria-label="Type" v-model="type" :options="['', ...types]" :option-label="option => option || 'All types'" />
          </label>
          <label v-if="view === 'catalog'" class="line-check" for="line-advanced">
            <ToggleSwitch input-id="line-advanced" v-model="advanced" :pt="{ input: { 'data-ui': 'line.filter.metadata' } }" /> Advanced metadata
          </label>
        </template>
      </div>

      <Disclosure data-ui="line.warnings.panel" v-if="currentCatalog?.warnings.length" class="line-warnings" toggle-id="line.warnings.toggle">
        <template #heading>Catalog and mapping warnings ({{ currentCatalog.warnings.length }})</template>
        <ul><li data-ui="line.warnings.row" :data-ui-key="warning.message" v-for="warning in currentCatalog.warnings" :key="warning.message">{{ warning.message }}</li></ul>
      </Disclosure>
      <Message v-if="catalogError" severity="error">{{ catalogError }}</Message>

      <template v-if="view === 'diagnostics'">
        <div class="line-actions" role="group" aria-label="Communication log actions">
          <Button data-ui="line.log.refresh" label="Refresh log" :disabled="loadingLog" @click="loadLog" />
          <Button data-ui="line.log.json" label="Download log JSON" severity="secondary" :disabled="!diagnostics?.length" @click="exportLog('json')" />
          <Button data-ui="line.log.csv" label="Download log CSV" severity="secondary" :disabled="!diagnostics?.length" @click="exportLog('csv')" />
          <span v-if="loadingLog" role="status">Reading communication log…</span>
          <span v-else-if="!diagnostics?.length">Download available after the first read event.</span>
        </div>
        <Message v-if="diagnosticsError" severity="error">{{ diagnosticsError }}</Message>
        <ul v-else-if="diagnostics?.length" class="line-readings">
          <li data-ui="line.log.row" :data-ui-key="event.at + '.' + index" v-for="(event, index) in [...diagnostics].reverse()" :key="index">
            {{ localTime(event.at) }} · {{ event.operation }} · {{ event.outcome }}
            <small>HTTP {{ event.http_status ?? 'no reply' }} · PLC row {{ event.row_id ?? 'unknown' }}</small>
          </li>
        </ul>
      </template>
      <template v-else>
        <p v-if="view === 'alarms'">Raw alarm bits. Confirm their polarity with the supplier.</p>
        <div v-if="view !== 'overview' && !currentCatalog" class="grid gap-2" :role="catalogError ? undefined : 'status'">
          <p v-if="catalogError">Signals unavailable.</p>
          <template v-else>
            <span>Loading signals…</span>
            <Skeleton height="4rem" aria-hidden="true" />
          </template>
        </div>
        <div v-else-if="!visibleRows.length" class="flex flex-wrap items-center gap-3">
          <p>{{ view === 'tank' ? 'No confirmed signals for this physical tank. Check the local mapping.' :
            filtersActive ? 'No signals match the current filters.' : 'No signals in this view.' }}</p>
          <Button v-if="filtersActive" data-ui="line.filter.clear" label="Clear filters" icon="pi pi-filter-slash"
            severity="secondary" outlined @click="clearFilters" />
        </div>
        <ul v-else class="line-readings">
          <li data-ui="line.readings.row" :data-ui-key="row.name" v-for="row in visibleRows" :key="row.name" :data-tag="row.tag?.tag_name">
            <div class="line-value">
              <strong>{{ row.name }}</strong>
              <Tag v-if="row.tag" :value="row.tag.read_write === 'W' ? 'W · readback' : 'R · read-only'" severity="secondary" />
              <span :class="{ 'line-primary': view === 'overview' }"
                :role="row.role === 'water_level_blocked' ? 'status' : undefined">{{ formatValue(row) }}</span>
              <small>{{ sampleAge(row.sample) }}</small>
            </div>
            <p v-if="row.tag?.description">{{ row.tag.description }}</p>
            <small v-if="row.role">{{ row.tag?.tag_name }}</small>
            <small v-if="row.sample?.value == null">
              {{ row.sample?.quality === 'stale' ? 'PLC sample expired.' : row.sample?.reason ?? 'Signal unavailable.' }}
            </small>
            <Disclosure data-ui="line.readings.last.panel" :data-ui-key="row.name" v-if="row.sample?.last_read" toggle-id="line.readings.last.toggle" :toggle-key="row.name">
              <template #heading>Last read</template>
              {{ row.sample.last_read.value }} {{ row.sample.unit }} · {{ localTime(row.sample.last_read.read_at) }}
            </Disclosure>
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
