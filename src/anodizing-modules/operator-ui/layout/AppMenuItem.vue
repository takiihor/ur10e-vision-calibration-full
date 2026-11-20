<!-- Adapted from Sakai Vue 5.0.0 (PrimeTek, MIT). See THIRD-PARTY-NOTICES.txt. -->
<script setup>
import Tag from 'primevue/tag';

defineProps({ item: { type: Object, required: true }, active: Boolean });
defineEmits(['navigate']);
</script>

<template>
    <li :class="{ 'active-menuitem': active }">
        <a :href="item.url" :class="{ 'active-route': active }" :aria-current="active ? 'page' : undefined"
            @click="$emit('navigate')">
            <i :class="item.icon" class="layout-menuitem-icon" aria-hidden="true" />
            <span class="layout-menuitem-text">{{ item.label }}</span>
            <Tag v-if="item.legacy" severity="secondary" value="Legacy" />
        </a>
    </li>
</template>
