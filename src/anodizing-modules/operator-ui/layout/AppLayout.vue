<!-- Adapted from Sakai Vue 5.0.0 (PrimeTek, MIT). See THIRD-PARTY-NOTICES.txt. -->
<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import AppSidebar from './AppSidebar.vue';
import AppTopbar from './AppTopbar.vue';

const props = defineProps({ isAgv: Boolean, transport: String });
const isDesktop = ref(window.innerWidth >= 992);
const staticMenuInactive = ref(false);
const mobileMenuActive = ref(false);

const menuOpen = computed(() => isDesktop.value ? !staticMenuInactive.value : mobileMenuActive.value);
const containerClass = computed(() => ({
    'layout-mobile-active': mobileMenuActive.value,
    'layout-static-inactive': staticMenuInactive.value,
}));

function toggleMenu() {
    if (isDesktop.value) staticMenuInactive.value = !staticMenuInactive.value;
    else mobileMenuActive.value = !mobileMenuActive.value;
}

function hideMobileMenu() {
    mobileMenuActive.value = false;
}

function updateViewport() {
    isDesktop.value = window.innerWidth >= 992;
    if (isDesktop.value) hideMobileMenu();
}

watch(() => props.isAgv, hideMobileMenu);
onMounted(() => window.addEventListener('resize', updateViewport));
onUnmounted(() => window.removeEventListener('resize', updateViewport));
</script>

<template>
    <div class="layout-wrapper layout-static" :class="containerClass">
        <AppTopbar :menu-open="menuOpen" :transport="transport" @toggle-menu="toggleMenu" @navigate="hideMobileMenu" />
        <AppSidebar :open="menuOpen" :is-agv="isAgv" @navigate="hideMobileMenu" />
        <main id="main" class="layout-main-container" :inert="mobileMenuActive || undefined">
            <div class="layout-main">
                <slot />
            </div>
        </main>
        <div class="layout-mask" aria-hidden="true" @click="hideMobileMenu" />
    </div>
</template>
