/**
 * useSystemHealth — single shared subscriber to /api/dashboard.
 *
 * Replaces the polling storm flagged in AUDIT-r14 finding 03:
 *   Shell useSomatic 10s + Home 15s + GlassBrain 8s + CommandPalette (on-open)
 *   = ~29-30 /api/dashboard GETs per minute steady-state across 3 tabs.
 *
 * Design:
 *   - One module-level store; every component that needs dashboard data
 *     calls useSystemHealth() which subscribes to the store.
 *   - The store ticks every POLL_MS (default 15s — matches the slowest
 *     consumer's interval and trusts Lane 06's 5s server cache on
 *     /api/dashboard for de-dup; the brain caches the heavier somatic +
 *     memory.stats calls behind that route).
 *   - WS `state_push` frames force an immediate refresh so live edges
 *     (device pair, skill reload) propagate without waiting for the
 *     next tick.
 *   - Subscribers receive the full dashboard payload; small helpers
 *     (useSomatic, useDashboard) expose useful sub-slices to avoid
 *     forcing every consumer to remember the JSON shape.
 */
import { useEffect, useSyncExternalStore } from 'react';
import { apiJson } from '../lib/api';

const POLL_MS = 15000;
const STALE_MS = POLL_MS * 2;

let snapshot = {
  data: null,
  loading: true,
  error: null,
  lastFetched: 0,
};
let timer = null;
let refCount = 0;
let inflight = null;
const listeners = new Set();

function emit() {
  for (const l of listeners) {
    try { l(); } catch { /* ignore */ }
  }
}

async function tick(force = false) {
  if (inflight && !force) return inflight;
  const promise = (async () => {
    try {
      // `silent: true` — the dashboard tick happens forever in the
      // background; we never want every transient 500 to flash an
      // error toast at the user. Real failures still bubble through
      // snapshot.error so consumer pages can render an inline state.
      const data = await apiJson('/api/dashboard', { silent: true });
      snapshot = { data, loading: false, error: null, lastFetched: Date.now() };
    } catch (err) {
      snapshot = {
        data: snapshot.data,
        loading: false,
        error: err,
        lastFetched: snapshot.lastFetched,
      };
    } finally {
      inflight = null;
      emit();
    }
  })();
  inflight = promise;
  return promise;
}

function start() {
  if (timer) return;
  tick(true);
  timer = setInterval(() => tick(false), POLL_MS);
}

function stop() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

function subscribe(listener) {
  listeners.add(listener);
  refCount += 1;
  if (refCount === 1) start();
  return () => {
    listeners.delete(listener);
    refCount -= 1;
    if (refCount <= 0) {
      refCount = 0;
      stop();
    }
  };
}

function getSnapshot() {
  return snapshot;
}

/**
 * Force a refresh outside the normal tick (use after a mutation like
 * pairing a device, reloading a skill, switching a provider).
 */
export function refreshSystemHealth() {
  return tick(true);
}

/** Test-only — reset to a clean state between vitest cases. */
export function _resetSystemHealthForTesting(initial) {
  stop();
  refCount = 0;
  listeners.clear();
  inflight = null;
  snapshot = initial || { data: null, loading: true, error: null, lastFetched: 0 };
}

/** Subscribe + return the full dashboard snapshot. */
export function useSystemHealth() {
  const snap = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  useEffect(() => {
    // Force a refresh on mount if our cached data is stale, so a tab
    // that's been backgrounded for a long time doesn't render
    // arbitrarily-old vitals.
    if (snap.data && Date.now() - snap.lastFetched > STALE_MS) {
      tick(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return snap;
}

/**
 * Convenience for the Shell Ambient bar — derive the orb mode + heart
 * rate from the shared snapshot rather than each consumer poking the
 * raw payload.
 */
export function useSomaticHealth() {
  const { data } = useSystemHealth();
  const s = data?.somatic || {};
  const load = Number(s.cognitive_load || 0);
  const hr = Number(s.heart_rate || 0);
  const orbMode = load > 0.7 ? 'alerting' : load > 0.4 ? 'thinking' : 'idle';
  return {
    cognitiveLoad: Number.isFinite(load) ? load : 0,
    heartRate: Number.isFinite(hr) ? hr : 0,
    orbMode,
  };
}
