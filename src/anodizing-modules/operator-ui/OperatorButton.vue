<script setup>
import Button from 'primevue/button';
import vTooltip from 'primevue/tooltip';
import ProgressSpinner from 'primevue/progressspinner';
import StateIcon from './StateIcon.vue';

defineProps({
  dataUi: String,
  dataUiKey: [String, Number],
  id: { type: String, required: true },
  label: { type: String, required: true },
  why: { type: String, default: '' },
  whyId: String,
  tooltip: [String, Object],
  explain: Boolean,
  fluid: Boolean,
  outlined: Boolean,
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
  <div class="control grid min-w-0 max-w-full auto-rows-min grid-cols-[minmax(0,1fr)] content-start justify-items-start gap-[.2rem]" :class="{ 'w-full': fluid }" v-tooltip.bottom="tooltip || (!whyId && why)">
    <Button :data-ui="dataUi" :data-ui-key="dataUiKey" :id="'agv-' + id" :label="label" type="button" :disabled="Boolean(why)"
      :class="{ urgent, stop }" :fluid="fluid" :outlined="outlined" :severity="stop ? 'danger' : primary ? undefined : 'secondary'"
      :icon="stop ? 'pi pi-stop-circle' : undefined"
      :aria-describedby="(whyId || 'agv-' + id + '-why') + ' agv-' + id + '-outcome'"
      :aria-keyshortcuts="shortcut ? 'Escape' : undefined" :aria-current="current ? 'true' : undefined"
      :aria-busy="outcome?.pending || outcome?.tone === 'running'" @click="$emit('press')" />
    <small v-if="!whyId" :id="'agv-' + id + '-why'">{{ why && explain ? label + ' blocked. ' + why : why }}</small>
    <span :id="'agv-' + id + '-outcome'" :data-state="outcome?.tone"><ProgressSpinner v-if="outcome?.pending" class="control-spinner" aria-hidden="true" /><StateIcon v-else :tone="outcome?.tone" />{{ outcome?.text }}</span>
  </div>
</template>
