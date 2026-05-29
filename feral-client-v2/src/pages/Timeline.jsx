import React, { useCallback, useEffect, useRef, useState } from 'react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import EmptyState from '../ui/EmptyState';
import { apiJson } from '../lib/api';

/**
 * Timeline — chronological event feed from the brain.
 *
 * AUDIT-r14 finding 04 fix: backend at `api/routes/timeline.py:95`
 * returns `{entries, count, days}` but the UI was reading
 * `d.timeline || d.items` and always getting `undefined`. The page
 * therefore rendered empty even when the brain had data.
 *
 * Also adds the `days` + `type` filter the finding flagged as a top-3
 * improvement so users can scope to "last 24h calendar events only".
 */

// RC polish: send the canonical backend filter names so non-"All"
// picks don't silently return empty. The route in
// ``feral-core/api/routes/timeline.py`` accepts ``all``, ``memories``,
// ``events``, ``health``, ``chat`` (with legacy aliases for
// ``memory``/``calendar``). Labels stay operator-friendly.
const TYPE_OPTIONS = [
  { value: '', label: 'All sources' },
  { value: 'chat', label: 'Chat' },
  { value: 'events', label: 'Calendar' },
  { value: 'health', label: 'Health' },
  { value: 'memories', label: 'Memory' },
];

const DAYS_OPTIONS = [
  { value: 1, label: 'Last 24h' },
  { value: 7, label: 'Last 7d' },
  { value: 30, label: 'Last 30d' },
];

function formatTimestamp(item) {
  const t = item.time || item.timestamp || item.at;
  if (!t) return '';
  if (typeof t === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(t)) {
    return new Date(t).toLocaleString();
  }
  const n = Number(t);
  if (Number.isFinite(n) && n > 0) {
    const ms = n < 1e12 ? n * 1000 : n;
    return new Date(ms).toLocaleString();
  }
  return String(t);
}

export default function Timeline() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const [days, setDays] = useState(7);
  const [type, setType] = useState('');
  // Bump on Refresh; the fetch effect depends on this so an operator
  // press re-fires the HTTP GET even when days/type haven't moved.
  const [reloadCounter, setReloadCounter] = useState(0);
  // Used so a stale in-flight request (filter changed before the
  // previous fetch resolved) can't clobber the latest results.
  const requestSeqRef = useRef(0);

  // Page-mount + filter-change fetch. Plain useEffect — no useCallback
  // indirection — so the HTTP GET fires unconditionally on mount and
  // again whenever days/type/reloadCounter change. Stuck "Loading
  // timeline… (0)" was caused by the page subscribing to a WS frame
  // that never arrives; the canonical contract is the REST endpoint
  // at api/routes/timeline.py.
  useEffect(() => {
    const seq = ++requestSeqRef.current;
    let cancelled = false;
    setLoading(true);
    setErr('');
    (async () => {
      try {
        const params = new URLSearchParams();
        params.set('days', String(days));
        if (type) params.set('type', type);
        const d = await apiJson(`/api/timeline?${params.toString()}`);
        if (cancelled || requestSeqRef.current !== seq) return;
        // Backend canonical shape: {entries, count, days}. Tolerate
        // legacy keys (`timeline` / `items`) for back-compat with older
        // brains in case the user is running a mismatched version.
        const rows = d.entries || d.timeline || d.items || [];
        setItems(Array.isArray(rows) ? rows : []);
      } catch (e) {
        if (cancelled || requestSeqRef.current !== seq) return;
        setItems([]);
        setErr(e?.message || 'failed to load timeline');
      } finally {
        if (!cancelled && requestSeqRef.current === seq) {
          setLoading(false);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [days, type, reloadCounter]);

  const refresh = useCallback(() => {
    setReloadCounter((n) => n + 1);
  }, []);

  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title={`Timeline (${items.length})`}
        actions={(
          <>
            <select className="v2-select" value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Range">
              {DAYS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <select className="v2-select" value={type} onChange={(e) => setType(e.target.value)} aria-label="Type">
              {TYPE_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}>Refresh</button>
          </>
        )}
      >
        {err && <div className="v2-chip v2-chip--error" role="alert">{err}</div>}
        {loading && <EmptyState title="Loading timeline…" />}
        {!loading && items.length === 0 && (
          <EmptyState title="No events in this range" hint="Try a wider time window or different source." />
        )}
        <ul className="v2-timeline" data-testid="timeline-list">
          {items.map((item, idx) => (
            <li key={item.id || `${item.source}-${item.time}-${idx}`} className="v2-timeline-row">
              <Glass level={0} radius="sm" padding="sm">
                <div className="v2-timeline-time">{formatTimestamp(item)}{item.source && <span className="v2-chip" style={{ marginLeft: 8 }}>{item.source}</span>}</div>
                <div className="v2-timeline-text">{item.text || item.title || item.summary || JSON.stringify(item).slice(0, 200)}</div>
              </Glass>
            </li>
          ))}
        </ul>
      </Pane>
    </div>
  );
}
