/**
 * WebSocket helper for v2. Wraps the browser WebSocket with reconnect logic
 * and a topic-based subscribe() API so pages don't have to juggle onmessage.
 */
import { WS_URL } from './config';

const DEFAULT_RECONNECT_MS = 2000;

export class FeralSocket {
  constructor(url = WS_URL, { reconnectMs = DEFAULT_RECONNECT_MS } = {}) {
    this.url = url;
    this.reconnectMs = reconnectMs;
    this.ws = null;
    this.listeners = new Set();
    this.stateListeners = new Set();
    this.state = 'closed';
    this.stopped = false;
  }

  connect() {
    if (this.ws || this.stopped) return;
    try {
      this.ws = new WebSocket(this.url);
    } catch (err) {
      this._transition('error');
      this._scheduleReconnect();
      return;
    }
    this._transition('connecting');

    this.ws.onopen = () => this._transition('open');
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
    setTimeout(() => this.connect(), this.reconnectMs);
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
