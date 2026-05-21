/**
 * Thin fetch wrapper. All v2 REST calls go through apiFetch so we have a
 * single chokepoint to add auth headers, handle 401s, and surface errors.
 */
import { API_BASE } from './config';
import { pushGlobalError } from '../hooks/useGlobalErrors';

function readApiKey() {
  try {
    if (typeof localStorage === 'undefined') return null;
    return localStorage.getItem('feral_api_key');
  } catch {
    return null;
  }
}

function extractFields(body, status, path) {
  const raw = body && typeof body === 'object' ? body : {};
  let detail = '';
  let reason = '';
  let code = '';

  if (typeof raw.error === 'string' && raw.error.trim()) {
    detail = raw.error.trim();
  } else if (raw.error && typeof raw.error === 'object') {
    code = raw.error.code || code;
    reason = raw.error.reason || reason;
    detail = raw.error.detail || raw.error.message || detail;
  }

  if (!detail && typeof raw.detail === 'string' && raw.detail.trim()) {
    detail = raw.detail.trim();
  } else if (!detail && raw.detail && typeof raw.detail === 'object') {
    detail = typeof raw.detail.message === 'string'
      ? raw.detail.message
      : JSON.stringify(raw.detail);
  }

  if (!detail && typeof raw.message === 'string' && raw.message.trim()) {
    detail = raw.message.trim();
  }

  if (!reason && typeof raw.reason === 'string') reason = raw.reason;
  if (!code && typeof raw.code === 'string') code = raw.code;

  if (!detail && status) {
    detail = status >= 400 ? `Request failed (${status})` : 'Request failed';
  }
  if (!detail) detail = 'Request failed';

  return { status, code, reason, detail, raw, path };
}

export class ApiError extends Error {
  constructor({ status = 0, code = '', reason = '', detail = '', raw = null, path = '' } = {}) {
    const message = detail || reason || code || 'Request failed';
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.reason = reason;
    this.detail = detail;
    this.raw = raw;
    this.path = path;
  }

  static fromResponse(response, body, path) {
    const fields = extractFields(body, response?.status ?? 0, path);
    return new ApiError(fields);
  }

  static fromWebSocket(msg) {
    const data = msg?.data || msg?.payload || msg;
    const detail = typeof data === 'string'
      ? data
      : (data?.detail || data?.error || data?.message || msg?.error || 'WebSocket error');
    return new ApiError({
      status: 0,
      code: msg?.code || data?.code || 'ws_error',
      reason: msg?.reason || data?.reason || '',
      detail: typeof detail === 'string' ? detail : JSON.stringify(detail),
      raw: msg,
      path: 'websocket',
    });
  }
}

function bodyHasError(body) {
  if (!body || typeof body !== 'object') return false;
  if (typeof body.error === 'string' && body.error.trim()) return true;
  if (body.error && typeof body.error === 'object') return true;
  return false;
}

async function readJsonBody(response) {
  const text = await response.text();
  if (!text || !text.trim()) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { _raw: text };
  }
}

function surfaceError(err, silent) {
  if (!silent) pushGlobalError(err);
  throw err;
}

export async function apiFetch(path, init = {}) {
  const silent = init.silent === true;
  const { silent: _silent, ...fetchInit } = init;
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`;
  const headers = new Headers(fetchInit.headers || {});
  const key = readApiKey();
  if (key && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${key}`);
  }
  if (fetchInit.body && !headers.has('Content-Type') && typeof fetchInit.body === 'string') {
    headers.set('Content-Type', 'application/json');
  }

  let response;
  try {
    response = await fetch(url, { ...fetchInit, headers, credentials: 'same-origin' });
  } catch (networkErr) {
    const err = new ApiError({
      status: 0,
      code: 'network',
      reason: 'fetch_failed',
      detail: networkErr?.message || 'Network request failed',
      raw: networkErr,
      path,
    });
    return surfaceError(err, silent);
  }

  if (!response.ok) {
    const body = await readJsonBody(response);
    const err = ApiError.fromResponse(response, body, path);
    return surfaceError(err, silent);
  }

  if (typeof response.clone === 'function') {
    const body = await readJsonBody(response.clone()).catch(() => null);
    if (body && bodyHasError(body)) {
      const err = ApiError.fromResponse(response, body, path);
      return surfaceError(err, silent);
    }
  }

  return response;
}

export async function apiJson(path, init = {}) {
  const r = await apiFetch(path, init);
  return r.json();
}
