<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import Card from 'primevue/card';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import { downloadBlob, request, requestDownload } from './api';

const props = defineProps({ quality: String, variable: Function, catalog: Object });
const emit = defineEmits(['outcome', 'changed']);
const path = '/api/tests/line';
const state = ref(null);
const archives = ref([]);
const archive = ref(null);
const archiveFilter = ref('');
const historyFilter = ref('');
const session = ref('');
const sessionUntil = ref(0);
const now = ref(Date.now());
const working = ref('');
const error = ref('');
const archiveError = ref('');
const statusError = ref('');
const feedback = ref(null);
const command = ref('');
const query = ref('');
const group = ref('');
const selected = ref('');
const review = ref(null);
const selectedRun = ref('');
const human = ref('Not checked');
const notes = ref('');
let timer, retired = false, reading = false, waitingSince = 0, pendingRun = '', lastPhase = '';
let archiveRequest = 0;

const remaining = computed(() => Math.max(0, Math.ceil((sessionUntil.value - now.value) / 1000)));
const armed = computed(() => Boolean(session.value && remaining.value));
const allCases = computed(() => state.value?.cases ?? []);
const manual = computed(() => (props.catalog?.tags ?? []).filter(tag => tag.read_write === 'W' && tag.data_type === 'Bool'));
const groups = computed(() => [...new Set(manual.value.map(tag => tag.group))].sort());
const shownCommands = computed(() => {
  const text = query.value.trim().toLowerCase();
  return manual.value.filter(tag => (!group.value || tag.group === group.value) &&
    [tag.tag_name, tag.description, tag.device_address].some(value => value.toLowerCase().includes(text)));
});
const cases = computed(() => allCases.value.filter(item => !command.value || item.command === command.value));
const mapping = computed(() => cases.value.find(item => item.id === selected.value));
const active = computed(() => state.value?.active);
const history = computed(() => state.value?.history ?? []);
const shownHistory = computed(() => history.value.filter(run => !historyFilter.value || run.api === historyFilter.value));
const saved = computed(() => selectedRun.value ? history.value.find(run => run.id === selectedRun.value) :
  shownHistory.value[0]);
const rows = computed(() => (archive.value?.rows ?? []).filter(row =>
  !archiveFilter.value || row.api === archiveFilter.value));
const unavailable = 'Line test service unavailable. Check the simulator and reload the page.';
const modeReady = computed(() => props.variable('line.Remote')?.value === 1 && props.variable('line.Local')?.value === 0);
const armReason = computed(() => statusError.value || state.value?.reason ||
  (state.value?.enabled ? '' : 'Checking line test availability.'));
const reviewReason = computed(() => armReason.value || (!armed.value ? 'Arm a simulator write session.' :
  !mapping.value ? 'Choose a reviewed command and feedback case.' :
    props.quality !== 'fresh' || !modeReady.value ? 'Wait for a complete fresh row with Remote = 1 and Local = 0.' : ''));
const approvalSeconds = computed(() => Math.max(0, Math.ceil(((review.value?.until ?? 0) - now.value) / 1000)));
const actionTitles = { arm: 'Session armed', disarm: 'Session disarmed', prepare: 'Approval ready', judge: 'Judgment saved' };
const localTime = stamp => stamp ? new Date(stamp).toLocaleTimeString(undefined, { fractionalSecondDigits: 3 }) : 'Unknown';

function show(text, tone, select = false) {
  if (feedback.value?.text === text && feedback.value?.tone === tone) return;
  feedback.value = { text, tone, pending: tone === 'running' || tone === 'waiting' };
  emit('outcome', feedback.value, select);
}

async function refresh() {
  if (reading || retired) return;
  reading = true;
  try {
    if (session.value && (active.value || pendingRun)) {
      await request(path, { action: 'heartbeat', session: session.value }, 2500);
    }
    const result = await request(path, null, 2500);
    if (retired) return;
    statusError.value = '';
    state.value = result;
    if (result.active) {
      const run = result.active;
      if (lastPhase !== run.state) {
        waitingSince = Date.now();
        lastPhase = run.state;
      }
      if (run.id === pendingRun && (run.state !== 'checking' || Date.now() - waitingSince >= 1000)) {
        const text = { checking: 'Checking catalog and baseline', observing: 'Waiting for later feedback',
          clearing: 'Waiting for command zero confirmation' }[run.state] ?? 'Sending simulator command';
        show(text + (run.deadline_s == null ? '…' : `… ${Math.ceil(run.deadline_s)} s`), 'waiting');
      }
    } else if (pendingRun && result.latest?.id === pendingRun) {
      const run = result.latest;
      pendingRun = '';
      selectedRun.value = run.id;
      show(`API ${run.api}. ${run.reason}`, run.api === 'Pass' || run.api === 'Observed' ? 'done' : 'failed');
    }
  } catch {
    if (!retired) statusError.value = unavailable;
  } finally {
    reading = false;
  }
}

async function perform(action, fields = {}, accept = () => {}) {
  if (working.value) return;
  working.value = action;
  error.value = '';
  show('Sending…', 'running', true);
  const timeout = setTimeout(() => show('Waiting for the line test service… 3 s', 'waiting'), 1000);
  try {
    const result = await request(path, { action, ...fields }, 4000);
    if (retired) return;
    accept(result);
    if (actionTitles[action]) show(actionTitles[action] + '.', 'done');
    if (action === 'start') {
      pendingRun = result.id;
      emit('changed');
    }
    void refresh();
  } catch (failure) {
    if (retired) return;
    error.value = failure.status === 400 ? failure.message :
      failure.status === 403 ? 'Page access expired. Reload to check the test status.' : unavailable;
    if (action === 'start' && failure.status !== 400 && failure.status !== 403) pendingRun = fields.request_id;
    show(error.value, 'failed');
  } finally {
    clearTimeout(timeout);
    working.value = '';
  }
}

function arm() {
  void perform('arm', {}, result => {
    session.value = result.session;
    now.value = Date.now();
    sessionUntil.value = now.value + result.seconds * 1000;
  });
}

function disarm() {
  void perform('disarm', { session: session.value }, () => {
    session.value = '';
    review.value = null;
  });
}

function prepare() {
  const id = crypto.randomUUID();
  const item = mapping.value;
  void perform('prepare', { session: session.value, case: item.id, request_id: id }, result => {
    review.value = { ...result, id, item, until: Date.now() + result.seconds * 1000 };
  });
}

function start() {
  const approval = review.value;
  review.value = null;
  void perform('start', { session: session.value, request_id: approval.id, approval: approval.approval });
}

function saveJudgment() {
  void perform('judge', { request_id: saved.value.id, result: human.value, notes: notes.value });
}

async function loadArchives() {
  try {
    const value = await request(path + '/archives');
    archives.value = value.archives;
    archiveError.value = '';
  } catch {
    archiveError.value = 'Archives unavailable. Check the imported evidence and reload.';
  }
}

async function selectArchive(event) {
  const id = event.target.value;
  const selection = ++archiveRequest;
  archive.value = null;
  archiveError.value = '';
  if (!id) return;
  try {
    const result = await request(path + '/archives/' + id);
    if (selection === archiveRequest) archive.value = result;
  } catch {
    if (selection === archiveRequest) archiveError.value = 'Archive unavailable. Select an imported archive.';
  }
}

async function download(url, name) {
  try {
    downloadBlob(await requestDownload(path + '/' + url), name);
  } catch {
    archiveError.value = 'Download unavailable. Check the local evidence and try again.';
  }
}

watch([command, group, query], () => {
  if (command.value && !shownCommands.value.some(tag => tag.tag_name === command.value)) command.value = '';
  if (!cases.value.some(item => item.id === selected.value)) selected.value = '';
});
watch(selected, () => { review.value = null; });
watch(() => saved.value?.id, () => {
  human.value = saved.value?.human?.result ?? 'Not checked';
  notes.value = saved.value?.human?.notes ?? '';
});
watch(historyFilter, () => { selectedRun.value = ''; });
onMounted(() => {
  void refresh();
  void loadArchives();
  timer = setInterval(() => {
    now.value = Date.now();
    void refresh();
  }, 750);
});
onUnmounted(() => {
  retired = true;
  clearInterval(timer);
});

defineExpose({
  cancelReview() {
    if (!review.value) return false;
    review.value = null;
    show('Approval cancelled.', 'done', true);
    return true;
  },
});
</script>

<template>
  <Card class="panel line-content">
    <template #title><h2>0/1 command test <Tag severity="warn" value="Simulation" /></h2></template>
    <template #content>
      <p v-for="item in state?.unresolved ?? []" :key="item.id">
        Unresolved test {{ item.id }}<template v-if="item.command"> · {{ item.command }}</template>.
      </p>
      <Message v-if="statusError" severity="error" role="alert">{{ statusError }}</Message>
      <div class="line-actions">
        <Button v-if="!armed" id="line-arm" label="Arm simulator writes" :disabled="Boolean(armReason || working)" @click="arm" />
        <template v-else>
          <Tag severity="warn" :value="'Armed · ' + remaining + ' s'" />
          <Button label="Disarm simulator writes" severity="secondary" :disabled="Boolean(working)" @click="disarm" />
        </template>
        <span v-if="!armed && armReason">{{ armReason }}</span>
      </div>
      <div class="line-controls">
        <template v-if="manual.length">
          <label for="line-command-search">Search command name, description or address
            <InputText id="line-command-search" v-model="query" type="search" />
          </label>
          <label for="line-command-group">Command group
            <select id="line-command-group" v-model="group"><option value="">All groups</option>
              <option v-for="item in groups" :key="item">{{ item }}</option>
            </select>
          </label>
          <label for="line-command">Writable 0/1 command
            <select id="line-command" v-model="command"><option value="">All commands</option>
              <option v-for="tag in shownCommands" :key="tag.tag_name" :value="tag.tag_name">
                {{ tag.tag_name }} · {{ tag.device_address }}
              </option>
            </select>
          </label>
        </template>
        <label for="line-case">Reviewed feedback case
          <select id="line-case" v-model="selected"><option value="">Choose a case</option>
            <option v-for="item in cases" :key="item.id" :value="item.id">{{ item.command }} → {{ item.feedback }}</option>
          </select>
        </label>
      </div>
      <p v-if="command && !cases.length">This command has no reviewed simulator case. Review the local mapping to enable a test.</p>
      <p v-if="mapping">
        {{ mapping.basis }} · Feedback {{ mapping.feedback }}: {{ mapping.baseline }} → {{ mapping.expected }}
        · Up to {{ mapping.seconds }} s.
        <span v-if="mapping.provisional">Provisional mapping. Matching feedback is recorded as Observed.</span>
      </p>
      <p v-if="mapping?.guidance">{{ mapping.guidance }}</p>
      <div class="line-actions">
        <Button id="line-review" label="Review 0→1→0 test" :disabled="Boolean(reviewReason || working || review)" @click="prepare" />
        <span v-if="reviewReason">{{ reviewReason }}</span>
      </div>
      <section v-if="review" class="line-approval" aria-labelledby="line-approval-heading">
        <h3 id="line-approval-heading">Approve one simulator test</h3>
        <p>Baseline PLC row {{ review.baseline.id }} at {{ localTime(review.baseline.source_at) }}.
          {{ review.item.command }} {{ review.metadata[2]?.device_address }}: 0 → 1 → 0.
          {{ review.item.feedback }} {{ review.metadata[3]?.device_address }}: {{ review.item.baseline }} → {{ review.item.expected }}.
          Approval expires in {{ approvalSeconds }} s.</p>
        <div class="line-actions">
          <Button id="line-confirm" label="Approve and send 1" :disabled="Boolean(working || reviewReason || !approvalSeconds)" @click="start" />
          <Button label="Cancel approval" severity="secondary" @click="review = null" />
          <span v-if="!approvalSeconds">Approval expired. Cancel and review a new test.</span>
        </div>
      </section>
      <p v-if="feedback" :data-state="feedback.tone" role="status">{{ feedback.text }}</p>
      <Message v-else-if="error" severity="error">{{ error }}</Message>

      <section v-if="active" aria-labelledby="line-active-heading">
        <h3 id="line-active-heading">Active simulator test</h3>
        <p role="status">{{ { checking: 'Checking catalog and baseline', observing: 'Watching feedback',
          clearing: 'Restoring command zero' }[active.state] ?? 'Sending' }}
          <template v-if="active.deadline_s != null"> · {{ Math.ceil(active.deadline_s) }} s left</template></p>
        <dl class="capabilities">
          <dt>Command</dt><dd>{{ active.case.command }}</dd>
          <dt>Feedback</dt><dd>{{ active.case.feedback }}</dd>
          <dt>Baseline</dt><dd>{{ active.baseline ? 'PLC row ' + active.baseline.id : 'Checking' }}</dd>
          <dt>Receipt for 1</dt><dd>{{ active.one ? (active.one.accepted ? 'Accepted' : 'Unconfirmed') + ' · ' + localTime(active.one.at) : 'Pending' }}</dd>
          <dt>Later feedback</dt><dd>{{ active.observation ? 'Matched in PLC row ' + active.observation.id : 'Pending' }}</dd>
        </dl>
        <p>To request an early clear, use <strong>Line Stop · clear command</strong> above.</p>
      </section>
    </template>
  </Card>

  <Card class="panel line-content">
    <template #title><h2>Line test evidence</h2></template>
    <template #content>
      <div class="line-controls">
        <label for="line-history-filter">Simulator API result
          <select id="line-history-filter" v-model="historyFilter"><option value="">All results</option>
            <option v-for="item in ['Pass', 'Fail', 'Observed', 'Refused', 'Restore unconfirmed']" :key="item">{{ item }}</option>
          </select>
        </label>
        <label for="line-run">Simulator test
          <select id="line-run" v-model="selectedRun"><option value="">Latest matching test</option>
            <option v-for="record in shownHistory" :key="record.id" :value="record.id">
              {{ record.case.command }} · {{ localTime(record.at) }} · {{ record.api }}
            </option>
          </select>
        </label>
      </div>
      <p v-if="!saved">No matching simulator tests.</p>
      <template v-else>
        <p><Tag severity="secondary" :value="'API · ' + saved.api" /> <Tag severity="secondary" :value="'Human · ' + (saved.human?.result ?? 'Not checked')" /></p>
        <p>{{ saved.reason }}</p>
        <details>
          <summary>Baseline, receipts and readback</summary>
          <dl class="capabilities">
            <dt>Command and feedback</dt><dd>{{ saved.case.command }} → {{ saved.case.feedback }}</dd>
            <dt>Mapping</dt><dd>{{ saved.case.basis }}</dd>
            <dt>Before</dt><dd>{{ saved.baseline ? 'PLC row ' + saved.baseline.id + ' · ' + localTime(saved.baseline.source_at) : 'Unavailable' }}</dd>
            <dt>Accepted 1</dt><dd>{{ saved.one?.accepted ? localTime(saved.one.at) : 'Unconfirmed' }}</dd>
            <dt>Later feedback match</dt><dd>{{ saved.observation?.id ?? 'Unconfirmed' }}</dd>
            <dt>Accepted 0</dt><dd>{{ saved.zero_attempts?.at(-1)?.accepted ? localTime(saved.zero_attempts.at(-1).at) : 'Unconfirmed' }}</dd>
            <dt>Later zero readback</dt><dd>{{ saved.zero_readback?.id ?? 'Unconfirmed' }}</dd>
          </dl>
          <ul v-if="saved.judgments?.length"><li v-for="(item, index) in saved.judgments" :key="index">
            {{ localTime(item.at) }} · {{ item.result }} · {{ item.notes }}
          </li></ul>
        </details>
        <div class="line-controls">
          <label for="line-human-result">Human result
            <select id="line-human-result" v-model="human"><option>Not checked</option><option>Pass</option><option>Fail</option></select>
          </label>
          <label for="line-human-notes">Observation or notes
            <Textarea id="line-human-notes" v-model="notes" maxlength="2000" rows="2" />
          </label>
        </div>
        <div class="line-actions">
          <Button id="line-save-judgment" label="Save human result" :disabled="Boolean(working)" @click="saveJudgment" />
          <Button v-for="[format, suffix] in [['summary', 'md'], ['json', 'json'], ['csv', 'csv']]" :key="format"
            :label="'Download ' + format" severity="secondary"
            @click="download('run/' + saved.id + '/' + format, 'line-test-' + saved.id + '.' + suffix)" />
        </div>
      </template>
      <div class="line-controls">
        <label for="line-archive">Historical archive
          <select id="line-archive" @change="selectArchive"><option value="">Choose an imported archive</option>
            <option v-for="item in archives" :key="item.id" :value="item.id">
              {{ item.context?.mode ?? 'Original reports' }} · {{ item.files.length }} files · {{ item.id.slice(0, 10) }}
            </option>
          </select>
        </label>
        <label v-if="archive" for="line-archive-filter">Archived API result
          <select id="line-archive-filter" v-model="archiveFilter"><option value="">All results</option>
            <option>Pass</option><option>Fail</option><option>Unchecked</option>
          </select>
        </label>
      </div>
      <Message v-if="archiveError" severity="error" role="alert">{{ archiveError }}</Message>
      <template v-if="archive">
        <p v-if="archive.context">Historical source · {{ archive.context.mode }} · {{ archive.context.api_address }}</p>
        <p>API · {{ archive.api_counts.Pass }} Pass / {{ archive.api_counts.Fail }} Fail / {{ archive.api_counts.Unchecked }} Unchecked</p>
        <p>Human · {{ archive.human_counts.Pass }} Pass / {{ archive.human_counts.Fail }} Fail /
          {{ archive.human_counts['Not checked'] }} Not checked</p>
        <p>{{ archive.history_events }} saved history events. {{ archive.unmatched }} unmatched entries are available in the originals.</p>
        <details>
          <summary>Historical labels and provenance</summary>
          <ul class="line-readings"><li v-for="row in rows" :key="JSON.stringify(row.identity)">
            <strong>{{ row.identity[0] }} · {{ row.identity[1] }}</strong>
            <p>API · {{ row.api }} · {{ row.api_source }}<br />Human · {{ row.human }}</p>
            <small v-if="row.api_evidence">{{ row.api_evidence.source }} {{ row.api_evidence.evidence_ref }}
              {{ row.api_evidence.checked_at || row.api_evidence.at }}</small>
            <small v-if="row.human_evidence">{{ row.human_evidence.source }} {{ row.human_evidence.evidence_ref }}
              {{ row.human_evidence.observed_at || row.human_evidence.at }}</small>
          </li></ul>
        </details>
        <details>
          <summary>Original files ({{ archive.files.length }})</summary>
          <ul><li v-for="(name, index) in archive.files" :key="name">
            <Button :label="'Download ' + name" severity="secondary"
              @click="download('archives/' + archive.id + '/' + index, name.split('/').at(-1))" />
          </li></ul>
        </details>
      </template>
    </template>
  </Card>
</template>
