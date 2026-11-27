<!-- Adapted from Sakai Vue 5.0.0 (PrimeTek, MIT). See THIRD-PARTY-NOTICES.txt. -->
<script setup>
import AppMenu from './AppMenu.vue';

defineProps({ open: Boolean, route: String });
defineEmits(['navigate']);
</script>

<template>
    <aside data-ui="layout.sidebar" id="operator-nav" class="layout-sidebar" :inert="!open || undefined" :aria-hidden="open ? undefined : true">
        <AppMenu :route="route" @navigate="$emit('navigate')" />
    </aside>
</template>
