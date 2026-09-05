import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { FolderLock, Trash2, Plus, Eye, Pencil } from 'lucide-react';
import Pane from '../ui/Pane';
import EmptyState from '../ui/EmptyState';
import { apiJson, apiFetch } from '../lib/api';

/**
 * Folder grants: which folders FERAL is allowed to touch.
 *
 * Three routes have existed under `/api/security/grants` (GET, POST,
 * DELETE) and grepping src/ for "security/grants" returned zero hits, so
 * no client had ever called them. The only way to see or change the
 * sandbox was `feral grant` in a terminal, which is also what chat tells
 * a user to do when a tool is refused. Being sent to a terminal to
 * unblock a GUI is the gap this closes.
 *
 * Shapes below were captured by running SandboxPolicy directly against a
 * temp FERAL_HOME, not read off the route:
 *
 *   list_grants()               -> [{path, mode, granted_at}]
 *   grant_folder(path, mode)    -> {ok: true, path, mode}
 *   revoke_folder(path)         -> true
 *
 * Two API details that shape this code:
 *
 *   1. POST answers a validation failure with HTTP 200 and
 *      `{ok: false, error: "..."}`. apiFetch throws an ApiError on any
 *      200 whose body carries a non-empty `error` string, so these calls
 *      pass `silent: true` and read the envelope back off `.raw` rather
 *      than letting a routine validation message become a global toast.
 *   2. DELETE takes `path` as a query parameter, not a body.
 */

/** Modes the POST route accepts, with 'write' kept as a legacy alias. */
export const MODES = ['read', 'readwrite'];

export function modeLabel(mode) {
  if (mode === 'readwrite' || mode === 'write') return 'Read and write';
  if (mode === 'read') return 'Read only';
  return mode || 'unknown';
}

export function canWrite(mode) {
  return mode === 'readwrite' || mode === 'write';
}

export function formatGranted(ts) {
  const n = Number(ts || 0);
  if (!n || !Number.isFinite(n)) return '';
  try {
    return new Date(n * 1000).toLocaleDateString();
  } catch {
    return '';
  }
}

/**
 * Above this many rows the list stops being a list and becomes a wall,
 * so a filter box and a count appear.
 *
 * This is not hypothetical. A live workspace_grants.json held 876 grants
 * in 172 KB, 870 of them pytest sandboxes that no longer existed, and
 * this page rendered 2,665 lines. The brain now prunes those, but the
 * page is the security surface and has to stay readable on its own: a
 * folder list only ever grows, and an operator who cannot find their own
 * two grants in it cannot review what FERAL is allowed to touch.
 */
export const FILTER_THRESHOLD = 25;

/** Roots the operating system hands out and reclaims. */
const TEMP_PREFIXES = [
  '/tmp/', '/private/tmp/', '/var/tmp/', '/private/var/tmp/',
  '/var/folders/', '/private/var/folders/',
];

export function isTemporaryPath(p) {
  const s = String(p || '');
  if (/(^|\/)pytest-of-[^/]*(\/|$)/.test(s)) return true;
  return TEMP_PREFIXES.some((prefix) => s.startsWith(prefix));
}

/** Folders a person chose first; scratch directories after them. */
export function orderGrants(grants) {
  const real = [];
  const temp = [];
  for (const g of grants || []) {
    (isTemporaryPath(g?.path) ? temp : real).push(g);
  }
  return real.concat(temp);
}

/** Case-insensitive substring match on the path. Empty query matches all. */
export function filterGrants(grants, query) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return grants || [];
  return (grants || []).filter(
    (g) => String(g?.path || '').toLowerCase().includes(q),
  );
}

/** Read {ok:false, error} out of either a body or a thrown ApiError. */
export function envelopeOf(valueOrError) {
  if (valueOrError && typeof valueOrError === 'object') {
    if ('ok' in valueOrError) return valueOrError;
    if (valueOrError.raw && typeof valueOrError.raw === 'object') return valueOrError.raw;
  }
  return null;
}

export default function Grants() {
  const [grants, setGrants] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [path, setPath] = useState('');
  const [mode, setMode] = useState('read');
  const [busy, setBusy] = useState('');
  const [query, setQuery] = useState('');

  const ordered = useMemo(() => orderGrants(grants), [grants]);
  const shown = useMemo(() => filterGrants(ordered, query), [ordered, query]);
  const crowded = grants.length > FILTER_THRESHOLD;

  const load = useCallback(async () => {
    try {
      const data = await apiJson('/api/security/grants');
      setGrants(Array.isArray(data?.grants) ? data.grants : []);
      setError('');
    } catch (e) {
      setError(e?.message || 'could not reach the brain');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const add = useCallback(async (e) => {
    e.preventDefault();
    const trimmed = path.trim();
    if (!trimmed) return;
    setBusy('add');
    setError('');
    try {
      const r = await apiFetch('/api/security/grants', {
        method: 'POST',
        silent: true,
        body: JSON.stringify({ path: trimmed, mode }),
      });
      const body = await r.json().catch(() => ({}));
      if (body?.ok === false) {
        setError(body.error || 'could not grant that folder');
        return;
      }
      setPath('');
      await load();
    } catch (err) {
      // A validation failure is a 200 that apiFetch turns into a throw.
      // It is an answer to the user, not a broken request.
      const env = envelopeOf(err);
      setError(env?.error || err?.message || 'could not grant that folder');
    } finally {
      setBusy('');
    }
  }, [path, mode, load]);

  const revoke = useCallback(async (target) => {
    setBusy(target);
    setError('');
    try {
      // `path` is a query parameter on this route, not a body.
      const r = await apiFetch(
        `/api/security/grants?path=${encodeURIComponent(target)}`,
        { method: 'DELETE', silent: true },
      );
      const body = await r.json().catch(() => ({}));
      if (body?.ok === false) {
        setError(body.error || 'could not revoke that folder');
        return;
      }
      await load();
    } catch (err) {
      const env = envelopeOf(err);
      setError(env?.error || err?.message || 'could not revoke that folder');
    } finally {
      setBusy('');
    }
  }, [load]);

  return (
    <div className="v2-page v2-grants">
      <Pane title="Folders FERAL can use" leading={<FolderLock size={16} aria-hidden="true" />}>
        <p className="v2-p v2-p--muted">
          FERAL can only read or change files inside the folders listed here.
          Everything else on this machine is off limits, including to skills
          you install later.
        </p>

        {error && <div className="v2-grants-error" role="status">{error}</div>}

        <form className="v2-grants-add" onSubmit={add}>
          <input
            className="v2-input"
            type="text"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/Users/you/Projects"
            aria-label="Folder to allow"
          />
          <select
            className="v2-input v2-grants-mode"
            value={mode}
            onChange={(e) => setMode(e.target.value)}
            aria-label="Access level"
          >
            {MODES.map((m) => (
              <option key={m} value={m}>{modeLabel(m)}</option>
            ))}
          </select>
          <button
            type="submit"
            className="v2-btn v2-btn--primary"
            disabled={busy === 'add' || !path.trim()}
          >
            <Plus size={13} aria-hidden="true" /> Allow
          </button>
        </form>

        {!loading && grants.length === 0 && !error && (
          <EmptyState
            icon={<FolderLock size={22} aria-hidden="true" />}
            title="No folders allowed yet"
            hint="Until you allow one, FERAL cannot read or write files anywhere on this machine."
          />
        )}

        {crowded && (
          <div className="v2-grants-find">
            <input
              className="v2-input"
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter by path"
              aria-label="Filter folders"
            />
            <span className="v2-grants-count" role="status">
              {shown.length === grants.length
                ? `${grants.length} folders`
                : `${shown.length} of ${grants.length} folders`}
            </span>
          </div>
        )}

        {crowded && shown.length === 0 && (
          <p className="v2-p v2-p--muted">No folder matches that filter.</p>
        )}

        {shown.length > 0 && (
          <ul className="v2-grants-list" aria-label="Allowed folders">
            {shown.map((g) => (
              <li key={g.path} className="v2-grant">
                <span
                  className="v2-grant-mode"
                  data-write={canWrite(g.mode) ? 'yes' : 'no'}
                  title={modeLabel(g.mode)}
                >
                  {canWrite(g.mode)
                    ? <Pencil size={12} aria-hidden="true" />
                    : <Eye size={12} aria-hidden="true" />}
                  {modeLabel(g.mode)}
                </span>
                <span className="v2-grant-path" title={g.path}>{g.path}</span>
                {formatGranted(g.granted_at) && (
                  <span className="v2-grant-when">since {formatGranted(g.granted_at)}</span>
                )}
                <button
                  type="button"
                  className="v2-btn v2-btn--ghost"
                  disabled={busy === g.path}
                  onClick={() => revoke(g.path)}
                  aria-label={`Stop allowing ${g.path}`}
                >
                  <Trash2 size={13} aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Pane>
    </div>
  );
}
