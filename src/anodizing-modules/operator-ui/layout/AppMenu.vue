<!-- Adapted from Sakai Vue 5.0.0 (PrimeTek, MIT). See THIRD-PARTY-NOTICES.txt. -->
<script setup>
import AppMenuItem from './AppMenuItem.vue';

defineProps({ route: String });
defineEmits(['navigate']);

const model = [
    { ui: 'nav.home', label: 'Home', url: '#/', icon: 'pi pi-fw pi-home' },
    { ui: 'nav.calibration', label: 'Calibration', url: '#/calibration', icon: 'pi pi-fw pi-camera' },
    { ui: 'nav.teaching', label: 'Teaching', url: '#/teaching', icon: 'pi pi-fw pi-book' },
    { ui: 'nav.agv', label: 'AGV', url: '#/agv', icon: 'pi pi-fw pi-truck' },
    { ui: 'nav.scene', label: 'Scene', url: '#/scene', icon: 'pi pi-fw pi-map' },
    { ui: 'nav.line', label: 'Line', url: '#/line', icon: 'pi pi-fw pi-chart-line' },
    { ui: 'nav.recorder', label: 'Recorder', url: '#/records', icon: 'pi pi-fw pi-file' },
    { ui: 'nav.pick.place', label: 'Pick/place', url: '#/pick-place', icon: 'pi pi-fw pi-box' },
];

const moduleUrl = route => route?.replace(/^#\/tests\/(robot|mcu|tool-power)$/, '#/pick-place')
    .replace(/^#\/(?:line|tests\/line-api)(?:\/.*)?$/, '#/line').replace(/^#\/tests\//, '#/') || '#/';
</script>

<template>
    <nav data-ui="nav.panel" aria-label="Modules">
        <ul class="layout-menu">
            <li class="layout-root-menuitem">
                <div class="layout-menuitem-root-text">Modules</div>
                <ul class="layout-submenu">
                    <AppMenuItem v-for="item in model" :key="item.url" :item="item"
                        :active="item.url === moduleUrl(route)" @navigate="$emit('navigate')" />
                </ul>
            </li>
        </ul>
    </nav>
</template>
