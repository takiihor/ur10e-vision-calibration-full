<script setup>
defineProps({ tone: String });

const icons = {
  running: 'pi-sync',
  waiting: 'pi-clock',
  done: 'pi-check-circle',
  failed: 'pi-times-circle',
  serious: 'pi-exclamation-triangle',
};
</script>

<template>
  <i v-if="icons[tone]" class="pi state-icon" :class="icons[tone]" aria-hidden="true"></i>
</template>
