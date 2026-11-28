<script setup>
import Disclosure from './Disclosure.vue';
import InputText from 'primevue/inputtext';
import Select from 'primevue/select';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Tag from 'primevue/tag';
import vTooltip from 'primevue/tooltip';
import Message from 'primevue/message';
import { computed, watch, onMounted, onUnmounted, ref } from 'vue';
import { request } from './api';
import { guidance, refused, stationReport } from './agvFeedback';

const props = defineProps({ readOnly: Boolean });
const emit = defineEmits(['warning']);
const path = '/api/agv-bluetooth';
const state = ref(null);
const session = ref(null);
const busy = ref(false);
const connecting = ref(false);
const error = ref('');
const outcome = ref('');
const field = ref(null);
const value = ref('');
const review = ref(null);
let timer;
let retired = false;
const writable = computed(() => state.value?.fields?.filter(item => item.writable) ?? []);
const selected = computed(() => writable.value.find(item => item.field === field.value));
const canReview = computed(() => {
  const item = selected.value;
  return item && Number.isInteger(item.value) && item.value >= item.minimum && item.value <= item.maximum &&
    Number.isInteger(value.value) && value.value >= item.minimum && value.value <= item.maximum && value.value !== item.value;
});
const undo = computed(() => state.value?.undo);
const canUndo = computed(() => undo.value &&
  writable.value.find(item => item.field === undo.value.field)?.value === undo.value.old);
const connected = computed(() => Boolean(session.value && state.value?.connected));
const connection = computed(() => connecting.value ? 'Scanning and connecting…' : connected.value ? 'Connected' : state.value?.active
  ? 'Another Bluetooth maintenance session is open.' : 'Disconnected');
const routeRows = computed(() => Object.entries(state.value?.route?.records ?? {}).flatMap(([direction, rows]) =>
  rows.map((raw, index) => ({ key: direction + '-' + index, direction: direction === 'outbound' ? 'Outbound' : 'Return',
    record: index + 1, raw }))));
const routeBlock = computed(() => guidance(state.value?.feedback?.blocked, state.value?.blocked));
const enroll = computed(() => props.readOnly ? 'This browser needs operator enrollment. Enroll with the saved operator key.' : '');
const commonBlock = computed(() => enroll.value || (busy.value ? 'A Bluetooth request is in progress. Wait for it to finish.' :
  !connected.value ? 'This tab has no open Bluetooth session. Press Connect first.' : ''));
const settingsBlock = computed(() => commonBlock.value || routeBlock.value ||
  (review.value ? 'A review is already open. Confirm or cancel it first.' : ''));
const connectBlock = computed(() => enroll.value || (busy.value ? 'A Bluetooth request is in progress. Wait for it to finish.' :
  !state.value ? 'Bluetooth status has not loaded. Wait or reload the page.' :
    !state.value.configured ? "The AGV's identity is missing. Ask the technician to configure its Bluetooth name or address." :
      state.value.active ? 'A Bluetooth session is open. Disconnect it before opening a new one.' : ''));
const writeBlock = computed(() => settingsBlock.value || (canReview.value ? '' : !selected.value ?
  'No setting is selected. Choose an editable setting and read its current value.' :
  selected.value.value == null ? 'The current value is unknown. Press Read settings.' :
  `Enter a value different from ${selected.value.value}, between ${selected.value.minimum} and ${selected.value.maximum}.`));
const valueError = computed(() => selected.value?.value != null && value.value !== '' && !settingsBlock.value && !canReview.value
  ? writeBlock.value : '');
const undoBlock = computed(() => settingsBlock.value || (canUndo.value ? '' : !undo.value ?
  'There is no saved write to undo. Read settings to check the current values.' :
  'The current value differs from the saved write. Read settings and check the undo record.'));
const confirmBlock = computed(() => commonBlock.value || routeBlock.value);
const actionLabel = {
  connect: 'Connect Bluetooth', read: 'Read settings', disconnect: 'Disconnect Bluetooth',
  write: 'Write Bluetooth setting', undo: 'Undo Bluetooth write',
  'review-clear': 'Review route clear', clear: 'Clear route', heartbeat: 'Keep Bluetooth session open',
};
const serviceUnavailable = 'Katana’s Bluetooth page cannot reach its service. Reload the page and check the connection.';
const route = computed(() => state.value?.route);
watch(() => Boolean(error.value || route.value?.error || (state.value?.active && routeBlock.value)),
  warning => emit('warning', warning));
const clearResult = computed(() => state.value?.clear_result);
const cleared = computed(() => clearResult.value?.after?.every(count => count === 0));
const reviewTitle = computed(() => ({
  clear: 'Erase the stored route', undo: 'Undo last write', write: 'Write one setting',
})[review.value?.action]);
const confirmLabel = computed(() => ({
  clear: 'Confirm route clear', undo: 'Confirm undo', write: 'Confirm write',
})[review.value?.action]);

function formatCounts(counts) {
  return counts ? `Outbound ${counts[0]} · Return ${counts[1]}` : 'Counts unavailable';
}

function newSession() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
}

async function refresh() {
  try {
    const next = await request(path);
    if (retired) return;
    state.value = next;
  } catch (failure) {
    if (!retired) error.value = refused('Read Bluetooth page status', failure, serviceUnavailable);
  }
}

async function run(action, details = {}) {
  if (props.readOnly) return;
  const current = session.value;
  busy.value = true;
  connecting.value = action === 'connect';
  if (action !== 'heartbeat') {
    error.value = '';
    outcome.value = '';
  }
  try {
    const next = await request(path, { action, session: current, ...details }, 45000);
    if (retired || current !== session.value) return;
    state.value = next;
    if (action === 'disconnect') {
      session.value = null;
      review.value = null;
    }
    if (action === 'read') outcome.value = 'Bluetooth read complete.';
    if (action === 'review-clear') review.value = { action: 'clear', ...next.clear_review };
    if (action === 'write' || action === 'undo') {
      outcome.value = action === 'undo' ? 'Previous value restored and verified.' : 'New value verified.';
      value.value = '';
    }
  } catch (failure) {
    if (retired || current !== session.value) return;
    const label = details.field ? `${actionLabel[action]} ${details.field}` : actionLabel[action];
    error.value = refused(label, failure, serviceUnavailable);
    if (action === 'connect' || action === 'heartbeat') {
      close();
    } else {
      await refresh();
    }
  } finally {
    busy.value = false;
    connecting.value = false;
  }
}

function connect() {
  if (connectBlock.value) return;
  session.value = newSession();
  void run('connect');
}

function close() {
  if (!session.value) return;
  const current = session.value;
  session.value = null;
  review.value = null;
  void request(path, { action: 'disconnect', session: current }, 24000, true).catch(() => {});
}

function reviewWrite() {
  if (writeBlock.value) return;
  review.value = { action: 'write', field: selected.value.field, old: selected.value.value, value: Number(value.value) };
}

function confirm() {
  if (confirmBlock.value || !review.value) return;
  const { action, before, ...details } = review.value;
  review.value = null;
  void run(action, { ...details, confirmed: true });
}

onMounted(() => {
  window.addEventListener('pagehide', close);
  void refresh();
  timer = window.setInterval(() => {
    if (busy.value) return;
    if (session.value) void run('heartbeat');
    else void refresh();
  }, 4000);
});
onUnmounted(() => {
  retired = true;
  window.clearInterval(timer);
  window.removeEventListener('pagehide', close);
  close();
});
</script>

<template>
  <Card data-ui="agv.bluetooth.page" class="agv-bluetooth" aria-labelledby="bluetooth-title">
    <template #title><h2 id="bluetooth-title">Bluetooth maintenance</h2></template>
    <template #content>
    <Tag role="status" :value="connection + (busy && connected ? ' · Working…' : '')"
      :severity="connected ? 'success' : 'secondary'" />
    <Message v-if="state?.active" severity="warn">RS485 Connect and Arm are blocked because Bluetooth is open. Disconnect here before using Wired.</Message>
    <Message v-if="routeBlock" severity="warn">Bluetooth setting changes and route clear blocked. {{ routeBlock }}</Message>
    <div class="field-row">
      <Button data-ui="agv.bluetooth.connect" type="button" :disabled="Boolean(connectBlock)" v-tooltip.top="connectBlock"
        aria-describedby="bluetooth-connect-block" @click="connect">Connect</Button>
      <Button data-ui="agv.bluetooth.read" type="button" :disabled="Boolean(commonBlock)" v-tooltip.top="commonBlock"
        aria-describedby="bluetooth-session-block" @click="run('read')">Read settings</Button>
      <Button data-ui="agv.bluetooth.disconnect" type="button" :disabled="Boolean(commonBlock)" v-tooltip.top="commonBlock"
        aria-describedby="bluetooth-session-block" @click="run('disconnect')">Disconnect</Button>
    </div>
    <p v-if="connectBlock" id="bluetooth-connect-block">Connect Bluetooth blocked. {{ connectBlock }}</p>
    <p v-if="commonBlock" id="bluetooth-session-block">Read settings and Disconnect blocked. {{ commonBlock }}</p>
    <Message v-if="error" severity="error">{{ error }}</Message>
    <Message v-if="outcome" severity="info" variant="simple" role="status">{{ outcome }}</Message>

    <h3>RS485 learned route</h3>
    <p>{{ stationReport(state?.max_stations) }}</p>
    <p>The learned station count and current position come from Wired status on the AGV page.
      Bluetooth can read raw route records. It cannot show the decoded learned route.</p>
    <h3>Stored Bluetooth route</h3>
    <p v-if="route?.counts">{{ formatCounts(route.counts) }}</p>
    <p v-else>Connect and read settings to see the stored route.</p>
    <Message v-if="route?.error" severity="error">Read station records refused. {{ guidance(state?.feedback?.route, route.error) }}</Message>
    <Disclosure data-ui="agv.bluetooth.records" v-if="route?.records && route?.counts?.some(count => count > 0)" toggle-id="agv.bluetooth.recordstoggle">
      <template #heading>Station records</template>
      <DataTable data-ui="agv.bluetooth.route.table" :value="routeRows" data-key="key" size="small"
        :pt="{ table: { 'aria-label': 'Stored Bluetooth route' }, bodyRow: ({ props }) => ({ 'data-ui': 'agv.bluetooth.record', 'data-ui-key': props.rowData.key }) }">
        <Column field="direction" header="Direction" />
        <Column field="record" header="Record" />
        <Column field="raw" header="Raw bytes"><template #body="{ data }"><code>{{ data.raw }}</code></template></Column>
      </DataTable>
    </Disclosure>
    <div class="field-row">
      <Button data-ui="agv.bluetooth.reviewclear" type="button" :disabled="Boolean(settingsBlock)" v-tooltip.top="settingsBlock" aria-describedby="bluetooth-clear-block"
        @click="run('review-clear')">Clear route</Button>
      <small v-if="settingsBlock" id="bluetooth-clear-block">Clear route blocked. {{ settingsBlock }}</small>
    </div>
    <p>Station dwell, action and bulk edits are not yet available. Their effects on this controller are unverified.</p>
    <p>Bluetooth jog, Start, learning start and Stop. Not available: the Bluetooth stop is not verified yet.</p>
    <div v-if="clearResult" data-ui="agv.bluetooth.clearresult">
      <p>Before clear: {{ formatCounts(clearResult.before) }}</p>
      <p>After clear: {{ formatCounts(clearResult.after) }}</p>
      <template v-if="cleared">
        <h4>Learn the route with the green button</h4>
        <ol>
          <li>Disconnect Bluetooth and follow the site's attended learning procedure.</li>
          <li>Place the AGV on the tape about 1 m before the double line, with the display facing the line.</li>
          <li>Power on and press the onboard green button toward the double line.</li>
          <li>Blue finds the start, purple learns the route, and pulsing red at the far end marks completion.</li>
          <li>Check the learned stations against the floor marks.</li>
        </ol>
      </template>
    </div>

    <form data-ui="agv.bluetooth.write" v-if="connected && !readOnly" class="field-row" novalidate aria-label="Write one setting" @submit.prevent="reviewWrite">
      <label for="bluetooth-field">Setting
        <Select :pt="{ label: { 'data-ui': 'agv.bluetooth.field' } }" input-id="bluetooth-field" aria-label="Setting" v-model="field" placeholder="Select a setting"
          :options="writable" option-value="field" :option-label="item => item.field + ' · ' + item.name" @update:model-value="value = ''" />
      </label>
      <label for="bluetooth-value">New value
        <InputText data-ui="agv.bluetooth.value" id="bluetooth-value" v-model.number="value" type="number"
          :min="selected?.minimum" :max="selected?.maximum" step="1" :disabled="!selected" fluid
          :invalid="Boolean(valueError)" :aria-invalid="valueError ? 'true' : undefined"
          :aria-describedby="valueError ? 'bluetooth-value-error' : undefined" />
        <small v-if="valueError" id="bluetooth-value-error" data-state="failed">{{ valueError }}</small>
      </label>
      <Button data-ui="agv.bluetooth.reviewwrite" type="submit" :disabled="Boolean(writeBlock)" v-tooltip.top="writeBlock" :aria-describedby="valueError ? 'bluetooth-value-error' : writeBlock ? 'bluetooth-write-block' : undefined">Review write</Button>
      <Button data-ui="agv.bluetooth.reviewundo" type="button" :disabled="Boolean(undoBlock)" v-tooltip.top="undoBlock" aria-describedby="bluetooth-undo-block"
        @click="review = { action: 'undo', ...undo }">Review undo</Button>
      <small v-if="writeBlock && !valueError" id="bluetooth-write-block">Review write blocked. {{ writeBlock }}</small>
      <small v-if="undoBlock" id="bluetooth-undo-block">Review undo blocked. {{ undoBlock }}</small>
    </form>
    <p v-if="connected && !readOnly && selected">{{ selected.meaning }} Current {{ selected.value ?? 'unknown' }}.
      Allowed {{ selected.minimum }}–{{ selected.maximum }}.</p>
    <section data-ui="agv.bluetooth.review" v-if="review && !readOnly" aria-labelledby="bluetooth-confirm" class="agv-bluetooth-review">
      <h3 id="bluetooth-confirm">{{ reviewTitle }}</h3>
      <template v-if="review.action === 'clear'">
        <p>{{ formatCounts(review.before) }}</p>
        <p>Park the AGV before confirming. Clearing erases both directions of the stored route. There is no undo.</p>
      </template>
      <template v-else>
        <p>{{ review.field }} · {{ writable.find(item => item.field === review.field)?.name }}</p>
        <p>Old value {{ review.old }} → New value {{ review.value }}</p>
      </template>
      <Button data-ui="agv.bluetooth.confirm" type="button" :disabled="Boolean(confirmBlock)" v-tooltip.top="confirmBlock" aria-describedby="bluetooth-confirm-block" @click="confirm">
        {{ confirmLabel }}
      </Button>
      <small v-if="confirmBlock" id="bluetooth-confirm-block">{{ confirmLabel }} blocked. {{ confirmBlock }}</small>
      <Button data-ui="agv.bluetooth.cancel" type="button" @click="review = null">Cancel</Button>
    </section>

    <DataTable data-ui="agv.bluetooth.settings.table" :value="state?.fields ?? []" data-key="field" size="small"
      :pt="{ table: { 'aria-label': 'Bluetooth settings and controller words' },
        bodyRow: ({ props }) => ({ 'data-ui': 'agv.bluetooth.row', 'data-ui-key': props.rowData.field }) }">
      <template #header>Bluetooth settings and controller words</template>
      <Column field="field" header="Field" :pt="{ bodyCell: { role: 'rowheader' } }" />
      <Column field="name" header="Name" />
      <Column field="raw" header="Raw (little-endian)"><template #body="{ data }"><code>{{ data.raw ?? '—' }}</code></template></Column>
      <Column field="meaning" header="Meaning"><template #body="{ data }">
        {{ data.meaning }}<template v-if="data.read_only_reason"> Read-only. {{ data.read_only_reason }}</template>
      </template></Column>
    </DataTable>
    </template>
  </Card>
</template>

<style scoped>
.agv-bluetooth .p-message { margin-block: .4rem; }
.field-row { align-items: end; margin-block: .75rem; gap: .75rem; flex-wrap: wrap; }
label { display: grid; gap: .3rem; }
#bluetooth-value { width: 6rem; }

.agv-bluetooth-review { border-inline-start: .2rem solid currentColor; padding-inline: 1rem; }
code { white-space: nowrap; }
</style>
