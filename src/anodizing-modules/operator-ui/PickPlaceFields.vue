<script setup>
import InputText from 'primevue/inputtext';

defineProps({ fields: Array, values: Object, prefix: String, dataUi: String });
defineEmits(['edit']);
</script>

<template>
  <div class="field-row">
    <label v-for="[key, label, min, max, step] in fields" :key="key" :for="prefix + '-' + key">
      {{ label }}
      <InputText :data-ui="dataUi" :data-ui-key="key" :id="prefix + '-' + key" :model-value="values[key]" :type="min == null ? 'text' : 'number'"
        :min="min" :max="max" :step="step ?? 'any'" autocomplete="off"
        @update:model-value="$emit('edit', key, $event)" />
    </label>
  </div>
</template>
