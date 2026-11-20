<script setup>
import { computed, ref } from 'vue';
import Button from 'primevue/button';
import Checkbox from 'primevue/checkbox';
import OperatorButton from './OperatorButton.vue';
import StateIcon from './StateIcon.vue';
import AgvRecord from './AgvRecord.vue';
import {
  faultText, fieldLabels, linkQualityText, localTime, modes, speedText, stationCallRequired,
} from '../app/anodizing_web/web/agv-record.mjs';

const props = defineProps({ agv: { type: Object, required: true } });
const agv = props.agv;
const status = computed(() => agv.freshReport);
const standard = computed(() => status.value?.mode === 0);
const zero = computed(() => status.value?.max_stations === 0);
const stationMode = computed(() => status.value?.mode === 2 && !zero.value);
const age = computed(() => agv.age == null ? 'No status report yet.' : `Last report ${agv.age.toFixed(1)} s ago.`);
const disconnectReason = computed(() => !agv.state.connected ? 'Connect first.' :
  agv.state.recipe_active ? 'Stop the active recipe before disconnecting.' : agv.busy ? 'Wait for the current request.' : '');
const portsBusy = ref(false);
const movement = ['forward', 'reverse', 'resume'];
const inquiry = ['query_status', 'read_speed'];
const extra = computed(() => [...agv.commands.values()].filter(({ name, motion }) =>
  motion && ![...movement, 'set_speed', 'call_station'].includes(name)));
const details = computed(() => Object.entries(fieldLabels).filter(([field]) => field in (agv.state.status ?? {})));
const bool = value => value === true ? 'On' : value === false ? 'Off' : 'Unknown';
const actual = computed(() => {
  const speed = status.value?.actual_speed_m_h;
  return speed == null ? 'Unknown' : speedText(speed) + (speed ? speed < 0 ? ' · Reverse' : ' · Forward' : '');
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

function connect() {
  if (agv.connectReason) return;
  const { mode, port, address, channel, stations } = agv.form;
  void agv.post({ action: 'connect', mode, port, address, channel, stations }, 'connect');
}
</script>

<template>
  <div class="agv-grid">
    <div class="agv-main">
      <section class="panel" aria-labelledby="status-title">
        <div class="section-title">
          <h2 id="status-title">AGV status</h2>
          <span>{{ agv.state.connected ? agv.state.settings.mode === 'simulator' ? 'Simulator' : 'Serial AGV' : 'No open connection' }}</span>
        </div>
        <dl class="readouts">
          <div><dt>Motion</dt><dd id="agv-motion">{{ status?.stopped === true ? 'Stopped' : status?.stopped === false ? 'Running' : 'Unknown' }}</dd></div>
          <div><dt>{{ standard || zero ? 'Stop counter' : 'Current station' }}</dt><dd>{{ status?.current_station ?? 'Unknown' }}</dd></div>
          <div v-if="!standard && !zero"><dt>Target station</dt><dd>{{ status?.target_station ?? 'Unknown' }}</dd></div>
          <div><dt>Actual speed</dt><dd>{{ actual }}</dd></div>
          <div><dt>Battery</dt><dd>{{ status?.battery_percent == null ? 'Unknown' : status.battery_percent + '%' }}</dd></div>
          <div><dt>Fault</dt><dd>{{ status ? faultText(status) : 'Unknown' }}</dd></div>
        </dl>
        <p v-if="status">Mode {{ status.mode }} · {{ modes[status.mode] ?? 'unknown' }} · {{ standard ? 'Stop line' : 'Station' }} arrival {{ bool(status.station_arrival) }} ·
          Obstacle stop {{ bool(status.obstacle_stop) }} · Command stop {{ bool(status.command_stop) }} · Pause {{ bool(status.paused) }}</p>
        <p v-else>{{ age }}</p>
        <p v-if="agv.state.connected && agv.state.link_quality">{{ linkQualityText(agv.state.link_quality) }}</p>
        <details>
          <summary>Machine details</summary>
          <p v-if="agv.state.status_time">{{ agv.stale || !agv.state.connected ? 'Last read' : 'Reported' }} {{ localTime(agv.state.status_time) }}</p>
          <dl class="capabilities">
            <template v-for="[field, caption] in details" :key="field">
              <dt>{{ (agv.state.status.mode === 0 && field === 'position_units') ? 'Distance to the stop line (units)' : caption }}</dt>
              <dd>{{ field === 'set_speed_m_h' ? speedText(agv.state.status[field]) : typeof agv.state.status[field] === 'boolean' ? bool(agv.state.status[field]) : agv.state.status[field] }}</dd>
            </template>
          </dl>
        </details>
      </section>
      <section class="panel" aria-labelledby="stations-title">
        <h2 id="stations-title">Stations</h2>
        <p v-if="standard">{{ stationCallRequired }}</p>
        <p v-else-if="zero">No route learned. The report shows the stop counter. Teach the route with the AGV’s green button. See the XBOT station guide (V1.61, pp. 2–3).</p>
        <p v-else-if="stationMode">{{ status.max_stations }} learned stations. The current station is marked below.</p>
        <p v-else>Query status to check for a learned route.</p>
        <div v-if="stationMode" class="controls">
          <OperatorButton v-for="station in status.max_stations" :id="'station-' + station" :key="station"
            :label="'Go to station ' + station + (station === status.current_station ? ' · Current' : '')" :current="station === status.current_station"
            :why="agv.commandReason('call_station', station)" :outcome="agv.feedback('station-' + station)"
            @press="agv.send('call_station', 'station-' + station, station)" />
        </div>
        <div v-if="agv.commands.has('call_station') && !standard && !zero" class="field-row">
          <label for="agv-manual-station">Manual station number
            <input id="agv-manual-station" v-model.number="agv.form.station" type="number" min="1" :max="status?.max_stations || 255">
          </label>
          <OperatorButton id="call_station" label="Call station" :why="agv.commandReason('call_station', agv.form.station)"
            :outcome="agv.feedback('call_station')" @press="agv.send('call_station', 'call_station', agv.form.station)" />
        </div>
      </section>
    </div>

    <aside class="operation" aria-label="Connection and commands">
      <section class="panel">
        <h2>Connection</h2>
        <label for="agv-mode">Transport
          <select id="agv-mode" v-model="agv.form.mode" :disabled="Boolean(agv.connectionLocked)" aria-describedby="link-lock"
            @change="agv.pickPort(); if (agv.form.mode === 'serial') refreshPorts()">
            <option value="simulator">Simulator</option>
            <option value="serial">Serial port</option>
          </select>
        </label>
        <small v-if="agv.connectionLocked" id="link-lock">{{ agv.connectionLocked }}</small>
        <div v-if="agv.form.mode === 'serial'" class="port-picker">
          <label for="agv-port">Port
            <select id="agv-port" v-model="agv.form.port" :disabled="Boolean(agv.connectionLocked) || !agv.capability?.serial_available"
              aria-describedby="link-lock port-error">
              <option value="">Select a port</option>
              <option v-for="port in agv.ports" :key="port.device" :value="port.device">
                {{ port.device }}{{ port.description && port.description !== 'n/a' ? ' · ' + port.description : '' }}
              </option>
            </select>
          </label>
          <Button id="agv-refresh-ports" type="button" severity="secondary" :label="portsBusy ? 'Refreshing ports…' : 'Refresh ports'"
            :aria-busy="portsBusy" @click="refreshPorts" />
          <p v-if="agv.portError" id="port-error" class="alarm" data-state="serious"><StateIcon tone="serious" />{{ agv.portError }}</p>
        </div>
        <details>
          <summary>Link settings</summary>
          <div class="field-row">
            <label for="agv-address">Address (hex)
              <input id="agv-address" v-model="agv.form.address" autocomplete="off" spellcheck="false" :disabled="Boolean(agv.connectionLocked)" aria-describedby="link-lock">
            </label>
            <label for="agv-channel">Channel (hex)
              <input id="agv-channel" v-model="agv.form.channel" autocomplete="off" spellcheck="false" :disabled="Boolean(agv.connectionLocked)" aria-describedby="link-lock">
            </label>
            <label v-if="agv.form.mode === 'simulator'" for="agv-stations-config">Simulator stations
              <input id="agv-stations-config" v-model.number="agv.form.stations" type="number" min="0" max="255" :disabled="Boolean(agv.connectionLocked)" aria-describedby="link-lock">
            </label>
          </div>
        </details>
        <div class="controls">
          <OperatorButton id="connect" label="Connect" primary :why="agv.connectReason" :outcome="agv.feedback('connect')" @press="connect" />
          <OperatorButton id="disconnect" label="Disconnect" :why="disconnectReason" :outcome="agv.feedback('disconnect')"
            @press="agv.post({ action: 'disconnect' }, 'disconnect')" />
        </div>
        <p v-if="agv.frameError" role="status">{{ agv.frameError }}</p>
        <label v-if="agv.state.connected && agv.state.settings.mode === 'simulator'" class="check" for="agv-silence">
          <Checkbox input-id="agv-silence" binary :model-value="Boolean(agv.state.silent)" :disabled="agv.busy"
            :pt="{ input: { 'aria-describedby': 'agv-silence-why' } }"
            @update:model-value="agv.post({ action: 'silence', silent: $event }, 'silence')" />
          Simulate a silent link
        </label>
        <small v-if="agv.busy && agv.state.connected && agv.state.settings.mode === 'simulator'" id="agv-silence-why">Wait for the current request.</small>
        <span v-if="agv.feedback('silence')">{{ agv.feedback('silence').text }}</span>
        <label for="agv-sound" class="check">
          <Checkbox input-id="agv-sound" binary :model-value="agv.sound" @update:model-value="agv.setSound" />
          Sound an alert for a stale link or a new fault
        </label>
        <div class="controls">
          <OperatorButton v-for="name in inquiry" :id="name" :key="name" :label="name === 'query_status' ? 'Query status' : 'Read speed'" :primary="name === 'query_status'"
            :why="agv.commandReason(name)" :outcome="agv.feedback(name)" @press="agv.send(name)" />
        </div>
      </section>
      <section class="panel movement" :data-armed="Boolean(agv.armed)">
        <h2>Movement and speed</h2>
        <p v-if="agv.state.robot_interlock">Robot travel pose required for motion. Save the pose on Teaching.</p>
        <p v-else-if="agv.state.robot_interlock === false">AGV-only test bench · No robot interlock.</p>
        <label for="agv-armed" class="check">
          <Checkbox input-id="agv-armed" binary :model-value="Boolean(agv.armed)" :disabled="Boolean(agv.armReason)"
            :pt="{ input: { 'aria-describedby': 'agv-armed-why agv-armed-outcome' } }"
            @update:model-value="agv.post({ action: 'arm', armed: $event }, 'armed')" />
          Armed
        </label>
        <small id="agv-armed-why">{{ agv.armReason }}</small>
        <p id="agv-armed-outcome" :data-state="agv.feedback('armed')?.tone"><StateIcon :tone="agv.feedback('armed')?.tone" />{{ agv.feedback('armed')?.text }}</p>
        <div v-if="agv.commands.has('set_speed')" class="field-row">
          <label for="agv-speed">Set speed (500–4000 m/h)
            <input id="agv-speed" v-model.number="agv.form.speed" type="number" min="500" max="4000">
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
        <details v-if="extra.length">
          <summary>More commands</summary>
          <div class="controls">
            <OperatorButton v-for="command in extra" :id="command.name" :key="command.name" :label="command.label"
              :why="agv.commandReason(command.name)" :outcome="agv.feedback(command.name)" @press="agv.send(command.name)" />
          </div>
        </details>
      </section>
    </aside>

    <AgvRecord :agv="agv" />
  </div>
</template>
