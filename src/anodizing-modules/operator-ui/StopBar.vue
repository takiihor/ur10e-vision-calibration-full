<script setup>
import { computed, watch } from 'vue';
import Message from 'primevue/message';
import Tag from 'primevue/tag';
import Toast from 'primevue/toast';
import { useToast } from 'primevue/usetoast';
import OperatorButton from './OperatorButton.vue';
import StateIcon from './StateIcon.vue';

const props = defineProps({ agv: { type: Object, required: true }, targets: { type: Array, default: () => [] }, targetOutcomes: Object });
defineEmits(['target-stop']);

const toast = useToast();
const feedback = computed(() => props.agv.currentFeedback);
const final = computed(() => ['done', 'failed'].includes(feedback.value?.tone));
const feedbackSeverity = { done: 'success', failed: 'error', waiting: 'warn' };

watch([() => feedback.value?.text, () => feedback.value?.tone], ([text, tone]) => {
  toast.removeGroup('command-result');
  if (final.value) toast.add({ group: 'command-result', summary: text, tone, closable: false });
});
</script>

<template>
  <section class="stop-bar" aria-label="Urgent controls">
    <div class="stop-line">
      <div class="stop-actions">
        <OperatorButton id="stop" label="AGV Stop (software)" urgent stop shortcut
          :why="agv.commandReason('stop')" :outcome="agv.feedback('stop')" @press="agv.send('stop')" />
        <OperatorButton id="pause" label="AGV Pause (station-call mode)" urgent
          :why="agv.commandReason('pause')" :outcome="agv.feedback('pause')" @press="agv.send('pause')" />
      </div>
      <div v-if="targets.length" class="stop-actions">
        <OperatorButton v-for="target in targets" :key="target.job_id" :id="target.kind" :label="target.label" urgent stop
          :outcome="targetOutcomes[target.kind + '/' + target.job_id]" @press="$emit('target-stop', target)" />
      </div>
      <div class="head-status">
        <Tag id="agv-link" :value="agv.connectionLabel"
          :severity="agv.stale || agv.serviceError || agv.state.error ? 'danger' : 'secondary'" />
        <Message variant="simple" :severity="feedbackSeverity[feedback?.tone] ?? 'info'"
          :pt="{ root: { id: 'agv-outcome', role: 'status', 'aria-live': 'polite', 'data-state': feedback?.tone } }">
          <span id="command-result"><template v-if="!final"><StateIcon :tone="feedback?.tone" />{{ feedback?.text }}</template></span>
        </Message>
      </div>
    </div>
    <p class="safety-note"><strong>Not an emergency stop. Use the AGV’s e-stop.</strong> Esc sends AGV Stop. <span v-if="targets.length">For robot motion use the robot’s physical e-stop.</span></p>
    <div id="agv-alarms" aria-live="assertive" aria-atomic="true">
      <Message v-for="{ text, tone } in agv.alarms" :key="text" :severity="tone === 'failed' ? 'error' : 'warn'"
        :pt="{ root: { class: 'alarm', 'data-state': tone, role: null, 'aria-live': null, 'aria-atomic': null } }">
        <StateIcon :tone="tone" />{{ text }}
      </Message>
    </div>
    <!-- PrimeVue Toast uses a body portal. Show its result inside the persistent status region. -->
    <Toast group="command-result" :auto-z-index="false" :pt="{ root: { hidden: true }, transition: { css: false } }">
      <template #container="{ message }">
        <Teleport to="#command-result">
          <span class="command-toast-inline"><StateIcon :tone="message.tone" />{{ message.summary }}</span>
        </Teleport>
      </template>
    </Toast>
  </section>
</template>
