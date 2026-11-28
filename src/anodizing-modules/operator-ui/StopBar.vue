<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import Drawer from 'primevue/drawer';
import Message from 'primevue/message';
import Tag from 'primevue/tag';
import vTooltip from 'primevue/tooltip';
import { radioOverrides } from '../app/anodizing_web/web/agv-record.mjs';
import Toast from 'primevue/toast';
import { useToast } from 'primevue/usetoast';
import AgvOwnership from './AgvOwnership.vue';
import OperatorButton from './OperatorButton.vue';
import StateIcon from './StateIcon.vue';

const props = defineProps({
  agv: { type: Object, required: true }, targets: { type: Array, default: () => [] }, targetOutcomes: Object,
  route: String, agvPage: Boolean, showOwnership: Boolean, transport: String, linkLost: Boolean, lineActive: Object, lineAlarm: String, lineStopOutcome: Object, lineEscape: Boolean,
});
defineEmits(['target-stop', 'line-stop']);

const narrowScreen = window.matchMedia('(max-width: 56.25rem)');
const narrow = ref(narrowScreen.matches);
const drawerOpen = ref(false);
const controls = ref(null);
const drawerToggle = ref(null);
let resizeObserver;

function measureControls() {
  if (!controls.value) return;
  const height = narrow.value ? Math.ceil(controls.value.getBoundingClientRect().height) : 0;
  document.documentElement.style.setProperty('--operator-controls-height', height + 'px');
}

function resizeControls(event) {
  narrow.value = event.matches;
  if (!narrow.value) drawerOpen.value = false;
  void nextTick(measureControls);
}

function focusControlsButton() {
  if (narrow.value && document.activeElement === document.body) drawerToggle.value?.$el.focus();
}

watch(() => props.route, () => { drawerOpen.value = false; });
onMounted(() => {
  measureControls();
  narrowScreen.addEventListener('change', resizeControls);
  if ('ResizeObserver' in window) {
    resizeObserver = new ResizeObserver(measureControls);
    resizeObserver.observe(controls.value);
  }
});
onUnmounted(() => {
  narrowScreen.removeEventListener('change', resizeControls);
  resizeObserver?.disconnect();
  document.documentElement.style.removeProperty('--operator-controls-height');
});

const controlContainer = computed(() => narrow.value
  ? { id: 'operator-control-drawer', class: 'operator-control-drawer', visible: drawerOpen.value, header: 'AGV controls',
      position: 'right', modal: false, blockScroll: false,
      pt: { root: { role: 'dialog', 'aria-label': 'AGV controls' } } }
  : {});

const toast = useToast();
const stopWhy = computed(() => props.agv.commandReason('stop'));
const pauseWhy = computed(() => props.agv.commandReason('pause'));
const stopHelp = computed(() => [
  'The AGV’s physical e-stop is the emergency stop.',
  props.lineEscape && props.lineActive ? 'Esc requests line clear.' : 'Esc sends AGV Stop.',
  stopWhy.value,
].filter(Boolean).join(' '));
const pauseHelp = computed(() => pauseWhy.value || 'Pause applies in station-call mode.');
const tooltip = text => ({ value: text, class: 'control-help-tooltip' });
const linkLabel = computed(() => props.linkLost ? 'Link lost' : props.agv.connectionLabel);
const linkDetail = computed(() => [props.linkLost
  ? props.agv.connectionDetail.replace(/^[^·]+/, 'Link lost ').trim() : props.agv.connectionDetail,
  props.transport].filter(Boolean).join(' · '));
// A line incident carries the timestamp and the diagnosis for the same link loss.
const alarms = computed(() => props.agv.alarms.filter(({ text }) =>
  !props.linkLost || !/^(?:AGV link lost\b|Link stale\.)/i.test(text)));
const sharedLinkReason = reason => props.linkLost && reason === props.agv.executorDownReason ? 'agv-link-loss' : undefined;
const connectReasonId = computed(() => props.agvPage && props.agv.operatorReadOnly
  ? 'link-lock' : sharedLinkReason(props.agv.connectReason));
const disconnectReason = computed(() => {
  const { agv } = props;
  return agv.operatorKeyReason || agv.blockText('programme_motion_block') ||
    (agv.state.recipe_active ? 'Stop the active recipe before disconnecting.' :
      agv.state.halt_pending || agv.state.stop_unconfirmed ? 'Verify the pending halt before disconnecting.' :
        agv.busy ? 'Wait for the current request.' : '');
});
function connect() {
  const { agv } = props;
  if (agv.connectReason) return;
  const { mode, port, stations } = agv.form;
  void agv.post({ action: 'connect', mode, port, stations, ...radioOverrides(agv.form) }, 'connect');
}

const priorLinkResult = computed(() => props.agv.feedback(props.agv.state.connected ? 'connect' : 'disconnect'));
const feedback = computed(() => props.agv.currentFeedback);
const final = computed(() => ['done', 'failed'].includes(feedback.value?.tone));
const feedbackSeverity = { done: 'success', failed: 'error', waiting: 'warn' };

watch([() => feedback.value?.text, () => feedback.value?.tone], ([text, tone]) => {
  toast.removeGroup('command-result');
  if (final.value) toast.add({ group: 'command-result', summary: text, tone, closable: false });
});
</script>

<template>
  <section ref="controls" data-ui="safety.panel" class="stop-bar sticky flex min-w-0 flex-col self-start" aria-label="AGV controls">
    <div class="stop-strip">
      <div class="stop-help" v-tooltip.bottom.focus="tooltip(stopHelp)" :tabindex="stopWhy ? 0 : undefined"
        :aria-describedby="stopWhy ? 'agv-stop-help' : undefined">
        <OperatorButton data-ui="agv.stop" id="stop" label="AGV Stop (software)" urgent stop shortcut
          :why="stopWhy" why-id="agv-stop-help" :outcome="agv.feedback('stop')" @press="agv.send('stop')"
          :tooltip="tooltip(stopHelp)" />
        <span id="agv-stop-help" class="toolbar-description">{{ stopHelp }}</span>
      </div>
      <Tag v-if="narrow" id="agv-link" role="img" v-tooltip.bottom="linkDetail" :aria-label="linkDetail" :value="linkLabel"
          :severity="linkLost || agv.connectionState === 'Link lost' ? 'danger' : agv.stale ? 'warn' : agv.state.connected ? 'success' : 'secondary'" />
      <Button v-if="narrow" ref="drawerToggle" data-ui="safety.controls.open" label="Controls" icon="pi pi-sliders-h" severity="secondary"
        aria-haspopup="dialog" :aria-expanded="drawerOpen" :aria-controls="drawerOpen ? 'operator-control-drawer' : undefined"
        @click="drawerOpen = !drawerOpen" />
    </div>
    <div class="controls-scroll grid min-h-0 gap-[.45rem] overflow-y-auto px-1 py-[.2rem]">
      <component :is="narrow ? Drawer : 'div'" v-bind="controlContainer" @update:visible="drawerOpen = $event" @after-hide="focusControlsButton">
        <div class="controls-stack grid min-w-0 gap-[.45rem] px-3">
          <div class="stop-help pause-help" v-tooltip.bottom.focus="tooltip(pauseHelp)" :tabindex="pauseWhy ? 0 : undefined"
            :aria-describedby="pauseWhy ? 'agv-pause-why' : undefined">
            <OperatorButton data-ui="agv.pause" id="pause" label="Pause AGV" outlined fluid
              :why="pauseWhy" why-id="agv-pause-why" :outcome="agv.feedback('pause')" @press="agv.send('pause')"
              :tooltip="tooltip(pauseHelp)" />
          </div>
          <Tag v-if="!narrow" id="agv-link" role="img" v-tooltip.bottom="linkDetail" :aria-label="linkDetail" :value="linkLabel"
            :severity="linkLost || agv.connectionState === 'Link lost' ? 'danger' : agv.stale ? 'warn' : agv.state.connected ? 'success' : 'secondary'" />
          <OperatorButton v-if="!agv.state.connected" data-ui="agv.link.connect" id="connect" label="Connect" primary fluid
            :why="agv.operatorKeyReason || agv.connectReason" :why-id="connectReasonId"
            :outcome="agv.feedback('connect')" @press="connect" />
          <OperatorButton v-else data-ui="agv.link.disconnect" id="disconnect" label="Disconnect" outlined fluid
            :why="disconnectReason" :why-id="agvPage && agv.operatorReadOnly ? 'link-lock' : undefined"
            :outcome="agv.feedback('disconnect')" @press="agv.post({ action: 'disconnect' }, 'disconnect')" />
          <Message v-if="priorLinkResult?.tone === 'done' && priorLinkResult.text" variant="simple" severity="success" role="status"
            :id="agv.state.connected ? 'agv-connect-outcome' : 'agv-disconnect-outcome'">{{ priorLinkResult.text }}</Message>
        </div>
        <AgvOwnership v-if="narrow" :key="route" class="mt-2" :agv="agv" :full="showOwnership"
          :arm-reason-id="sharedLinkReason(agv.armReason)" />
      </component>
      <slot name="notice" />
      <div id="agv-alarms" aria-live="assertive" aria-atomic="true">
        <Message v-if="agv.state.stop_unconfirmed" severity="error">
          Stop unconfirmed. Verify a safe halt. Use the AGV e-stop if needed.
        </Message>
        <Message v-if="agv.state.stop_primary_error" severity="warn">{{ agv.state.stop_primary_error }}</Message>
        <Message v-for="{ text, tone } in alarms" :key="text" :severity="tone === 'failed' ? 'error' : 'warn'"
          :pt="{ root: { class: 'alarm', 'data-state': tone, role: null, 'aria-live': null, 'aria-atomic': null }, transition: { css: false } }">
          <StateIcon :tone="tone" />{{ text }}
        </Message>
      </div>
      <AgvOwnership v-if="!narrow" :key="route" :agv="agv" :full="showOwnership"
        :arm-reason-id="sharedLinkReason(agv.armReason)" />
    </div>
    <span id="agv-pause-why" class="toolbar-description">{{ pauseHelp }}</span>
    <div v-if="agv.operatorReadOnly || agv.state.executor_stop_history?.confirmed" class="flex flex-wrap items-center gap-x-[.65rem]">
      <small v-if="agv.operatorReadOnly" data-ui="operator.readonly" class="pause-notice">
        Read-only · <a data-ui="operator.enroll" href="/operator-key" aria-describedby="agv-pause-why">Enroll this browser</a>
      </small>
      <small v-if="agv.state.executor_stop_history?.confirmed" class="stop-history">
        Previous Stop confirmed · generation {{ agv.state.executor_stop_history.generation }}
      </small>
    </div>
    <div v-if="targets.length || lineActive || lineStopOutcome" class="flex flex-wrap items-center gap-[.55rem] pt-[.35rem]">
      <div v-if="targets.length" class="flex flex-wrap gap-[.55rem]">
        <OperatorButton data-ui="safety.module.stop" :data-ui-key="target.kind + '.' + target.job_id" v-for="target in targets" :key="target.job_id" :id="target.kind" :label="target.label" urgent stop
          :outcome="targetOutcomes[target.kind + '/' + target.job_id]" @press="$emit('target-stop', target)" />
      </div>
      <div v-if="lineActive || lineStopOutcome" class="flex flex-wrap gap-[.55rem] basis-56">
        <OperatorButton data-ui="line.stop" id="line-stop" label="Line Stop · clear command" urgent stop
          :shortcut="lineEscape && Boolean(lineActive)" :why="!lineActive ? 'No active line test.' : ''"
          :outcome="lineStopOutcome" @press="$emit('line-stop')" />
      </div>
    </div>
    <Message v-show="feedback?.text" variant="simple" :severity="feedbackSeverity[feedback?.tone] ?? 'info'"
      :pt="{ root: { id: 'agv-outcome', role: 'status', 'aria-live': 'polite', 'data-state': feedback?.tone } }">
      <span id="command-result"><template v-if="!final"><StateIcon :tone="feedback?.tone" />{{ feedback?.text }}</template></span>
    </Message>
    <p v-if="targets.length" class="safety-note">For robot motion use the robot’s physical e-stop.</p>
    <p v-if="lineActive || lineAlarm || lineEscape" class="safety-note">
      <strong>Not an emergency stop. Use the line’s physical e-stop.</strong>
    </p>
    <div aria-live="assertive" aria-atomic="true">
      <Message v-if="lineAlarm" severity="error" class="alarm" :pt="{ root: { role: null, 'aria-live': null } }">
        <StateIcon tone="failed" />{{ lineAlarm }}
      </Message>
    </div>
    <!-- PrimeVue Toast uses a body portal. Show its result inside the persistent status region. -->
    <Toast group="command-result" :auto-z-index="false" :pt="{ root: { hidden: true }, transition: { css: false } }">
      <template #container="{ message }">
        <Teleport to="#command-result">
          <span class="command-toast-inline"><StateIcon :tone="message.tone" />{{ message.summary }}</span>
        </Teleport>
      </template>
    </Toast>
  </section>
</template>
