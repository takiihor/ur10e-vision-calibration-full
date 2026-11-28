(() => {
  const storageKey = 'anodizing.operator.enrollment';
  window.operatorFetch = (path, options = {}) => {
    let enrollment;
    try { enrollment = sessionStorage.getItem(storageKey); } catch {}
    if (enrollment && new URL(path, location.href).origin === location.origin) {
      const headers = new Headers(options.headers);
      headers.set('X-Operator-Enrollment', enrollment);
      return fetch(path, { ...options, headers, redirect: 'error' });
    }
    return fetch(path, options);
  };

  const form = document.getElementById('operator-enrollment');
  form?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const status = document.getElementById('enrollment-status');
    const body = new URLSearchParams(new FormData(form));
    form.reset();
    try {
      const response = await fetch('/operator-key', { method: 'POST', body, redirect: 'error' });
      const result = await response.json();
      if (!response.ok) {
        status.textContent = result.error;
        return;
      }
      sessionStorage.setItem(storageKey, result.enrollment);
      location.replace('/dev/');
    } catch {
      status.textContent = 'Enrollment unavailable. Check the browser and try again.';
    }
  });
})();
