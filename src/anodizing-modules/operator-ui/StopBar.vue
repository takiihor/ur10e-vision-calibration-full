<script setup>
import { computed, watch } from 'vue';
import Message from 'primevue/message';
import Tag from 'primevue/tag';
import Toast from 'primevue/toast';
import { useToast } from 'primevue/usetoast';
import OperatorButton from './OperatorButton.vue';
import StateIcon from './StateIcon.vue';

const props = defineProps({
  agv: { type: Object, required: true }, targets: { type: Array, default: () => [] }, targetOutcomes: Object,
  lineActive: Object, lineAlarm: String, lineStopOutcome: Object, lineEscape: Boolean,
});
defineEmits(['target-stop', 'line-stop']);

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
  <section data-ui="safety.panel" class="stop-bar" aria-label="Urgent controls">
    <div class="stop-line">
      <div class="stop-actions">
        <OperatorButton data-ui="agv.stop" id="stop" label="AGV Stop (software)" urgent stop shortcut
          :why="agv.commandReason('stop')" :outcome="agv.feedback('stop')" @press="agv.send('stop')" />
        <OperatorButton data-ui="agv.pause" id="pause" label="AGV Pause (station-call mode)" urgent
          :why="agv.commandReason('pause')" :outcome="agv.feedback('pause')" @press="agv.send('pause')" />
      </div>
      <div v-if="targets.length" class="stop-actions">
        <OperatorButton data-ui="safety.module.stop" :data-ui-key="target.kind + '.' + target.job_id" v-for="target in targets" :key="target.job_id" :id="target.kind" :label="target.label" urgent stop
          :outcome="targetOutcomes[target.kind + '/' + target.job_id]" @press="$emit('target-stop', target)" />
      </div>
      <div v-if="lineActive || lineStopOutcome" class="stop-actions line-stop-action">
        <OperatorButton data-ui="line.stop" id="line-stop" label="Line Stop · clear command" urgent stop
          :shortcut="lineEscape && Boolean(lineActive)" :why="!lineActive ? 'No active line test.' : ''"
          :outcome="lineStopOutcome" @press="$emit('line-stop')" />
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
    <p v-if="agv.state.executor_stop_history?.confirmed" class="safety-note">
      Previous Stop confirmed · generation {{ agv.state.executor_stop_history.generation }}
    </p>
    <p class="safety-note"><strong>The AGV’s physical e-stop is the emergency stop.</strong>
      {{ lineEscape && lineActive ? 'Esc requests line clear.' : 'Esc sends AGV Stop.' }}
      <span v-if="targets.length">For robot motion use the robot’s physical e-stop.</span></p>
    <p v-if="lineActive || lineAlarm || lineEscape" class="safety-note">
      <strong>Not an emergency stop. Use the line’s physical e-stop.</strong>
    </p>
    <div aria-live="assertive" aria-atomic="true">
      <Message v-if="lineAlarm" severity="error" class="alarm" :pt="{ root: { role: null, 'aria-live': null } }">
        <StateIcon tone="failed" />{{ lineAlarm }}
      </Message>
    </div>
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
