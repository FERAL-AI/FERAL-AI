import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, Square } from 'lucide-react';
import Pane from '../ui/Pane';
import EmptyState from '../ui/EmptyState';
import { apiJson } from '../lib/api';
import { jobKindLabel } from './Home';

/**
 * Jobs: everything the brain is doing right now, as its own place.
 *
 * The approved design puts Jobs in the dock, one of the eight you return
 * to. It existed only as a pane two thirds of the way down Home, which
 * is not somewhere you go to answer "what is running", it is somewhere
 * you scroll past.
 *
 * `GET /api/jobs` merges six sources: TaskFlows, routines, mitosis
 * specialists, Tool Genesis drafts, live HUP daemons, and backgrounded
 * shell commands. It reports `degraded` per source, which matters more
 * than it sounds: a source that failed and a source with nothing to say
 * both return an empty list, and this endpoint exists precisely to tell
 * those apart. A dead aggregator reads as a calm system, so the degraded
 * set is rendered rather than swallowed.
 */

const POLL_MS = 4000;

/** Seconds since a wall-clock epoch stamp, as a short human string. */
export function ranFor(startedAt, now = Date.now() / 1000) {
  const secs = Math.floor(now - Number(startedAt || 0));
  if (!Number.isFinite(secs) || secs < 0 || secs > 60 * 60 * 24 * 365) return '';
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`;
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
}

/** A job is only cancellable when the brain names a route for it. */
export function cancelRouteOf(job) {
  const via = String(job?.cancellable_via || '');
  const m = via.match(/^(POST|DELETE)\s+(\S+)$/);
  return m ? { method: m[1], path: m[2] } : null;
}

export default function Jobs() {
  const [items, setItems] = useState([]);
  const [counts, setCounts] = useState({});
  const [degraded, setDegraded] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const timer = useRef(null);

  const load = useCallback(async () => {
    try {
      const d = await apiJson('/api/jobs?limit=100');
      setItems(Array.isArray(d?.items) ? d.items : []);
      setCounts(d?.counts_by_kind || {});
      setDegraded(d?.degraded || {});
      setError('');
    } catch (e) {
      setError(e?.message || 'could not reach the brain');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    timer.current = setInterval(load, POLL_MS);
    return () => clearInterval(timer.current);
  }, [load]);

  const running = items.filter((i) => i.status === 'running' || i.status === 'connected');

  return (
    <div className="v2-page v2-jobs">
      <Pane
        title="Running now"
        leading={<Activity size={16} aria-hidden="true" />}
        actions={
          <span className="v2-jobs-count">
            {running.length === 1 ? '1 active' : `${running.length} active`}
          </span>
        }
      >
        {error && <div className="v2-jobs-error" role="status">{error}</div>}

        {Object.keys(degraded).length > 0 && (
          <div className="v2-jobs-degraded" role="status">
            {Object.entries(degraded).map(([kind, why]) => (
              <span key={kind} className="v2-jobs-degraded-row">
                {`${jobKindLabel(kind)} could not be read: ${why}`}
              </span>
            ))}
          </div>
        )}

        {!loading && items.length === 0 && !error && (
          <EmptyState
            icon={<Activity size={22} aria-hidden="true" />}
            title="Nothing running"
            hint="Flows, routines, specialists, drafts, devices and shell jobs all show up here."
          />
        )}

        {items.length > 0 && (
          <ul className="v2-jobs-list" aria-label="Active jobs">
            {items.map((j) => {
              const cancel = cancelRouteOf(j);
              const ran = ranFor(j.started_at);
              return (
                <li key={j.id} className="v2-job" data-status={j.status}>
                  <span className="v2-job-kind">{jobKindLabel(j.kind)}</span>
                  <span className="v2-job-name" title={j.name}>{j.name}</span>
                  <span className="v2-job-status">
                    {j.status}
                    {typeof j.progress === 'number' && ` · ${Math.round(j.progress * 100)}%`}
                    {ran && ` · ${ran}`}
                  </span>
                  {cancel ? (
                    <span className="v2-job-cancel" title={j.cancellable_via}>
                      <Square size={11} aria-hidden="true" /> stoppable
                    </span>
                  ) : (
                    <span className="v2-job-cancel v2-job-cancel--none">not stoppable here</span>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        {Object.keys(counts).length > 0 && (
          <div className="v2-jobs-tally">
            {Object.entries(counts).map(([kind, n]) => (
              <span key={kind} className="v2-chip v2-chip--muted">{`${jobKindLabel(kind)}: ${n}`}</span>
            ))}
          </div>
        )}
      </Pane>
    </div>
  );
}
