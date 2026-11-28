<script setup>
import { ref, watch } from 'vue';
import Accordion from 'primevue/accordion';
import AccordionContent from 'primevue/accordioncontent';
import AccordionHeader from 'primevue/accordionheader';
import AccordionPanel from 'primevue/accordionpanel';

const props = defineProps({ open: Boolean, toggleId: String, toggleKey: [String, Number] });
const emit = defineEmits(['toggle']);
const active = ref(props.open ? 'content' : undefined);
watch(() => props.open, open => { active.value = open ? 'content' : undefined; });
</script>

<template>
  <Accordion class="operator-disclosure" v-model:value="active" @update:value="emit('toggle', $event === 'content')">
    <AccordionPanel value="content">
      <AccordionHeader :data-ui="toggleId" :data-ui-key="toggleKey"><slot name="heading" /></AccordionHeader>
      <AccordionContent><slot /></AccordionContent>
    </AccordionPanel>
  </Accordion>
</template>
