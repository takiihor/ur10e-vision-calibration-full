<script setup>
import Disclosure from './Disclosure.vue';
import Checkbox from 'primevue/checkbox';
import { computed, nextTick, ref, watch } from 'vue';
import Accordion from 'primevue/accordion';
import AccordionContent from 'primevue/accordioncontent';
import AccordionHeader from 'primevue/accordionheader';
import AccordionPanel from 'primevue/accordionpanel';
import Button from 'primevue/button';
import Tabs from 'primevue/tabs';
import TabPanels from 'primevue/tabpanels';
import TabPanel from 'primevue/tabpanel';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import Badge from 'primevue/badge';
import Card from 'primevue/card';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import ToggleSwitch from 'primevue/toggleswitch';
import OperatorButton from './OperatorButton.vue';
import StateIcon from './StateIcon.vue';
import AgvRecord from './AgvRecord.vue';
import AgvBluetooth from './AgvBluetooth.vue';
import { simulationEnabled } from './api';
import { usePageTab } from './usePageTab';
import { learnedRange, ownerLabel, stationReport, teachStations } from './agvFeedback';
import {
  faultText, fieldLabels, linkQualityText, localTime, modes, motionText, speedText, stationCallRequired,
  radioPlaceholder, stationLimit,
} from '../app/anodizing_web/web/agv-record.mjs';

const { agv, linkLost } = defineProps({
  agv: { type: Object, required: true },
  linkLost: Boolean,
});
const tab = usePageTab('agv', ['overview', 'stations', 'connection', 'speed', 'bluetooth', 'details']);
const bluetoothWarning = ref(false);
watch(tab, value => { if (value !== 'bluetooth') bluetoothWarning.value = false; });
const status = computed(() => agv.freshReport);
const standard = computed(() => status.value?.mode === 0);
const maximum = computed(() => agv.maximumStation);
const manualDeadline = computed(() => agv.manualTravel
  ? new Date(agv.state.executor_health.manual_travel_until * 1000).toLocaleString()
  : null);
const zero = computed(() => maximum.value === 0);
const stationMode = computed(() => status.value?.mode === 2 && maximum.value > 0);
const parking = computed(() => agv.state.station_profile?.parking_station ?? 17);
const latestStation = computed(() => {
  const last = agv.state.status;
  if (stationLimit(last) !== undefined) {
    return { value: last.max_stations, current_station: last.current_station, observed_at: agv.state.status_time,
      stale: !agv.state.connected || agv.stale };
  }
  return agv.state.wired_station;
});
const learnedMaximum = computed(() => stationLimit({ max_stations: latestStation.value?.value }));
const stations = computed(() => Array.from({ length: Math.max(17, learnedMaximum.value ?? 0) }, (_, i) => i + 1)
  .filter(station => station !== parking.value));
const rangeReason = computed(() => {
  const limit = learnedMaximum.value;
  if (limit === undefined || limit >= Math.max(17, parking.value)) return '';
  const which = limit === 0 ? 'All station calls' : `Calls above station ${limit}`;
  return `${which} blocked. ${learnedRange(latestStation.value.value)} ${teachStations}`;
});
const callReason = computed(() => agv.commandReason('call_station', agv.state.manual_visit?.station ?? 1));
const manualVisit = computed(() => agv.state.manual_visit);
const confirmation = ref(null);
const confirmationHeading = ref(null);
watch(tab, value => { if (value !== 'stations') confirmation.value = null; }, { flush: 'sync' });
watch([() => agv.haltRevision, () => agv.state.executor_boot, () => agv.state.executor_session,
  () => agv.state.stop_generation, () => agv.state.connection_id, () => agv.state.stop_unconfirmed,
  () => agv.armed, () => agv.manualTravel, () => agv.isolatedTrack, () => Boolean(agv.state.motion_block)], () => {
  confirmation.value = null;
}, { flush: 'sync' });

function requestStation(station, control) {
  if (!agv.manualTravel) return agv.send('call_station', control, station);
  if (agv.commandReason('call_station', station)) return;
  confirmation.value = { station, control };
  nextTick(() => confirmationHeading.value?.focus());
}

function confirmStation() {
  const request = confirmation.value;
  confirmation.value = null;
  if (!request || !agv.manualTravel) return;
  const { station, control } = request;
  return agv.send('call_station', control, station, false, {
    id: crypto.randomUUID().replaceAll('-', ''), station, confirmed_at: Date.now() / 1000,
  });
}

const evidenceFaults = computed(() => Object.values(agv.state.evidence_faults ?? {}));
const operatorCheck = computed(() => {
  const check = agv.state.operator_check;
  return check && (check.technician || (!agv.state.programme_motion_block && check.run_id == null)) ? check : null;
});
const operator = ref('');
const location = ref('');
const confirmed = ref(false);
watch(() => JSON.stringify([operatorCheck.value, agv.state.review_cursor, agv.state.executor_boot,
  agv.state.executor_session, agv.state.stop_generation, agv.state.connection_id, agv.state.stop_unconfirmed]), () => {
  operator.value = '';
  location.value = '';
  confirmed.value = false;
}, { flush: 'sync' });
const abortReason = computed(() => agv.operatorKeyReason || (agv.state.stop_unconfirmed ? 'Verify a safe halt first.' :
  agv.busy ? 'Wait for the current request.' :
    !operator.value.trim() || !location.value.trim() || !confirmed.value ? 'Confirm the AGV location and enter the operator.' : ''));

function abortVisit() {
  if (!operatorCheck.value || operatorCheck.value.technician || evidenceFaults.value.length || abortReason.value) return;
  void agv.post({ action: 'operator_check', choice: 'abort', job_id: operatorCheck.value.job_id,
    basket: null, operator: operator.value.trim(), location: location.value.trim(), confirmed: true }, 'abort-station');
}

const visitOutcome = computed(() => manualVisit.value?.result?.outcome);
const visitSummary = computed(() => {
  const visit = manualVisit.value;
  if (!visit) return '';
  const state = visit.result?.outcome ?? (visit.result ? 'in progress' : 'awaiting executor evidence');
  return `Station ${visit.station} visit ${state}`;
});
const age = computed(() => agv.age == null ? 'No status report yet.' : `Last report ${agv.age.toFixed(1)} s ago.`);
const sharedLinkReason = computed(() => agv.operatorReadOnly ? 'link-lock' : undefined);
const portsBusy = ref(false);
const transports = computed(() => {
  if (agv.capability?.executor_link) return [{ label: 'IPC executor', value: 'wired' }];
  const available = [{ label: 'Serial port', value: 'serial' }];
  if (agv.capability?.wired_port || agv.state.wired_port) available.unshift({ label: 'Wired RS-485', value: 'wired' });
  if (simulationEnabled()) available.unshift({ label: 'Simulator', value: 'simulator' });
  return available;
});
const portOptions = computed(() => agv.ports.map(({ device, description }) => ({
  value: device, label: description && description !== 'n/a' ? `${device} · ${description}` : device,
})));
const movement = ['forward', 'reverse', 'resume'];
const inquiry = ['query_status', 'read_speed'];
const extra = computed(() => [...agv.commands.values()].filter(({ name, motion }) =>
  motion && ![...movement, 'set_speed', 'call_station'].includes(name)));
const details = computed(() => Object.entries(fieldLabels).filter(([field]) => field in (agv.state.status ?? {})));
const bool = value => value === true ? 'On' : value === false ? 'Off' : 'Unknown';
const batteryTrends = new Map([['charging', 'Charging'], ['in_use', 'In use'], ['steady', 'Steady']]);
const stats = computed(() => {
  const report = status.value;
  const level = report?.battery_percent;
  const battery = Number.isFinite(level) && level >= 0 && level <= 100 ? level : null;
  const speed = report?.actual_speed_m_h;
  const motion = motionText(report);
  const actual = speed == null ? 'Unknown' : speedText(speed) + (speed ? speed < 0 ? ' · Reverse' : ' · Forward' : '');
  const counter = standard.value || zero.value;
  return [
    { icon: 'car', label: 'Motion', metrics: [
      { key: 'motion', value: motion, id: 'agv-motion', tone: motion === 'Running' ? 'running' : undefined },
      { key: 'speed', label: 'Actual speed', value: actual },
    ] },
    { icon: counter ? 'hashtag' : 'map-marker', label: counter ? 'Stop counter' : 'Station', metrics: counter
      ? [{ key: 'counter', value: report?.current_station ?? 'Unknown' }]
      : [{ key: 'current', value: stationLabel(report?.current_station, true) },
        { key: 'target', value: stationLabel(report?.target_station, true), target: true }], note: counter ? null : 'Current → target' },
    { icon: 'bolt', label: 'Battery', metrics: [
      { key: 'battery', value: battery === null ? 'Unknown' : battery + '%' },
    ], note: battery !== null && agv.age !== null && agv.age < 3 ? batteryTrends.get(agv.state.battery_trend) : null,
    warning: battery !== null && battery < 20 ? (battery < 10 ? 'error' : 'warn') : null },
    { icon: 'exclamation-circle', label: 'Fault', metrics: [
      { key: 'fault', value: report ? faultText(report) : 'Unknown', tone: report?.fault_code ? 'failed' : undefined },
    ], note: 'Controller report' },
  ];
});

function stationLabel(station, showNumber = false) {
  if (station == null) return 'Unknown';
  const name = agv.state.station_profile?.station_labels?.[station];
  const local = station === parking.value ? name ? `Parking · ${name}` : 'Parking' : name;
  return local ? local + (showNumber ? ` (${station})` : '') : `Station ${station}`;
}

function stationReason(station) {
  if (rangeReason.value && station > learnedMaximum.value) return rangeReason.value;
  return agv.state.manual_visit ? agv.commandReason('call_station', station) : callReason.value;
}

function stationReasonId(station) {
  const reason = stationReason(station);
  if (reason && reason === rangeReason.value) return 'agv-range-warning';
  if (reason && reason === callReason.value) return 'agv-station-call-reason';
}

function label(name) {
  return agv.commands.get(name)?.label ?? name;
}

async function refreshPorts() {
  if (portsBusy.value) return;
  portsBusy.value = true;
  try { await agv.refreshPorts(); }
  finally { portsBusy.value = false; }
}

function changeTransport(mode) {
  agv.changeTransport(mode);
  if (mode === 'serial') void refreshPorts();
}

function requestSilence(event) {
  event.preventDefault();
  if (!agv.busy && !agv.operatorReadOnly) void agv.post({ action: 'silence', silent: !agv.state.silent }, 'silence');
}

</script>

<template>
  <div data-ui="agv.page" class="agv-page min-w-0">
    <Tabs v-model:value="tab" :show-navigators="false" class="page-tabs">
      <TabList data-ui="agv.bluetooth.tabs" :pt="{ tabList: { 'aria-label': 'AGV panels' } }">
        <Tab data-ui="agv.bluetooth.wiredtab" value="overview">Overview</Tab>
        <Tab data-ui="agv.tabs.stations" value="stations">Stations &amp; move
          <Badge v-if="(!standard && rangeReason) || operatorCheck || agv.state.quality_alarm" value="Warning" severity="warn" />
        </Tab>
        <Tab data-ui="agv.tabs.connection" value="connection">Connection
          <Badge v-if="linkLost || agv.stale || agv.serviceError || agv.state.error || agv.frameError || agv.portError" value="Warning" severity="warn" />
        </Tab>
        <Tab data-ui="agv.tabs.speed" value="speed">Speed &amp; settings
          <Badge v-if="agv.state.stop_unconfirmed || (agv.state.connected && (agv.state.status?.fault_code || agv.state.status?.obstacle_stop))" value="Warning" severity="warn" />
        </Tab>
        <Tab data-ui="agv.bluetooth.tab" value="bluetooth">Bluetooth
          <Badge v-if="agv.state.bluetooth_block || bluetoothWarning" value="Warning" severity="warn" />
        </Tab>
        <Tab data-ui="agv.tabs.details" value="details">Machine details &amp; log
          <Badge v-if="agv.journal.log_gap || evidenceFaults.length" value="Warning" severity="warn" />
        </Tab>
      </TabList>
      <section data-ui="agv.status.panel" class="my-4 grid grid-cols-[repeat(auto-fit,minmax(min(100%,11.5rem),1fr))] gap-[.65rem]" aria-label="AGV readouts">
        <Card v-for="tile in stats" :key="tile.label" class="min-w-0 [--p-card-background:var(--p-operator-canvas)] [--p-card-body-padding:.7rem]" role="presentation">
          <template #content>
            <div class="flex items-center justify-between gap-[.4rem] text-[.8125rem] font-bold tracking-[.04em] text-muted-color uppercase">
              {{ tile.label }}
              <span class="shrink-0" aria-hidden="true"><i :class="'pi pi-' + tile.icon" /></span>
            </div>
            <div v-for="metric in tile.metrics" :key="metric.key" data-ui="agv.status.metric" :data-ui-key="metric.key"
              :id="metric.id" class="stats-value" :class="{ 'stats-secondary': metric.label, 'stats-target': metric.target }" :data-state="metric.tone">
              <template v-if="metric.target">→ </template><template v-else-if="metric.label">{{ metric.label }} · </template>{{ metric.value }}
            </div>
            <small v-if="tile.note">{{ tile.note }}</small>
            <Message v-if="tile.warning" :severity="tile.warning" variant="simple">Battery low. Please charge.</Message>
          </template>
        </Card>
      </section>
      <div class="tab-workspace">
        <div class="agv-main">
          <Message v-if="agv.state.bluetooth_block" severity="warn">{{ agv.blockText('bluetooth_block') }}</Message>
          <Card v-if="operatorCheck || manualVisit || evidenceFaults.length || agv.state.quality_alarm"
            class="agv-surface" role="presentation">
            <template #title><h2>Station visit status</h2></template>
            <template #content>
              <Message v-if="agv.state.quality_alarm" severity="warn">{{ agv.state.quality_alarm.reason }}</Message>
              <Message v-for="fault in evidenceFaults" :key="fault" severity="error">{{ fault }}</Message>
              <section v-if="operatorCheck" data-ui="agv.check.panel" aria-labelledby="agv-operator-check">
                <h3 id="agv-operator-check">Operator check</h3>
                <Message severity="warn">{{ agv.blockText('operator_check', operatorCheck.reason) }}</Message>
                <p><template v-if="operatorCheck.job_id">Job {{ operatorCheck.job_id }} · </template>
                  Last observed location {{ operatorCheck.location ?? 'unknown' }}</p>
                <p v-if="operatorCheck.earlier_location">Earlier observed location {{ operatorCheck.earlier_location }}</p>
                <template v-if="!operatorCheck.technician && !evidenceFaults.length">
                  <div class="field-row">
                    <label for="agv-check-operator">Operator
                      <InputText id="agv-check-operator" data-ui="agv.check.operator" v-model="operator" maxlength="500" />
                    </label>
                    <label for="agv-check-location">Observed AGV location
                      <InputText id="agv-check-location" data-ui="agv.check.location" v-model="location" maxlength="500" />
                    </label>
                  </div>
                  <label class="check">
                    <Checkbox v-model="confirmed" binary :pt="{ input: { 'data-ui': 'agv.check.confirmed' } }" />
                    I physically checked the AGV location
                  </label>
                  <OperatorButton explain data-ui="agv.check.abort" id="abort-station"
                    :label="manualVisit?.id === operatorCheck.job_id ? 'Abort station visit' : 'Confirm safe hold'"
                    :why="abortReason" :outcome="agv.feedback('abort-station')" @press="abortVisit" />
                </template>
              </section>
              <div v-if="manualVisit">
                <Message severity="info" variant="simple" role="status">{{ visitSummary }} {{ agv.blockText('visit', manualVisit.result?.error || manualVisit.doubt) }}</Message>
                <OperatorButton explain v-if="visitOutcome === 'succeeded' && !operatorCheck && !evidenceFaults.length" data-ui="agv.call.acknowledge"
                  id="acknowledge-station" label="Acknowledge result" :why="agv.busy ? 'Wait for the current request.' : ''"
                  :outcome="agv.feedback('acknowledge-station')"
                  @press="agv.post({ action: 'acknowledge_station', job_id: manualVisit.id }, 'acknowledge-station')" />
              </div>
            </template>
          </Card>
          <TabPanels data-ui="agv.controls.panel">
            <TabPanel value="overview">
              <Card class="agv-surface" role="presentation">
                <template #title><h2>AGV status</h2></template>
                <template #content>
                  <p v-if="status">Mode {{ status.mode }} · {{ modes[status.mode] ?? 'unknown' }} · {{ standard ? 'Stop line' : 'Station' }} arrival {{ bool(status.station_arrival) }} ·
                    Obstacle stop {{ bool(status.obstacle_stop) }} · Command stop {{ bool(status.command_stop) }} · Pause {{ bool(status.paused) }}</p>
                  <p v-else>{{ age }}</p>
                  <p v-if="agv.state.connected && agv.state.link_quality">{{ linkQualityText(agv.state.link_quality) }}</p>
                </template>
              </Card>
            </TabPanel>
            <TabPanel value="stations" class="agv-main">
              <Card data-ui="agv.call.panel" class="panel agv-surface" aria-labelledby="stations-title">
                <template #title><h2 id="stations-title">Stations</h2></template>
                <template #content>
                <p v-if="standard">{{ stationCallRequired }}</p>
                <p v-if="!standard">{{ stationReport(latestStation) }}</p>
                <p v-if="manualDeadline" data-ui="agv.call.window">
                  <template v-if="agv.isolatedTrack">
                    Supervised folded-arm travel on the line tape. Tank-clear and equipment-off signals are not connected yet: check the path and the tank equipment by eye before each call.
                    The AGV’s fresh battery percentage supplies the reserve check.
                  </template>
                  Manual travel deadline {{ manualDeadline }} (local time). DRAFT battery floor {{ agv.state.executor_health.manual_travel_min_battery }}%.
                </p>
                <Message v-if="!standard && rangeReason" id="agv-range-warning" data-ui="agv.call.range.warning" severity="warn">
                  {{ rangeReason }}
                </Message>
                <Message v-if="!standard && callReason" id="agv-station-call-reason" data-ui="agv.call.reason" severity="info">Station calls blocked. {{ callReason }}</Message>
                <div v-if="!manualVisit && agv.state.manual_visit_history">
                  {{ agv.state.manual_visit_history.refused ? 'Last station visit refused' :
                    agv.state.manual_visit_history.operator_check ? 'Station visit aborted' : 'Last acknowledged station visit' }}
                  {{ agv.state.manual_visit_history.id }}
                  <p v-if="agv.state.manual_visit_history.refused">Call to station {{ agv.state.manual_visit_history.station }} refused.
                    {{ agv.blockText('refused', agv.state.manual_visit_history.refused) }}</p>
                </div>
                <section v-if="confirmation" data-ui="agv.call.attestation" aria-labelledby="station-confirmation-heading">
                  <h3 id="station-confirmation-heading" ref="confirmationHeading" tabindex="-1">Confirm station {{ confirmation.station }}</h3>
                  <p>I am at the e-stop, the arm is folded, and the path and tank equipment are clear.</p>
                  <p v-if="agv.isolatedTrack">My observation of the folded arm applies to this station call only.</p>
                  <div class="field-row">
                    <OperatorButton explain id="confirm-station" data-ui="agv.call.confirm" label="Confirm and call"
                      :why="agv.commandReason('call_station', confirmation.station)" @press="confirmStation" />
                    <OperatorButton explain id="cancel-station" data-ui="agv.call.cancel" label="Cancel" @press="confirmation = null" />
                  </div>
                </section>
                <div v-if="!standard" data-ui="agv.call.grid" class="station-grid my-[.65rem] grid grid-cols-[repeat(auto-fit,minmax(min(100%,7rem),1fr))] gap-[.45rem]">
                  <OperatorButton explain data-ui="agv.call.station" :data-ui-key="station" v-for="station in stations" :id="'station-' + station" :key="station"
                    :label="stationLabel(station) + (station === status?.current_station && stationMode ? ' · Current' : '')"
                    :current="station === status?.current_station && stationMode"
                    :why="stationReason(station)" :why-id="stationReasonId(station)"
                    :outcome="agv.feedback('station-' + station)"
                    @press="requestStation(station, 'station-' + station)" />
                </div>
                <OperatorButton explain v-if="!standard" data-ui="agv.call.parking" :id="'station-' + parking" class="parking-call mt-[.65rem] border-t border-section pt-[.65rem]"
                  :label="stationLabel(parking) + (parking === status?.current_station && stationMode ? ' · Current' : '')"
                  :current="parking === status?.current_station && stationMode"
                  :why="stationReason(parking)" :why-id="stationReasonId(parking)"
                  :outcome="agv.feedback('station-' + parking)"
                  @press="requestStation(parking, 'station-' + parking)" />
                <div v-if="agv.commands.has('call_station') && !standard && !zero" class="field-row">
                  <label for="agv-manual-station">Manual station number{{ maximum ? ` (1–${maximum})` : '' }}
                    <InputNumber :pt="{ pcInputText: { root: { 'data-ui': 'agv.call.number' } } }" input-id="agv-manual-station" v-model="agv.form.station" fluid :use-grouping="false"
                      @input="agv.form.station = $event.value" />
                  </label>
                  <OperatorButton explain data-ui="agv.call.submit" id="call_station" label="Call station" :why="agv.commandReason('call_station', agv.form.station)"
                    :outcome="agv.feedback('call_station')" @press="requestStation(agv.form.station, 'call_station')" />
                </div>
              </template>
              </Card>
            </TabPanel>
            <TabPanel value="connection">
              <Card data-ui="agv.link.panel" class="panel agv-surface">
                <template #title><h2>Connection</h2></template>
                <template #content>
                  <Message v-if="agv.connectionLocked" severity="info" :pt="{ root: { id: 'link-lock', class: 'link-lock', role: 'note', 'aria-live': 'off' } }">{{ agv.connectionLocked }}</Message>
                  <p v-if="agv.form.mode !== 'wired'">{{ agv.connectionState }}<template v-if="agv.state.connected"> · {{ agv.state.settings?.mode === 'simulator' ? 'Simulator' : 'Serial AGV' }}</template></p>
                  <label id="agv-mode-label" for="agv-mode">Transport</label>
                  <Select input-id="agv-mode" aria-labelledby="agv-mode-label" :pt="{ label: { 'data-ui': 'agv.link.transport', 'aria-describedby': 'link-lock' } }"
                    :model-value="agv.form.mode" :options="transports" option-label="label" option-value="value" fluid
                    :disabled="Boolean(agv.connectionLocked)" @update:model-value="changeTransport" />
                  <div v-if="agv.form.mode === 'serial'" class="port-picker">
                    <div class="port-field">
                      <label id="agv-port-label" for="agv-port">Port</label>
                      <Select input-id="agv-port" v-model="agv.form.port" aria-labelledby="agv-port-label"
                        :pt="{ label: { 'data-ui': 'agv.link.port', 'aria-describedby': 'link-lock port-error' } }"
                        :options="portOptions" option-label="label" option-value="value" placeholder="Select a port" fluid
                        :disabled="Boolean(agv.connectionLocked) || !agv.capability?.serial_available" />
                    </div>
                    <div class="control">
                      <Button data-ui="agv.link.ports.refresh" id="agv-refresh-ports" type="button" severity="secondary" label="Refresh ports"
                        :disabled="portsBusy" :aria-busy="portsBusy" aria-describedby="port-refresh-status" @click="refreshPorts" />
                      <span v-if="portsBusy" id="port-refresh-status" class="outcome" data-state="running" role="status">
                        <ProgressSpinner class="field-spinner" aria-hidden="true" />Refreshing ports…
                      </span>
                    </div>
                    <Message v-if="agv.portError" severity="error" :pt="{ root: { id: 'port-error' } }">{{ agv.portError }}</Message>
                  </div>
                  <p v-if="agv.form.mode === 'wired'">{{ agv.isolatedTrack ? 'Supervised wired link' : 'Status-only link' }} · {{ agv.capability?.wired_port ?? agv.state.wired_port }}</p>
                  <Accordion class="agv-disclosure">
                    <AccordionPanel data-ui="agv.link.settings" value="settings">
                      <AccordionHeader data-ui="agv.link.settings.toggle">Link settings</AccordionHeader>
                      <AccordionContent>
                        <div class="field-row">
                          <label for="agv-address">Address (hex)
                            <InputText data-ui="agv.link.address" id="agv-address" v-model="agv.form.address" autocomplete="off" spellcheck="false"
                              :placeholder="radioPlaceholder(agv.form.mode, agv.capability?.radio_profile, 'address')"
                              :disabled="Boolean(agv.connectionLocked)" aria-describedby="link-lock" />
                          </label>
                          <label for="agv-channel">Channel (hex)
                            <InputText data-ui="agv.link.channel" id="agv-channel" v-model="agv.form.channel" autocomplete="off" spellcheck="false"
                              :placeholder="radioPlaceholder(agv.form.mode, agv.capability?.radio_profile, 'channel')"
                              :disabled="Boolean(agv.connectionLocked)" aria-describedby="link-lock" />
                          </label>
                          <label v-if="agv.form.mode === 'simulator'" for="agv-stations-config">Simulator stations (0–255)
                            <InputNumber input-id="agv-stations-config" v-model="agv.form.stations" fluid :use-grouping="false"
                              :disabled="Boolean(agv.connectionLocked)" :pt="{ pcInputText: { root: { 'data-ui': 'agv.link.simulator.stations', 'aria-describedby': 'link-lock' } } }"
                              @input="agv.form.stations = $event.value" />
                          </label>
                        </div>
                      </AccordionContent>
                    </AccordionPanel>
                  </Accordion>
                  <Message v-if="agv.frameError" severity="warn" :pt="{ root: { role: 'status', 'aria-live': 'polite' } }">{{ agv.frameError }}</Message>
                  <template v-if="agv.state.connected && agv.state.settings.mode === 'simulator'">
                    <label class="check" for="agv-silence">
                      <ToggleSwitch input-id="agv-silence" :model-value="Boolean(agv.state.silent)" :disabled="agv.operatorReadOnly || agv.busy"
                        :pt="{ input: { 'data-ui': 'agv.link.silence', 'aria-describedby': (sharedLinkReason || 'agv-silence-why') + ' agv-silence-outcome', onClick: requestSilence } }" />
                      Simulate a silent link
                    </label>
                    <small v-if="!sharedLinkReason" id="agv-silence-why" class="outcome">{{ agv.busy ? 'Wait for the current request.' : '' }}</small>
                    <p id="agv-silence-outcome" class="outcome" :data-state="agv.feedback('silence')?.tone">
                      <ProgressSpinner v-if="agv.feedback('silence')?.pending" class="field-spinner" aria-hidden="true" />
                      <StateIcon v-else :tone="agv.feedback('silence')?.tone" />{{ agv.feedback('silence')?.text }}
                    </p>
                </template>
                <label for="agv-sound" class="check">
                  <ToggleSwitch :pt="{ input: { 'data-ui': 'agv.link.sound' } }" input-id="agv-sound" :model-value="agv.sound" @update:model-value="agv.setSound" />
                  Sound an alert for a stale link or a new fault
                </label>
                <div class="controls">
                  <OperatorButton explain data-ui="agv.read.command" :data-ui-key="name" v-for="name in inquiry" :id="name" :key="name" :label="name === 'query_status' ? 'Query status' : 'Read speed'" :primary="name === 'query_status'"
                    :why="agv.commandReason(name)" :why-id="sharedLinkReason" :outcome="agv.feedback(name)" @press="agv.send(name)" />
                </div>
                </template>
              </Card>
            </TabPanel>
            <TabPanel value="speed">
              <Card data-ui="agv.movement.panel" class="panel agv-surface movement" :data-armed="Boolean(agv.armed)">
                <template #title><h2>Movement and speed</h2></template>
                <template #content>
                  <p v-if="agv.isolatedTrack">The UR10e is disconnected. Check the folded arm at the e-stop before each station call.</p>
                  <p v-else-if="agv.state.robot_interlock">Robot travel pose required for motion. Save the pose on Teaching.</p>
                  <p v-else-if="agv.state.robot_interlock === false">AGV-only test bench · No robot interlock.</p>
                  <div v-if="agv.commands.has('set_speed')" class="field-row">
                    <label for="agv-speed">Set speed (500–4000 m/h)
                      <InputNumber :pt="{ pcInputText: { root: { 'data-ui': 'agv.speed.input' } } }" input-id="agv-speed" v-model="agv.form.speed" :use-grouping="false" fluid @input="agv.form.speed = $event.value" />
                    </label>
                    <OperatorButton explain data-ui="agv.speed.set" id="set_speed" label="Set speed" :why="agv.commandReason('set_speed', agv.form.speed)"
                      :outcome="agv.feedback('set_speed')" @press="agv.send('set_speed', 'set_speed', agv.form.speed)" />
                  </div>
                  <div v-if="agv.commands.has('set_speed')" class="controls">
                    <OperatorButton explain data-ui="agv.speed.preset" :data-ui-key="speed" v-for="speed in [500, 1000, 1500]" :id="'speed-' + speed" :key="speed" :label="speedText(speed)"
                      :why="agv.commandReason('set_speed', speed)" :outcome="agv.feedback('speed-' + speed)"
                      @press="agv.send('set_speed', 'speed-' + speed, speed)" />
                  </div>
                  <div class="controls">
                    <template v-for="name in movement" :key="name">
                      <OperatorButton explain data-ui="agv.movement.command" :data-ui-key="name" v-if="agv.commands.has(name)" :id="name" :label="label(name)"
                        :why="agv.commandReason(name)" :outcome="agv.feedback(name)" @press="agv.send(name)" />
                    </template>
                  </div>
                  <Accordion v-if="extra.length" class="agv-disclosure">
                    <AccordionPanel data-ui="agv.movement.extra" value="more">
                      <AccordionHeader data-ui="agv.movement.extra.toggle">More commands</AccordionHeader>
                      <AccordionContent>
                        <div class="controls">
                          <OperatorButton explain data-ui="agv.movement.extra.command" :data-ui-key="command.name" v-for="command in extra" :id="command.name" :key="command.name" :label="command.label"
                            :why="agv.commandReason(command.name)" :outcome="agv.feedback(command.name)" @press="agv.send(command.name)" />
                        </div>
                      </AccordionContent>
                    </AccordionPanel>
                  </Accordion>
                </template>
              </Card>
            </TabPanel>
            <TabPanel value="bluetooth">
              <AgvBluetooth v-if="tab === 'bluetooth'" :read-only="agv.operatorReadOnly" @warning="bluetoothWarning = $event" />
            </TabPanel>
            <TabPanel value="details" class="agv-main">
              <Accordion class="agv-disclosure">
                <AccordionPanel data-ui="agv.status.details" value="details">
                  <AccordionHeader data-ui="agv.status.details.toggle">Machine details</AccordionHeader>
                  <AccordionContent>
                    <p v-if="agv.state.status_time">{{ agv.stale || !agv.state.connected ? 'Last read' : 'Reported' }} {{ localTime(agv.state.status_time) }}</p>
                    <dl class="capabilities">
                      <template v-for="[field, caption] in details" :key="field">
                        <dt data-ui="agv.status.detail.row" :data-ui-key="field">{{ (agv.state.status.mode === 0 && field === 'position_units') ? 'Distance to the stop line (units)' : caption }}</dt>
                        <dd>{{ field === 'set_speed_m_h' ? speedText(agv.state.status[field]) : typeof agv.state.status[field] === 'boolean' ? bool(agv.state.status[field]) : agv.state.status[field] }}</dd>
                      </template>
                    </dl>
                  </AccordionContent>
                </AccordionPanel>
              </Accordion>
              <Disclosure data-ui="agv.jobs.recent" v-if="agv.state.recent_jobs?.length" open toggle-id="agv.jobs.toggle">
                <template #heading>Recent client jobs</template>
                <ul>
                  <li data-ui="agv.jobs.item" :data-ui-key="job.job_id" v-for="job in agv.state.recent_jobs" :key="job.job_id">
                    {{ ownerLabel(job.client_name) }} · {{ job.job_id }} · {{ job.state }}
                  </li>
                </ul>
              </Disclosure>
              <Disclosure data-ui="agv.raw.activity" v-if="agv.state.recent_raw?.length" open toggle-id="agv.raw.activity.toggle">
                <template #heading>Recent developer commands</template>
                <ul>
                  <li data-ui="agv.raw.activity.item" :data-ui-key="entry.id" v-for="entry in agv.state.recent_raw" :key="entry.id">
                    {{ localTime(entry.utc) }} · {{ entry.client_name }} · {{ entry.command }} · {{ entry.outcome }}
                  </li>
                </ul>
              </Disclosure>
              <Disclosure data-ui="agv.raw.settings" v-if="agv.state.raw_settings?.length" open toggle-id="agv.raw.settings.toggle">
                <template #heading>Developer speed changes</template>
                <ul>
                  <li data-ui="agv.raw.settings.item" :data-ui-key="setting.id" v-for="setting in agv.state.raw_settings" :key="setting.id">
                    {{ setting.before }} → {{ setting.after }} m/h · {{ setting.undone ? 'Restored' : (setting.outcome ?? 'Pending verification') }}
                    · Snapshot {{ setting.id }}
                  </li>
                </ul>
              </Disclosure>
              <AgvRecord :agv="agv" />
            </TabPanel>
          </TabPanels>
        </div>
      </div>
    </Tabs>
  </div>
</template>

<style scoped>
.agv-surface { padding: 0; }
.agv-surface h2 { margin: 0; }
.station-grid :deep(button), .parking-call :deep(button) { min-height: 44px; overflow-wrap: anywhere; }
.station-grid :deep(button) { width: 100%; }
.stats-value { font-size: 1.75rem; line-height: 1.22; font-weight: 700; overflow-wrap: anywhere; }
.stats-target { display: inline; }
.stats-value:has(+ .stats-target) { display: inline; margin-inline-end: .25em; }
.stats-secondary { font-size: .95rem; font-weight: 600; color: var(--p-operator-muted); }
.link-lock { margin-block-end: .5rem; }
.agv-disclosure { margin-top: .65rem; }
.port-field { flex: 1 1 12rem; }
.outcome:empty { display: none; }
.field-spinner { width: 1em; height: 1em; margin: 0 .4em 0 0; vertical-align: -.14em; }
</style>
