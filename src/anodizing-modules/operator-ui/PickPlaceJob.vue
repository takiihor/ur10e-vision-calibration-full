<script setup>
import { computed, ref, watch } from 'vue';
import Checkbox from 'primevue/checkbox';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import OperatorButton from './OperatorButton.vue';
import { ownerReason, terminal } from './pickPlace';

const props = defineProps({
  owner: Object, job: Object, problem: String, running: String, revision: Number, stopId: String, mcuBlocked: String,
  outcomes: Object, clock: Number, evidence: String,
});
const emit = defineEmits(['inspect', 'confirm']);
const id = defineModel('id', { type: String, required: true });
const confirmed = ref(false);
const submitted = ref(null);
const manual = computed(() => props.job?.manual ?? props.job?.result?.manual);
const prompt = computed(() => props.job?.state === 'waiting' && !manual.value?.repeat ? manual.value?.prompt : null);
const promptKey = computed(() => prompt.value ? JSON.stringify([props.job.id, prompt.value]) : '');
watch([promptKey, () => props.problem, () => props.revision, () => props.stopId], () => { confirmed.value = false; });
const confirmWhy = computed(() => {
  if (props.running) return 'Wait for the current request.';
  if (!prompt.value || props.problem || props.owner?.active !== props.job?.id) return 'Wait for an active, current prompt.';
  if (props.stopId === props.job.id) return 'Stop requested for this job. Inspect its outcome.';
  if (submitted.value === promptKey.value) return 'Confirmation already requested. Wait for the next phase or inspect the job.';
  return ownerReason(props.owner, false) || props.mcuBlocked || (!confirmed.value ? 'Check the physical setup and tick the confirmation.' : '');
});
const phase = computed(() => prompt.value?.phase);
const label = computed(() => ({
  plan: manual.value?.robot_step ? 'Approve checked step / start movement' : 'Approve plan / start movement',
  grip: 'Confirm grip / lift 100 mm', release: 'Confirm release / retract 100 mm',
})[phase.value]);
const declaration = computed(() => phase.value === 'grip' ? 'I physically checked that the item is held.' :
  phase.value === 'release' ? 'I physically checked that the item is released.' : manual.value?.robot_step ?
    'I checked the active TCP, endpoints, speed, scene, model and joint bounds. The tool and item fit the scene. The area is clear.' :
    'I reviewed this exact route, workspace, speed and scene. The area is clear.');
const repeat = computed(() => manual.value?.repeat);
const leg = computed(() => manual.value?.agv_leg);
const legCountdown = computed(() => Math.max(0, Math.ceil(
  (leg.value?.ack_s == null ? leg.value?.ack_deadline_epoch : leg.value?.deadline_epoch) - props.clock / 1000,
)));
const route = computed(() => manual.value?.route);
const formatPose = pose => pose?.map((value, index) => (index < 3 ? value * 1000 : value).toFixed(3)).join(', ');
const states = { running: 'Running', waiting: 'Waiting for confirmation', done: 'Completed', failed: 'Failed', interrupted: 'Interrupted' };
const operationName = computed(() => {
  const action = manual.value?.operation || props.job?.auto_action;
  if (repeat.value || action === 'REPEAT') return 'Automatic test';
  if (manual.value?.robot_step || action === 'STEP') return 'Robot step';
  return action || 'Operation';
});
const jobState = computed(() => manual.value?.stage === 'interrupted' && props.job?.state === 'failed'
  ? 'Interrupted' : states[props.job?.state]);

function confirm() {
  if (confirmWhy.value) return;
  submitted.value = promptKey.value;
  confirmed.value = false;
  emit('confirm', props.job.id, prompt.value);
}
</script>

<template>
  <section class="panel pick-operation" aria-labelledby="pick-operation-heading">
    <h2 id="pick-operation-heading">Current operation</h2>
    <Message v-if="manual?.agv_alarm" severity="error" role="alert">{{ manual.agv_alarm }}</Message>
    <p v-if="problem" role="status" data-state="failed">{{ problem }}</p>
    <p v-else-if="job?.cancelled_before_acceptance" role="status">Request cancelled before acceptance. Review and approve a new operation.</p>
    <div v-else-if="job" role="status" aria-live="polite">
      <strong :data-state="terminal(job) ? (job.state === 'done' ? 'done' : 'failed') : 'running'">
        {{ operationName }} · {{ jobState }}
      </strong>
      <p v-if="manual?.stage && !terminal(job)">{{ manual.stage.replaceAll('_', ' ') }}</p>
      <p v-if="repeat">{{ repeat.completed_transfers }}/{{ repeat.transfers }} commanded transfers completed ·
        {{ repeat.source || 'A' }} → {{ repeat.destination || 'B' }}.
        Commanded rod endpoint {{ repeat.commanded_rod_endpoint || 'unknown' }} (unverified).</p>
      <p v-if="manual?.agv_robot_station && !terminal(job)">Robot at AGV station {{ manual.agv_robot_station }}.</p>
      <p v-if="leg">AGV to station {{ leg.to_station }}.
        <template v-if="leg.ack_s != null">Acknowledged in {{ leg.ack_s.toFixed(1) }} s.</template>
        <template v-if="leg.arrival_s != null"> Arrived in {{ leg.arrival_s.toFixed(1) }} s.</template>
        <template v-else-if="!terminal(job) && Number.isFinite(legCountdown)">
          {{ leg.ack_s == null ? 'Waiting for acknowledgement' : 'Waiting for arrival' }} · {{ legCountdown }} s left.
        </template>
      </p>
      <p>Item state · {{ (manual?.item_state || 'unknown').replaceAll('_', ' ') }}</p>
      <p v-if="job.error">Check the saved details and physical state before starting new work.</p>
    </div>
    <p v-else role="status">No operation selected. Item state unknown.</p>
    <slot name="recovery" />
    <div v-if="manual?.robot_step" class="pick-readout">
      <p>Exact step · {{ manual.robot_step.axis.length === 1 ? 'Base ' + manual.robot_step.axis.toUpperCase()
        : 'Tool-local ' + manual.robot_step.axis[1].toUpperCase() + ' rotation' }}
        {{ manual.robot_step.increment }} {{ manual.robot_step.axis.length === 1 ? 'mm' : 'degrees' }}
        at {{ manual.robot_step.joint_speed_deg_s.toFixed(2) }} degrees/s.</p>
      <p>Start · {{ formatPose(manual.robot_step.start_tcp_pose_m_rad) }}<br>
        Target · {{ formatPose(manual.robot_step.target_tcp_pose_m_rad) }}<br>XYZ in mm, rotation vector in radians.</p>
      <p>The server checks the joint path after approval. It can curve and change TCP height or orientation.</p>
    </div>
    <div v-else-if="route?.above && route?.endpoint" class="pick-readout">
      <p>Stopped start Z {{ ((manual.command_start || route.above)[2] * 1000).toFixed(3) }} mm ·
        taught endpoint Z {{ ((manual.taught_pick_tcp_pose_m_rad || route.endpoint)[2] * 1000).toFixed(3) }} mm ·
        descent {{ ((route.above[2] - route.endpoint[2]) * 1000).toFixed(3) }} mm · {{ manual.speed_mm_s }} mm/s.</p>
      <p>Hold actual stopped XY and orientation on the vertical legs.
        {{ manual.operation === 'DESCEND' ? 'Descent only.' : repeat ? 'Automatic MCU command, then straight 100 mm withdrawal.' : 'Confirm grip or release, then straight 100 mm withdrawal.' }}</p>
    </div>
    <template v-if="label">
      <p v-if="phase === 'grip' || phase === 'release'">Use the MCU controls, then check the item physically.</p>
      <label class="pick-check" for="pick-confirm"><Checkbox v-model="confirmed" input-id="pick-confirm" binary
        :disabled="Boolean(problem || running)" />{{ declaration }}</label>
      <OperatorButton id="pick-confirm" :label="label" :why="confirmWhy" :outcome="outcomes['pick-confirm']" primary @press="confirm" />
    </template>
    <details>
      <summary>Inspect operation and evidence</summary>
      <label for="pick-job-id">Operation ID</label>
      <InputText id="pick-job-id" v-model="id" autocomplete="off" />
      <div class="controls">
        <OperatorButton id="pick-inspect" label="Inspect status" :why="!id.trim() ? 'Enter the operation ID.' : ''"
          :outcome="outcomes['pick-inspect']" @press="$emit('inspect', id.trim())" />
        <OperatorButton v-if="owner?.active && owner.active !== job?.id" id="pick-follow" label="Follow active operation"
          @press="$emit('inspect', owner.active)" />
      </div>
      <p>Inspection reads saved status. Stop ends the operation. It does not release the item or resume motion.
        Accepted automatic tests continue if the page disconnects.</p>
      <pre v-if="evidence">{{ evidence }}</pre>
      <pre v-if="job">{{ JSON.stringify(job, null, 2) }}</pre>
    </details>
  </section>
</template>
