<script setup>
import { downloadBlob } from './api';
import { computed, ref } from 'vue';
import Accordion from 'primevue/accordion';
import AccordionContent from 'primevue/accordioncontent';
import AccordionHeader from 'primevue/accordionheader';
import AccordionPanel from 'primevue/accordionpanel';
import Button from 'primevue/button';
import Column from 'primevue/column';
import ConfirmDialog from 'primevue/confirmdialog';
import DataTable from 'primevue/datatable';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Panel from 'primevue/panel';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import { useConfirm } from 'primevue/useconfirm';
import {
  checklist, checklistCommands, connectionKeyFor, exportRecord, foundFields,
  isoNow, localTime, markdown, outcomesList, resultText,
} from '../app/anodizing_web/web/agv-record.mjs';

const { agv } = defineProps({ agv: { type: Object, required: true } });
const markText = ref('');
const confirm = useConfirm();
const steps = checklist.map(([title, instructions, watch], index) => ({
  number: index + 1, title, instructions, watch,
}));
const resultSeverity = { 'Not tested': 'secondary', Pass: 'success', Fail: 'danger', Skipped: 'warn' };
const foundFieldType = { date: 'date', 'restart-seconds': 'number' };
const raw = computed(() => agv.journal.log.slice(-30).reverse());
const commands = computed(() => Object.values(agv.journal.commands).slice(-30).reverse());

function confirmReset(event) {
  confirm.require({
    target: event.currentTarget,
    header: 'Start a new test session',
    message: 'This clears the current checklist, settings and record. Download the record first if you need it.',
    acceptLabel: 'Start new session',
    rejectLabel: 'Cancel',
    rejectProps: { severity: 'secondary' },
    defaultFocus: 'reject',
    closeOnEscape: false,
    modal: false,
    blockScroll: false,
    accept: agv.newRecord,
  });
}

function updateTest(number, field, value) {
  const saved = agv.session.checklist[number] ?? {};
  const next = { ...saved, [field]: value };
  if (field === 'result') {
    const marked = isoNow();
    const recent = new Date(Date.now() - 120000).toISOString();
    const since = saved.opened_at && saved.opened_at < recent ? saved.opened_at : recent;
    const during = entry => entry.timestamp >= since && entry.timestamp <= marked;
    next.evidence = {
      marked_at: marked, status: agv.state.status ?? null, status_age_s: agv.age,
      connected: Boolean(agv.state.connected), stale: agv.stale, transport: agv.state.settings?.mode ?? null,
      commands: Object.values(agv.journal.commands).filter(entry => during(entry) &&
        (entry.requested_commands ?? [entry.requested_command ?? entry.name])
          .some(command => checklistCommands[number - 1].includes(command))),
      events: agv.journal.events.filter(during),
    };
  }
  agv.session.checklist[number] = next;
  agv.persistSession();
  if (field === 'result') agv.persistJournal();
}

function opened(number) {
  if (number && !agv.session.checklist[number]?.opened_at) updateTest(number, 'opened_at', isoNow());
}

function updateSetting(key, value) {
  agv.session.settings[key] = String(value ?? '');
  agv.persistSession();
}

function observed() {
  const text = markText.value.trim();
  if (!text) return;
  const timestamp = isoNow();
  agv.journal.events.push({ timestamp, field: 'observed', text, connection: connectionKeyFor(agv.state),
    observation_id: crypto.randomUUID() });
  markText.value = '';
  agv.recordNote = 'Observed event recorded at ' + localTime(timestamp) + '.';
  agv.persistSession();
  agv.persistJournal();
}

function download(format) {
  const record = exportRecord(agv.state, agv.session, agv.journal);
  const data = format === 'json' ? JSON.stringify(record, null, 2) : markdown(record, agv.commands);
  downloadBlob(new Blob([data], { type: format === 'json' ? 'application/json' : 'text/markdown' }),
    format === 'json' ? 'agv-test-record.json' : 'agv-test-report.md');
}
</script>

<template>
  <div data-ui="agv.record.workspace" class="agv-workflow">
    <Panel data-ui="agv.record.checklist" class="panel agv-surface">
      <template #header><h2>Commissioning checklist</h2></template>
      <Message severity="secondary" :pt="{ root: { role: 'note', 'aria-live': 'off' } }">
        <strong>Before testing</strong> Confirm the robot is idle, the track is clear, a person is at the AGV’s e-stop, and fresh status is visible.
        The AGV may restart by itself in Auto mode or when automatic restart is enabled. Check both settings before moving.
      </Message>
      <div class="checklist">
        <Accordion v-for="{ number, title, instructions, watch } in steps" :key="number" @update:value="opened">
          <AccordionPanel data-ui="agv.record.checklist.row" :data-ui-key="number" :id="'agv-test-' + number" :value="number">
            <AccordionHeader data-ui="agv.record.checklist.toggle" :data-ui-key="number">
              <span class="checklist-label">{{ number }}. {{ title }}</span>
              <Tag :value="agv.session.checklist[number]?.result ?? 'Not tested'"
                :severity="resultSeverity[agv.session.checklist[number]?.result] ?? 'secondary'" />
            </AccordionHeader>
            <AccordionContent>
              <p>{{ instructions }}</p>
              <p>Watch for this. {{ watch }}</p>
              <div class="field-row">
                <div class="checklist-field">
                  <label :id="'agv-result-label-' + number" :for="'agv-result-' + number">Result</label>
                  <Select :pt="{ label: { 'data-ui': 'agv.record.checklist.result', 'data-ui-key': number } }" :input-id="'agv-result-' + number" :aria-labelledby="'agv-result-label-' + number"
                    :model-value="agv.session.checklist[number]?.result ?? 'Not tested'" :options="outcomesList" fluid
                    @update:model-value="updateTest(number, 'result', $event)" />
                </div>
                <label :for="'agv-notes-' + number">Notes
                  <Textarea data-ui="agv.record.checklist.notes" :data-ui-key="number" :id="'agv-notes-' + number" rows="3" :model-value="agv.session.checklist[number]?.notes ?? ''"
                    @update:model-value="updateTest(number, 'notes', $event)" />
                </label>
              </div>
              <p v-if="agv.session.checklist[number]?.evidence">
                Observed {{ localTime(agv.session.checklist[number].evidence.marked_at) }} ·
                {{ agv.session.checklist[number].evidence.commands.length }} command results ·
                {{ agv.session.checklist[number].evidence.events.length }} events
              </p>
            </AccordionContent>
          </AccordionPanel>
        </Accordion>
      </div>
    </Panel>
    <Panel data-ui="agv.record.events" class="panel agv-surface">
      <template #header><h2>Events</h2></template>
      <form data-ui="agv.record.event.form" class="field-row" @submit.prevent="observed">
        <label for="agv-mark-text">What did you observe?
          <InputText data-ui="agv.record.event.text" id="agv-mark-text" v-model="markText" maxlength="120" placeholder="e.g. AGV stopped" required />
        </label>
        <Button data-ui="agv.record.event.add" id="agv-mark" label="Mark event" type="submit" severity="secondary" />
      </form>
      <ol id="agv-event-feed">
        <li data-ui="agv.record.event.row" :data-ui-key="event.id == null ? event.timestamp + '.' + event.field : event.connection + '.' + event.id" v-for="(event, index) in agv.journal.events.slice(-12).reverse()" :key="index">
          {{ localTime(event.timestamp) }} · {{ event.text }}
        </li>
      </ol>
    </Panel>
    <Accordion class="panel agv-surface">
      <AccordionPanel data-ui="agv.record.settings" value="found-settings">
        <AccordionHeader data-ui="agv.record.settings.toggle">Settings as found</AccordionHeader>
        <AccordionContent>
          <div class="controls found-fields">
            <label v-for="(label, key) in foundFields" :key="key" :for="'agv-found-' + key">{{ label }}
              <InputText data-ui="agv.record.settings.field" :data-ui-key="key" :id="'agv-found-' + key" :type="foundFieldType[key] ?? 'text'"
                :min="key === 'restart-seconds' ? 0 : undefined" :step="key === 'restart-seconds' ? 'any' : undefined"
                :model-value="agv.session.settings[key] ?? ''" @update:model-value="updateSetting(key, $event)" />
            </label>
          </div>
        </AccordionContent>
      </AccordionPanel>
    </Accordion>
    <Panel data-ui="agv.record.exports" class="panel agv-surface">
      <template #header><h2>Test record</h2></template>
      <div class="controls">
        <Button data-ui="agv.record.export.markdown" id="agv-download-report" label="Download readable report" type="button" severity="secondary" @click="download('markdown')" />
        <Button data-ui="agv.record.export.json" id="agv-download" label="Download JSON record" type="button" severity="secondary" @click="download('json')" />
        <Button data-ui="agv.record.reset" id="agv-new-session" label="New test session" type="button" severity="secondary" @click="confirmReset" />
      </div>
      <p id="agv-record-note" role="status">{{ agv.recordNote }}</p>
      <ConfirmDialog :draggable="false" :pt="{ root: { 'data-ui': 'agv.record.reset.dialog', class: 'record-dialog', 'aria-describedby': 'agv-reset-description' },
          pcCloseButton: { root: { 'data-ui': 'agv.record.reset.close' } },
          pcAcceptButton: { root: { 'data-ui': 'agv.record.reset.accept' } },
          pcRejectButton: { root: { 'data-ui': 'agv.record.reset.cancel' } } }">
        <template #message="{ message }">
          <p id="agv-reset-description">{{ message.message }}</p>
        </template>
      </ConfirmDialog>
      <Accordion lazy>
        <AccordionPanel data-ui="agv.record.commands" value="command-results">
          <AccordionHeader data-ui="agv.record.commands.toggle">Recent command results</AccordionHeader>
          <AccordionContent>
            <DataTable id="agv-command-results" :value="commands" class="record-table" scrollable scroll-height="24rem"
              :pt="{ tableContainer: { 'data-ui': 'agv.record.commands.table', tabindex: 0 },
                bodyRow: ({ props }) => ({ 'data-ui': 'agv.record.commands.row', 'data-ui-key': props.rowData.connection + '.' + props.rowData.tx_id }) }" :table-props="{ 'aria-label': 'Recent command results' }">
              <template #empty>No command results in this session.</template>
              <Column field="timestamp" header="Time"><template #body="{ data }">{{ localTime(data.timestamp) }}</template></Column>
              <Column field="name" header="Command"><template #body="{ data }">{{ agv.commands.get(data.name)?.label ?? data.name }}</template></Column>
              <Column field="state" header="Outcome">
                <template #body="{ data }">
                  <span :data-state="resultText(data.name, data).tone">{{ resultText(data.name, data).text }}</span>
                </template>
              </Column>
            </DataTable>
          </AccordionContent>
        </AccordionPanel>
      </Accordion>
    </Panel>
    <Accordion class="panel agv-surface" lazy>
      <AccordionPanel data-ui="agv.record.frames" value="raw-log">
        <AccordionHeader data-ui="agv.record.frames.toggle">Raw TX/RX log</AccordionHeader>
        <AccordionContent>
          <Message v-if="agv.journal.log_gap" severity="warn">Some log entries are missing from this record.</Message>
          <p v-if="agv.state.status_hex">Raw status frame <code>{{ agv.state.status_hex }}</code></p>
          <DataTable id="agv-log" :value="raw" class="record-table" scrollable scroll-height="24rem"
            :pt="{ tableContainer: { 'data-ui': 'agv.record.frames.table', tabindex: 0 },
              bodyRow: ({ props }) => ({ 'data-ui': 'agv.record.frames.row', 'data-ui-key': props.rowData.connection + '.' + props.rowData.id }) }" :table-props="{ 'aria-label': 'Timestamped TX/RX log' }">
            <template #empty>No frames in this session.</template>
            <Column field="timestamp" header="Time"><template #body="{ data }">{{ localTime(data.timestamp) }}</template></Column>
            <Column field="direction" header="Direction" />
            <Column field="hex" header="Frame"><template #body="{ data }"><code>{{ data.hex }}</code></template></Column>
            <Column field="decode" header="Decoded"><template #body="{ data }"><code>{{ JSON.stringify(data.decode) }}</code></template></Column>
          </DataTable>
        </AccordionContent>
      </AccordionPanel>
    </Accordion>
  </div>
</template>

<style scoped>
.agv-surface { padding: 0; }
.agv-surface h2 { margin: 0; }
.checklist { margin-top: .75rem; }
.checklist :deep(.p-accordionheader) { gap: .4rem; }
.checklist-label { flex: 1; text-align: start; }
.checklist-field, .found-fields > label { flex: 1 1 11rem; min-width: 0; }
#agv-event-feed:empty { display: none; }
.record-table code, p > code { overflow-wrap: anywhere; }
</style>
