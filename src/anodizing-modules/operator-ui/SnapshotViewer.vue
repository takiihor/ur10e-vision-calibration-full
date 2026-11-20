<script setup>
import { onUnmounted, ref, watch } from 'vue';
import { requestDownload } from './api';

const props = defineProps({ label: String, snapshot: Object, path: String, image: String });
const emit = defineEmits(['loaded']);
const url = ref('');
const loading = ref(false);
const failed = ref(false);
let revision = 0;

function reset() {
  if (url.value) URL.revokeObjectURL(url.value);
  url.value = '';
  emit('loaded', null);
}

watch([() => props.path, () => props.image], async ([path, image]) => {
  const current = ++revision;
  reset();
  failed.value = false;
  loading.value = Boolean(path);
  if (image) {
    url.value = 'data:image/jpeg;base64,' + image;
    loading.value = false;
    return;
  }
  if (!path) return;
  try {
    const blob = await requestDownload(path);
    if (current === revision) url.value = URL.createObjectURL(blob);
  } catch {
    if (current === revision) failed.value = true;
  } finally {
    if (current === revision) loading.value = false;
  }
}, { immediate: true });

onUnmounted(() => {
  revision++;
  if (url.value) URL.revokeObjectURL(url.value);
});
</script>

<template>
  <figure class="teacher-snapshot">
    <img v-if="url && !failed" :key="url" :src="url" :alt="label"
      @load="$emit('loaded', snapshot?.id ?? null)" @error="failed = true; $emit('loaded', null)" />
    <figcaption>
      <strong>{{ label }}</strong>
      <span v-if="snapshot"> · {{ snapshot.detection }} · {{ snapshot.provider }}
        {{ snapshot.camera || '' }} · <time :datetime="snapshot.acquired_at">{{ new Date(snapshot.acquired_at).toLocaleString() }}</time></span>
      <small v-if="snapshot">Capture ID {{ snapshot.id }} · Saved snapshot</small>
      <small v-else-if="path || image">Legacy image · Capture time and detection identity unavailable.</small>
      <small v-else>No saved snapshot.</small>
      <small v-if="loading" role="status">Loading saved image…</small>
      <small v-if="failed" role="status">Image unavailable. Open the saved project or capture again.</small>
    </figcaption>
  </figure>
</template>
