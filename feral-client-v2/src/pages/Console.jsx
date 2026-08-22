import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Gauge } from 'lucide-react';
import Pane from '../ui/Pane';
import { apiJson } from '../lib/api';
import { jobKindLabel } from './Home';
import { elapsed } from '../shell/WorkRail';
import { useMachineVitals } from '../hooks/useMachineVitals';

/**
 * Console: the machine, not a transcript.
 *
 * The approved design's first line is "the default view is the machine,
 * not a transcript. Chat is one place you go." This is that view, and it
 * is the landing route.
 *
 * What it deliberately is not: a briefing. Home already greets you, sums
 * up the day and suggests things. That is a fine page and it stays,
 * reachable by name. It is the wrong thing to open onto, because the
 * question you have when you sit down is "what is this thing doing", and
 * a greeting does not answer it.
 *
 * Everything here is read from a live endpoint on a short poll. Nothing
 * is derived from configuration: the failure this codebase produces over
 * and over is a surface that reports success while doing nothing, so a
 * console that showed intent rather than state would be worse than none.
 */

const POLL_MS = 4000;

/** One line per source, so a dead aggregator cannot read as an idle one. */
export function sourceRows(counts, degraded) {
  const kinds = new Set([...Object.keys(counts || {}), ...Object.keys(degraded || {})]);
  return [...kinds].sort().map((kind) => ({
    kind,
    label: jobKindLabel(kind),
    count: Number((counts || {})[kind] || 0),
    failure: (degraded || {})[kind] || '',
  }));
}

export default function Console() {
  const [jobs, setJobs] = useState({ items: [], counts_by_kind: {}, degraded: {} });
  const [approvals, setApprovals] = useState([]);
  const vitals = useMachineVitals();
  const [error, setError] = useState('');
  const timer = useRef(null);

  const load = useCallback(async () => {
    // /api/dashboard is no longer fetched here. Its only use was the
    // `health` key for the brain figure, which is the health-readings
    // summary and not liveness, and the shared vitals poller already
    // requests that endpoint on the same 4s cadence. Two components
    // polling the same URL for one field is the duplication the vitals
    // hook exists to remove.
    const [j, a] = await Promise.allSettled([
      apiJson('/api/jobs?limit=60'),
      apiJson('/api/approvals'),
    ]);
    if (j.status === 'fulfilled') setJobs(j.value || {});
    if (a.status === 'fulfilled') setApprovals(a.value?.approvals || []);
    setError(
      [j, a].every((r) => r.status === 'rejected') ? 'could not reach the brain' : '',
    );
  }, []);

  useEffect(() => {
    load();
    timer.current = setInterval(load, POLL_MS);
    return () => clearInterval(timer.current);
  }, [load]);

  const items = Array.isArray(jobs.items) ? jobs.items : [];
  const running = items.filter((i) => i.status === 'running' || i.status === 'connected');
  const rows = sourceRows(jobs.counts_by_kind, jobs.degraded);

  return (
    <div className="v2-page v2-console">
      {error && <div className="v2-console-error" role="status">{error}</div>}

      <div className="v2-console-grid">
        <Pane title="Right now" leading={<Gauge size={16} aria-hidden="true" />}>
          <div className="v2-console-figures">
            <Link to="/jobs" className="v2-console-figure">
              <span className="v2-console-n">{running.length}</span>
              <span className="v2-console-lbl">running</span>
            </Link>
            <Link to="/approvals" className="v2-console-figure" data-tone={approvals.length ? 'warn' : 'plain'}>
              <span className="v2-console-n">{approvals.length}</span>
              <span className="v2-console-lbl">need you</span>
            </Link>
            {/* This read `health` off /api/dashboard, which is
                `latest_health`: the health-READINGS summary, and `{}` on
                any brain with no sensor data. So `health.status` was
                undefined and the figure rendered a bare "?" next to two
                real numbers, which says nothing and looks broken.
                Liveness is a different question and the shell already
                answers it: `reachable` from the shared vitals poller,
                the same signal the brand light uses, so the two cannot
                disagree. */}
            <Link
              to="/health"
              className="v2-console-figure"
              data-tone={vitals.reachable ? 'plain' : 'warn'}
            >
              <span className="v2-console-n">{vitals.reachable ? 'ok' : 'down'}</span>
              <span className="v2-console-lbl">brain</span>
            </Link>
          </div>

          {running.length === 0 && approvals.length === 0 && (
            <p className="v2-console-quiet">
              Nothing running and nothing waiting. Start something in
              {' '}
              <Link to="/chat">Chat</Link>.
            </p>
          )}
        </Pane>

        <Pane title="In flight">
          {running.length === 0 && <p className="v2-console-quiet">Idle.</p>}
          {running.length > 0 && (
            <ul className="v2-console-list" aria-label="Running work">
              {running.map((r) => (
                <li key={r.id} className="v2-console-row">
                  <span className="v2-console-kind">{jobKindLabel(r.kind)}</span>
                  <span className="v2-console-name" title={r.name}>{r.name}</span>
                  <span className="v2-console-meta">{elapsed(r.started_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </Pane>

        <Pane title="Sources">
          {/* The point of this pane: a source that FAILED and a source
              with nothing to report both return an empty list, and only
              /api/jobs can tell them apart. Showing the count alone
              would hide exactly the case worth seeing. */}
          <ul className="v2-console-list" aria-label="Job sources">
            {rows.map((s) => (
              <li key={s.kind} className="v2-console-row" data-failed={s.failure ? 'yes' : 'no'}>
                <span className="v2-console-kind">{s.label}</span>
                <span className="v2-console-name">
                  {s.failure ? `could not be read: ${s.failure}` : `${s.count}`}
                </span>
              </li>
            ))}
            {rows.length === 0 && <li className="v2-console-quiet">No sources reporting.</li>}
          </ul>
        </Pane>
      </div>
    </div>
  );
}
