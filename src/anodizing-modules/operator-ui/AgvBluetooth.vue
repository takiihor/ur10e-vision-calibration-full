<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { request } from './api';
import { localTime, stationRangeText } from '../app/anodizing_web/web/agv-record.mjs';

const props = defineProps({ readOnly: Boolean });
const path = '/api/agv-bluetooth';
const state = ref(null);
const wired = ref(null);
const session = ref(null);
const busy = ref(false);
const connecting = ref(false);
const error = ref('');
const outcome = ref('');
const field = ref('');
const value = ref('');
const review = ref(null);
let timer;
let retired = false;
const writable = computed(() => state.value?.fields?.filter(item => item.writable) ?? []);
const selected = computed(() => writable.value.find(item => item.field === field.value));
const canReview = computed(() => [0, 1].includes(selected.value?.value) &&
  ['0', '1'].includes(String(value.value)) && Number(value.value) !== selected.value.value);
const undo = computed(() => state.value?.undo);
const canUndo = computed(() => undo.value &&
  writable.value.find(item => item.field === undo.value.field)?.value === undo.value.old);
const connected = computed(() => Boolean(session.value && state.value?.connected));
const connection = computed(() => connecting.value ? 'Scanning and connecting…' : connected.value ? 'Connected' : state.value?.active
  ? 'Another Bluetooth maintenance session is open.' : 'Disconnected');
const station = computed(() => state.value?.max_stations);

function newSession() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
}

async function refresh() {
  try {
    const next = await request(path);
    if (retired) return;
    state.value = next;
    if (!station.value) {
      const cached = await request('/api/agv?cached=1', null, 3000).catch(() => null);
      if (!retired) wired.value = cached?.settings?.mode === 'wired' && cached.executor_health?.simulated !== true &&
        cached.status?.max_stations != null && cached.status.current_station != null && cached.status_time ? cached : null;
    }
  } catch (failure) {
    if (!retired) error.value = failure.status ? failure.message : 'Bluetooth page service unavailable.';
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
    if (action === 'read') outcome.value = 'Settings read.';
    if (action === 'write' || action === 'undo') {
      outcome.value = action === 'undo' ? 'Previous value restored and verified.' : 'New value verified.';
      value.value = '';
    }
  } catch (failure) {
    if (retired || current !== session.value) return;
    error.value = failure.status ? failure.message : 'The Bluetooth page lost contact with its service.';
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
  if (props.readOnly) return;
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
  if (busy.value || !connected.value || state.value?.blocked || !canReview.value || review.value) return;
  review.value = { action: 'write', field: selected.value.field, old: selected.value.value, value: Number(value.value) };
}

function confirm() {
  if (busy.value || !connected.value || state.value?.blocked || !review.value) return;
  const { action, ...details } = review.value;
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
  <section data-ui="agv.bluetooth.page" class="panel agv-surface agv-bluetooth" aria-labelledby="bluetooth-title">
    <h2 id="bluetooth-title">Bluetooth maintenance</h2>
    <p role="status">{{ connection }}<template v-if="busy && connected"> · Working…</template></p>
    <p v-if="state && !state.configured">The Katana site profile needs the AGV's Bluetooth name or address.</p>
    <p v-if="state?.active">RS485 Connect and Arm are blocked while Bluetooth is open.</p>
    <p v-if="state?.blocked">{{ state.blocked }}</p>
    <div class="field-row">
      <button data-ui="agv.bluetooth.connect" type="button" :disabled="readOnly || busy || !state?.configured || state.active" @click="connect">Connect</button>
      <button data-ui="agv.bluetooth.read" type="button" :disabled="readOnly || busy || !connected" @click="run('read')">Read settings</button>
      <button data-ui="agv.bluetooth.disconnect" type="button" :disabled="readOnly || busy || !connected" @click="run('disconnect')">Disconnect</button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="outcome" role="status">{{ outcome }}</p>

    <h3>RS485 learned route</h3>
    <p v-if="station">{{ stationRangeText({ max_stations: station.value }) }} {{ station.source }} · {{ station.observed_at }}{{ station.stale ? ' · Last known' : '' }}</p>
    <p v-else-if="wired">
      {{ stationRangeText(wired.status) }}
      It was at station {{ wired.status.current_station }} (wired status read {{ localTime(wired.status_time).split('.')[0] }}).
    </p>
    <p v-else>Station count unknown: on the AGV page choose Wired, Connect, then Query status.</p>
    <p>The AGV's stored route itself cannot be read over Bluetooth.</p>

    <form data-ui="agv.bluetooth.write" v-if="connected && !readOnly" class="field-row" aria-label="Write one setting" @submit.prevent="reviewWrite">
      <label for="bluetooth-field">Setting
        <select data-ui="agv.bluetooth.field" id="bluetooth-field" v-model="field" @change="value = ''">
          <option value="" disabled>Select a setting</option>
          <option v-for="item in writable" :key="item.field" :value="item.field">{{ item.field }} · {{ item.name }}</option>
        </select>
      </label>
      <label for="bluetooth-value">New value
        <input data-ui="agv.bluetooth.value" id="bluetooth-value" v-model="value" type="number" min="0" max="1" step="1">
      </label>
      <button data-ui="agv.bluetooth.reviewwrite" type="submit" :disabled="busy || !!state.blocked || !canReview || !!review">Review write</button>
      <button data-ui="agv.bluetooth.reviewundo" type="button" :disabled="busy || !!state.blocked || !canUndo || !!review"
        @click="review = { action: 'undo', ...undo }">Review undo</button>
    </form>
    <section data-ui="agv.bluetooth.review" v-if="review && !readOnly" aria-labelledby="bluetooth-confirm" class="agv-bluetooth-review">
      <h3 id="bluetooth-confirm">{{ review.action === 'undo' ? 'Undo last write' : 'Write one setting' }}</h3>
      <p>{{ review.field }} · {{ writable.find(item => item.field === review.field)?.name }}</p>
      <p>Old value {{ review.old }} → New value {{ review.value }}</p>
      <button data-ui="agv.bluetooth.confirm" type="button" :disabled="busy || !connected || !!state?.blocked" @click="confirm">
        {{ review.action === 'undo' ? 'Confirm undo' : 'Confirm write' }}
      </button>
      <button data-ui="agv.bluetooth.cancel" type="button" @click="review = null">Cancel</button>
    </section>

    <div class="agv-bluetooth-table">
      <table>
        <caption>Bluetooth settings</caption>
        <thead><tr><th scope="col">Field</th><th scope="col">Name</th><th scope="col">Raw (little-endian)</th><th scope="col">Meaning</th></tr></thead>
        <tbody>
          <tr data-ui="agv.bluetooth.row" v-for="item in state?.fields" :key="item.field" :data-ui-key="item.field">
            <th scope="row">{{ item.field }}</th><td>{{ item.name }}</td>
            <td><code>{{ item.raw ?? '—' }}</code></td><td>{{ item.meaning }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.agv-bluetooth { padding: 1rem; max-width: 76rem; }
.field-row { align-items: end; margin-block: .75rem; gap: .75rem; flex-wrap: wrap; }
label { display: grid; gap: .3rem; }
#bluetooth-value { width: 6rem; }
button { padding: .4rem .75rem; }
button + button { margin-inline-start: .4rem; }
.agv-bluetooth-review { border-inline-start: .2rem solid currentColor; padding-inline: 1rem; }
.agv-bluetooth-table { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; text-align: left; }
caption { text-align: left; font-weight: 600; margin-block: 1rem .5rem; }
td, th { vertical-align: top; padding: .45rem; border-block-end: 1px solid var(--p-content-border-color, #c6ced9); }
code { white-space: nowrap; }
</style>
