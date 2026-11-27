<script setup>
import Button from 'primevue/button';
import ProgressSpinner from 'primevue/progressspinner';
import StateIcon from './StateIcon.vue';

defineProps({
  dataUi: String,
  dataUiKey: [String, Number],
  id: { type: String, required: true },
  label: { type: String, required: true },
  why: { type: String, default: '' },
  whyId: String,
  outcome: { type: Object, default: null },
  urgent: Boolean,
  stop: Boolean,
  primary: Boolean,
  shortcut: Boolean,
  current: Boolean,
});
defineEmits(['press']);
</script>

<template>
  <div class="control">
    <Button :data-ui="dataUi" :data-ui-key="dataUiKey" :id="'agv-' + id" :label="label" type="button" :disabled="Boolean(why)"
      :class="{ urgent, stop }" :severity="stop ? 'danger' : primary ? undefined : 'secondary'"
      :icon="stop ? 'pi pi-stop-circle' : undefined"
      :aria-describedby="(whyId || 'agv-' + id + '-why') + ' agv-' + id + '-outcome'"
      :aria-keyshortcuts="shortcut ? 'Escape' : undefined" :aria-current="current ? 'true' : undefined"
      :aria-busy="outcome?.pending || outcome?.tone === 'running'" @click="$emit('press')" />
    <small v-if="!whyId" :id="'agv-' + id + '-why'">{{ why }}</small>
    <span :id="'agv-' + id + '-outcome'" :data-state="outcome?.tone"><ProgressSpinner v-if="outcome?.pending" class="control-spinner" aria-hidden="true" /><StateIcon v-else :tone="outcome?.tone" />{{ outcome?.text }}</span>
  </div>
</template>
