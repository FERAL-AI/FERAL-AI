/**
 * useResource: one GET, three honest outcomes.
 *
 * The defect this exists to kill: every page in this client used to
 * write its fetch as
 *
 *     apiJson('/api/baseline/alerts').then(setAlerts).catch(() => setAlerts([]))
 *
 * and then rendered "No anomalies detected" whenever the array was
 * empty. A dropped request therefore became an affirmative statement
 * about the user's health. `apiJson` throws on any non-2xx or network
 * failure and pushes a global toast, but ErrorToast auto-dismisses
 * after 6 seconds while the false empty state stays on screen forever.
 *
 * The contract here is the fix:
 *
 *   - `data` is ONLY ever what the brain actually returned. On failure
 *     it is left exactly as it was (so `null` on a first-load failure).
 *     The hook never substitutes `[]` or `{}` for an answer it does not
 *     have, so a page cannot render an empty state off a failed fetch.
 *   - `error` is the `ApiError` instance, not a string, because
 *     `status` / `code` / `reason` are what let <ErrorState /> tell a
 *     401 apart from a 502. Anything thrown that is not already an
 *     ApiError is wrapped into one so consumers get a stable shape.
 *   - `loading` means "no attempt has settled yet for this path". It
 *     does NOT flip back to true for poll ticks or `refresh()` calls,
 *     so a polling surface never flashes its skeleton.
 *
 * The rendering rule every converted call site follows:
 *
 *     if (loading && !data)  -> loading state
 *     if (error && !data)    -> <ErrorState />     ("we could not ask")
 *     if (data is empty)     -> <EmptyState />     ("we asked, nothing")
 *
 * Options:
 *   enabled     boolean  skip the fetch entirely (e.g. a popup that is
 *                        closed). Flipping false -> true refetches.
 *   select      fn       map the raw JSON to what the page renders.
 *                        Runs only on success, so it can never mint an
 *                        empty value out of a failure.
 *   initialData any      value for `data` before the first success.
 *                        Leave it `null` unless the page genuinely
 *                        cannot represent "unknown".
 *   pollMs      number   re-fetch interval; 0 (default) means one shot.
 *   silent      boolean  forwarded to apiFetch to suppress the global
 *                        toast. Use it for polling surfaces: an inline
 *                        ErrorState is already permanent and truthful.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { apiJson, ApiError } from '../lib/api';

export function toApiError(err, path) {
  if (err instanceof ApiError) return err;
  return new ApiError({
    status: 0,
    code: 'client',
    reason: err?.name || '',
    detail: err?.message || 'Request failed',
    raw: err,
    path: path || '',
  });
}

export function useResource(path, options = {}) {
  const {
    enabled = true,
    select,
    initialData = null,
    pollMs = 0,
    silent = false,
  } = options;

  const selectRef = useRef(select);
  selectRef.current = select;
  // Captured once: callers routinely pass a fresh `[]` literal every
  // render and we must not treat that as a change.
  const initialRef = useRef(initialData);
  const reqRef = useRef(0);
  const aliveRef = useRef(true);

  const [state, setState] = useState(() => ({
    data: initialRef.current,
    error: null,
    loading: enabled && !!path,
  }));

  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const load = useCallback(async () => {
    if (!path || !enabled) return null;
    const ticket = ++reqRef.current;
    try {
      const raw = await apiJson(path, silent ? { silent: true } : {});
      if (!aliveRef.current || ticket !== reqRef.current) return null;
      const next = selectRef.current ? selectRef.current(raw) : raw;
      setState({ data: next, error: null, loading: false });
      return next;
    } catch (err) {
      if (!aliveRef.current || ticket !== reqRef.current) return null;
      // Keep whatever `data` we already had. Replacing it with an empty
      // value here is the entire bug class this hook exists to remove.
      setState((prev) => ({
        data: prev.data,
        error: toApiError(err, path),
        loading: false,
      }));
      return null;
    }
  }, [path, enabled, silent]);

  useEffect(() => {
    if (!path || !enabled) {
      // Invalidate anything in flight so a late response cannot land on
      // a disabled resource.
      reqRef.current += 1;
      setState({ data: initialRef.current, error: null, loading: false });
      return undefined;
    }
    setState((prev) => (prev.loading ? prev : { ...prev, loading: true }));
    load();
    if (!pollMs) return undefined;
    const timer = setInterval(load, pollMs);
    return () => clearInterval(timer);
  }, [path, enabled, pollMs, load]);

  return {
    data: state.data,
    error: state.error,
    loading: state.loading,
    refresh: load,
  };
}

/**
 * allSettled helper for the two pages that fan out to several
 * endpoints at once. `Promise.allSettled` never rejects, which is how
 * Devices.jsx ended up with a `catch` block that could not run and a
 * `setError(null)` that ran unconditionally. Pass the settled array in
 * and get back the first real failure, or null when at least one call
 * succeeded and the caller only cares about a total outage.
 */
export function firstRejection(results) {
  for (const r of results || []) {
    if (r && r.status === 'rejected') return toApiError(r.reason);
  }
  return null;
}

export default useResource;
