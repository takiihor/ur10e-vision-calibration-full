<script setup>
import { computed, nextTick, ref, watch } from 'vue';
import Card from 'primevue/card';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import ProgressSpinner from 'primevue/progressspinner';
import ToggleSwitch from 'primevue/toggleswitch';
import OperatorButton from './OperatorButton.vue';
import StateIcon from './StateIcon.vue';
import { ownerLabel } from './agvFeedback';

const { agv, full, armReasonId } = defineProps({ agv: { type: Object, required: true }, full: Boolean, armReasonId: String });
const ownerSummary = computed(() => {
  const owner = ownerLabel(agv.state.owner) ?? 'None';
  const armed = agv.armed ? agv.state.armed_for ? 'Armed for ' + ownerLabel(agv.state.armed_for) : 'Armed' : 'Disarmed';
  return `Owner ${owner} · ${armed}`;
});
const ownerReason = computed(() => {
  if (agv.operatorReadOnly) return 'Enroll the operator key to change the job owner.';
  if (agv.state.programme_motion_block) return agv.blockText('programme_motion_block');
  if (agv.state.executor_job && ['accepted', 'running', 'waiting'].includes(agv.state.executor_job.state)) {
    return `Job ${agv.state.executor_job.job_id ?? agv.state.executor_job.id ?? 'unknown'} is active. Wait for it to finish or use Stop.`;
  }
  if (agv.state.manual_visit) return `The visit to station ${agv.state.manual_visit.station} is still retained. Review its result before changing the job owner.`;
  if (agv.state.stop_unconfirmed) return 'Verify a safe halt before changing the job owner.';
  if (agv.state.owner && !['agv-page', 'calibration'].includes(agv.state.owner)) {
    return `${agv.state.owner} owns the executor. Ask it to release control or use Stop.`;
  }
  return agv.busy ? 'Wait for the current request.' : '';
});
const armConfirmation = ref(null);
const armOperator = ref('');
const armHeading = ref(null);
const moduleArm = computed(() => agv.state.job_gateway && agv.state.owner && agv.state.owner !== 'agv-page');
const armReview = computed(() => ({
  executor_boot: agv.state.executor_boot, executor_session: agv.state.executor_session,
  stop_generation: agv.state.stop_generation, executor_owner: agv.state.owner,
  connection_id: agv.state.connection_id, lease_id: agv.state.module_lease_id,
}));
const armExpiry = computed(() => {
  const current = agv.state.module_attestation;
  if (!current) return null;
  const time = new Date(current.expires_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  const seconds = Math.ceil(current.remaining_s);
  return { time, countdown: `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}` };
});
watch([() => JSON.stringify(armReview.value), () => agv.haltRevision, () => agv.armReason, () => agv.armed], () => {
  armConfirmation.value = null;
  armOperator.value = '';
}, { flush: 'sync' });

function confirmArm() {
  if (agv.armReason || agv.state.module_arm_enrolled === false || !armConfirmation.value || !armOperator.value.trim()) return;
  const { lease_id, ...review } = armConfirmation.value;
  const operator = armOperator.value.trim();
  armConfirmation.value = null;
  armOperator.value = '';
  void agv.post({ action: 'arm', armed: true, ...review,
    module_attestation: { confirmed: true, operator, owner: review.executor_owner, lease_id } }, 'armed');
}
// A link setting changes on the reported result. Cancel the native switch's immediate toggle.
function requestArm(event) {
  event.preventDefault();
  if (agv.armReason) return;
  if (moduleArm.value && !agv.armed) {
    armConfirmation.value = armReview.value;
    nextTick(() => armHeading.value?.focus());
    return;
  }
  void agv.post({ action: 'arm', armed: !agv.armed }, 'armed');
}
</script>

<template>
  <Card v-if="full" data-ui="agv.owner.panel" class="ownership-card movement [--p-card-body-padding:.75rem]" aria-labelledby="job-owner-title" :data-armed="Boolean(agv.armed)">
    <template #title><h2 id="job-owner-title" class="m-0">Job ownership</h2></template>
    <template #content>
      <p>{{ ownerSummary }}<template v-if="armExpiry"> · confirmation valid until {{ armExpiry.time }} ({{ armExpiry.countdown }} remaining)</template></p>
      <Message v-if="agv.state.module_attestation_expired" severity="warn">The module Arm confirmation expired. Confirm and Arm again.</Message>
      <div v-if="agv.state.job_gateway" class="my-[.45rem] grid gap-[.45rem]" role="group" aria-label="Job owner actions">
        <OperatorButton explain fluid outlined data-ui="agv.owner.manual" id="job-owner-agv" label="Select AGV page" :why="ownerReason"
          :outcome="agv.feedback('job-owner-agv')" @press="agv.post({ action: 'claim_owner', owner: 'agv-page' }, 'job-owner-agv')" />
        <OperatorButton explain fluid outlined data-ui="agv.owner.calibration" v-if="agv.state.calibration_available" id="job-owner-calibration" label="Select Calibration module"
          :why="ownerReason" :outcome="agv.feedback('job-owner-calibration')"
          @press="agv.post({ action: 'claim_owner', owner: 'calibration' }, 'job-owner-calibration')" />
        <OperatorButton explain fluid outlined data-ui="agv.owner.release" v-if="agv.state.owner" id="job-owner-release" label="Release owner" :why="ownerReason"
          :outcome="agv.feedback('job-owner-release')" @press="agv.post({ action: 'claim_owner', owner: null }, 'job-owner-release')" />
      </div>
      <p v-if="agv.state.executor_job">Current job {{ agv.state.executor_job.job_id }} · {{ agv.state.executor_job.state }}</p>
      <label for="agv-armed" class="check">
        <ToggleSwitch input-id="agv-armed" :model-value="Boolean(agv.armed)" :disabled="Boolean(agv.armReason)"
          :pt="{ input: { 'data-ui': 'agv.movement.arm', 'aria-describedby': (armReasonId || 'agv-armed-why') + ' agv-armed-outcome', onClick: requestArm } }" />
        Armed
      </label>
      <small v-if="!armReasonId" id="agv-armed-why" class="outcome">{{ agv.armReason ? (agv.armed ? 'Disarm' : 'Arm') + ' blocked. ' + agv.armReason : '' }}</small>
      <p id="agv-armed-outcome" class="outcome" :data-state="agv.feedback('armed')?.tone">
        <ProgressSpinner v-if="agv.feedback('armed')?.pending" class="field-spinner" aria-hidden="true" />
        <StateIcon v-else :tone="agv.feedback('armed')?.tone" />{{ agv.feedback('armed')?.text }}
      </p>
      <section v-if="armConfirmation" data-ui="agv.arm.attestation" aria-labelledby="module-arm-heading">
        <h3 id="module-arm-heading" ref="armHeading" tabindex="-1">Arm for {{ ownerLabel(armConfirmation.executor_owner) }}</h3>
        <p>I am at the e-stop, the arm is folded, and the path and tank equipment are clear.</p>
        <p>This confirmation covers the module’s moves for up to 30 minutes while it holds control.</p>
        <p v-if="agv.state.module_arm_enrolled === false"><a data-ui="agv.arm.enroll" href="/operator-key">Enroll this browser again to confirm Arm for a module.</a></p>
        <label for="module-arm-operator">Operator name</label>
        <InputText id="module-arm-operator" v-model="armOperator" data-ui="agv.arm.operator" maxlength="80" autocomplete="off" />
        <div class="controls">
          <OperatorButton explain id="confirm-module-arm" data-ui="agv.arm.confirm" label="Confirm and Arm"
            :why="agv.armReason || (agv.state.module_arm_enrolled === false ? 'Enroll this browser again.' : !armOperator.trim() ? 'Enter the operator name.' : '')" @press="confirmArm" />
          <OperatorButton explain id="cancel-module-arm" data-ui="agv.arm.cancel" label="Cancel" @press="armConfirmation = null; armOperator = ''" />
        </div>
      </section>
    </template>
  </Card>
  <p v-else class="text-sm text-muted-color">{{ ownerSummary }}</p>
</template>
