import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ShieldCheck, Terminal, Bot, MessageSquare, Activity, ChevronDown } from 'lucide-react';
import { apiJson, apiFetch } from '../lib/api';
import { useChatThread } from './ChatThreadContext';

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

/** Which rail sections the operator has folded away. */
const RAIL_CLOSED_KEY = 'feral_v2_rail_closed';

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

/**
 * How recent an entry has to be to count as "just happened".
 *
 * The section was showing rows 126 hours old, which is five days, under
 * a heading that promises the opposite. There was no window at all: it
 * took `entries.slice(0, 5)` from the timeline, and on a brain that has
 * been quiet for a week the five most recent things are all ancient.
 *
 * Twelve hours because the heading is about a working session, not
 * about history. Anything older belongs on the timeline page, which is
 * what the section links to.
 */
export const JUST_HAPPENED_WINDOW_S = 12 * 60 * 60;

/** Timeline rows recent enough to sit under "Just happened". */
export function recentEnough(entries, now = Date.now() / 1000) {
  const rows = Array.isArray(entries) ? entries : [];
  return rows.filter((e) => {
    const ts = Number(e?.timestamp || 0);
    // A row with no usable timestamp is dropped rather than shown as
    // "just happened" on no evidence.
    if (!Number.isFinite(ts) || ts <= 0) return false;
    const age = now - ts;
    return age >= 0 && age <= JUST_HAPPENED_WINDOW_S;
  });
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

/**
 * The glyph for a running job, from the kind the brain reports.
 *
 * The design gives every rail row an icon, and it is not decoration:
 * the rail mixes shell commands with sub-agents, and at a glance the
 * icon is what separates "a build is running" from "an agent is part
 * way through seven steps". Text alone makes them one undifferentiated
 * list.
 */
export function iconForJob(kind) {
  const k = String(kind || '');
  if (k === 'background_bash' || k.includes('shell') || k.includes('bash')) return Terminal;
  if (k.includes('agent') || k.includes('subagent') || k.includes('task')) return Bot;
  return Activity;
}

export default function WorkRail() {
  const navigate = useNavigate();
  const [needs, setNeeds] = useState([]);
  const [running, setRunning] = useState([]);
  const [recent, setRecent] = useState([]);
  const [threads, setThreads] = useState([]);
  const [busy, setBusy] = useState('');
  // The shell already owns thread loading, so the rail asks it rather
  // than writing localStorage behind Chat's back: Chat reads the active
  // id once on mount, so poking storage would do nothing at all when
  // you are already on /chat, which is exactly where you use this.
  const thread = useChatThread();
  // Keyed by request_id. A failed decision used to leave the row in
  // place with no message anywhere on screen, which reads exactly like
  // a click that missed.
  const [failed, setFailed] = useState({});
  const timer = useRef(null);

  const load = useCallback(async () => {
    // Each source is read independently. One failing must not blank the
    // whole rail: a rail that disappears when the timeline is slow is
    // worse than a rail with two sections.
    const [a, j, t, c] = await Promise.allSettled([
      apiJson('/api/approvals'),
      apiJson('/api/jobs?limit=40'),
      apiJson('/api/timeline?limit=6'),
      // Verified against the running brain: GET /api/conversations
      // answers {conversations, total, has_more, limit, offset, query}
      // and a row is {id, title, created_at, updated_at, message_count,
      // pinned, preview, title_custom}.
      apiJson('/api/conversations?limit=5&offset=0'),
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
      // Filtered by age BEFORE the slice. Taking the newest five and
      // then filtering would still show nothing on a quiet brain, but
      // taking five unfiltered is what put five-day-old rows under a
      // heading that says "just happened".
      setRecent(recentEnough(t.value?.entries).slice(0, 5));
    }
    if (c.status === 'fulfilled') {
      const rows = c.value?.conversations;
      setThreads(Array.isArray(rows) ? rows.slice(0, 5) : []);
    }
  }, []);

  useEffect(() => {
    load();
    timer.current = setInterval(load, POLL_MS);
    return () => clearInterval(timer.current);
  }, [load]);

  /*
   * Sections fold. On a busy machine the rail is four groups deep and
   * the one you care about is pushed under the ones you do not, and
   * there was no way to put any of them away.
   *
   * The choice is remembered, because a section you closed reopening on
   * every navigation is the same as not having the control.
   */
  const [closed, setClosed] = useState(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem(RAIL_CLOSED_KEY) || '[]'));
    } catch {
      return new Set();
    }
  });

  const openThread = useCallback(async (id) => {
    try {
      await thread?.loadConversation?.(id);
    } catch {
      // Loading failed; still go to Chat rather than leaving the click
      // with no effect. Chat reports its own load errors.
    }
    navigate('/chat');
  }, [navigate, thread]);

  const toggleSection = useCallback((key) => {
    setClosed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key); else next.add(key);
      try {
        localStorage.setItem(RAIL_CLOSED_KEY, JSON.stringify([...next]));
      } catch {
        // Private mode. The rail still folds for this session.
      }
      return next;
    });
  }, []);

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
        <button
          type="button"
          className="v2-rail-head"
          data-tone={needs.length > 0 ? 'needs' : 'plain'}
          aria-expanded={!closed.has('needs')}
          onClick={() => toggleSection('needs')}
        >
          <span>Needs you</span>
          {needs.length > 0 && <span className="v2-rail-n">{needs.length}</span>}
          <ChevronDown
            size={11}
            aria-hidden="true"
            className="v2-rail-caret"
            data-open={closed.has('needs') ? 'no' : 'yes'}
          />
        </button>
        {!closed.has('needs') && needs.length === 0
          && <p className="v2-rail-quiet">Nothing waiting.</p>}
        {!closed.has('needs') && needs.map((n) => (
          <article key={n.request_id} className="v2-rail-card v2-rail-card--needs">
            <button
              type="button"
              className="v2-rail-title"
              onClick={() => navigate('/approvals')}
              title={n.tool_name}
            >
              <ShieldCheck size={13} aria-hidden="true" className="v2-rail-ic" />
              <span className="v2-rail-title-t">{n.tool_name}</span>
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
        <button
          type="button"
          className="v2-rail-head"
          data-tone={running.length > 0 ? 'running' : 'plain'}
          aria-expanded={!closed.has('running')}
          onClick={() => toggleSection('running')}
        >
          <span>Running</span>
          {running.length > 0 && <span className="v2-rail-n">{running.length}</span>}
          <ChevronDown
            size={11}
            aria-hidden="true"
            className="v2-rail-caret"
            data-open={closed.has('running') ? 'no' : 'yes'}
          />
        </button>
        {!closed.has('running') && running.length === 0
          && <p className="v2-rail-quiet">Idle.</p>}
        {!closed.has('running') && running.map((r) => {
          const Icon = iconForJob(r.kind);
          return (
          <article key={r.id} className="v2-rail-card v2-rail-card--running">
            <button
              type="button"
              className="v2-rail-title"
              onClick={() => navigate('/jobs')}
              title={r.name}
            >
              <Icon size={13} aria-hidden="true" className="v2-rail-ic" />
              <span className="v2-rail-title-t">{r.name}</span>
            </button>
            <p className="v2-rail-sub">
              {[r.context_session_id, elapsed(r.started_at)].filter(Boolean).join(' · ')}
            </p>
          </article>
          );
        })}
      </section>

      {/* The design's RECENT: quick access to conversations you were
          just in. The rail had no route back to a thread at all, so
          returning to one meant opening Chat and finding it in a
          picker. These are conversations, not timeline rows, and they
          open the thread rather than a history page. */}
      <section className="v2-rail-sect">
        <button
          type="button"
          className="v2-rail-head"
          aria-expanded={!closed.has('recent')}
          onClick={() => toggleSection('recent')}
        >
          <span>Recent</span>
          {threads.length > 0 && <span className="v2-rail-n">{threads.length}</span>}
          <ChevronDown
            size={11}
            aria-hidden="true"
            className="v2-rail-caret"
            data-open={closed.has('recent') ? 'no' : 'yes'}
          />
        </button>
        {!closed.has('recent') && threads.length === 0
          && <p className="v2-rail-quiet">No conversations yet.</p>}
        {!closed.has('recent') && threads.map((c) => (
          <button
            key={c.id}
            type="button"
            className="v2-rail-recent"
            onClick={() => openThread(c.id)}
            title={c.title || 'Conversation'}
          >
            <MessageSquare size={12} aria-hidden="true" className="v2-rail-ic" />
            <span className="v2-rail-recent-t">{c.title || 'Conversation'}</span>
            {c.updated_at
              ? <span className="v2-rail-recent-w">{elapsed(c.updated_at)}</span>
              : null}
          </button>
        ))}
      </section>

      <section className="v2-rail-sect">
        <button
          type="button"
          className="v2-rail-head"
          aria-expanded={!closed.has('happened')}
          onClick={() => toggleSection('happened')}
        >
          <span>Just happened</span>
          <ChevronDown
            size={11}
            aria-hidden="true"
            className="v2-rail-caret"
            data-open={closed.has('happened') ? 'no' : 'yes'}
          />
        </button>
        {!closed.has('happened') && recent.length === 0
          && <p className="v2-rail-quiet">Nothing in the last few hours.</p>}
        {!closed.has('happened') && recent.map((e, i) => (
          <button
            key={e?.metadata?.id || `${e?.timestamp || ''}-${i}`}
            type="button"
            className="v2-rail-recent"
            onClick={() => navigate('/timeline')}
            title={e?.content || e?.title || ''}
          >
            <MessageSquare size={12} aria-hidden="true" className="v2-rail-ic" />
            <span className="v2-rail-recent-t">{recentTitle(e)}</span>
            {e?.timestamp ? <span className="v2-rail-recent-w">{elapsed(e.timestamp)}</span> : null}
          </button>
        ))}
      </section>
    </aside>
  );
}
