import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiJson, apiFetch } from '../lib/api';

/**
 * A dock tile's contents, fanned out above the dock.
 *
 * The approved design: "Press and hold a tile, or right-click it, and
 * its contents fan out above the dock: the two approvals with an approve
 * verb, the running jobs with kill and steer. You act from the stack
 * without navigating anywhere. That is the Dock idea a folder stack
 * already uses, pointed at work instead of files."
 *
 * Only tiles that lead to a list of actionable things have a stack.
 * Settings has no stack because there is nothing to act on from one.
 *
 * The list is fetched when the stack opens rather than kept warm by the
 * shared poller. A stack is a rare, deliberate gesture, and the poller
 * exists to paint two counters cheaply; putting full lists in it would
 * make every client of it pay for something almost nobody opens.
 */

/** Milliseconds of hold before a press becomes a stack, not a click. */
export const HOLD_MS = 450;

/** Which tiles have contents worth fanning out. */
export const STACKABLE = {
  '/approvals': { title: 'Needs you', source: '/api/approvals' },
  '/jobs': { title: 'Running', source: '/api/jobs?limit=40' },
};

export function isStackable(to) {
  return Object.prototype.hasOwnProperty.call(STACKABLE, to);
}

/** Normalise either endpoint's payload into rows the stack can render. */
export function rowsFrom(to, payload) {
  if (to === '/approvals') {
    const list = Array.isArray(payload?.approvals) ? payload.approvals : [];
    return list.map((a) => ({
      id: a.request_id,
      title: a.tool_name,
      sub: String(a.args?.path || a.args?.command || a.args?.url || a.safety_level || ''),
      verb: 'approve',
    }));
  }
  const items = Array.isArray(payload?.items) ? payload.items : [];
  return items
    .filter((i) => i.status === 'running' || i.status === 'connected')
    .map((i) => ({
      id: i.id,
      title: i.name,
      sub: String(i.kind || ''),
      // Only offer the verb when the brain names a route for it. A kill
      // button that 404s is worse than no button.
      verb: /^(POST|DELETE)\s+\S+$/.test(String(i.cancellable_via || '')) ? 'kill' : '',
      cancel: i.cancellable_via,
    }));
}

export default function DockStack({ to, onClose }) {
  const navigate = useNavigate();
  const [rows, setRows] = useState(null);
  const [busy, setBusy] = useState('');
  // Same defect as the work rail had: `silent: true` plus a bare catch
  // meant a failed verb left the row sitting there with no explanation
  // anywhere, which is indistinguishable from a click that missed.
  const [failed, setFailed] = useState({});
  const ref = useRef(null);
  const meta = STACKABLE[to];

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const d = await apiJson(meta.source);
        if (alive) setRows(rowsFrom(to, d));
      } catch {
        if (alive) setRows([]);
      }
    })();
    return () => { alive = false; };
  }, [to, meta.source]);

  // Escape and any click outside close it, the way a menu should.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    const onDown = (e) => { if (ref.current && !ref.current.contains(e.target)) onClose(); };
    window.addEventListener('keydown', onKey);
    window.addEventListener('pointerdown', onDown);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('pointerdown', onDown);
    };
  }, [onClose]);

  const act = useCallback(async (row) => {
    setBusy(row.id);
    setFailed((f) => { const n = { ...f }; delete n[row.id]; return n; });
    try {
      if (to === '/approvals') {
        await apiFetch(`/api/approvals/${encodeURIComponent(row.id)}/approve`, {
          method: 'POST', silent: true, body: JSON.stringify({}),
        });
      } else if (row.cancel) {
        const [method, path] = String(row.cancel).split(/\s+/);
        await apiFetch(path, { method, silent: true });
      }
      setRows((r) => (r || []).filter((x) => x.id !== row.id));
    } catch (e) {
      // Leave the row (dropping it would claim an action that did not
      // land) and say why, on the row itself.
      setFailed((f) => ({ ...f, [row.id]: e?.message || 'That did not go through.' }));
    } finally {
      setBusy('');
    }
  }, [to]);

  return (
    <div
      className="v2-stack"
      ref={ref}
      role="dialog"
      aria-label={`${meta.title} stack`}
    >
      <header className="v2-stack-head">{meta.title}</header>

      {rows === null && <p className="v2-stack-quiet">Reading…</p>}
      {rows !== null && rows.length === 0 && (
        <p className="v2-stack-quiet">Nothing here right now.</p>
      )}

      {(rows || []).map((r) => (
        <div key={r.id} className="v2-stack-row" data-failed={failed[r.id] ? 'yes' : 'no'}>
          <span className="v2-stack-dot" aria-hidden="true" />
          <button
            type="button"
            className="v2-stack-title"
            onClick={() => { onClose(); navigate(to); }}
            title={r.title}
          >
            <span className="v2-stack-t">{r.title}</span>
            {r.sub && <span className="v2-stack-s">{r.sub}</span>}
          </button>
          {r.verb ? (
            <button
              type="button"
              className="v2-stack-verb"
              disabled={busy === r.id}
              onClick={() => act(r)}
            >
              {r.verb}
            </button>
          ) : (
            <span className="v2-stack-verb v2-stack-verb--none">no verb</span>
          )}
          {failed[r.id] && (
            <p className="v2-stack-failed" role="alert">{failed[r.id]}</p>
          )}
        </div>
      ))}

      <button
        type="button"
        className="v2-stack-all"
        onClick={() => { onClose(); navigate(to); }}
      >
        Open {meta.title}
      </button>
    </div>
  );
}
