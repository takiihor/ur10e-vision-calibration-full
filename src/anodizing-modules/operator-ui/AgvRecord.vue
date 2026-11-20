<script setup>
import { computed, ref } from 'vue';
import Button from 'primevue/button';
import Dialog from 'primevue/dialog';
import {
  checklist, checklistCommands, connectionKeyFor, exportRecord, foundFields,
  isoNow, localTime, markdown, outcomesList,
} from '../app/anodizing_web/web/agv-record.mjs';

const { agv } = defineProps({ agv: { type: Object, required: true } });
const markText = ref('');
const confirmReset = ref(false);
const raw = computed(() => agv.journal.log.slice(-30).reverse().map(entry =>
  `${localTime(entry.timestamp)}  ${entry.direction}  ${entry.hex}\n  ${JSON.stringify(entry.decode)}`).join('\n'));

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
      commands: Object.values(agv.journal.commands).filter(entry => during(entry) && checklistCommands[number - 1].includes(entry.name)),
      events: agv.journal.events.filter(during),
    };
  }
  agv.session.checklist[number] = next;
  agv.persistSession();
  if (field === 'result') agv.persistJournal();
}

function opened(event, number) {
  if (event.currentTarget.open && !agv.session.checklist[number]?.opened_at) updateTest(number, 'opened_at', isoNow());
}

function observed() {
  const text = markText.value.trim();
  if (!text) return;
  const timestamp = isoNow();
  agv.journal.events.push({ timestamp, field: 'observed', text, connection: connectionKeyFor(agv.state) });
  markText.value = '';
  agv.recordNote = 'Observed event recorded at ' + localTime(timestamp) + '.';
  agv.persistSession();
  agv.persistJournal();
}

function download(format) {
  const record = exportRecord(agv.state, agv.session, agv.journal);
  const data = format === 'json' ? JSON.stringify(record, null, 2) : markdown(record, agv.commands);
  const url = URL.createObjectURL(new Blob([data], { type: format === 'json' ? 'application/json' : 'text/markdown' }));
  const link = Object.assign(document.createElement('a'), {
    href: url, download: format === 'json' ? 'agv-test-record.json' : 'agv-test-report.md',
  });
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
</script>

<template>
  <div class="agv-workflow">
    <section class="panel">
      <h2>Commissioning checklist</h2>
      <p><strong>Before testing</strong> Confirm the robot is idle, the track is clear, a person is at the AGV’s e-stop, and fresh status is visible.</p>
      <p>The AGV may restart by itself in Auto mode or when automatic restart is enabled. Check both settings before moving.</p>
      <details v-for="([title, instructions, watch], index) in checklist" :key="title" :id="'agv-test-' + (index + 1)"
        @toggle="opened($event, index + 1)">
        <summary>{{ title }} · {{ agv.session.checklist[index + 1]?.result ?? 'Not tested' }}</summary>
        <p>{{ instructions }}</p>
        <p>Watch for this. {{ watch }}</p>
        <label :for="'agv-result-' + (index + 1)">Result
          <select :id="'agv-result-' + (index + 1)" :value="agv.session.checklist[index + 1]?.result ?? 'Not tested'"
            @change="updateTest(index + 1, 'result', $event.target.value)">
            <option v-for="result in outcomesList" :key="result">{{ result }}</option>
          </select>
        </label>
        <label :for="'agv-notes-' + (index + 1)">Notes
          <textarea :id="'agv-notes-' + (index + 1)" rows="3" :value="agv.session.checklist[index + 1]?.notes ?? ''"
            @input="updateTest(index + 1, 'notes', $event.target.value)"></textarea>
        </label>
        <p v-if="agv.session.checklist[index + 1]?.evidence">
          Observed {{ localTime(agv.session.checklist[index + 1].evidence.marked_at) }} ·
          {{ agv.session.checklist[index + 1].evidence.commands.length }} command results ·
          {{ agv.session.checklist[index + 1].evidence.events.length }} events
        </p>
      </details>
    </section>
    <section class="panel">
      <h2>Events</h2>
      <form class="field-row" @submit.prevent="observed">
        <label for="agv-mark-text">What did you observe?
          <input id="agv-mark-text" v-model="markText" maxlength="120" placeholder="e.g. AGV stopped" required>
        </label>
        <Button id="agv-mark" label="Mark event" type="submit" />
      </form>
      <ol id="agv-event-feed">
        <li v-for="(event, index) in agv.journal.events.slice(-12).reverse()" :key="index">
          {{ localTime(event.timestamp) }} · {{ event.text }}
        </li>
      </ol>
    </section>
    <details class="panel">
      <summary>Settings as found</summary>
      <div class="controls">
        <label v-for="(label, key) in foundFields" :key="key" :for="'agv-found-' + key">{{ label }}
          <input :id="'agv-found-' + key" v-model="agv.session.settings[key]"
            :type="key === 'date' ? 'date' : key === 'restart-seconds' ? 'number' : 'text'"
            @input="agv.session.settings[key] = $event.target.value; agv.persistSession()">
        </label>
      </div>
    </details>
    <section class="panel">
      <h2>Test record</h2>
      <div class="controls">
        <Button id="agv-download-report" label="Download readable report" type="button" @click="download('markdown')" />
        <Button id="agv-download" label="Download JSON record" type="button" @click="download('json')" />
        <Button id="agv-new-session" label="New test session" type="button" @click="confirmReset = true" />
      </div>
      <p id="agv-record-note" role="status">{{ agv.recordNote }}</p>
      <Dialog v-model:visible="confirmReset" modal header="Start a new test session" :close-on-escape="false"
        :closable="false" :pt="{ root: { class: 'record-dialog' } }">
        <p>This clears the current checklist, settings and record. Download the record first if you need it.</p>
        <template #footer>
          <Button label="Cancel" autofocus @click="confirmReset = false" />
          <Button label="Start new session" @click="agv.newRecord(); confirmReset = false" />
        </template>
      </Dialog>
    </section>
    <details class="panel">
      <summary>Raw TX/RX log</summary>
      <p v-if="agv.journal.log_gap">Some log entries are missing from this record.</p>
      <pre aria-label="Raw status frame">{{ agv.state.status_hex }}</pre>
      <pre id="agv-log" tabindex="0" aria-label="Timestamped TX/RX log">{{ raw }}</pre>
    </details>
  </div>
</template>
