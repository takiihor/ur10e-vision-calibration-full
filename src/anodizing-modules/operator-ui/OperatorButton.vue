<script setup>
import Button from 'primevue/button';

defineProps({
  id: { type: String, required: true },
  label: { type: String, required: true },
  why: { type: String, default: '' },
  outcome: { type: Object, default: null },
  urgent: Boolean,
  shortcut: Boolean,
  current: Boolean,
});
defineEmits(['press']);
</script>

<template>
  <div class="control">
    <Button :id="'agv-' + id" :label="label" type="button" :disabled="Boolean(why)"
      :class="{ urgent, stop: id === 'stop' }" :aria-describedby="'agv-' + id + '-why agv-' + id + '-outcome'"
      :aria-keyshortcuts="shortcut ? 'Escape' : undefined" :aria-current="current ? 'true' : undefined"
      :aria-busy="outcome?.tone === 'running'" @click="$emit('press')" />
    <small :id="'agv-' + id + '-why'">{{ why }}</small>
    <span :id="'agv-' + id + '-outcome'" :data-state="outcome?.tone">{{ outcome?.text }}</span>
  </div>
</template>
