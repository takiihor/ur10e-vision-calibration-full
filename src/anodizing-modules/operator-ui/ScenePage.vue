<script setup>
import { computed, defineAsyncComponent, ref, watch } from 'vue';
import Button from 'primevue/button';
import Card from 'primevue/card';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Tag from 'primevue/tag';
import { request } from './api';
import { sceneLabels } from './sceneState';

const SceneCanvas = defineAsyncComponent(() => import('./SceneCanvas.vue'));
const props = defineProps({
  config: Object, lineTanks: Object, lineMode: String, configError: String, stateError: String,
  variable: { type: Function, required: true },
});
const projectPath = ref('');
const project = ref(null);
const loading = ref(false);
const projectError = ref('');
const labels = computed(() => sceneLabels(props.config, props.variable, project.value?.tank, props.lineTanks));
const joints = computed(() => props.variable('robot.joints'));
const model = computed(() => props.variable('robot.model')?.value);
const projectStatus = computed(() => {
  if (loading.value) return 'Loading project…';
  if (projectError.value) return projectError.value;
  if (!project.value) return '';
  if (!project.value.tank) return 'Tank not taught.';
  const tank = props.config?.tanks.find(item => item.id === project.value.tank);
  return tank ? `Taught tank · ${tank.label}` : 'The taught tank is missing from Scene setup.';
});

watch(projectPath, () => {
  project.value = null;
  projectError.value = '';
}, { flush: 'sync' });

async function loadProject() {
  const selection = projectPath.value;
  if (!selection.trim() || loading.value) return;
  loading.value = true;
  project.value = null;
  projectError.value = '';
  try {
    const result = await request('/api/teacher-project?' + new URLSearchParams({ project: selection.trim() }));
    if (selection === projectPath.value && typeof result.project === 'string') project.value = result;
  } catch {
    if (selection === projectPath.value) projectError.value = 'Project unavailable. Check the saved Teaching project path.';
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div data-ui="scene.page" class="scene-grid grid min-w-0 gap-4 min-[1181px]:grid-cols-[minmax(0,2fr)_minmax(16rem,1fr)]">
    <Card data-ui="scene.view.panel" class="panel">
      <template #title><h2>Line view</h2></template>
      <template #content>
        <p><strong>Display only. No collision checking.</strong>
          <Tag v-if="config?.simulation" severity="info" value="Simulation" />
          <Tag v-if="lineMode === 'simulation'" severity="warn" value="Line simulation" />
        </p>
        <p v-if="config?.track">Track and mount · Entered, not measured.</p>
        <p v-if="model?.nominal">Nominal UR10e model.</p>
        <Message v-if="configError" severity="error">{{ configError }}</Message>
        <p v-else-if="!config" role="status">Loading Scene setup…</p>
        <Message v-else-if="!config.drawing" severity="secondary">
          Drawing unavailable. Configure the local GLB and its SHA-256 in the site profile.
        </Message>
        <template v-else>
          <Message v-if="!config.track || !config.robot_mount" severity="secondary">
            Placement incomplete. Check the entered track, station distances and robot mount.
          </Message>
          <SceneCanvas :config="config" :labels="labels" :joints="joints" :model="model" />
        </template>
        <Button data-ui="scene.legacy.open" as="a" href="/preview/tool-envelope.html" severity="secondary" class="page-link"
          label="Open Legacy tool and collision preview" />
      </template>
    </Card>
    <Card data-ui="scene.labels.panel" class="panel">
      <template #title><h2 id="scene-labels-heading">Scene labels</h2></template>
      <template #content>
        <Message v-if="stateError" severity="error">{{ stateError }}</Message>
        <ul class="scene-label-list" aria-labelledby="scene-labels-heading">
          <li data-ui="scene.labels.row" :data-ui-key="item.id" v-for="item in labels" :key="item.id" :data-label="item.id">
            <strong>{{ item.name }}</strong>
            <Tag :value="item.state" :severity="item.unknown ? 'secondary' : 'info'" />
            <span class="scene-age">{{ item.age }}</span>
            <small v-if="item.detail">{{ item.detail }}</small>
            <small v-if="item.taught">Taught tank</small>
          </li>
        </ul>
        <form data-ui="scene.project.form" class="scene-project" @submit.prevent="loadProject">
          <label for="scene-project">Teaching project path</label>
          <InputText data-ui="scene.project.path" id="scene-project" v-model="projectPath" autocomplete="off" aria-describedby="scene-project-status" />
          <Button data-ui="scene.project.load" type="submit" label="Load project" severity="secondary" :disabled="loading || !projectPath.trim()" />
          <Message severity="info" variant="simple" id="scene-project-status" role="status">{{ projectStatus || 'Enter a saved project to show its taught tank.' }}</Message>
        </form>
      </template>
    </Card>
  </div>
</template>
