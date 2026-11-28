<!-- Adapted from Sakai Vue 5.0.0 (PrimeTek, MIT). See THIRD-PARTY-NOTICES.txt. -->
<script setup>
import AppTopbar from './AppTopbar.vue';

defineProps({ route: String });
</script>

<template>
    <div class="layout-wrapper min-h-dvh">
        <AppTopbar :route="route" />
        <div class="layout-workspace gap-[clamp(.75rem,1.4vw,1.4rem)] px-(--layout-gutter) pt-3 pb-8">
            <slot name="urgent" />
            <main data-ui="layout.main" id="main" class="layout-main-container min-w-0">
                <div class="min-[1960px]:mx-auto min-[1960px]:max-w-[1504px]">
                    <slot />
                </div>
            </main>
        </div>
    </div>
</template>
