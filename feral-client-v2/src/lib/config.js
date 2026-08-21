/**
 * Brain API/WS endpoints for feral-client-v2.
 *
 * Originally a copy of feral-client/src/config.js, kept byte-identical so v1
 * and v2 resolved the same Brain when served from the same origin. v1 was
 * deleted in 2026.8.12, so this is now the only copy and nothing constrains
 * it to match a sibling.
 */

const rawBase = (import.meta.env.VITE_BRAIN_BASE_URL || '').trim();

let apiBase = rawBase.replace(/\/$/, '');
if (!apiBase) {
  const host =
    import.meta.env.VITE_BRAIN_HOST ||
    (typeof window !== 'undefined' && window.location.hostname) ||
    'localhost';
  const port =
    import.meta.env.VITE_BRAIN_PORT ||
    (typeof window !== 'undefined' && window.location.port) ||
    '9090';
  const scheme =
    typeof window !== 'undefined' && window.location.protocol === 'https:'
      ? 'https'
      : 'http';
  const origin = `${host}${port ? `:${port}` : ''}`;
  apiBase = `${scheme}://${origin}`;
}

const wsBase = apiBase.startsWith('https://')
  ? apiBase.replace(/^https:\/\//, 'wss://')
  : apiBase.replace(/^http:\/\//, 'ws://');

export const API_BASE = apiBase;
export const WS_BASE = wsBase;
export const WS_URL = `${wsBase}/v1/session`;
