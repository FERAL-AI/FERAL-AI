import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Activity, Cpu, Layers, RefreshCw, Radio, Zap, Brain, Shield } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import ConsciousnessMindMap from '../components/ConsciousnessMindMap';
import { useFeralSocket } from '../hooks/useFeralSocket';
import { apiJson } from '../lib/api';
import { useSystemHealth, refreshSystemHealth } from '../hooks/useSystemHealth';

/**
 * Glass Brain v2 — a live, introspective view of everything alive inside
 * FERAL right now. No iframes, no v1 embeds. One pure native surface:
 *   • system vitals (brain / sessions / skills / devices / load)
 *   • consciousness mind-map of every in-flight ConsciousnessEntity
 *   • entity kind legend with live counts
 *   • raw event stream (WS frames) for debugging
 */

const KIND_LABELS = {
  intent: 'Intents',
  flow: 'Flows',
  thought: 'Thoughts',
  device_stream: 'Device streams',
  turn: 'Turns',
};

const KIND_DOT = {
  intent: 'var(--v2-state-live)',
  flow: 'var(--v2-text-primary)',
  thought: 'var(--v2-state-warn)',
  device_stream: 'var(--v2-state-live)',
  turn: 'var(--v2-text-secondary)',
};

export default function GlassBrain() {
  const socket = useFeralSocket();
  const [events, setEvents] = useState([]);
  const [summary, setSummary] = useState({ total: 0, byKind: {}, byStatus: {} });
  // AUDIT-r14 finding 03 dedup: the page used to mount its own 8s
  // /api/dashboard poll on top of Home's 15s + Shell's 10s. Now it
  // subscribes to the single shared store via useSystemHealth; only
  // consciousness/state stays page-local because no other surface
  // consumes it.
  const { data: dashboard, error: dashboardError } = useSystemHealth();
  const [refreshKey, setRefreshKey] = useState(0);
  const [consErr, setConsErr] = useState('');

  useEffect(() => {
    const unsub = socket.subscribe((msg) => {
      if (!msg || typeof msg !== 'object') return;
      setEvents((prev) => [{ id: Date.now() + Math.random(), msg }, ...prev].slice(0, 120));
    });
    return unsub;
  }, [socket]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const cons = await apiJson('/api/consciousness/state?include_abandoned=false', { silent: true });
        if (cancelled) return;
        if (cons && Array.isArray(cons.entities)) {
          const byKind = {};
          const byStatus = {};
          for (const e of cons.entities) {
            byKind[e.kind] = (byKind[e.kind] || 0) + 1;
            byStatus[e.status] = (byStatus[e.status] || 0) + 1;
          }
          setSummary({ total: cons.entities.length, byKind, byStatus });
        }
      } catch (e) {
        setConsErr(e?.message || 'consciousness fetch failed');
      }
    }
    load();
    const id = setInterval(load, 8000);
    return () => { cancelled = true; clearInterval(id); };
  }, [refreshKey]);

  const vitals = useMemo(() => {
    const d = dashboard || {};
    // AUDIT-r14 finding 03 fix #4: the prior Brain tile checked
    // `d.health?.status === 'ok'` but `/api/dashboard` returns
    // `health` as a vitals map (heart_rate, hrv, …) with no `status`
    // key, so the tile rendered "—" forever. The brain is "online"
    // when the shared system-health store has a non-error snapshot;
    // no need for a string check inside `health`.
    const brainOnline = !dashboardError && !!dashboard;
    return [
      { icon: Cpu, label: 'Brain', value: brainOnline ? 'online' : (dashboardError?.detail || 'offline'), tone: brainOnline ? 'live' : 'warn' },
      { icon: Activity, label: 'In-flight', value: summary.total, tone: summary.total > 0 ? 'live' : 'muted' },
      { icon: Layers, label: 'Sessions', value: d.session_count ?? '—' },
      { icon: Radio, label: 'Devices', value: d.device_count ?? '—' },
      { icon: Zap, label: 'Skills', value: d.skill_count ?? d.skills_count ?? (Array.isArray(d.skills) ? d.skills.length : '—') },
    ];
  }, [dashboard, dashboardError, summary]);

  const hasNodes = summary.total > 0;
  // Legend rows: when at least one entity is in flight we anchor the
  // baseline kinds (intent/flow) so the colour key stays stable while
  // the graph fills in. With zero entities there is nothing to legend,
  // and rendering the dots used to bleed coloured `border-radius: 50%`
  // pills into the centred empty-state prompt — see
  // GlassBrain.empty-state.test.jsx.
  const kindRows = useMemo(
    () => (hasNodes
      ? Object.keys(KIND_LABELS)
        .map((k) => ({ kind: k, label: KIND_LABELS[k], count: summary.byKind[k] || 0 }))
        .filter((r) => r.count > 0 || r.kind === 'intent' || r.kind === 'flow')
      : []),
    [summary, hasNodes],
  );

  return (
    <div className="v2-page v2-page--stack v2-glass-brain" data-testid="v2-marker">
      <Pane
        title="Glass Brain"
        actions={(
          <>
            <Link to="/oversight" className="v2-btn v2-btn--ghost" title="Supervisor audit + kill switch">
              <Shield size={13} /> Oversight
            </Link>
            <Link to="/memory/context" className="v2-btn v2-btn--ghost" title="Inspect what multi-memory surfaced per LLM turn">
              <Brain size={13} /> Memory context
            </Link>
            <button
              type="button"
              className="v2-btn"
              onClick={() => { setRefreshKey((k) => k + 1); refreshSystemHealth(); }}
              title="Refresh"
            >
              <RefreshCw size={13} /> Refresh
            </button>
          </>
        )}
      >
        <p className="v2-p v2-p--muted">
          A window into the agent's operational self-model. Every node is a live
          <em> ConsciousnessEntity</em> — an intent, flow, paused thought, or device stream
          FERAL is currently aware of. Edges connect them to the session, skill, or device
          that owns them. Nothing here is mocked; it all comes from the consciousness store
          and WebSocket event bus in real time.
        </p>

        <div className="v2-glass-brain-vitals">
          {vitals.map(({ icon: Icon, label, value, tone }) => (
            <Glass key={label} level={1} radius="md" padding="sm" className="v2-glass-brain-vital">
              <div className="v2-glass-brain-vital-icon"><Icon size={14} /></div>
              <div className="v2-glass-brain-vital-text">
                <div className="v2-glass-brain-vital-label">{label}</div>
                <div className={`v2-glass-brain-vital-value${tone ? ` is-${tone}` : ''}`}>{value}</div>
              </div>
            </Glass>
          ))}
        </div>
      </Pane>

      <Pane
        title="Consciousness mind-map"
        actions={hasNodes ? (
          <div className="v2-glass-brain-legend" aria-label="Entity legend">
            {kindRows.map(({ kind, label, count }) => (
              <span key={kind} className="v2-glass-brain-legend-row" title={`${count} ${label.toLowerCase()} in flight`}>
                <span
                  className="v2-glass-brain-legend-dot"
                  style={{ background: KIND_DOT[kind] || 'var(--v2-text-tertiary)' }}
                  aria-hidden="true"
                />
                <span className="v2-glass-brain-legend-label">{label}</span>
                <span className="v2-glass-brain-legend-count">{count}</span>
              </span>
            ))}
          </div>
        ) : null}
      >
        <ConsciousnessMindMap />
      </Pane>

      <Pane title="Event stream">
        <p className="v2-p v2-p--muted">
          Every WebSocket frame emitted by the Brain lands here in real time.
          Useful for debugging flow state and agent handoffs.
        </p>
        <Glass level={0} radius="md" padding="md">
          <ul className="v2-event-log">
            {events.length === 0 && <li className="v2-empty">Listening…</li>}
            {events.map(({ id, msg }) => (
              <li key={id} className="v2-event-row">
                <span className="v2-event-type">{msg.type || msg.hop || 'event'}</span>
                <span className="v2-event-body">{JSON.stringify(msg.payload || msg).slice(0, 200)}</span>
              </li>
            ))}
          </ul>
        </Glass>
      </Pane>
    </div>
  );
}
