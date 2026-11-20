<script setup>
import OperatorButton from './OperatorButton.vue';
import StateIcon from './StateIcon.vue';

defineProps({ agv: { type: Object, required: true }, targets: { type: Array, default: () => [] }, targetOutcomes: Object });
defineEmits(['target-stop']);
</script>

<template>
  <section class="stop-bar" aria-label="Urgent controls">
    <div class="stop-line">
      <OperatorButton id="stop" label="AGV Stop (software)" urgent stop shortcut
        :why="agv.commandReason('stop')" :outcome="agv.feedback('stop')" @press="agv.send('stop')" />
      <OperatorButton id="pause" label="AGV Pause" urgent
        :why="agv.commandReason('pause')" :outcome="agv.feedback('pause')" @press="agv.send('pause')" />
      <OperatorButton v-for="target in targets" :key="target.job_id" :id="target.kind" :label="target.label" urgent stop
        :outcome="targetOutcomes[target.kind + '/' + target.job_id]" @press="$emit('target-stop', target)" />
      <div class="head-status">
        <strong id="agv-link">{{ agv.connectionLabel }}</strong>
        <p id="agv-outcome" role="status" aria-live="polite" :data-state="agv.currentFeedback?.tone"><StateIcon :tone="agv.currentFeedback?.tone" />{{ agv.currentFeedback?.text }}</p>
      </div>
    </div>
    <p class="safety-note"><strong>Not an emergency stop. Use the AGV’s e-stop.</strong> Esc sends AGV Stop. <span v-if="targets.length">For robot motion use the robot’s physical e-stop.</span></p>
    <div id="agv-alarms" aria-live="assertive" aria-atomic="true">
      <p v-for="{ text, tone } in agv.alarms" :key="text" class="alarm" :data-state="tone"><StateIcon :tone="tone" />{{ text }}</p>
    </div>
  </section>
</template>
