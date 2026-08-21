import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiJson, apiFetch } from '../lib/api';

/**
 * The work rail: what needs you, what is running, what just happened.
 *
 * This is the piece the approved design leads with, and the reason its
 * headline is "the default view is the machine, not a transcript". The
 * shell used to be navigation plus a page; the machine's actual state
 * lived on surfaces you had to go and find. Approvals blocked with
 * nothing on screen, jobs were a pane two thirds down Home, and finished
 * work was only in the timeline.
 *
 * Three sections, each one an answer to a question you actually have:
 *
 *   NEEDS YOU      a tool call is blocked on your decision, from any
 *                  surface: this chat, a routine, a channel, the phone
 *   RUNNING        work in flight, with the verb that stops it
 *   JUST HAPPENED  what finished, so the rail is not amnesiac
 *
 * Every row carries its verb inline, because the design's whole argument
 * is that you act from here rather than navigating somewhere to act. A
 * row with no verb is a row that should not be in the rail.
 */

const POLL_MS = 4000;

/** Where an approval came from, read off the session id prefix. */
export function originLabel(sessionId) {
  const sid = String(sessionId || '');
  if (sid.startsWith('channel_')) return sid.split('_')[1] || 'a channel';
  if (sid.startsWith('voice-')) return 'voice';
  if (sid.startsWith('phone-')) return 'phone';
  if (sid.startsWith('cron_') || sid.startsWith('routine_')) return 'a routine';
  return 'this chat';
}

/** The short line under a row title. */
export function subtitleOf(approval) {
  const args = approval?.args || {};
  const first = args.path || args.command || args.url || args.script || args.query;
  return first ? String(first).slice(0, 46) : originLabel(approval?.session_id);
}

/** Elapsed seconds as the design renders it: 4m12s, 955ms, 12s. */
export function elapsed(startedAt, now = Date.now() / 1000) {
  const s = Math.floor(now - Number(startedAt || 0));
  if (!Number.isFinite(s) || s < 0 || s > 60 * 60 * 24 * 365) return '';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60}s`;
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
}

/** A timeline row's one-line label, from the shape the brain sends. */
export function recentTitle(entry) {
  const t = String(entry?.title || '').trim();
  if (t) return t.slice(0, 38);
  // `type` is the only other field guaranteed present, and it reads as
  // an identifier, so make it legible rather than printing memory_error.
  const kind = String(entry?.type || 'activity').replace(/_/g, ' ');
  return kind.charAt(0).toUpperCase() + kind.slice(1);
}

export default function WorkRail() {
  const navigate = useNavigate();
  const [needs, setNeeds] = useState([]);
  const [running, setRunning] = useState([]);
  const [recent, setRecent] = useState([]);
  const [busy, setBusy] = useState('');
  // Keyed by request_id. A failed decision used to leave the row in
  // place with no message anywhere on screen, which reads exactly like
  // a click that missed.
  const [failed, setFailed] = useState({});
  const timer = useRef(null);

  const load = useCallback(async () => {
    // Each source is read independently. One failing must not blank the
    // whole rail: a rail that disappears when the timeline is slow is
    // worse than a rail with two sections.
    const [a, j, t] = await Promise.allSettled([
      apiJson('/api/approvals'),
      apiJson('/api/jobs?limit=40'),
      apiJson('/api/timeline?limit=6'),
    ]);
    if (a.status === 'fulfilled') {
      setNeeds(Array.isArray(a.value?.approvals) ? a.value.approvals : []);
    }
    if (j.status === 'fulfilled') {
      const items = Array.isArray(j.value?.items) ? j.value.items : [];
      setRunning(items.filter((i) => i.status === 'running' || i.status === 'connected'));
    }
    if (t.status === 'fulfilled') {
      // Verified against the running brain: GET /api/timeline answers
      // {count, days, entries}, and a row is
      // {type, timestamp, title, content, metadata}. The id lives under
      // metadata, not at the top level.
      const rows = t.value?.entries;
      setRecent(Array.isArray(rows) ? rows.slice(0, 5) : []);
    }
  }, []);

  useEffect(() => {
    load();
    timer.current = setInterval(load, POLL_MS);
    return () => clearInterval(timer.current);
  }, [load]);

  const decide = useCallback(async (requestId, approved) => {
    setBusy(requestId);
    setFailed((f) => { const n = { ...f }; delete n[requestId]; return n; });
    try {
      await apiFetch(
        `/api/approvals/${encodeURIComponent(requestId)}/${approved ? 'approve' : 'reject'}`,
        { method: 'POST', silent: true, body: JSON.stringify({}) },
      );
      setNeeds((rows) => rows.filter((r) => r.request_id !== requestId));
    } catch (e) {
      // The row stays, which is right: dropping it would claim a
      // decision that did not land. What was missing is that nothing
      // said so. `silent: true` suppresses the global error surface, so
      // a 404 produced a row that simply did not go away and zero error
      // text anywhere in the DOM. The reason belongs on the row that
      // failed, not on another page the user has no reason to open.
      setFailed((f) => ({
        ...f,
        [requestId]: e?.message || 'That did not go through.',
      }));
    } finally {
      setBusy('');
      load();
    }
  }, [load]);

  return (
    <aside className="v2-rail" aria-label="Work">
      <section className="v2-rail-sect">
        <header className="v2-rail-head">
          <span>Needs you</span>
          {needs.length > 0 && <span className="v2-rail-n">{needs.length}</span>}
        </header>
        {needs.length === 0 && <p className="v2-rail-quiet">Nothing waiting.</p>}
        {needs.map((n) => (
          <article key={n.request_id} className="v2-rail-card v2-rail-card--needs">
            <button
              type="button"
              className="v2-rail-title"
              onClick={() => navigate('/approvals')}
              title={n.tool_name}
            >
              {n.tool_name}
            </button>
            <p className="v2-rail-sub">{subtitleOf(n)}</p>
            {failed[n.request_id] && (
              <p className="v2-rail-failed" role="alert">
                {failed[n.request_id]}
              </p>
            )}
            <div className="v2-rail-verbs">
              <button
                type="button"
                className="v2-rail-verb"
                disabled={busy === n.request_id}
                onClick={() => decide(n.request_id, true)}
              >
                approve
              </button>
              <button
                type="button"
                className="v2-rail-verb"
                disabled={busy === n.request_id}
                onClick={() => navigate('/approvals')}
              >
                review
              </button>
            </div>
          </article>
        ))}
      </section>

      <section className="v2-rail-sect">
        <header className="v2-rail-head">
          <span>Running</span>
          {running.length > 0 && <span className="v2-rail-n">{running.length}</span>}
        </header>
        {running.length === 0 && <p className="v2-rail-quiet">Idle.</p>}
        {running.map((r) => (
          <article key={r.id} className="v2-rail-card">
            <button
              type="button"
              className="v2-rail-title"
              onClick={() => navigate('/jobs')}
              title={r.name}
            >
              {r.name}
            </button>
            <p className="v2-rail-sub">
              {[r.context_session_id, elapsed(r.started_at)].filter(Boolean).join(' · ')}
            </p>
          </article>
        ))}
      </section>

      <section className="v2-rail-sect">
        <header className="v2-rail-head"><span>Just happened</span></header>
        {recent.length === 0 && <p className="v2-rail-quiet">Nothing yet.</p>}
        {recent.map((e, i) => (
          <button
            key={e?.metadata?.id || `${e?.timestamp || ''}-${i}`}
            type="button"
            className="v2-rail-recent"
            onClick={() => navigate('/timeline')}
            title={e?.content || e?.title || ''}
          >
            <span className="v2-rail-recent-t">{recentTitle(e)}</span>
            {e?.timestamp ? <span className="v2-rail-recent-w">{elapsed(e.timestamp)}</span> : null}
          </button>
        ))}
      </section>
    </aside>
  );
}
