<!-- Adapted from Sakai Vue 5.0.0 (PrimeTek, MIT). See THIRD-PARTY-NOTICES.txt. -->
<script setup>
import AppMenu from './AppMenu.vue';

defineProps({ open: Boolean, isAgv: Boolean });
defineEmits(['navigate']);
</script>

<template>
    <aside id="operator-nav" class="layout-sidebar" :inert="!open || undefined" :aria-hidden="open ? undefined : true">
        <AppMenu :is-agv="isAgv" @navigate="$emit('navigate')" />
    </aside>
</template>
