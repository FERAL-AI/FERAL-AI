import { useEffect, useMemo } from 'react';
import { FeralSocket, wireSocketGlobalErrors } from '../lib/feralSocket';

/**
 * Single shared FeralSocket instance for the whole v2 app. Components call
 * `socket.subscribe(fn)` to receive every message; Ambient, LiveOps, Chat,
 * Forge all share the same connection.
 *
 * The `sendUiEvent` helper bound below is the contract SDUI trees use
 * to fire a user action back at the brain. v1 used to hard-code
 * `screen_id: 'main'` and drop `value`; v2 fixes both and also threads
 * an optional `app_id` so the brain's ui_handlers can scope events to
 * third-party apps.
 */
let sharedSocket = null;

function getShared() {
  if (!sharedSocket) {
    sharedSocket = new FeralSocket();
    wireSocketGlobalErrors(sharedSocket);
    sharedSocket.connect();
  }
  return sharedSocket;
}

/**
 * Test-only seam — returns the underlying singleton so vitest can
 * inject a simulated WS message via `_getSharedSocketForTesting()
 * ._dispatch({ type: 'state_push', event: ..., data: ... })`. Not
 * referenced from production code paths; the export exists solely
 * so the WS-driven Home / Devices subdevice update tests can fire
 * deltas without spinning up a real WebSocket on a CI host.
 */
export function _getSharedSocketForTesting() {
  return sharedSocket;
}

export function _resetSharedSocketForTesting() {
  if (sharedSocket && sharedSocket.ws && typeof sharedSocket.ws.close === 'function') {
    try { sharedSocket.ws.close(); } catch { /* ignore */ }
  }
  sharedSocket = null;
}

/**
 * Build a typed ui_event envelope and push it over the shared socket.
 *
 * @param {FeralSocket} socket
 * @param {object} args
 *   - screen_id: string (required; pass the mounted surface id)
 *   - action_id: string (required)
 *   - event:     "tap" | "toggle" | "slider" | "text_input" | "dismiss"
 *   - value:     JSON-serialisable payload (forms use { values: {...} })
 *   - app_id:    optional third-party app id, routed through AppRegistry
 *   - session_id: optional, defaults to whatever the socket was opened with
 */
export function sendUiEvent(socket, { screen_id, action_id, event = 'tap', value, app_id, session_id } = {}) {
  if (!socket || !action_id) return false;
  const payload = { screen_id: screen_id || 'main', action_id, event };
  if (value !== undefined) payload.value = value;
  if (app_id) payload.app_id = app_id;
  return socket.send({
    hop: 'client',
    type: 'ui_event',
    session_id: session_id || '',
    payload,
  });
}

export function useFeralSocket() {
  const socket = useMemo(() => getShared(), []);
  useEffect(() => {
    wireSocketGlobalErrors(socket);
    socket.connect();
  }, [socket]);
  return socket;
}
