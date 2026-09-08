/**
 * Brain API/WS endpoints for feral-client-v2.
 *
 * Originally a copy of feral-client/src/config.js, kept byte-identical so v1
 * and v2 resolved the same Brain when served from the same origin. v1 was
 * deleted in 2026.8.12, so this is now the only copy and nothing constrains
 * it to match a sibling.
 */

export function resolveBrainEndpoints({
  baseUrl = '',
  host = '',
  port = '',
  location,
} = {}) {
  let apiBase = String(baseUrl || '').trim().replace(/\/$/, '');

  const hasHttpLocation =
    (location?.protocol === 'http:' || location?.protocol === 'https:') &&
    location?.hostname;

  if (!apiBase && !host && !port && hasHttpLocation) {
    const scheme = location.protocol === 'https:' ? 'https' : 'http';
    const origin =
      location.origin && location.origin !== 'null'
        ? location.origin
        : `${scheme}://${location.hostname}${location.port ? `:${location.port}` : ''}`;
    apiBase = origin.replace(/\/$/, '');
  }

  if (!apiBase) {
    const resolvedHost = host || location?.hostname || 'localhost';
    const resolvedPort = port || location?.port || '9090';
    const scheme = location?.protocol === 'https:' ? 'https' : 'http';
    const origin = `${resolvedHost}${resolvedPort ? `:${resolvedPort}` : ''}`;
    apiBase = `${scheme}://${origin}`;
  }

  const wsBase = apiBase.startsWith('https://')
    ? apiBase.replace(/^https:\/\//, 'wss://')
    : apiBase.replace(/^http:\/\//, 'ws://');

  return {
    API_BASE: apiBase,
    WS_BASE: wsBase,
    WS_URL: `${wsBase}/v1/session`,
  };
}

const endpoints = resolveBrainEndpoints({
  baseUrl: import.meta.env.VITE_BRAIN_BASE_URL,
  host: import.meta.env.VITE_BRAIN_HOST,
  port: import.meta.env.VITE_BRAIN_PORT,
  location: typeof window !== 'undefined' ? window.location : undefined,
});

export const { API_BASE, WS_BASE, WS_URL } = endpoints;
