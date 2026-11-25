<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { request } from './api';

const emit = defineEmits(['changed']);
const recipe = ref(null);
const run = ref(null);
const limits = ref({});
const edited = ref({});
const review = ref({ terminal_handling: '', timing_evidence: '' });
const hasEdits = computed(() => Object.keys(edited.value).length > 0);
const reviewComplete = computed(() => Object.values(review.value).every(value => value.trim()));
const basket = ref('');
const disposition = ref('');
const ready = ref(false);
const busy = ref(false);
const error = ref('');
let timer;
let alive = true;
const fields = [
  ['min', 'minimum dwell'],
  ['overstay', 'maximum overstay'],
  ['air', 'maximum air time'],
];
const active = computed(() => run.value && (
  run.value.pending || run.value.ack_pending || run.value.stop_pending
  || ['running', 'held', 'recovery required', 'inspection'].includes(run.value.state)
));

async function refresh() {
  const next = await request('/api/programme');
  if (!alive) return;
  if (next.recipe?.id !== recipe.value?.id || next.recipe?.state !== recipe.value?.state) {
    recipe.value = next.recipe;
    limits.value = Object.fromEntries(next.recipe?.steps.map(step => [step.id, { ...step.limits }]) ?? []);
    edited.value = {};
    review.value = { terminal_handling: '', timing_evidence: '' };
    ready.value = false;
  }
  run.value = next.run;
}

function change(step, field, raw) {
  const value = raw === '' ? null : Number(raw);
  limits.value[step][field] = value;
  edited.value[step] = { ...edited.value[step], [field]: value };
  ready.value = false;
}

async function act(action, extra = {}) {
  if (busy.value && action !== 'stop') return;
  busy.value = true;
  error.value = '';
  try {
    await request('/api/programme', { action, ...extra });
    await refresh();
    emit('changed');
  } catch (failure) {
    error.value = failure.message || 'Programme service unavailable.';
  } finally {
    busy.value = false;
  }
}

function save() {
  return act('save', { revision: recipe.value.id, limits: edited.value });
}

onMounted(() => {
  const poll = async () => {
    try { if (!busy.value) await refresh(); } catch (failure) { error.value = failure.message; }
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
    <h2 id="programme-heading">Basket programme</h2>
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
    <div v-if="!active" class="programme-actions">
      <label>Basket ID <input data-ui="programme.basket.id" v-model="basket" aria-label="Basket ID" maxlength="80"></label>
      <label><input data-ui="programme.basket.ready" v-model="ready" type="checkbox"> Review the effective revision and confirm basket at input</label>
      <button data-ui="programme.run.start" type="button" data-action="start" :disabled="busy || !ready || !basket.trim() || hasEdits"
        @click="act('start', { revision: recipe.id, basket: basket.trim() }); ready = false">Run simulated programme</button>
    </div>
    <div v-if="run" aria-live="polite">
      <p><strong>{{ run.basket }}</strong> · {{ run.state }} · {{ run.cursor }}/{{ run.recipe.steps.length }} steps</p>
      <p v-if="run.stop_pending" role="alert">Stop unconfirmed. Further dispatch is blocked.</p>
      <p v-if="run.reason">{{ run.reason }}</p>
      <p v-if="run.final_hold">Last confirmed hold {{ run.final_hold }}</p>
      <p v-if="run.checkpoint?.intent">
        Last recorded intent {{ run.checkpoint.intent.effect }}
        {{ run.checkpoint.intent.value?.tank ?? run.checkpoint.intent.value }}
      </p>
      <p v-if="run.checkpoint?.confirmed?.length">
        Last recorded effect {{ run.checkpoint.confirmed.at(-1).effect }}
        {{ run.checkpoint.confirmed.at(-1).value?.tank ?? run.checkpoint.confirmed.at(-1).value }}
      </p>
      <div class="programme-actions">
        <button data-ui="programme.run.stop" v-if="active" type="button"
          data-action="stop" @click="act('stop')">Stop programme</button>
        <button data-ui="programme.run.resume" v-if="run.state === 'held'" type="button"
          :disabled="busy || run.stop_pending || Boolean(run.pending && (!run.pending_missing || run.pending_attempted !== false))"
          @click="act('resume', { job_id: run.id })">Resume reviewed simulation</button>
        <template v-if="run.ack_missing || ['inspection', 'recovery required'].includes(run.state)">
          <label>Basket disposition <input data-ui="programme.inspection.note" v-model="disposition" maxlength="500"></label>
          <button data-ui="programme.inspection.record" type="button" :disabled="busy || !disposition.trim() || Boolean(run.pending && !run.pending_missing)"
            @click="act('disposition', { job_id: run.id, note: disposition })">Record disposition</button>
        </template>
      </div>
      <details data-ui="programme.record.panel" v-if="run.steps.length" data-action="basket-record">
        <summary data-ui="programme.record.summary">Basket record · revision {{ run.recipe.id }}</summary>
        <ul>
          <li data-ui="programme.record.step" :data-ui-key="step.id" v-for="step in run.steps" :key="step.id">
            {{ step.id }} · {{ step.tank }} · dwell {{ step.dwell_s }} s · air {{ step.air_s ?? 'unknown' }}
            <template v-if="step.air_s != null"> s</template> · PLC cycle {{ step.plc.cycle }}
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
