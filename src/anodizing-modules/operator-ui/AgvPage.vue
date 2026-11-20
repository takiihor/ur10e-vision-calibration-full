<script setup>
import { computed, ref } from 'vue';
import Accordion from 'primevue/accordion';
import AccordionContent from 'primevue/accordioncontent';
import AccordionHeader from 'primevue/accordionheader';
import AccordionPanel from 'primevue/accordionpanel';
import Button from 'primevue/button';
import Card from 'primevue/card';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Panel from 'primevue/panel';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import ToggleSwitch from 'primevue/toggleswitch';
import OperatorButton from './OperatorButton.vue';
import StateIcon from './StateIcon.vue';
import AgvRecord from './AgvRecord.vue';
import {
  faultText, fieldLabels, linkQualityText, localTime, modes, speedText, stationCallRequired, stationLimit,
} from '../app/anodizing_web/web/agv-record.mjs';

const { agv } = defineProps({ agv: { type: Object, required: true } });
const status = computed(() => agv.freshReport);
const standard = computed(() => status.value?.mode === 0);
const maximum = computed(() => stationLimit(status.value));
const zero = computed(() => maximum.value === 0);
const stationMode = computed(() => status.value?.mode === 2 && maximum.value > 0);
const age = computed(() => agv.age == null ? 'No status report yet.' : `Last report ${agv.age.toFixed(1)} s ago.`);
const disconnectReason = computed(() => !agv.state.connected ? 'Connect first.' :
  agv.state.recipe_active ? 'Stop the active recipe before disconnecting.' : agv.busy ? 'Wait for the current request.' : '');
const portsBusy = ref(false);
const transports = [{ label: 'Simulator', value: 'simulator' }, { label: 'Serial port', value: 'serial' }];
const portOptions = computed(() => agv.ports.map(({ device, description }) => ({
  value: device, label: description && description !== 'n/a' ? `${device} · ${description}` : device,
})));
const movement = ['forward', 'reverse', 'resume'];
const inquiry = ['query_status', 'read_speed'];
const extra = computed(() => [...agv.commands.values()].filter(({ name, motion }) =>
  motion && ![...movement, 'set_speed', 'call_station'].includes(name)));
const details = computed(() => Object.entries(fieldLabels).filter(([field]) => field in (agv.state.status ?? {})));
const bool = value => value === true ? 'On' : value === false ? 'Off' : 'Unknown';
const stats = computed(() => {
  const report = status.value;
  const speed = report?.actual_speed_m_h;
  const running = report?.stopped === false || (speed != null && speed !== 0);
  const motion = running ? 'Running' : report?.stopped === true && speed === 0 ? 'Stopped' : 'Unknown';
  const actual = speed == null ? 'Unknown' : speedText(speed) + (speed ? speed < 0 ? ' · Reverse' : ' · Forward' : '');
  const stations = standard.value || zero.value
    ? [{ label: 'Stop counter', icon: 'hashtag', value: report?.current_station ?? 'Unknown', glance: true }]
    : [
        { label: 'Current station', icon: 'map-marker', value: report?.current_station ?? 'Unknown', glance: true },
        { label: 'Target station', icon: 'flag', value: report?.target_station ?? 'Unknown', glance: true },
      ];
  return [
    { label: 'Motion', icon: 'car', value: motion, id: 'agv-motion', glance: true },
    ...stations,
    { label: 'Actual speed', icon: 'gauge', value: actual },
    { label: 'Battery', icon: 'bolt', value: report?.battery_percent == null ? 'Unknown' : report.battery_percent + '%' },
    { label: 'Fault', icon: 'exclamation-circle', value: report ? faultText(report) : 'Unknown', tone: report?.fault_code ? 'failed' : undefined },
  ];
});

function label(name) {
  if (standard.value && name === 'forward') return 'Forward';
  if (standard.value && name === 'reverse') return 'Reverse';
  return agv.commands.get(name)?.label ?? name;
}

async function refreshPorts() {
  if (portsBusy.value) return;
  portsBusy.value = true;
  try { await agv.refreshPorts(); }
  finally { portsBusy.value = false; }
}

function changeTransport(mode) {
  agv.form.mode = mode;
  agv.pickPort();
  if (mode === 'serial') void refreshPorts();
}

// A link setting changes on the reported result. Cancel the native switch's immediate toggle.
function requestArm(event) {
  event.preventDefault();
  if (!agv.armReason) void agv.post({ action: 'arm', armed: !agv.armed }, 'armed');
}

function requestSilence(event) {
  event.preventDefault();
  if (!agv.busy) void agv.post({ action: 'silence', silent: !agv.state.silent }, 'silence');
}

function connect() {
  if (agv.connectReason) return;
  const { mode, port, address, channel, stations } = agv.form;
  void agv.post({ action: 'connect', mode, port, address, channel, stations }, 'connect');
}
</script>

<template>
  <div class="agv-grid">
    <div class="agv-main">
      <Panel class="panel agv-surface" aria-labelledby="status-title">
        <template #header>
          <div class="section-title">
            <h2 id="status-title">AGV status</h2>
            <Tag severity="secondary"
              :value="agv.state.connected ? agv.state.settings.mode === 'simulator' ? 'Simulator' : 'Serial AGV' : 'No open connection'" />
          </div>
        </template>
        <div class="stats">
          <Card v-for="stat in stats" :key="stat.label" class="stats-widget">
            <template #content>
              <div class="stats-header">
                <div>
                  <div class="stats-label">{{ stat.label }}</div>
                  <div :id="stat.id" class="stats-value" :class="{ glance: stat.glance }" :data-state="stat.tone">{{ stat.value }}</div>
                </div>
                <span class="stats-icon" aria-hidden="true"><i :class="'pi pi-' + stat.icon" /></span>
              </div>
            </template>
          </Card>
        </div>
        <p v-if="status">Mode {{ status.mode }} · {{ modes[status.mode] ?? 'unknown' }} · {{ standard ? 'Stop line' : 'Station' }} arrival {{ bool(status.station_arrival) }} ·
          Obstacle stop {{ bool(status.obstacle_stop) }} · Command stop {{ bool(status.command_stop) }} · Pause {{ bool(status.paused) }}</p>
        <p v-else>{{ age }}</p>
        <p v-if="agv.state.connected && agv.state.link_quality">{{ linkQualityText(agv.state.link_quality) }}</p>
        <Accordion class="agv-disclosure">
          <AccordionPanel value="details">
            <AccordionHeader>Machine details</AccordionHeader>
            <AccordionContent>
              <p v-if="agv.state.status_time">{{ agv.stale || !agv.state.connected ? 'Last read' : 'Reported' }} {{ localTime(agv.state.status_time) }}</p>
              <dl class="capabilities">
                <template v-for="[field, caption] in details" :key="field">
                  <dt>{{ (agv.state.status.mode === 0 && field === 'position_units') ? 'Distance to the stop line (units)' : caption }}</dt>
                  <dd>{{ field === 'set_speed_m_h' ? speedText(agv.state.status[field]) : typeof agv.state.status[field] === 'boolean' ? bool(agv.state.status[field]) : agv.state.status[field] }}</dd>
                </template>
              </dl>
            </AccordionContent>
          </AccordionPanel>
        </Accordion>
      </Panel>
      <Panel class="panel agv-surface" aria-labelledby="stations-title">
        <template #header><h2 id="stations-title">Stations</h2></template>
        <p v-if="standard">{{ stationCallRequired }}</p>
        <p v-else-if="zero">No route learned. The report shows the stop counter. Teach the route with the AGV’s green button. See the XBOT station guide (V1.61, pp. 2–3).</p>
        <p v-else-if="stationMode">{{ maximum }} stations on the learned route. The current station is marked below.</p>
        <p v-else>Query status to check for a learned route.</p>
        <div v-if="stationMode" class="controls">
          <OperatorButton v-for="station in maximum" :id="'station-' + station" :key="station"
            :label="'Go to station ' + station + (station === status.current_station ? ' · Current' : '')" :current="station === status.current_station"
            :why="agv.commandReason('call_station', station)" :outcome="agv.feedback('station-' + station)"
            @press="agv.send('call_station', 'station-' + station, station)" />
        </div>
        <div v-if="agv.commands.has('call_station') && !standard && !zero" class="field-row">
          <label for="agv-manual-station">Manual station number{{ maximum ? ` (1–${maximum})` : '' }}
            <InputNumber input-id="agv-manual-station" v-model="agv.form.station" fluid :use-grouping="false"
              @input="agv.form.station = $event.value" />
          </label>
          <OperatorButton id="call_station" label="Call station" :why="agv.commandReason('call_station', agv.form.station)"
            :outcome="agv.feedback('call_station')" @press="agv.send('call_station', 'call_station', agv.form.station)" />
        </div>
      </Panel>
    </div>

    <aside class="operation" aria-label="Connection and commands">
      <Panel class="panel agv-surface">
        <template #header><h2>Connection</h2></template>
        <label id="agv-mode-label" for="agv-mode">Transport</label>
        <Select input-id="agv-mode" aria-labelledby="agv-mode-label" :pt="{ label: { 'aria-describedby': 'link-lock' } }"
          :model-value="agv.form.mode" :options="transports" option-label="label" option-value="value" fluid
          :disabled="Boolean(agv.connectionLocked)" @update:model-value="changeTransport" />
        <small id="link-lock" class="outcome">{{ agv.connectionLocked }}</small>
        <div v-if="agv.form.mode === 'serial'" class="port-picker">
          <div class="port-field">
            <label id="agv-port-label" for="agv-port">Port</label>
            <Select input-id="agv-port" v-model="agv.form.port" aria-labelledby="agv-port-label"
              :pt="{ label: { 'aria-describedby': 'link-lock port-error' } }"
              :options="portOptions" option-label="label" option-value="value" placeholder="Select a port" fluid
              :disabled="Boolean(agv.connectionLocked) || !agv.capability?.serial_available" />
          </div>
          <div class="control">
            <Button id="agv-refresh-ports" type="button" severity="secondary" label="Refresh ports"
              :disabled="portsBusy" :aria-busy="portsBusy" aria-describedby="port-refresh-status" @click="refreshPorts" />
            <span v-if="portsBusy" id="port-refresh-status" class="outcome" data-state="running" role="status">
              <ProgressSpinner class="field-spinner" aria-hidden="true" />Refreshing ports…
            </span>
          </div>
          <Message v-if="agv.portError" severity="error" :pt="{ root: { id: 'port-error' } }">{{ agv.portError }}</Message>
        </div>
        <Accordion class="agv-disclosure">
          <AccordionPanel value="settings">
            <AccordionHeader>Link settings</AccordionHeader>
            <AccordionContent>
              <div class="field-row">
                <label for="agv-address">Address (hex)
                  <InputText id="agv-address" v-model="agv.form.address" autocomplete="off" spellcheck="false"
                    :disabled="Boolean(agv.connectionLocked)" aria-describedby="link-lock" />
                </label>
                <label for="agv-channel">Channel (hex)
                  <InputText id="agv-channel" v-model="agv.form.channel" autocomplete="off" spellcheck="false"
                    :disabled="Boolean(agv.connectionLocked)" aria-describedby="link-lock" />
                </label>
                <label v-if="agv.form.mode === 'simulator'" for="agv-stations-config">Simulator stations (0–255)
                  <InputNumber input-id="agv-stations-config" v-model="agv.form.stations" fluid :use-grouping="false"
                    :disabled="Boolean(agv.connectionLocked)" :pt="{ pcInputText: { root: { 'aria-describedby': 'link-lock' } } }"
                    @input="agv.form.stations = $event.value" />
                </label>
              </div>
            </AccordionContent>
          </AccordionPanel>
        </Accordion>
        <div class="controls">
          <OperatorButton id="connect" label="Connect" primary :why="agv.connectReason" :outcome="agv.feedback('connect')" @press="connect" />
          <OperatorButton id="disconnect" label="Disconnect" :why="disconnectReason" :outcome="agv.feedback('disconnect')"
            @press="agv.post({ action: 'disconnect' }, 'disconnect')" />
        </div>
        <Message v-if="agv.frameError" severity="warn" :pt="{ root: { role: 'status', 'aria-live': 'polite' } }">{{ agv.frameError }}</Message>
        <template v-if="agv.state.connected && agv.state.settings.mode === 'simulator'">
          <label class="check" for="agv-silence">
            <ToggleSwitch input-id="agv-silence" :model-value="Boolean(agv.state.silent)" :disabled="agv.busy"
              :pt="{ input: { 'aria-describedby': 'agv-silence-why agv-silence-outcome', onClick: requestSilence } }" />
            Simulate a silent link
          </label>
          <small id="agv-silence-why" class="outcome">{{ agv.busy ? 'Wait for the current request.' : '' }}</small>
          <p id="agv-silence-outcome" class="outcome" :data-state="agv.feedback('silence')?.tone">
            <ProgressSpinner v-if="agv.feedback('silence')?.pending" class="field-spinner" aria-hidden="true" />
            <StateIcon v-else :tone="agv.feedback('silence')?.tone" />{{ agv.feedback('silence')?.text }}
          </p>
        </template>
        <label for="agv-sound" class="check">
          <ToggleSwitch input-id="agv-sound" :model-value="agv.sound" @update:model-value="agv.setSound" />
          Sound an alert for a stale link or a new fault
        </label>
        <div class="controls">
          <OperatorButton v-for="name in inquiry" :id="name" :key="name" :label="name === 'query_status' ? 'Query status' : 'Read speed'" :primary="name === 'query_status'"
            :why="agv.commandReason(name)" :outcome="agv.feedback(name)" @press="agv.send(name)" />
        </div>
      </Panel>
      <Panel class="panel agv-surface movement" :data-armed="Boolean(agv.armed)">
        <template #header><h2>Movement and speed</h2></template>
        <p v-if="agv.state.robot_interlock">Robot travel pose required for motion. Save the pose on Teaching.</p>
        <p v-else-if="agv.state.robot_interlock === false">AGV-only test bench · No robot interlock.</p>
        <label for="agv-armed" class="check">
          <ToggleSwitch input-id="agv-armed" :model-value="Boolean(agv.armed)" :disabled="Boolean(agv.armReason)"
            :pt="{ input: { 'aria-describedby': 'agv-armed-why agv-armed-outcome', onClick: requestArm } }" />
          Armed
        </label>
        <small id="agv-armed-why" class="outcome">{{ agv.armReason }}</small>
        <p id="agv-armed-outcome" class="outcome" :data-state="agv.feedback('armed')?.tone">
          <ProgressSpinner v-if="agv.feedback('armed')?.pending" class="field-spinner" aria-hidden="true" />
          <StateIcon v-else :tone="agv.feedback('armed')?.tone" />{{ agv.feedback('armed')?.text }}
        </p>
        <div v-if="agv.commands.has('set_speed')" class="field-row">
          <label for="agv-speed">Set speed (500–4000 m/h)
            <InputNumber input-id="agv-speed" v-model="agv.form.speed" :use-grouping="false" fluid @input="agv.form.speed = $event.value" />
          </label>
          <OperatorButton id="set_speed" label="Set speed" :why="agv.commandReason('set_speed', agv.form.speed)"
            :outcome="agv.feedback('set_speed')" @press="agv.send('set_speed', 'set_speed', agv.form.speed)" />
        </div>
        <div v-if="agv.commands.has('set_speed')" class="controls">
          <OperatorButton v-for="speed in [500, 1000, 1500]" :id="'speed-' + speed" :key="speed" :label="speedText(speed)"
            :why="agv.commandReason('set_speed', speed)" :outcome="agv.feedback('speed-' + speed)"
            @press="agv.send('set_speed', 'speed-' + speed, speed)" />
        </div>
        <div class="controls">
          <template v-for="name in movement" :key="name">
            <OperatorButton v-if="agv.commands.has(name)" :id="name" :label="label(name)"
              :why="agv.commandReason(name)" :outcome="agv.feedback(name)" @press="agv.send(name)" />
          </template>
        </div>
        <p v-if="standard">Verify the travel direction for the installed vehicle and track.</p>
        <Accordion v-if="extra.length" class="agv-disclosure">
          <AccordionPanel value="more">
            <AccordionHeader>More commands</AccordionHeader>
            <AccordionContent>
              <div class="controls">
                <OperatorButton v-for="command in extra" :id="command.name" :key="command.name" :label="command.label"
                  :why="agv.commandReason(command.name)" :outcome="agv.feedback(command.name)" @press="agv.send(command.name)" />
              </div>
            </AccordionContent>
          </AccordionPanel>
        </Accordion>
      </Panel>
    </aside>

    <AgvRecord :agv="agv" />
  </div>
</template>

<style scoped>
.agv-surface { padding: 0; }
.agv-surface h2 { margin: 0; }
.section-title { flex: 1; align-items: center; }
.stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 11rem), 1fr));
  gap: .65rem; margin-bottom: .8rem;
}
.stats-widget { min-width: 0; background: var(--p-operator-canvas); border: 1px solid var(--p-operator-pressed); }
.stats-widget :deep(.p-card-body) { padding: .7rem; }
.stats-header { display: flex; align-items: start; justify-content: space-between; gap: .5rem; }
.stats-label { color: var(--p-operator-muted); font-weight: 600; }
.stats-value { font-weight: 700; overflow-wrap: anywhere; }
.stats-value.glance { font-size: 1.75rem; }
.stats-icon {
  display: grid; place-items: center; flex: 0 0 2.5rem; height: 2.5rem; border-radius: 6px;
  background: var(--p-operator-raised); color: var(--p-operator-muted);
}
.agv-disclosure { margin-top: .65rem; }
.port-field { flex: 1 1 12rem; }
.outcome:empty { display: none; }
.field-spinner { width: 1em; height: 1em; margin: 0 .4em 0 0; vertical-align: -.14em; }
</style>
