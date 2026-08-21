import { useEffect, useState } from 'react';
import { apiJson } from '../lib/api';

/**
 * One poll of the machine's live counters, shared by everything that
 * renders them.
 *
 * The system bar and the dock both need "is anything running" and "is
 * anything waiting on me". Polling per component meant three components
 * hitting /api/jobs, /api/approvals and /api/dashboard on their own
 * timers, so an idle brain served roughly forty requests a minute to
 * paint two numbers that had not changed.
 *
 * A module-level poller with refcounted subscribers instead: the first
 * mount starts it, the last unmount stops it, and every subscriber sees
 * the same snapshot. That also removes a subtler problem, which is that
 * two independent pollers disagree for a second or two after a change
 * and the UI shows a running job in one place and none in another.
 */

const POLL_MS = 4000;

const EMPTY = {
  running: 0,      // jobs of any kind currently in flight
  shells: 0,       // backgrounded shell commands specifically
  needs: 0,        // tool calls blocked on a decision
  devices: 0,
  tokens: 0,
  cost: 0,
  autonomy: '',
  reachable: true, // false once every source has failed
};

let snapshot = { ...EMPTY };
const listeners = new Set();
let timer = null;

async function poll() {
  const [j, a, d] = await Promise.allSettled([
    apiJson('/api/jobs?limit=60'),
    apiJson('/api/approvals'),
    apiJson('/api/dashboard'),
  ]);
  const next = { ...snapshot };

  if (j.status === 'fulfilled') {
    const items = Array.isArray(j.value?.items) ? j.value.items : [];
    const live = items.filter((i) => i.status === 'running' || i.status === 'connected');
    next.running = live.length;
    next.shells = live.filter((i) => i.kind === 'background_bash').length;
  }
  if (a.status === 'fulfilled') next.needs = Number(a.value?.count || 0);
  if (d.status === 'fulfilled') {
    const v = d.value || {};
    next.devices = Number(v.online_count ?? v.device_count ?? 0);
    next.tokens = Number(v.memory?.tokens ?? v.tokens_used ?? 0);
    next.cost = Number(v.cost_today ?? v.spend_today ?? 0);
    next.autonomy = String(v.autonomy || v.health?.autonomy_mode || '');
  }
  // Only a total failure means unreachable. One slow endpoint must not
  // make the shell claim the brain is down.
  next.reachable = [j, a, d].some((r) => r.status === 'fulfilled');

  snapshot = next;
  listeners.forEach((fn) => fn(snapshot));
}

function subscribe(fn) {
  listeners.add(fn);
  if (timer === null) {
    poll();
    timer = setInterval(poll, POLL_MS);
  } else {
    fn(snapshot);
  }
  return () => {
    listeners.delete(fn);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

export function useMachineVitals() {
  const [v, setV] = useState(snapshot);
  useEffect(() => subscribe(setV), []);
  return v;
}

/** Test seam: reset the module poller between cases. */
export function __resetVitals() {
  if (timer !== null) clearInterval(timer);
  timer = null;
  listeners.clear();
  snapshot = { ...EMPTY };
}
