<!-- Adapted from Sakai Vue 5.0.0 (PrimeTek, MIT). See THIRD-PARTY-NOTICES.txt. -->
<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import Menu from 'primevue/menu';
import Tag from 'primevue/tag';

const props = defineProps({ route: String });
const current = computed(() => (props.route || '#/').replace(/^#\/tests\/(robot|mcu|tool-power)$/, '#/pick-place')
    .replace(/^#\/(?:line|tests\/line-api)(?:\/.*)?$/, '#/line').replace(/^#\/tests\//, '#/'));
const page = computed(() => current.value.slice(2));
const active = route => current.value === route ? 'page' : undefined;
const header = ref();
const mobileToggle = ref();
const menuOpen = ref(false);
const more = ref();
const moreOpen = ref(false);
let observer;
const moreItems = [
    { label: 'Home', url: '#/', 'data-ui': 'nav.home' },
    { label: 'Scene', url: '#/scene', 'data-ui': 'nav.scene' },
    { label: 'Line', url: '#/line', 'data-ui': 'nav.line' },
    { label: 'Recorder', url: '#/records', 'data-ui': 'nav.recorder' },
];

function closeMenus(restoreFocus = false) {
    if (restoreFocus && menuOpen.value) mobileToggle.value?.$el.focus();
    menuOpen.value = false;
    more.value?.hide();
    moreOpen.value = false;
}

function measure() {
    const height = Math.ceil(header.value.getBoundingClientRect().height);
    document.documentElement.style.setProperty('--operator-header-height', height + 'px');
}

watch(() => props.route, () => closeMenus());
onMounted(() => {
    measure();
    if ('ResizeObserver' in window) {
        observer = new ResizeObserver(measure);
        observer.observe(header.value);
    }
});
onUnmounted(() => {
    observer?.disconnect();
    document.documentElement.style.removeProperty('--operator-header-height');
});
</script>

<template>
    <header ref="header" data-ui="layout.header" class="layout-topbar sticky top-0 z-[2000]" :data-module="page">
        <div class="layout-topbar-nav relative flex items-center justify-between gap-2 px-(--layout-gutter) pt-1">
            <a data-ui="layout.home" href="#/" class="layout-topbar-logo inline-flex items-center gap-[.45rem] text-[1.2rem] font-[750]" @click="closeMenus()">
                Dev console <Tag class="layout-dev-tag" value="DEV" />
            </a>
            <nav data-ui="nav.panel" aria-label="Modules" @keydown.esc="closeMenus(true)">
                <Button ref="mobileToggle" data-ui="layout.navigation.toggle" class="layout-mobile-menu" type="button"
                    label="Menu" icon="pi pi-bars" variant="outlined"
                    :aria-expanded="menuOpen" aria-controls="operator-nav" @click="menuOpen ? closeMenus() : menuOpen = true" />
                <div data-ui="layout.sidebar" id="operator-nav" class="layout-nav-content flex items-center gap-1" :class="{ 'is-open': menuOpen }">
                    <a data-ui="nav.agv" class="layout-tab flex min-h-[44px] items-center px-3" href="#/agv" :aria-current="active('#/agv')" @click="closeMenus()">AGV</a>
                    <a data-ui="nav.calibration" class="layout-tab flex min-h-[44px] items-center px-3" href="#/calibration" :aria-current="active('#/calibration')" @click="closeMenus()">Calibration</a>
                    <a data-ui="nav.teaching" class="layout-tab flex min-h-[44px] items-center px-3" href="#/teaching" :aria-current="active('#/teaching')" @click="closeMenus()">Teaching</a>
                    <a data-ui="nav.pick.place" class="layout-tab flex min-h-[44px] items-center px-3" href="#/pick-place" :aria-current="active('#/pick-place')" @click="closeMenus()">Pick/place</a>
                    <Button data-ui="nav.more.toggle" id="operator-more-toggle" class="layout-more" label="More"
                        icon="pi pi-angle-down" icon-pos="right" variant="text" aria-haspopup="menu"
                        :aria-expanded="moreOpen" aria-controls="operator-more_list" @click="more.toggle($event)" />
                    <Menu ref="more" data-ui="nav.more" id="operator-more" :model="moreItems" popup :base-z-index="2000"
                        aria-labelledby="operator-more-toggle"
                        :pt="{ itemLink: ({ context }) => ({ 'data-ui': context.item['data-ui'], 'aria-current': active(context.item.url) }),
                            transition: { css: false } }"
                        @show="moreOpen = true" @hide="moreOpen = false" />
                </div>
                <div v-if="menuOpen" data-ui="layout.navigation.dismiss" class="layout-menu-dismiss fixed inset-x-0 bottom-0 z-1"
                    aria-hidden="true" @click="closeMenus()" />
            </nav>
        </div>
    </header>
</template>
