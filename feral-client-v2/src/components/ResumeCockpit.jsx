/**
 * ResumeCockpit — the full "Welcome back, you were working on..." pane.
 *
 * Unlike a dismissible banner this is a first-class Home pane that
 * stays visible as long as there's in-flight consciousness state. Each
 * entity is a row with its own action buttons; real-time updates
 * arrive via the /v1/session WebSocket `state_push` events emitted
 * from ConsciousnessStore._emit() on the brain side.
 *
 * Brain routes:
 *   GET  /api/consciousness/state      — full snapshot on mount
 *   POST /api/consciousness/resume     — re-activate a paused entity
 *   POST /api/consciousness/pause      — pause an active entity
 *   POST /api/consciousness/abandon    — mark as abandoned
 *
 * WebSocket events subscribed:
 *   consciousness_record   — upsert of an entity
 *   consciousness_status   — {id, status, at}
 *   consciousness_sweep    — {abandoned, at}
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  PlayCircle, PauseCircle, XCircle, Sparkles, Clock, Brain, Workflow,
  MessageCircle, Radio, Eye, RotateCcw,
} from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import StatusDot from '../ui/StatusDot';
import EmptyState from '../ui/EmptyState';
import { apiJson, apiFetch } from '../lib/api';
import { useBrainEvents } from '../hooks/useBrainEvents';

const KIND_ICON = {
  intent: Sparkles,
  flow: Workflow,
  thought: MessageCircle,
  device_stream: Radio,
  turn: Brain,
};

const STATUS_TONE = {
  active: 'live',
  paused: 'warn',
  waiting_user: 'warn',
  waiting_tool: 'warn',
  completed: 'neutral',
  abandoned: 'off',
};

function formatAge(ts) {
  if (!ts) return '—';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

function describeContext(entity) {
  const c = entity.context_json || {};
  switch (entity.kind) {
    case 'flow':
      if (c.step != null && c.steps != null) {
        return `step ${c.step + 1} of ${c.steps}`;
      }
      return null;
    case 'intent':
      if (c.plan && c.plan.nodes) return `${c.plan.nodes.length} nodes`;
      return null;
    case 'thought':
      return c.text ? `"${String(c.text).slice(0, 120)}…"` : null;
    case 'device_stream':
      if (c.node_id) return `from ${c.node_id}`;
      return null;
    default:
      return null;
  }
}

export default function ResumeCockpit() {
  const [entities, setEntities] = useState([]);
  const [summary, setSummary] = useState('');
  const [busyIds, setBusyIds] = useState(() => new Set());
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const [st, sum] = await Promise.allSettled([
        apiJson('/api/consciousness/state'),
        apiJson('/api/consciousness/summary'),
      ]);
      if (st.status === 'fulfilled') {
        setEntities(st.value?.entities || []);
      }
      if (sum.status === 'fulfilled') {
        setSummary(sum.value?.summary || '');
      }
      setError(null);
    } catch (e) {
      setError(e?.message || 'failed to load consciousness');
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // Real-time updates via the existing /v1/session WebSocket.
  const pushes = useBrainEvents({
    types: ['consciousness_record', 'consciousness_status', 'consciousness_sweep'],
    limit: 20,
  });

  useEffect(() => {
    if (!pushes || pushes.length === 0) return;
    // Any event should prompt a quick refresh; the brain is the
    // source of truth. Debounced naturally by the poll cadence.
    refresh();
  }, [pushes, refresh]);

  const act = useCallback(async (id, which) => {
    const next = new Set(busyIds);
    next.add(id);
    setBusyIds(next);
    try {
      await apiFetch(`/api/consciousness/${which}`, {
        method: 'POST',
        body: JSON.stringify({ id }),
      });
      // Refresh immediately — WS push will also fire but a local
      // optimistic refetch keeps the UI snappy.
      await refresh();
    } finally {
      const done = new Set(busyIds);
      done.delete(id);
      setBusyIds(done);
    }
  }, [busyIds, refresh]);

  const resumeAllPaused = useCallback(async () => {
    const paused = entities.filter((e) => e.status === 'paused');
    for (const e of paused) {
      // Sequential so the brain's orchestrator re-entry ordering
      // matches the UI action order.
      try {
        await apiFetch('/api/consciousness/resume', {
          method: 'POST', body: JSON.stringify({ id: e.id }),
        });
      } catch { /* keep going */ }
    }
    refresh();
  }, [entities, refresh]);

  const grouped = useMemo(() => {
    const byKind = {};
    for (const e of entities) {
      if (!byKind[e.kind]) byKind[e.kind] = [];
      byKind[e.kind].push(e);
    }
    return byKind;
  }, [entities]);

  const hasPaused = entities.some((e) => e.status === 'paused');
  const hasAny = entities.length > 0;

  return (
    <Pane
      title={`Consciousness${hasAny ? ` · ${entities.length}` : ''}`}
      actions={(
        <>
          {hasPaused && (
            <button
              type="button"
              className="v2-btn v2-btn--primary"
              onClick={resumeAllPaused}
              title="Re-activate every paused entity so the brain re-enters execution from its last step"
            >
              <PlayCircle size={13} /> Resume all paused
            </button>
          )}
          <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh} aria-label="Refresh">
            <RotateCcw size={13} />
          </button>
        </>
      )}
    >
      <p className="v2-p v2-p--muted">
        What the agent was doing across restarts. Distinct from "Right now" (live job runtime view):
        consciousness survives reboots, upgrades, and device handoffs so you never lose your place.
      </p>
      {summary && (
        <div className="v2-chip v2-chip--live" style={{ marginBottom: 10 }}>{summary}</div>
      )}
      {error && <div className="v2-chip v2-chip--error">{error}</div>}

      {!hasAny && (
        <EmptyState
          title="Clean slate"
          hint="No in-flight intents, flows, or paused thoughts. Start a TaskFlow or chat thread to see them here."
        />
      )}

      {Object.entries(grouped).map(([kind, rows]) => {
        const Icon = KIND_ICON[kind] || Eye;
        return (
          <div key={kind} className="v2-cockpit-group">
            <div className="v2-cockpit-group-head">
              <Icon size={14} aria-hidden="true" />
              <span className="v2-cockpit-group-label">{kind}</span>
              <span className="v2-chip v2-chip--muted">{rows.length}</span>
            </div>
            <div className="v2-cockpit-rows">
              {rows.map((e) => {
                const tone = STATUS_TONE[e.status] || 'neutral';
                const age = formatAge(e.last_heartbeat_at || e.updated_at || e.created_at);
                const ctx = describeContext(e);
                const busy = busyIds.has(e.id);
                return (
                  <Glass key={e.id} level={0} radius="md" padding="sm" className="v2-cockpit-row">
                    <header className="v2-cockpit-row-head">
                      <StatusDot
                        tone={tone}
                        pulse={e.status === 'active'}
                        label={`${e.summary || e.id.slice(0, 8)}: ${e.status || 'unknown'}`}
                      />
                      <div className="v2-cockpit-row-title">
                        <strong>{e.summary || e.id.slice(0, 8)}</strong>
                        <span className="v2-p v2-p--muted"> · {e.status}</span>
                      </div>
                      <span className="v2-chip v2-chip--muted"><Clock size={10} /> {age}</span>
                    </header>
                    {ctx && <div className="v2-p v2-p--muted" style={{ marginTop: 4 }}>{ctx}</div>}
                    <div className="v2-forge-actions" style={{ marginTop: 8 }}>
                      {e.status !== 'active' && (
                        <button
                          type="button"
                          className="v2-btn v2-btn--primary"
                          disabled={busy}
                          onClick={() => act(e.id, 'resume')}
                        >
                          <PlayCircle size={12} /> Resume
                        </button>
                      )}
                      {e.status === 'active' && (
                        <button
                          type="button"
                          className="v2-btn"
                          disabled={busy}
                          onClick={() => act(e.id, 'pause')}
                        >
                          <PauseCircle size={12} /> Pause
                        </button>
                      )}
                      <button
                        type="button"
                        className="v2-btn"
                        disabled={busy}
                        onClick={() => act(e.id, 'abandon')}
                        title="Drop this entity without resuming. Safe for stale work."
                      >
                        <XCircle size={12} /> Abandon
                      </button>
                    </div>
                  </Glass>
                );
              })}
            </div>
          </div>
        );
      })}
    </Pane>
  );
}
