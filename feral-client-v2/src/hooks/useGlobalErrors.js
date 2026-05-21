/**
 * Global error store for API + WebSocket failures.
 * Module-level pub/sub (no extra deps) + useSyncExternalStore hook.
 */
import { useCallback, useSyncExternalStore } from 'react';

let seq = 0;
let errors = [];
const listeners = new Set();

function emit() {
  listeners.forEach((fn) => {
    try { fn(); } catch { /* ignore */ }
  });
}

export function pushGlobalError(err) {
  if (!err) return;
  const id = `err_${++seq}`;
  const entry = {
    id,
    err,
    message: err?.detail || err?.reason || err?.message || 'Something went wrong',
    at: Date.now(),
  };
  errors = [entry, ...errors].slice(0, 20);
  emit();
  return id;
}

export function clearGlobalError(id) {
  if (!id) return;
  const next = errors.filter((e) => e.id !== id);
  if (next.length === errors.length) return;
  errors = next;
  emit();
}

export function clearAllGlobalErrors() {
  if (errors.length === 0) return;
  errors = [];
  emit();
}

function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot() {
  return errors;
}

export function useGlobalErrors() {
  const list = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
  const push = useCallback((err) => pushGlobalError(err), []);
  const clear = useCallback((id) => clearGlobalError(id), []);
  const clearAll = useCallback(() => clearAllGlobalErrors(), []);
  return { errors: list, push, clear, clearAll };
}

/** Test seam — reset store between vitest cases. */
export function _resetGlobalErrorsForTesting() {
  errors = [];
  seq = 0;
  emit();
}
