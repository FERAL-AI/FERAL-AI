/**
 * WebSocket helper for v2. Wraps the browser WebSocket with reconnect logic
 * and a topic-based subscribe() API so pages don't have to juggle onmessage.
 */
import { WS_URL } from './config';

const DEFAULT_RECONNECT_MS = 2000;

export class FeralSocket {
  constructor(url = WS_URL, { reconnectMs = DEFAULT_RECONNECT_MS } = {}) {
    this.baseUrl = url;
    this.url = url;
    this.reconnectMs = reconnectMs;
    this.ws = null;
    this.listeners = new Set();
    this.stateListeners = new Set();
    this.state = 'closed';
    this.stopped = false;
    // The orchestrator session token this socket is bound to. '' means
    // the default (primary) session — the brain picks primary_session_id.
    // A non-empty token is appended as ?session_id= so chat turns route
    // to the right thread (and stream back over THIS socket).
    this.sessionToken = '';
    // Set true while we are intentionally tearing the socket down to
    // rebind it to a different session, so the global error wiring can
    // skip the spurious "connection lost" toast on a thread switch.
    this._intentionalReconnect = false;
  }

  _buildUrl() {
    if (!this.sessionToken) return this.baseUrl;
    const sep = this.baseUrl.includes('?') ? '&' : '?';
    return `${this.baseUrl}${sep}session_id=${encodeURIComponent(this.sessionToken)}`;
  }

  /**
   * Rebind the socket to a different orchestrator session (chat thread).
   * Pass '' for the primary/default thread. No-op when already bound to
   * the requested session, so navigating within one thread never churns
   * the connection. Triggers one intentional reconnect otherwise.
   */
  setSession(sessionToken = '') {
    const next = sessionToken || '';
    if (next === this.sessionToken && this.ws) return;
    this.sessionToken = next;
    this.url = this._buildUrl();
    if (this.stopped) return;
    this._intentionalReconnect = true;
    if (this.ws) {
      try { this.ws.close(); } catch { /* ignore */ }
      // onclose clears this.ws and schedules a reconnect to the new url.
    } else {
      this.connect();
    }
  }

  connect() {
    if (this.ws || this.stopped) return;
    this.url = this._buildUrl();
    try {
      this.ws = new WebSocket(this.url);
    } catch (err) {
      this._transition('error');
      this._scheduleReconnect();
      return;
    }
    this._transition('connecting');

    this.ws.onopen = () => {
      this._intentionalReconnect = false;
      this._transition('open');
    };
    this.ws.onclose = () => {
      this.ws = null;
      this._transition('closed');
      this._scheduleReconnect();
    };
    this.ws.onerror = () => this._transition('error');
    this.ws.onmessage = (ev) => {
      let payload;
      try {
        payload = JSON.parse(ev.data);
      } catch {
        payload = { type: 'raw', data: ev.data };
      }
      this.listeners.forEach((fn) => {
        try { fn(payload); } catch {}
      });
    };
  }

  _scheduleReconnect() {
    if (this.stopped) return;
    // An intentional session rebind should reconnect promptly so a
    // thread switch feels instant; an unexpected drop backs off.
    const delay = this._intentionalReconnect ? 0 : this.reconnectMs;
    setTimeout(() => this.connect(), delay);
  }

  _transition(state) {
    this.state = state;
    this.stateListeners.forEach((fn) => {
      try { fn(state); } catch {}
    });
  }

  send(obj) {
    if (!this.ws || this.ws.readyState !== 1) return false;
    try {
      this.ws.send(typeof obj === 'string' ? obj : JSON.stringify(obj));
      return true;
    } catch {
      return false;
    }
  }

  // Same write path as ``send()`` but returns a tagged result so the
  // caller can distinguish "socket not open yet" from "serialize blew
  // up" and surface a targeted error to the user. ``send()`` is kept
  // for back-compat — most call-sites only need the boolean. Added by
  // RC polish bundle for Chat composer send-failure UX.
  sendOrFail(obj) {
    if (!this.ws || this.ws.readyState !== 1) {
      return { ok: false, reason: 'ws_not_open' };
    }
    try {
      this.ws.send(typeof obj === 'string' ? obj : JSON.stringify(obj));
      return { ok: true };
    } catch (err) {
      return { ok: false, reason: 'serialize_failed', error: String(err && err.message || err) };
    }
  }

  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  onState(fn) {
    this.stateListeners.add(fn);
    try { fn(this.state); } catch {}
    return () => this.stateListeners.delete(fn);
  }

  close() {
    this.stopped = true;
    if (this.ws) {
      try { this.ws.close(); } catch {}
    }
    this.ws = null;
    this._transition('closed');
  }
}
