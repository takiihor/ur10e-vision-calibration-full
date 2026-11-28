import { ref, watch } from 'vue';

export function usePageTab(page, values, fallback = values[0]) {
  const key = 'operator-tab.' + page;
  const active = ref(fallback);
  try {
    const saved = localStorage.getItem(key);
    if (values.includes(saved)) active.value = saved;
  } catch {
    // Storage may be disabled for this viewer.
  }
  watch(active, value => {
    try { localStorage.setItem(key, value); }
    catch { /* The tab still works for this visit. */ }
  });
  return active;
}
