import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, Square } from 'lucide-react';
import Pane from '../ui/Pane';
import EmptyState from '../ui/EmptyState';
import { apiJson, apiFetch } from '../lib/api';
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

/**
 * How long until a scheduled routine next fires, as a short human string.
 *
 * A routine has not started, so `ranFor(started_at)` is the wrong number
 * for it: it is the AGE of the row. Observed live, a routine created 71
 * days ago rendered as "scheduled · 1722h 29m", which reads as a routine
 * due in 71 days. `detail.next_run` is the wall-clock epoch the scheduler
 * will actually fire at (api/routes/jobs.py builds it from
 * `job.next_run`), so that is what the row shows instead. Returns '' when
 * the brain did not name a next run, rather than inventing one.
 */
export function nextRunIn(nextRun, now = Date.now() / 1000) {
  const at = Number(nextRun || 0);
  if (!Number.isFinite(at) || at <= 0) return '';
  const secs = Math.round(at - now);
  if (secs <= 0) return 'next due now';
  if (secs > 60 * 60 * 24 * 365) return '';
  if (secs < 60) return `next in ${secs}s`;
  if (secs < 3600) return `next in ${Math.floor(secs / 60)}m`;
  return `next in ${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
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
  const [stopping, setStopping] = useState('');
  // Keyed by job id. A stop that did not land has to say so on the row
  // it was clicked on; `silent: true` suppresses the global surface.
  const [failed, setFailed] = useState({});
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

  const stop = useCallback(async (job) => {
    const route = cancelRouteOf(job);
    if (!route) return;
    setStopping(job.id);
    setFailed((f) => { const n = { ...f }; delete n[job.id]; return n; });
    try {
      await apiFetch(route.path, {
        method: route.method,
        silent: true,
        ...(route.method === 'POST' ? { body: JSON.stringify({}) } : {}),
      });
      // Reload rather than dropping the row optimistically: the brain
      // decides whether a job is actually gone, and a row that vanishes
      // from a click that failed is the claim this codebase keeps
      // making by accident.
      await load();
    } catch (e) {
      setFailed((f) => ({ ...f, [job.id]: e?.message || 'That did not stop it.' }));
    } finally {
      setStopping('');
    }
  }, [load]);

  // `running` is genuinely only the running ones. The header used to say
  // "N active" off this same filter while the list below it showed every
  // job the brain reported, so five rows (routines at status "scheduled",
  // specialists at "ready") sat under the words "0 active". Reproduced
  // live. The count now describes what is actually on screen and keeps
  // the running number beside it, so neither number contradicts the list.
  const running = items.filter((i) => i.status === 'running' || i.status === 'connected');
  const countLabel = `${items.length} listed · ${running.length} running`;

  return (
    <div className="v2-page v2-jobs">
      <Pane
        title="Running now"
        leading={<Activity size={16} aria-hidden="true" />}
        actions={<span className="v2-jobs-count">{countLabel}</span>}
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
              // A routine has not started, so its age is meaningless next
              // to the word "scheduled"; show when it next fires instead.
              const ran = j.kind === 'routine'
                ? nextRunIn(j.detail?.next_run)
                : ranFor(j.started_at);
              return (
                <li key={j.id} className="v2-job" data-status={j.status}>
                  <span className="v2-job-kind">{jobKindLabel(j.kind)}</span>
                  <span className="v2-job-name" title={j.name}>{j.name}</span>
                  <span className="v2-job-status">
                    {j.status}
                    {typeof j.progress === 'number' && ` · ${Math.round(j.progress * 100)}%`}
                    {ran && ` · ${ran}`}
                  </span>
                  {/* This was a <span> reading "stoppable": the route
                      to stop the job was computed and then rendered as
                      a LABEL. So the page listing everything the brain
                      is doing offered no verb at all, and the one
                      affordance on each row looked like a checkbox that
                      did nothing when clicked. */}
                  {cancel ? (
                    <button
                      type="button"
                      className="v2-job-cancel v2-job-cancel--btn"
                      title={`Stop this job (${j.cancellable_via})`}
                      // The visible word is "stop", which out of context
                      // names nothing: a screen reader on a list of six
                      // jobs hears "stop" six times. The label says
                      // which one.
                      aria-label={`Stop ${j.name || jobKindLabel(j.kind)}`}
                      disabled={stopping === j.id}
                      onClick={() => stop(j)}
                    >
                      <Square size={11} aria-hidden="true" />
                      {stopping === j.id ? 'stopping' : 'stop'}
                    </button>
                  ) : (
                    <span
                      className="v2-job-cancel v2-job-cancel--none"
                      title="The brain does not name a route that can stop this one."
                    >
                      not stoppable here
                    </span>
                  )}
                  {failed[j.id] && (
                    <p className="v2-job-failed" role="alert">{failed[j.id]}</p>
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
