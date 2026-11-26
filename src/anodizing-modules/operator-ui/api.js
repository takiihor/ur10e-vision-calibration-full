function moduleToken() {
  return document.querySelector('meta[name="module-token"]')?.content;
}

export function operatorKeyRequired() {
  return !moduleToken();
}

async function fetchLocal(path, body, timeout, cache, keepalive) {
  const token = moduleToken();
  const options = {
    headers: token ? { 'X-Module-Token': token } : {},
    cache,
    keepalive,
    signal: AbortSignal.timeout(timeout),
  };
  if (body) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  return fetch(path, options);
}

export async function request(path, body, timeout = 8000, keepalive = false) {
  const response = await fetchLocal(path, body, timeout, 'no-store', keepalive);
  const value = await response.json();
  if (!response.ok) throw Object.assign(new Error(value.error), { status: response.status });
  return value;
}

export function simulationEnabled() {
  return document.querySelector('meta[name="module-simulation"]')?.content !== 'off';
}

export async function requestBytes(path, cache = 'no-cache') {
  const response = await fetchLocal(path, null, 8000, cache);
  if (!response.ok) throw new Error('Display asset unavailable.');
  return response.arrayBuffer();
}

export function requestError(error, connected) {
  if (error.status === 403 && operatorKeyRequired()) return error.message;
  if (error.status === 403) return 'Page access expired. Reload the page and check the AGV before sending again.';
  if (!error.status) return connected
    ? 'The page lost contact with its service. Use the AGV’s e-stop if needed. Reload and check the link.'
    : 'Page service unavailable. Reload the page and select Connect.';
  const message = error.message;
  if (message?.startsWith('AGV link lost. ')) return message;
  if (/^(Serial permission denied\.|Serial port is busy or missing\.|A robot job is running|Choose a learned station|Tick Armed|The AGV connection changed|Stop, Pause or disarming cancelled|The recipe is checking|Link lost\.|Could not find a gap|Robot state is uncertain|Job storage failed|AGV Stop or Pause is awaiting)/.test(message)) {
    return 'Refused · ' + message;
  }
  return 'Refused. Check the connection and settings, then query status.';
}

export async function requestDownload(path) {
  const response = await fetchLocal(path, null, 60000, 'no-store');
  if (!response.ok) throw Object.assign(new Error('Download unavailable.'), { status: response.status });
  return response.blob();
}

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  Object.assign(document.createElement('a'), { href: url, download: filename }).click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
