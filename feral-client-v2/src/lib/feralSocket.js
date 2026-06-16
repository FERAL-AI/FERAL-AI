/**
 * Error-aware WebSocket client entry for v2.
 * Core transport is FeralSocket in ws.js; this module wires global error surfacing.
 */
import { FeralSocket as BaseFeralSocket } from './ws';
import { pushGlobalError } from '../hooks/useGlobalErrors';
import { ApiError } from './api';

export { BaseFeralSocket as FeralSocket };

const wired = new WeakSet();

/**
 * Subscribe to WS error frames and unexpected disconnects; push to global store.
 */
export function wireSocketGlobalErrors(socket) {
  if (!socket || wired.has(socket)) return;
  wired.add(socket);

  socket.subscribe((msg) => {
    if (!msg || typeof msg !== 'object') return;
    if (msg.type === 'error') {
      pushGlobalError(ApiError.fromWebSocket(msg));
    }
  });

  let wasOpen = false;
  socket.onState((state) => {
    if (state === 'open') {
      wasOpen = true;
      return;
    }
    if (wasOpen && !socket.stopped && !socket._intentionalReconnect && (state === 'closed' || state === 'error')) {
      pushGlobalError(new ApiError({
        status: 0,
        code: 'ws_disconnect',
        reason: 'connection_lost',
        detail: 'Lost connection to the brain. Reconnecting…',
        raw: { state },
        path: 'websocket',
      }));
      wasOpen = false;
    }
  });
}
