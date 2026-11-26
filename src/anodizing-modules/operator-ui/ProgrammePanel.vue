<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { request } from './api';

const emit = defineEmits(['changed']);
const recipe = ref(null);
const run = ref(null);
const baskets = ref([]);
const selectedId = ref('');
const historical = ref(null);
const record = computed(() => selectedId.value ? historical.value : run.value);
const approval = ref(null);
const reviewCursor = ref(null);
const block = ref(null);
const limits = ref({});
const edited = ref({});
const review = ref({ terminal_handling: '', timing_evidence: '' });
const hasEdits = computed(() => Object.keys(edited.value).length > 0);
const reviewComplete = computed(() => Object.values(review.value).every(value => value.trim()));
const basket = ref('');
const operator = ref('');
const checkedBasket = ref('');
const location = ref('');
const confirmed = ref(false);
const step = ref(null);
const technician = ref(null);
const ready = ref(false);
const busy = ref(false);
const starting = ref(false);
const startUnresolved = ref(false);
const error = ref('');
let timer;
let alive = true;
const fields = [
  ['min', 'minimum dwell'],
  ['overstay', 'maximum overstay'],
  ['air', 'maximum air time'],
];
const check = computed(() => run.value?.operator_check);
const active = computed(() => run.value && (run.value.stop_pending || ['running', 'operator_check'].includes(run.value.state)));
const decisionReady = computed(() => confirmed.value && operator.value.trim() && checkedBasket.value.trim() && location.value.trim()
  && approval.value && !check.value?.technician && !run.value?.stop_pending && !technician.value && !busy.value);

function decide(action) {
  return act(action, {
    job_id: run.value.id, approval: approval.value, operator: operator.value.trim(), basket: checkedBasket.value.trim(),
    location: location.value.trim(), confirmed: confirmed.value, ...(action === 'resume' ? { step: step.value } : {}),
  });
}

async function refresh() {
  const resolvesStart = startUnresolved.value && !starting.value;
  const next = await request('/api/programme');
  if (!alive) return;
  if (next.recipe?.id !== recipe.value?.id || next.recipe?.state !== recipe.value?.state) {
    recipe.value = next.recipe;
    limits.value = Object.fromEntries(next.recipe?.steps.map(step => [step.id, { ...step.limits }]) ?? []);
    edited.value = {};
    review.value = { terminal_handling: '', timing_evidence: '' };
    ready.value = false;
  }
  if (next.review_cursor !== reviewCursor.value || JSON.stringify(next.approval) !== JSON.stringify(approval.value)) {
    ready.value = false;
    confirmed.value = false;
  }
  approval.value = next.approval;
  reviewCursor.value = next.review_cursor;
  if (run.value?.id !== next.run?.id || JSON.stringify(check.value) !== JSON.stringify(next.run?.operator_check)) {
    checkedBasket.value = '';
    location.value = '';
    confirmed.value = false;
    step.value = next.run?.operator_check?.step ?? null;
  }
  technician.value = next.technician;
  baskets.value = next.baskets ?? [];
  block.value = next.block;
  run.value = next.run;
  if (resolvesStart && !starting.value) startUnresolved.value = false;
}

async function selectRecord(id) {
  selectedId.value = id;
  historical.value = null;
  if (!id) return;
  try {
    const response = await request('/api/programme?run_id=' + encodeURIComponent(id));
    if (alive && selectedId.value === id) {
      if (response.run?.id !== id) throw new Error('The selected basket record is unavailable.');
      historical.value = response.run;
    }
  } catch (failure) {
    if (alive && selectedId.value === id) error.value = failure.message;
  }
}

async function refreshRecord() {
  if (selectedId.value) return selectRecord(selectedId.value);
  try {
    await request('/api/programme?run_id=' + encodeURIComponent(run.value.id));
    await refresh();
  } catch (failure) {
    if (alive) error.value = failure.message;
  }
}

function change(step, field, raw) {
  const value = raw === '' ? null : Number(raw);
  limits.value[step][field] = value;
  edited.value[step] = { ...edited.value[step], [field]: value };
  ready.value = false;
}

async function act(action, extra = {}) {
  if (busy.value && action !== 'stop') return;
  const stop = action === 'stop';
  if (!stop) busy.value = true;
  if (action === 'start') {
    starting.value = true;
    startUnresolved.value = true;
  }
  error.value = '';
  try {
    const requestReview = stop ? {} : { request_id: crypto.randomUUID(), review_cursor: reviewCursor.value };
    await request('/api/programme', { action, ...extra, ...requestReview });
    if (action === 'start') starting.value = false;
    await refresh();
    emit('changed');
  } catch (failure) {
    error.value = failure.message || 'Programme service unavailable.';
  } finally {
    if (action === 'start') starting.value = false;
    if (!stop) busy.value = false;
  }
}

function save() {
  return act('save', { revision: recipe.value.id, limits: edited.value });
}

onMounted(() => {
  const poll = async () => {
    try { if (!busy.value || starting.value) await refresh(); } catch (failure) { error.value = failure.message; }
    if (alive && (recipe.value || error.value)) timer = setTimeout(poll, 600);
  };
  void poll();
});

onUnmounted(() => {
  alive = false;
  clearTimeout(timer);
});
</script>

<template>
  <section data-ui="programme.panel" v-if="recipe" class="panel programme" aria-labelledby="programme-heading">
    <h2 id="programme-heading">Calibration module</h2>
    <p><strong>Simulation</strong> · {{ recipe.state }} · Revision {{ recipe.id }}</p>
    <div class="programme-table">
      <table>
        <thead><tr><th scope="col">Step and tank</th><th scope="col">Setpoint (s)</th>
          <th scope="col">Min dwell (s)</th><th scope="col">Max overstay (s)</th><th scope="col">Max air time (s)</th></tr></thead>
        <tbody>
          <tr data-ui="programme.step.row" :data-ui-key="step.id" v-for="step in recipe.steps" :key="step.id">
            <th scope="row">{{ step.id }} · {{ step.role }} · {{ step.tank }}</th>
            <td>{{ step.setpoint }}</td>
            <td v-for="[field, name] in fields" :key="field">
              <template v-if="field === 'air' && step.terminal">{{ recipe.review ? 'Reviewed terminal transfer' : 'Review terminal handling below' }}</template>
              <template v-else>
                <input data-ui="programme.limit.input" :data-ui-key="step.id + '.' + field" :aria-label="step.id + ' ' + name" type="number" step="any"
                  :min="field === 'overstay' ? 0 : .000001" :max="field === 'min' ? step.setpoint : undefined"
                  :value="limits[step.id][field] ?? ''" :placeholder="limits[step.id][field] == null ? 'Needs confirmation' : undefined"
                  @input="change(step.id, field, $event.target.value)">
                <small v-if="field === 'overstay' && step.role === 'anodize'">After PLC done</small>
                <small v-if="field === 'air' && step.air_source">From {{ step.air_source }}</small>
              </template>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <dl v-if="recipe.review">
      <dt>Reviewed terminal handling</dt><dd>{{ recipe.review.terminal_handling }}</dd>
      <dt>Reviewed PLC cycle and timing evidence</dt><dd>{{ recipe.review.timing_evidence }}</dd>
    </dl>
    <div class="programme-actions">
      <button data-ui="programme.draft.save" type="button" data-action="save" :disabled="busy || !hasEdits" @click="save">Save draft</button>
      <template v-if="recipe.state === 'DRAFT'">
        <label>Reviewed terminal handling <input data-ui="programme.revision.terminal" v-model="review.terminal_handling" maxlength="500"></label>
        <label>Reviewed PLC cycle and timing evidence <input data-ui="programme.revision.timing" v-model="review.timing_evidence" maxlength="500"></label>
        <button data-ui="programme.revision.finalize" type="button" data-action="finalize"
          :disabled="busy || hasEdits || !reviewComplete"
          @click="act('finalize', { revision: recipe.id, review })">Finalize reviewed limits</button>
      </template>
    </div>
    <p v-if="block && !check && !technician" role="status">{{ block }}</p>
    <section v-if="technician && !check" data-ui="programme.check.technician" aria-label="Operator check">
      <h3>Operator check</h3>
      <p role="alert">{{ technician }}</p>
    </section>
    <div v-if="!active && !technician" class="programme-actions">
      <label>Basket ID <input data-ui="programme.basket.id" v-model="basket" aria-label="Basket ID" maxlength="80"></label>
      <label><input data-ui="programme.basket.ready" v-model="ready" type="checkbox" :disabled="!approval"> Review the effective revision and confirm basket at input</label>
      <button data-ui="programme.run.start" type="button" data-action="start" :disabled="busy || !approval || !ready || !basket.trim() || hasEdits"
        @click="act('start', { revision: recipe.id, basket: basket.trim(), approval }); ready = false">Run Calibration simulation</button>
    </div>
    <div v-if="active || startUnresolved" class="programme-actions">
      <button data-ui="programme.run.stop" type="button" data-action="stop" @click="act('stop')">Stop programme</button>
    </div>
    <div v-if="run" aria-live="polite">
      <p><strong>{{ run.basket }}</strong> · {{ run.state }} · {{ run.cursor }}/{{ run.recipe.steps.length }} steps</p>
      <p v-if="run.inspection" role="alert">Basket flagged for inspection.</p>
      <p v-if="run.stop_pending" role="alert">Stop unconfirmed. Further dispatch is blocked.</p>
      <p v-if="run.reason && !check">{{ run.reason }}</p>
      <p v-if="run.final_hold && !check">Last confirmed hold {{ run.final_hold }}</p>
      <p v-if="run.checkpoint?.intent">
        Last recorded intent {{ run.checkpoint.intent.effect }}
        {{ run.checkpoint.intent.value?.tank ?? run.checkpoint.intent.value?.basket ?? run.checkpoint.intent.value }}
      </p>
      <p v-if="run.checkpoint?.confirmed?.length">
        Last recorded effect {{ run.checkpoint.confirmed.at(-1).effect }}
        {{ run.checkpoint.confirmed.at(-1).value?.tank ?? run.checkpoint.confirmed.at(-1).value?.basket ?? run.checkpoint.confirmed.at(-1).value }}
      </p>
      <section v-if="check" data-ui="programme.check.panel" aria-label="Operator check">
        <h3>Operator check</h3>
        <p role="alert">{{ check.reason }}<template v-if="check.technician && !check.reason?.toLowerCase().includes('call a technician')"> Please call a technician.</template></p>
        <dl>
          <dt>Last confirmed location</dt><dd>{{ check.location || 'Unknown' }}</dd>
          <dt>Observed basket</dt><dd>{{ check.basket || 'Unknown' }}</dd>
          <template v-if="check.observed_location"><dt>Observed location</dt><dd>{{ check.observed_location }}</dd></template>
          <dt>Job ID</dt><dd>{{ check.job_id || 'Unknown' }}</dd>
          <dt>Step</dt><dd>{{ check.step }}</dd>
        </dl>
        <p v-if="block && !check.technician">{{ block }}</p>
        <div v-if="!check.technician && !technician" class="programme-actions">
          <label>Operator <input data-ui="programme.check.operator" v-model="operator" maxlength="80" @input="confirmed = false"></label>
          <label>Confirmed basket ID <input data-ui="programme.check.basket" v-model="checkedBasket" maxlength="80" @input="confirmed = false"></label>
          <label>Confirmed location <input data-ui="programme.check.location" v-model="location" maxlength="80" @input="confirmed = false"></label>
          <label>Resume from step N
            <input data-ui="programme.check.step" v-model.number="step" type="number" min="1" :max="run.recipe.steps.length" step="1">
          </label>
          <label><input data-ui="programme.check.confirmed" type="checkbox" v-model="confirmed"> I physically checked the basket ID and location</label>
          <button data-ui="programme.run.resume" type="button" data-action="resume"
            :disabled="!decisionReady || !Number.isInteger(step) || step < 1 || step > run.recipe.steps.length"
            @click="decide('resume')">Resume from step {{ step || 'N' }}</button>
          <button data-ui="programme.run.abort" type="button" data-action="abort" :disabled="!decisionReady"
            @click="decide('abort')">Abort basket</button>
        </div>
      </section>
      <label v-if="baskets.length > 1">Basket record
        <select data-ui="programme.record.select" :value="selectedId" aria-label="Basket record" @change="selectRecord($event.target.value)">
          <option value="">Current · {{ run.basket }} · {{ run.id }}</option>
          <option v-for="item in baskets.filter(item => item.id !== run.id)" :key="item.id" :value="item.id">
            {{ item.basket }} · {{ item.id }} · {{ item.state }}
          </option>
        </select>
      </label>
      <details data-ui="programme.record.panel" v-if="record && (selectedId || record.steps.length || record.operator_choices?.length)" data-action="basket-record">
        <summary data-ui="programme.record.summary">Basket record · {{ record.basket }} · revision {{ record.recipe.id }}</summary>
        <p v-if="selectedId">{{ record.state }} · {{ record.cursor }}/{{ record.recipe.steps.length }} steps
          <template v-if="record.reason"> · {{ record.reason }}</template>
          <template v-if="record.inspection"> · Inspect</template>
        </p>
        <p v-if="selectedId && record.final_hold">Last confirmed hold {{ record.final_hold }}</p>
        <ul v-if="record.operator_choices?.length">
          <li data-ui="programme.record.choice" :data-ui-key="String(index)" v-for="(choice, index) in record.operator_choices" :key="index">
            {{ choice.choice === 'resume' ? 'Resume from step ' + choice.step : 'Abort basket' }} · {{ choice.operator }}
            · Confirmed {{ choice.basket }} at {{ choice.location }} · {{ choice.applied ? 'Recorded' : 'Unconfirmed' }}
            <p v-if="choice.evidence_note">{{ choice.evidence_note }}</p>
          </li>
        </ul>
        <button v-if="record.operator_choices?.some(choice => choice.evidence_note)" type="button"
          data-ui="programme.record.refresh" @click="refreshRecord">Refresh retained job evidence</button>
        <ul>
          <li data-ui="programme.record.step" :data-ui-key="step.id" v-for="step in record.steps" :key="(step.job_id || '') + step.id">
            {{ step.id }} · {{ step.tank }} · dwell {{ step.dwell_s }} s · air {{ step.air_s ?? 'unknown' }}
            <template v-if="step.air_s != null"> s</template> · PLC cycle {{ step.plc.cycle }}
            <template v-if="step.open_air_alarm_s != null"> · air alarm at {{ step.open_air_alarm_s }} s</template>
            <strong v-if="step.inspection"> · Inspect</strong>
          </li>
        </ul>
      </details>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.programme-table { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; text-align: left; }
th, td { padding: .4rem; vertical-align: top; }
input[type="number"] { width: 10rem; }
td small { display: block; }
.programme-actions { display: flex; flex-wrap: wrap; align-items: center; gap: .65rem; margin: .75rem 0; }
button, input { font: inherit; color: inherit; background: var(--p-content-background); border: 1px solid var(--p-content-border-color); padding: .25rem .5rem; border-radius: .25rem; }
button:disabled { opacity: .65; }
</style>
