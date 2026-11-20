<!-- Adapted from Sakai Vue 5.0.0 (PrimeTek, MIT). See THIRD-PARTY-NOTICES.txt. -->
<script setup>
import Button from 'primevue/button';
import Tag from 'primevue/tag';

defineProps({ menuOpen: Boolean, transport: String });
defineEmits(['toggle-menu', 'navigate']);
</script>

<template>
    <header class="layout-topbar">
        <div class="layout-topbar-logo-container">
            <Button class="layout-menu-button layout-topbar-action" type="button" severity="secondary" icon="pi pi-bars"
                aria-label="Toggle navigation" :aria-expanded="menuOpen" aria-controls="operator-nav"
                @click="$emit('toggle-menu')" />
            <a href="#/" class="layout-topbar-logo" @click="$emit('navigate')">Operator app</a>
        </div>
        <div class="layout-topbar-actions">
            <Tag class="transport-label" severity="secondary" :value="transport" />
        </div>
    </header>
</template>
