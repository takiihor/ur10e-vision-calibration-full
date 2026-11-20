export async function request(path, body, timeout = 8000) {
  const token = document.querySelector('meta[name="module-token"]').content;
  const options = {
    headers: { 'X-Module-Token': token },
    cache: 'no-store',
    signal: AbortSignal.timeout(timeout),
  };
  if (body) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) throw Object.assign(new Error(value.error), { status: response.status });
  return value;
}

export function requestError(error, connected) {
  if (error.status === 403) return 'Page access expired. Reload the page and check the AGV before sending again.';
  if (!error.status) return connected
    ? 'The page lost contact with its service. Use the AGV’s e-stop if needed. Reload and check the link.'
    : 'Page service unavailable. Reload the page and select Connect.';
  const message = error.message;
  if (/^(Serial permission denied\.|Serial port is busy or missing\.|A robot job is running|Choose a learned station|Tick Armed|The AGV connection changed|Stop, Pause or disarming cancelled|The recipe is checking|Link lost\.|Could not find a gap|Robot state is uncertain|Job storage failed|AGV Stop or Pause is awaiting)/.test(message)) {
    return 'Refused · ' + message;
  }
  return 'Refused. Check the connection and settings, then query status.';
}
