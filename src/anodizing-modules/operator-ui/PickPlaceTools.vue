<script setup>
import { onMounted, onUnmounted, ref, watch } from 'vue';
import Checkbox from 'primevue/checkbox';
import Select from 'primevue/select';
import OperatorButton from './OperatorButton.vue';
import { request } from './api';
import { agvReason, finishJob, jobPath, savedObject, store, stored } from './pickPlace';

const props = defineProps({
  owner: Object, motion: Object, project: String, item: String, tool: String, busy: String, fence: String,
  perform: Function, outcomes: Object,
});
const emit = defineEmits(['mcu-status', 'mcu-actuation', 'item']);
const tools = [
  { kind: 'mcu', name: 'MCU gripper commands', commands: [
    ['0', '0 · Read sensors'], ['1', '1 · Tight close'], ['2', '2 · Half close'], ['3', '3 · Open'],
  ] },
  { kind: 'tool-power', name: 'MCU supply', commands: [
    ['on', 'Power on 24 V'], ['off', 'Power off 0 V'], ['read', 'Read power status'],
  ] },
];
const key = (kind, name) => 'ano-' + name + '-' + kind;
const state = Object.fromEntries(tools.map(({ kind }) => [kind, ref(null)]));
const pending = Object.fromEntries(tools.map(({ kind }) => [kind, ref(savedObject(key(kind, 'send')))]));
const ids = Object.fromEntries(tools.map(({ kind }) => [kind, ref(pending[kind].value.request_id || stored(key(kind, 'job')))]));
const evidence = Object.fromEntries(tools.map(({ kind }) => [kind, ref(null)]));
const off = ref(false);
const confirmation = ref('');
const released = 'operator_confirmed_released';
let open = true, timer, reading = false;

function same(a, b) {
  return Object.keys(a).length === Object.keys(b).length &&
    Object.keys(a).every(field => JSON.stringify(a[field]) === JSON.stringify(b[field]));
}
function body(kind, command) {
  if (kind === 'tool-power') return { project: props.project, action: command, item_state: props.item || 'unknown' };
  const parent = state.mcu.value?.parent ?? null;
  return { project: props.project, command, parent, agv_off: command !== '0' && !parent && off.value };
}
function why(kind, command) {
  const value = state[kind].value;
  if (props.busy) return 'Wait for the current request.';
  if (props.fence) return props.fence;
  if (!props.owner || !value) return 'Wait for service status or reload to reconnect.';
  if (!props.project) return 'Enter endpoint A’s teaching project.';
  if (kind === 'tool-power' && command === 'off' && props.item !== released) return 'Select Released / no held item before power off.';
  const previous = pending[kind].value;
  if (previous.request_id) return same(previous.body ?? {}, body(kind, command)) ? '' :
    'Inspect the previous request or restore its exact settings.';
  if (props.owner.active && (kind === 'tool-power' || !value.parent || props.owner.active !== value.parent.job_id)) {
    return 'Another operation owns the service. Follow Current operation.';
  }
  if (kind === 'tool-power') return (command === 'read' ? value.read_available : value.write_available) ? '' :
    'Power request unavailable. Check service permissions, ownership and execution state.';
  if (!value.available) return 'MCU command unavailable. Check service permissions and wait for an eligible grip or release prompt.';
  return command !== '0' && !value.parent ? agvReason(props.motion, off.value) : '';
}
function remember(kind, id) {
  ids[kind].value = id;
  store(key(kind, 'job'), id);
}
function clearPending(kind) {
  pending[kind].value = {};
  store(key(kind, 'send'), null);
}
async function refresh() {
  if (reading) return;
  reading = true;
  const results = await Promise.allSettled(tools.map(({ kind }) => request('/api/' + kind, null, 2500)));
  reading = false;
  if (!open) return;
  for (const [index, { kind }] of tools.entries()) state[kind].value = results[index].value ?? null;
  confirmation.value = state.mcu.value ? state.mcu.value.confirmation_detail : 'MCU status unavailable. Wait for service status.';
  emit('mcu-status', pending.mcu.value.request_id ? 'Inspect the unconfirmed MCU request before replying to the prompt.' : confirmation.value);
}
watch(() => pending.mcu.value.request_id, id => {
  emit('mcu-status', id ? 'Inspect the unconfirmed MCU request before replying to the prompt.' : confirmation.value);
});
function send(kind, command) {
  if (why(kind, command)) return;
  const payload = body(kind, command);
  if (kind === 'mcu' && command !== '0') {
    off.value = false;
    emit('mcu-actuation');
  }
  void props.perform(kind + '-' + command, async acknowledged => {
    const existing = pending[kind].value;
    const next = existing.request_id ? existing : { request_id: crypto.randomUUID(), body: payload };
    pending[kind].value = next;
    store(key(kind, 'send'), JSON.stringify(next));
    remember(kind, next.request_id);
    let accepted;
    try {
      accepted = await request('/api/' + kind, { ...next.body, request_id: next.request_id });
      if (accepted.id !== next.request_id) throw new Error('Wrong request identity');
      clearPending(kind);
    } catch (error) {
      if (error.status >= 400 && error.status < 500 && !existing.request_id) clearPending(kind);
      throw error;
    }
    acknowledged();
    const job = await finishJob(accepted, () => open);
    if (!open) return;
    evidence[kind].value = job;
    if (job.state !== 'done') throw Object.assign(new Error(job.error || job.state), {
      operatorMessage: 'Request ended without a confirmed result. Inspect its evidence and check the physical state.',
    });
    return kind === 'mcu' ? 'MCU replied. Check the item physically.' : 'Power request completed. See the recorded voltage.';
  }).finally(() => { if (open) void refresh(); });
}
function inspect(kind) {
  const id = ids[kind].value;
  if (props.busy || !id) return;
  void props.perform(kind + '-check', async () => {
    const job = await request(jobPath(id), null, 3000);
    if (job.id !== id || job.module !== kind) throw new Error('Wrong request identity');
    evidence[kind].value = job;
    clearPending(kind);
    return 'Saved request status retrieved · ' + job.state + '.';
  }).finally(() => { if (open) void refresh(); });
}
function timestamp(value) {
  return value ? new Date(value * 1000).toLocaleString() : 'Time unavailable';
}
onMounted(() => {
  void refresh();
  timer = setInterval(refresh, 1000);
});
onUnmounted(() => {
  open = false;
  clearInterval(timer);
});
</script>

<template>
  <details data-ui="pick.tools.panel" :data-ui-key="kind" v-for="{ kind, name, commands } in tools" :key="kind" class="panel" :open="tool === kind || undefined">
    <summary data-ui="pick.tools.toggle" :data-ui-key="kind">{{ name }}</summary>
    <template v-if="kind === 'mcu'">
      <p>Each click sends one command. Automatic tests issue their own MCU commands. A reply does not verify grip or release.</p>
      <label class="pick-check" for="mcu-off"><Checkbox :pt="{ input: { 'data-ui': 'pick.tools.agv.off', 'data-ui-key': kind } }" v-model="off" input-id="mcu-off" binary
        :disabled="Boolean(owner?.active || busy || fence || !state.mcu.value)" />AGV is switched off for manual MCU actuation.</label>
      <p>{{ state.mcu.value?.parent ? 'MCU request uses the active grip or release prompt.' :
        agvReason(motion, off) || (off ? 'AGV switched off · operator declaration.' : motion?.detail) }}</p>
    </template>
    <template v-else>
      <label for="power-item">Operator-confirmed item state</label>
      <Select :pt="{ label: { 'data-ui': 'pick.tools.item', 'data-ui-key': kind } }" :model-value="item" input-id="power-item" :options="[
        { label: 'Released / no held item', value: released }, { label: 'Held', value: 'operator_confirmed_held' },
      ]" option-label="label" option-value="value" placeholder="Choose current state" @update:model-value="$emit('item', $event)" />
      <p>Power off requires Released / no held item.</p>
    </template>
    <div class="controls">
      <OperatorButton data-ui="pick.tools.command" :data-ui-key="kind + '.' + command" v-for="[command, label] in commands" :id="kind + '-' + command" :key="command"
        :label="pending[kind].value.request_id && !why(kind, command) ? 'Retry same request · ' + label : label"
        :why="why(kind, command)" :outcome="outcomes[kind + '-' + command]" @press="send(kind, command)" />
      <OperatorButton data-ui="pick.tools.check" :data-ui-key="kind" :id="kind + '-check'" label="Check request" :why="busy ? 'Wait for the current request.' :
        !ids[kind].value ? 'No saved request ID yet.' : ''" :outcome="outcomes[kind + '-check']" @press="inspect(kind)" />
    </div>
    <p v-if="pending[kind].value.request_id" role="status">Submission outcome unconfirmed. Check the request ID or explicitly retry the same request.</p>
    <p v-if="ids[kind].value">Request ID · {{ ids[kind].value }}</p>
    <template v-if="state[kind].value?.last_known">
      <template v-if="kind === 'mcu'">
        <p>Last recorded MCU command {{ state.mcu.value.last_known.command }} · {{ state.mcu.value.last_known.status }}.</p>
        <p v-if="state.mcu.value.last_known.fsr">FSR1 {{ state.mcu.value.last_known.fsr[0] }} · FSR2 {{ state.mcu.value.last_known.fsr[1] }}.</p>
      </template>
      <p v-else>Last recorded controller tool-port voltage ·
        {{ state[kind].value.last_known.controller_tool_output_voltage == null ? 'Unknown' : state[kind].value.last_known.controller_tool_output_voltage + ' V' }}
        · {{ timestamp(state[kind].value.last_known.observed_at) }}.</p>
    </template>
    <p v-else>No {{ kind === 'mcu' ? 'MCU reply' : 'voltage reading' }} recorded.</p>
    <details data-ui="pick.tools.evidence" :data-ui-key="kind" v-if="state[kind].value?.last_known || evidence[kind].value">
      <summary data-ui="pick.tools.evidence.toggle" :data-ui-key="kind">Recorded request and reply</summary>
      <pre>{{ JSON.stringify(evidence[kind].value ?? state[kind].value.last_known, null, 2) }}</pre>
    </details>
  </details>
</template>
