<!-- Adapted from Sakai Vue 5.0.0 (PrimeTek, MIT). See THIRD-PARTY-NOTICES.txt. -->
<script setup>
import AppMenuItem from './AppMenuItem.vue';

defineProps({ route: String });
defineEmits(['navigate']);

const model = [
    { label: 'Home', url: '#/', icon: 'pi pi-fw pi-home' },
    { label: 'Calibration', url: '/calibration', icon: 'pi pi-fw pi-camera', legacy: true },
    { label: 'Teaching', url: '/teaching', icon: 'pi pi-fw pi-book', legacy: true },
    { label: 'AGV', url: '#/agv', icon: 'pi pi-fw pi-truck' },
    { label: 'Scene', url: '#/scene', icon: 'pi pi-fw pi-map' },
    { label: 'Recorder', url: '#/records', icon: 'pi pi-fw pi-file' },
    { label: 'Pick/place', url: '/pick-place', icon: 'pi pi-fw pi-box', legacy: true },
];
</script>

<template>
    <nav aria-label="Modules">
        <ul class="layout-menu">
            <li class="layout-root-menuitem">
                <div class="layout-menuitem-root-text">Modules</div>
                <ul class="layout-submenu">
                    <AppMenuItem v-for="item in model" :key="item.url" :item="item"
                        :active="item.url === (route?.replace(/^#\/tests\//, '#/') || '#/')" @navigate="$emit('navigate')" />
                </ul>
            </li>
        </ul>
    </nav>
</template>
