/**
 * Oversight — live supervisor-event river.
 *
 * Reads GET /api/supervisor/events every 4 s, mounts a live WS
 * subscriber for `supervisor_event` frames so new rows land instantly,
 * and exposes the kill switch (POST /api/supervisor/pause).
 *
 * Filters: source (web / node / voice / cron / channel / proactive /
 * twin / …), actor (user / twin / system), decision (allowed / denied /
 * queued / error).
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { RefreshCw, Pause, Play, Shield, Clock, Filter, AlertTriangle } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import EmptyState from '../ui/EmptyState';
import StatusDot from '../ui/StatusDot';
import BackButton from '../ui/BackButton';
import { useFeralSocket } from '../hooks/useFeralSocket';
import { apiJson, apiFetch } from '../lib/api';

const SOURCE_OPTIONS = ['', 'web', 'voice', 'node', 'cron', 'channel', 'proactive', 'twin', 'ui'];
const DECISION_OPTIONS = ['', 'allowed', 'denied', 'queued', 'error'];

const DECISION_TONE = {
  allowed: 'live',
  denied: 'error',
  queued: 'warn',
  error: 'error',
};

/**
 * Local error surface. `src/ui/` has no shared error component yet and
 * another agent may be adding one, so this stays inside the page. Colours
 * come from tokens.css; nothing here hard-codes a hex.
 */
const ERROR_BOX_STYLE = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 8,
  marginTop: 10,
  padding: '8px 12px',
  border: '1px solid var(--v2-state-error-soft)',
  borderRadius: 'var(--v2-radius-sm)',
  background: 'var(--v2-state-error-soft)',
  color: 'var(--v2-state-error)',
  fontSize: 'var(--v2-size-sm)',
  lineHeight: 1.45,
};

const ERROR_TITLE_STYLE = { fontWeight: 600 };

/**
 * Turn an ApiError into a sentence that names the cause. `ApiError` carries
 * `status`, `code`, `reason` and `detail` (see lib/api.js); a 503 from
 * api/routes/supervisor.py means the Supervisor never initialised, so say so
 * rather than showing a bare status number.
 */
function describeCause(err) {
  const status = Number(err?.status) || 0;
  const detail = String(err?.detail || err?.reason || err?.message || '').trim();
  if (status === 503) {
    return `The Supervisor is unreachable (HTTP 503${detail ? `, ${detail}` : ''}).`;
  }
  if (status) {
    return `The request failed (HTTP ${status}${detail ? `, ${detail}` : ''}).`;
  }
  return detail ? `${detail}.` : 'The request failed.';
}

function relativeTime(ts) {
  if (!ts) return '—';
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return `${Math.max(0, Math.round(diff))}s ago`;
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  return `${Math.round(diff / 3600)}h ago`;
}

export default function Oversight() {
  const socket = useFeralSocket();
  const [events, setEvents] = useState([]);
  const [stats, setStats] = useState(null);
  const [filters, setFilters] = useState({ source: '', decision: '', actor: '' });
  const [paused, setPaused] = useState(false);
  const [loading, setLoading] = useState(true);
  // "We asked and there is nothing" and "we could not ask" are different
  // states. eventsError holds the second one so the river never renders an
  // empty audit log it never actually read.
  const [eventsError, setEventsError] = useState(null);
  // statsError means the paused pill below is last-known, not confirmed.
  const [statsError, setStatsError] = useState(null);
  // pauseError is the kill switch itself failing to apply.
  const [pauseError, setPauseError] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      params.set('limit', '80');
      if (filters.source) params.set('source', filters.source);
      if (filters.decision) params.set('decision', filters.decision);
      if (filters.actor) params.set('actor', filters.actor);
      try {
        const d = await apiJson(`/api/supervisor/events?${params.toString()}`);
        setEvents(d?.events || []);
        setEventsError(null);
      } catch (err) {
        setEventsError(describeCause(err));
      }
      try {
        const s = await apiJson('/api/supervisor/stats');
        setStats(s);
        setPaused(!!s.paused);
        // A successful stats read is fresh truth about the kill switch, so
        // any earlier "could not confirm" / "did not apply" notice is stale.
        setStatsError(null);
        setPauseError(null);
      } catch (err) {
        setStatsError(describeCause(err));
      }
    } finally {
      setLoading(false);
    }
  }, [filters]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 4000);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    const unsub = socket.subscribe((msg) => {
      if (!msg || msg.type !== 'supervisor_event') return;
      const payload = msg.payload || msg;
      setEvents((prev) => [{ ...payload }, ...prev].slice(0, 200));
    });
    return unsub;
  }, [socket]);

  /**
   * The kill switch. Never claim a halt we have not been told happened:
   * the pill and the button follow the `paused` value in the response body
   * (api/routes/supervisor.py returns `{"paused": sup.paused}`), and a failed
   * request rolls the control back to the value it had before the click.
   */
  const togglePause = async () => {
    const previous = paused;
    const next = !paused;
    setPauseError(null);
    try {
      const res = await apiFetch('/api/supervisor/pause', {
        method: 'POST',
        body: JSON.stringify({ paused: next }),
      });
      const body = await res.json().catch(() => null);
      // Server truth wins. Only fall back to the requested value if the body
      // carried no `paused` field at all.
      setPaused(typeof body?.paused === 'boolean' ? body.paused : next);
      refresh();
    } catch (err) {
      setPaused(previous);
      setPauseError(
        `${next ? 'Pause' : 'Resume'} was not applied, the Brain is still ${previous ? 'paused' : 'running'}. ${describeCause(err)}`,
      );
    }
  };

  // A kill switch that did not apply is more urgent than an unconfirmed pill.
  const controlError = pauseError
    || (statsError ? `Paused state not confirmed. ${statsError}` : null);

  const filtered = useMemo(() => {
    if (!filters.source && !filters.decision && !filters.actor) return events;
    return events.filter((e) => (
      (!filters.source || e.source === filters.source)
      && (!filters.decision || e.decision === filters.decision)
      && (!filters.actor || e.actor === filters.actor)
    ));
  }, [events, filters]);

  return (
    <div className="v2-page v2-page--stack v2-oversight" data-testid="v2-marker">
      <Pane
        title="Oversight"
        leading={<BackButton />}
        actions={(
          <>
            <button
              type="button"
              className={`v2-btn${paused ? ' v2-btn--primary' : ' v2-btn--ghost'}`}
              onClick={togglePause}
              title="Pause every orchestrator call — the kill switch."
            >
              {paused ? <><Play size={13} /> Resume</> : <><Pause size={13} /> Pause actions</>}
            </button>
            <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh} aria-label="Refresh">
              <RefreshCw size={13} />
            </button>
          </>
        )}
      >
        <p className="v2-p v2-p--muted">
          Every command the Brain acts on passes through the Supervisor.
          Web chat, voice, HUP nodes, cron, channels, proactive alerts,
          and the digital twin all land here as audit rows. Use Pause to
          halt every outgoing action immediately.
        </p>

        {controlError && (
          <div style={ERROR_BOX_STYLE} role="alert" data-testid="oversight-control-error">
            <AlertTriangle size={14} aria-hidden="true" style={{ flexShrink: 0, marginTop: 2 }} />
            <span>{controlError}</span>
          </div>
        )}

        <div className="v2-oversight-stats">
          <StatPill label="Total" value={stats?.total ?? '—'} />
          <StatPill
            label="Paused"
            value={paused ? 'yes' : 'no'}
            tone={paused ? 'warn' : 'live'}
            title={statsError
              ? 'Last known value. The Supervisor could not be reached to confirm it.'
              : undefined}
          />
          {stats?.by_source && Object.entries(stats.by_source).slice(0, 6).map(([src, n]) => (
            <StatPill key={src} label={src} value={n} />
          ))}
        </div>

        <div className="v2-oversight-filters">
          <Filter size={13} aria-hidden="true" />
          <Select
            label="source"
            value={filters.source}
            onChange={(v) => setFilters((f) => ({ ...f, source: v }))}
            options={SOURCE_OPTIONS}
          />
          <Select
            label="decision"
            value={filters.decision}
            onChange={(v) => setFilters((f) => ({ ...f, decision: v }))}
            options={DECISION_OPTIONS}
          />
          <input
            className="v2-input"
            style={{ minWidth: 120 }}
            placeholder="actor (user / twin / system)"
            value={filters.actor}
            onChange={(e) => setFilters((f) => ({ ...f, actor: e.target.value }))}
          />
        </div>
      </Pane>

      <Pane title={`Events · ${filtered.length}`}>
        {eventsError && (
          <div style={ERROR_BOX_STYLE} role="alert" data-testid="oversight-events-error">
            <AlertTriangle size={14} aria-hidden="true" style={{ flexShrink: 0, marginTop: 2 }} />
            <div>
              <div style={ERROR_TITLE_STYLE}>Audit log unavailable</div>
              <div>{eventsError}</div>
              <div>This is not an empty log. The events below, if any, may be stale.</div>
            </div>
          </div>
        )}
        {loading && !eventsError && filtered.length === 0 && <EmptyState title="Loading…" />}
        {!loading && !eventsError && filtered.length === 0 && (
          <EmptyState title="No events match this filter" />
        )}
        <ul className="v2-oversight-list">
          {filtered.map((ev) => (
            <li key={ev.event_id} className="v2-oversight-row">
              <Glass level={0} radius="md" padding="sm">
                <div className="v2-oversight-row-head">
                  <StatusDot tone={DECISION_TONE[ev.decision] || 'neutral'} />
                  <span className="v2-chip v2-chip--muted">{ev.source}</span>
                  <span className="v2-chip v2-chip--muted">{ev.kind}</span>
                  <span className="v2-chip v2-chip--muted" title={ev.actor}>by {ev.actor}</span>
                  <span className="v2-chip v2-chip--muted"><Clock size={10} /> {relativeTime(ev.ts)}</span>
                  <span className="v2-chip v2-chip--muted">{ev.latency_ms}ms</span>
                  <span className="v2-oversight-spacer" />
                  <span
                    className={`v2-chip v2-chip--${DECISION_TONE[ev.decision] === 'live' ? 'live' : DECISION_TONE[ev.decision] === 'error' ? 'error' : 'warn'}`}
                  >
                    {ev.decision}
                  </span>
                </div>
                <div className="v2-oversight-row-body">
                  <Shield size={11} aria-hidden="true" />
                  <code>{ev.payload_summary || '—'}</code>
                </div>
                <div className="v2-oversight-row-meta">
                  session {(ev.session_id || '').slice(0, 8) || '—'} · hash {ev.payload_hash}
                </div>
              </Glass>
            </li>
          ))}
        </ul>
      </Pane>
    </div>
  );
}

function Select({ label, value, onChange, options }) {
  return (
    <label className="v2-oversight-select">
      <span>{label}</span>
      <select className="v2-input" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o} value={o}>{o || 'any'}</option>
        ))}
      </select>
    </label>
  );
}

function StatPill({ label, value, tone, title }) {
  return (
    <Glass level={0} radius="md" padding="sm" className="v2-oversight-stat" title={title}>
      <div className="v2-stat-label">{label}</div>
      <div className={`v2-stat-value${tone ? ` is-${tone}` : ''}`}>{value}</div>
    </Glass>
  );
}
