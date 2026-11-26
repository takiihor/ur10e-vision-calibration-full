<script setup>
import { computed, nextTick, ref, watch } from 'vue';
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
import AgvBluetooth from './AgvBluetooth.vue';
import { simulationEnabled } from './api';
import {
  faultText, fieldLabels, linkQualityText, localTime, modes, motionText, speedText, stationCallRequired,
  radioOverrides, radioPlaceholder, stationRangeText,
} from '../app/anodizing_web/web/agv-record.mjs';

const { agv } = defineProps({ agv: { type: Object, required: true } });
const bluetoothOpen = ref(false);
const ownerLabels = new Map([['agv-page', 'AGV page'], ['calibration', 'Calibration module']]);
const ownerLabel = name => ownerLabels.get(name) ?? name;
const ownerReason = computed(() => {
  if (agv.operatorReadOnly) return 'Enroll the operator key to change the job owner.';
  if (agv.state.programme_motion_block) return agv.state.programme_motion_block;
  if (agv.state.executor_job && ['accepted', 'running', 'waiting'].includes(agv.state.executor_job.state)) {
    return 'Wait for the current job or use Stop.';
  }
  if (agv.state.manual_visit) return 'Resolve the retained station visit before changing the job owner.';
  if (agv.state.stop_unconfirmed) return 'Verify a safe halt before changing the job owner.';
  if (agv.state.owner && !['agv-page', 'calibration'].includes(agv.state.owner)) {
    return `${agv.state.owner} owns the executor. Ask it to release control or use Stop.`;
  }
  return agv.busy ? 'Wait for the current request.' : '';
});
const status = computed(() => agv.freshReport);
const standard = computed(() => status.value?.mode === 0);
const maximum = computed(() => agv.maximumStation);
const manualDeadline = computed(() => agv.manualTravel
  ? new Date(agv.state.executor_health.manual_travel_until * 1000).toLocaleString()
  : null);
const zero = computed(() => maximum.value === 0);
const stationMode = computed(() => status.value?.mode === 2 && maximum.value > 0);
const parking = computed(() => agv.state.station_profile?.parking_station ?? 17);
const stations = computed(() => Array.from({ length: agv.supervisedRange ? (maximum.value ?? 0) : Math.max(17, maximum.value ?? 0) }, (_, i) => i + 1)
  .filter(station => station !== parking.value));
const callReason = computed(() => agv.commandReason('call_station', agv.state.manual_visit?.station ?? 1));
const manualVisit = computed(() => agv.state.manual_visit);
const confirmation = ref(null);
const confirmationHeading = ref(null);
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
const disconnectReason = computed(() => agv.operatorKeyReason || (!agv.state.connected ? 'Connect first.' :
  agv.state.programme_motion_block || (agv.state.recipe_active ? 'Stop the active recipe before disconnecting.' :
    agv.state.halt_pending || agv.state.stop_unconfirmed ? 'Verify the pending halt before disconnecting.' :
      agv.busy ? 'Wait for the current request.' : '')));
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
const stats = computed(() => {
  const report = status.value;
  const speed = report?.actual_speed_m_h;
  const motion = motionText(report);
  const actual = speed == null ? 'Unknown' : speedText(speed) + (speed ? speed < 0 ? ' · Reverse' : ' · Forward' : '');
  const stations = standard.value || zero.value
    ? [{ key: 'counter', label: 'Stop counter', icon: 'hashtag', value: report?.current_station ?? 'Unknown', glance: true }]
    : [
        { key: 'current', label: 'Current station', icon: 'map-marker', value: stationLabel(report?.current_station, true), glance: true },
        { key: 'target', label: 'Target station', icon: 'flag', value: stationLabel(report?.target_station, true), glance: true },
      ];
  return [
    { key: 'motion', label: 'Motion', icon: 'car', value: motion, id: 'agv-motion', glance: true, tone: motion === 'Running' ? 'running' : undefined },
    ...stations,
    { key: 'speed', label: 'Actual speed', icon: 'gauge', value: actual },
    { key: 'battery', label: 'Battery', icon: 'bolt', value: report?.battery_percent == null ? 'Unknown' : report.battery_percent + '%' },
    { key: 'fault', label: 'Fault', icon: 'exclamation-circle', value: report ? faultText(report) : 'Unknown', tone: report?.fault_code ? 'failed' : undefined },
  ];
});

function stationLabel(station, showNumber = false) {
  if (station == null) return 'Unknown';
  const name = agv.state.station_profile?.station_labels?.[station];
  const local = station === parking.value ? name ? `Parking · ${name}` : 'Parking' : name;
  return local ? local + (showNumber ? ` (${station})` : '') : `Station ${station}`;
}

function stationReason(station) {
  if (agv.state.manual_visit) return agv.commandReason('call_station', station);
  return stationMode.value && station > maximum.value ? stationRangeText(status.value) : callReason.value;
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
  const { mode, port, stations } = agv.form;
  void agv.post({ action: 'connect', mode, port, stations, ...radioOverrides(agv.form) }, 'connect');
}
</script>

<template>
  <nav data-ui="agv.bluetooth.tabs" class="agv-maintenance-nav" aria-label="AGV pages">
    <Button data-ui="agv.bluetooth.wiredtab" type="button" severity="secondary" label="AGV controls" :aria-pressed="!bluetoothOpen"
      @click="bluetoothOpen = false" />
    <Button data-ui="agv.bluetooth.tab" type="button" severity="secondary" label="Bluetooth" :aria-pressed="bluetoothOpen"
      @click="bluetoothOpen = true" />
  </nav>
  <AgvBluetooth v-if="bluetoothOpen" :read-only="agv.operatorReadOnly" />
  <div v-else data-ui="agv.page" class="agv-grid">
    <div class="agv-main">
      <Panel data-ui="agv.status.panel" class="panel agv-surface" aria-labelledby="status-title">
        <template #header>
          <div class="section-title">
            <h2 id="status-title">AGV status</h2>
            <Tag severity="secondary"
              :value="agv.state.connected ? agv.state.settings.mode === 'simulator' ? 'Simulator' : 'Serial AGV' : 'No open connection'" />
          </div>
        </template>
        <Message v-if="agv.state.bluetooth_block" severity="warn">{{ agv.state.bluetooth_block }}</Message>
        <Message v-if="agv.state.stop_unconfirmed" severity="error" role="alert">
          Stop unconfirmed. Verify a safe halt. Use the AGV e-stop if needed.
        </Message>
        <section data-ui="agv.owner.panel" v-if="agv.state.job_gateway" aria-labelledby="job-owner-title">
          <h3 id="job-owner-title">Job ownership</h3>
          <p>Owner {{ ownerLabel(agv.state.owner) ?? 'None' }} ·
            {{ agv.state.armed_for ? 'Armed for ' + ownerLabel(agv.state.armed_for) : 'Disarmed' }}</p>
          <div class="field-row">
            <OperatorButton data-ui="agv.owner.manual" id="job-owner-agv" label="Select AGV page" :why="ownerReason"
              :outcome="agv.feedback('job-owner-agv')" @press="agv.post({ action: 'claim_owner', owner: 'agv-page' }, 'job-owner-agv')" />
            <OperatorButton data-ui="agv.owner.calibration" v-if="agv.state.calibration_available" id="job-owner-calibration" label="Select Calibration module"
              :why="ownerReason" :outcome="agv.feedback('job-owner-calibration')"
              @press="agv.post({ action: 'claim_owner', owner: 'calibration' }, 'job-owner-calibration')" />
            <OperatorButton data-ui="agv.owner.release" v-if="agv.state.owner" id="job-owner-release" label="Release owner" :why="ownerReason"
              :outcome="agv.feedback('job-owner-release')" @press="agv.post({ action: 'claim_owner', owner: null }, 'job-owner-release')" />
          </div>
          <p v-if="agv.state.executor_job">Current job {{ agv.state.executor_job.job_id }} · {{ agv.state.executor_job.state }}</p>
          <details data-ui="agv.jobs.recent" v-if="agv.state.recent_jobs?.length" open>
            <summary data-ui="agv.jobs.toggle">Recent client jobs</summary>
            <ul>
              <li data-ui="agv.jobs.item" :data-ui-key="job.job_id" v-for="job in agv.state.recent_jobs" :key="job.job_id">
                {{ ownerLabel(job.client_name) }} · {{ job.job_id }} · {{ job.state }}
              </li>
            </ul>
          </details>
        </section>
        <div class="stats">
          <Card data-ui="agv.status.metric" :data-ui-key="stat.key" v-for="stat in stats" :key="stat.label" class="stats-widget">
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
      </Panel>
      <Panel data-ui="agv.call.panel" class="panel agv-surface" aria-labelledby="stations-title">
        <template #header><h2 id="stations-title">Stations</h2></template>
        <p v-if="standard">{{ stationCallRequired }}</p>
        <p v-else-if="zero || stationMode">
          {{ stationRangeText(status) }}
          It is at station {{ status.current_station ?? 'unknown' }}<template v-if="agv.state.status_time"> (read {{ localTime(agv.state.status_time).split('.')[0] }})</template>.
        </p>
        <p v-else>Query status to check for a learned route.</p>
        <p v-if="manualDeadline" data-ui="agv.call.window">
          <template v-if="agv.isolatedTrack">
            Supervised folded-arm travel on the line tape. Tank-clear and equipment-off signals are not connected yet: check the path and the tank equipment by eye before each call.
            The AGV’s fresh battery percentage supplies the reserve check.
          </template>
          Manual travel deadline {{ manualDeadline }} (local time). DRAFT battery floor {{ agv.state.executor_health.manual_travel_min_battery }}%.
        </p>
        <Message v-if="!standard && (zero || (stationMode && maximum === 2))"
          data-ui="agv.call.range.warning" severity="warn">
          {{ zero ? 'Teach the route before calling a station.' : 'Teach the new tape before calling stations beyond the learned range.' }}
          Use the AGV’s green button (XBOT station guide V1.61, pp. 2–3), then press Query status.
        </Message>
        <p v-if="!standard && callReason" id="agv-station-call-reason" data-ui="agv.call.reason">{{ callReason }}</p>
        <p v-if="agv.state.quality_alarm" role="status">{{ agv.state.quality_alarm.reason }}</p>
        <p v-for="fault in evidenceFaults" :key="fault" role="alert">{{ fault }}</p>
        <section v-if="operatorCheck" data-ui="agv.check.panel" aria-labelledby="agv-operator-check">
          <h3 id="agv-operator-check">Operator check</h3>
          <p role="alert">{{ operatorCheck.reason }}</p>
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
              <input type="checkbox" data-ui="agv.check.confirmed" v-model="confirmed">
              I physically checked the AGV location
            </label>
            <OperatorButton data-ui="agv.check.abort" id="abort-station"
              :label="manualVisit?.id === operatorCheck.job_id ? 'Abort station visit' : 'Confirm safe hold'"
              :why="abortReason" :outcome="agv.feedback('abort-station')" @press="abortVisit" />
          </template>
        </section>
        <div v-if="manualVisit">
          <p role="status">{{ visitSummary }}{{ manualVisit.result?.error ? ' · ' + manualVisit.result.error : '' }}</p>
          <OperatorButton v-if="visitOutcome === 'succeeded' && !operatorCheck && !evidenceFaults.length" data-ui="agv.call.acknowledge"
            id="acknowledge-station" label="Acknowledge result" :why="agv.busy ? 'Wait for the current request.' : ''"
            :outcome="agv.feedback('acknowledge-station')"
            @press="agv.post({ action: 'acknowledge_station', job_id: manualVisit.id }, 'acknowledge-station')" />
        </div>
        <div v-if="!manualVisit && agv.state.manual_visit_history">
          {{ agv.state.manual_visit_history.refused ? 'Last station visit refused' :
            agv.state.manual_visit_history.operator_check ? 'Station visit aborted' : 'Last acknowledged station visit' }}
          {{ agv.state.manual_visit_history.id }}
        </div>
        <section v-if="confirmation" data-ui="agv.call.attestation" aria-labelledby="station-confirmation-heading">
          <h3 id="station-confirmation-heading" ref="confirmationHeading" tabindex="-1">Confirm station {{ confirmation.station }}</h3>
          <p>I am at the e-stop, the arm is folded, and the path and tank equipment are clear.</p>
          <p v-if="agv.isolatedTrack">My observation of the folded arm applies to this station call only.</p>
          <div class="field-row">
            <OperatorButton id="confirm-station" data-ui="agv.call.confirm" label="Confirm and call"
              :why="agv.commandReason('call_station', confirmation.station)" @press="confirmStation" />
            <OperatorButton id="cancel-station" data-ui="agv.call.cancel" label="Cancel" @press="confirmation = null" />
          </div>
        </section>
        <div v-if="!standard" data-ui="agv.call.grid" class="station-grid">
          <OperatorButton data-ui="agv.call.station" :data-ui-key="station" v-for="station in stations" :id="'station-' + station" :key="station"
            :label="stationLabel(station) + (station === status?.current_station && stationMode ? ' · Current' : '')"
            :current="station === status?.current_station && stationMode"
            :why="stationReason(station)" :why-id="callReason && stationReason(station) === callReason ? 'agv-station-call-reason' : undefined"
            :outcome="agv.feedback('station-' + station)"
            @press="requestStation(station, 'station-' + station)" />
        </div>
        <OperatorButton v-if="!standard && (!agv.supervisedRange || parking <= maximum)" data-ui="agv.call.parking" :id="'station-' + parking" class="parking-call"
          :label="stationLabel(parking) + (parking === status?.current_station && stationMode ? ' · Current' : '')"
          :current="parking === status?.current_station && stationMode"
          :why="stationReason(parking)" :why-id="callReason && stationReason(parking) === callReason ? 'agv-station-call-reason' : undefined"
          :outcome="agv.feedback('station-' + parking)"
          @press="requestStation(parking, 'station-' + parking)" />
        <div v-if="agv.commands.has('call_station') && !standard && !zero" class="field-row">
          <label for="agv-manual-station">Manual station number{{ maximum ? ` (1–${maximum})` : '' }}
            <InputNumber :pt="{ pcInputText: { root: { 'data-ui': 'agv.call.number' } } }" input-id="agv-manual-station" v-model="agv.form.station" fluid :use-grouping="false"
              @input="agv.form.station = $event.value" />
          </label>
          <OperatorButton data-ui="agv.call.submit" id="call_station" label="Call station" :why="agv.commandReason('call_station', agv.form.station)"
            :outcome="agv.feedback('call_station')" @press="requestStation(agv.form.station, 'call_station')" />
        </div>
      </Panel>
    </div>

    <aside data-ui="agv.controls.panel" class="operation" aria-label="Connection and commands">
      <Panel data-ui="agv.link.panel" class="panel agv-surface">
        <template #header><h2>Connection</h2></template>
        <label id="agv-mode-label" for="agv-mode">Transport</label>
        <Select input-id="agv-mode" aria-labelledby="agv-mode-label" :pt="{ label: { 'data-ui': 'agv.link.transport', 'aria-describedby': 'link-lock' } }"
          :model-value="agv.form.mode" :options="transports" option-label="label" option-value="value" fluid
          :disabled="Boolean(agv.connectionLocked)" @update:model-value="changeTransport" />
        <small id="link-lock" class="outcome">{{ agv.connectionLocked }}</small>
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
        <div class="controls">
          <OperatorButton data-ui="agv.link.connect" id="connect" label="Connect" primary :why="agv.connectReason" :outcome="agv.feedback('connect')" @press="connect" />
          <OperatorButton data-ui="agv.link.disconnect" id="disconnect" label="Disconnect" :why="disconnectReason" :outcome="agv.feedback('disconnect')"
            @press="agv.post({ action: 'disconnect' }, 'disconnect')" />
        </div>
        <Message v-if="agv.frameError" severity="warn" :pt="{ root: { role: 'status', 'aria-live': 'polite' } }">{{ agv.frameError }}</Message>
        <template v-if="agv.state.connected && agv.state.settings.mode === 'simulator'">
          <label class="check" for="agv-silence">
            <ToggleSwitch input-id="agv-silence" :model-value="Boolean(agv.state.silent)" :disabled="agv.busy"
              :pt="{ input: { 'data-ui': 'agv.link.silence', 'aria-describedby': 'agv-silence-why agv-silence-outcome', onClick: requestSilence } }" />
            Simulate a silent link
          </label>
          <small id="agv-silence-why" class="outcome">{{ agv.busy ? 'Wait for the current request.' : '' }}</small>
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
          <OperatorButton data-ui="agv.read.command" :data-ui-key="name" v-for="name in inquiry" :id="name" :key="name" :label="name === 'query_status' ? 'Query status' : 'Read speed'" :primary="name === 'query_status'"
            :why="agv.commandReason(name)" :outcome="agv.feedback(name)" @press="agv.send(name)" />
        </div>
      </Panel>
      <Panel data-ui="agv.movement.panel" class="panel agv-surface movement" :data-armed="Boolean(agv.armed)">
        <template #header><h2>Movement and speed</h2></template>
        <p v-if="agv.isolatedTrack">The UR10e is disconnected. Check the folded arm at the e-stop before each station call.</p>
        <p v-else-if="agv.state.robot_interlock">Robot travel pose required for motion. Save the pose on Teaching.</p>
        <p v-else-if="agv.state.robot_interlock === false">AGV-only test bench · No robot interlock.</p>
        <label for="agv-armed" class="check">
          <ToggleSwitch input-id="agv-armed" :model-value="Boolean(agv.armed)" :disabled="Boolean(agv.armReason)"
            :pt="{ input: { 'data-ui': 'agv.movement.arm', 'aria-describedby': 'agv-armed-why agv-armed-outcome', onClick: requestArm } }" />
          Armed
        </label>
        <small id="agv-armed-why" class="outcome">{{ agv.armReason }}</small>
        <p id="agv-armed-outcome" class="outcome" :data-state="agv.feedback('armed')?.tone">
          <ProgressSpinner v-if="agv.feedback('armed')?.pending" class="field-spinner" aria-hidden="true" />
          <StateIcon v-else :tone="agv.feedback('armed')?.tone" />{{ agv.feedback('armed')?.text }}
        </p>
        <div v-if="agv.commands.has('set_speed')" class="field-row">
          <label for="agv-speed">Set speed (500–4000 m/h)
            <InputNumber :pt="{ pcInputText: { root: { 'data-ui': 'agv.speed.input' } } }" input-id="agv-speed" v-model="agv.form.speed" :use-grouping="false" fluid @input="agv.form.speed = $event.value" />
          </label>
          <OperatorButton data-ui="agv.speed.set" id="set_speed" label="Set speed" :why="agv.commandReason('set_speed', agv.form.speed)"
            :outcome="agv.feedback('set_speed')" @press="agv.send('set_speed', 'set_speed', agv.form.speed)" />
        </div>
        <div v-if="agv.commands.has('set_speed')" class="controls">
          <OperatorButton data-ui="agv.speed.preset" :data-ui-key="speed" v-for="speed in [500, 1000, 1500]" :id="'speed-' + speed" :key="speed" :label="speedText(speed)"
            :why="agv.commandReason('set_speed', speed)" :outcome="agv.feedback('speed-' + speed)"
            @press="agv.send('set_speed', 'speed-' + speed, speed)" />
        </div>
        <div class="controls">
          <template v-for="name in movement" :key="name">
            <OperatorButton data-ui="agv.movement.command" :data-ui-key="name" v-if="agv.commands.has(name)" :id="name" :label="label(name)"
              :why="agv.commandReason(name)" :outcome="agv.feedback(name)" @press="agv.send(name)" />
          </template>
        </div>
        <Accordion v-if="extra.length" class="agv-disclosure">
          <AccordionPanel data-ui="agv.movement.extra" value="more">
            <AccordionHeader data-ui="agv.movement.extra.toggle">More commands</AccordionHeader>
            <AccordionContent>
              <div class="controls">
                <OperatorButton data-ui="agv.movement.extra.command" :data-ui-key="command.name" v-for="command in extra" :id="command.name" :key="command.name" :label="command.label"
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
.agv-maintenance-nav { display: flex; gap: .5rem; margin-block-end: .75rem; }
.agv-surface { padding: 0; }
.agv-surface h2 { margin: 0; }
.station-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 7rem), 1fr));
  gap: .45rem; margin-block: .65rem;
}
.station-grid :deep(button), .parking-call :deep(button) { min-height: 44px; overflow-wrap: anywhere; }
.station-grid :deep(button) { width: 100%; }
.parking-call { border-top: 1px solid var(--p-operator-pressed); padding-top: .65rem; margin-top: .65rem; }
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
