// @vitest-environment jsdom
import { expect, test, vi } from 'vitest';
import { request } from './api.js';

test('sends operator enrollment only to this origin and refuses redirects', async () => {
  const fetch = vi.fn(async () => ({ ok: true, json: () => ({}) }));
  vi.stubGlobal('fetch', fetch);
  sessionStorage.setItem('anodizing.operator.enrollment', 'fake-enrollment');
  try {
    await request('/api/operator-auth');
    const sameOrigin = fetch.mock.calls[0][1];
    expect(new Headers(sameOrigin.headers).get('X-Operator-Enrollment')).toBe('fake-enrollment');
    expect(sameOrigin.redirect).toBe('error');

    const otherPort = new URL(location.href);
    otherPort.port = location.port === '12345' ? '12346' : '12345';
    await request(otherPort.href);
    expect(new Headers(fetch.mock.calls[1][1].headers).has('X-Operator-Enrollment')).toBe(false);
  } finally {
    sessionStorage.clear();
    vi.unstubAllGlobals();
  }
});
