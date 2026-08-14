/**
 * ConsciousnessMindMap — force-directed visualisation of every
 * in-flight ConsciousnessEntity.
 *
 * Each entity is a node; edges connect it to its owner_session_id and
 * to any context-declared node_id / skill_id. Hovering shows the
 * summary; clicking navigates to the right page (flow -> /flows,
 * intent -> /intents, thought -> /chat, device_stream -> /devices).
 *
 * This is the "nobody else does this" visual: live, real-time,
 * showing the agent's operational self-model as a graph. Sourced
 * from the real ConsciousnessStore — no fake nodes, no placeholder
 * edges.
 *
 * Uses a lightweight SVG + simple verlet-style force loop rather
 * than pulling in Three.js / d3-force; the scene is small (≤50
 * nodes typically) and the existing Glass Brain iframe already
 * handles the 3D scene.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import ErrorState from '../ui/ErrorState';
import { useResource } from '../hooks/useResource';
import { useBrainEvents } from '../hooks/useBrainEvents';

const KIND_COLOR = {
  intent: 'var(--v2-state-live)',
  flow: 'var(--v2-text-primary)',
  thought: 'var(--v2-state-warn)',
  device_stream: 'var(--v2-state-live)',
  turn: 'var(--v2-text-secondary)',
};

const STATUS_OPACITY = {
  active: 1,
  paused: 0.6,
  waiting_user: 0.55,
  waiting_tool: 0.55,
  completed: 0.3,
  abandoned: 0.2,
};

const KIND_ROUTE = {
  flow: '/flows',
  intent: '/intents',
  thought: '/chat',
  device_stream: '/devices',
  turn: '/chat',
};

function layout(entities, width, height) {
  // Deterministic radial layout grouped by kind. Session anchor lives
  // in the centre; each kind is a ring at an increasing radius. Good
  // enough for ≤50 entities and keeps the result stable across
  // re-renders (no jitter when a node heartbeats).
  const cx = width / 2;
  const cy = height / 2;
  const kinds = Array.from(new Set(entities.map((e) => e.kind)));
  const radii = {};
  kinds.forEach((k, i) => { radii[k] = 70 + i * 55; });

  const pos = {};
  for (const k of kinds) {
    const group = entities.filter((e) => e.kind === k);
    const step = (2 * Math.PI) / Math.max(1, group.length);
    group.forEach((e, idx) => {
      const angle = idx * step - Math.PI / 2;
      pos[e.id] = {
        x: cx + radii[k] * Math.cos(angle),
        y: cy + radii[k] * Math.sin(angle),
      };
    });
  }
  return { cx, cy, pos, radii };
}

function shortLabel(entity) {
  const s = entity.summary || entity.id;
  return s.length > 22 ? s.slice(0, 21) + '…' : s;
}

export default function ConsciousnessMindMap() {
  const [hovered, setHovered] = useState(null);
  const navigate = useNavigate();
  const containerRef = useRef(null);
  const [size, setSize] = useState({ width: 640, height: 360 });

  // Was: `catch { setEntities([]) }`, which rendered "No in-flight
  // consciousness entities.", the graph claiming the agent is idle
  // when in truth the client never heard back. `data` stays null on
  // failure so the two cases below cannot collapse into one.
  const { data, error, loading, refresh } = useResource(
    '/api/consciousness/state?include_abandoned=false',
    { select: (d) => (Array.isArray(d?.entities) ? d.entities : []) },
  );
  const entities = useMemo(() => data || [], [data]);

  // Real-time WebSocket push via the existing event bus.
  const pushes = useBrainEvents({
    types: ['consciousness_record', 'consciousness_status', 'consciousness_sweep'],
    limit: 20,
  });
  useEffect(() => {
    if (pushes && pushes.length > 0) refresh();
  }, [pushes, refresh]);

  // Track container size so the SVG scales with the pane.
  useEffect(() => {
    if (!containerRef.current || typeof ResizeObserver === 'undefined') return;
    const el = containerRef.current;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const { width } = entry.contentRect;
        const height = Math.max(280, Math.min(560, width * 0.55));
        setSize({ width: Math.max(320, width), height });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const { cx, cy, pos, radii } = useMemo(
    () => layout(entities, size.width, size.height),
    [entities, size.width, size.height],
  );

  const edges = useMemo(() => {
    const list = [];
    for (const e of entities) {
      const p = pos[e.id];
      if (!p) continue;
      // Every entity links to the brain-centre anchor.
      list.push({ from: { x: cx, y: cy }, to: p, id: `center-${e.id}`, faint: true });
      // context_json.node_id / skill_id cross-links when present.
      const ctx = e.context_json || {};
      const linkId = ctx.node_id || ctx.skill_id || ctx.device_id;
      if (linkId) {
        const target = entities.find((other) => other.id === linkId)
          || entities.find((other) => (other.context_json || {}).node_id === linkId);
        if (target && pos[target.id]) {
          list.push({ from: p, to: pos[target.id], id: `link-${e.id}-${target.id}`, faint: false });
        }
      }
    }
    return list;
  }, [entities, pos, cx, cy]);

  const hoveredEntity = hovered ? entities.find((e) => e.id === hovered) : null;

  // "We could not ask", never the empty state below. Same container
  // geometry so the pane does not jump, but the message is an error,
  // not a statement about how busy the agent is.
  if (error && !data) {
    return (
      <div
        ref={containerRef}
        className="v2-mindmap v2-mindmap--empty"
        data-testid="consciousness-mindmap"
      >
        <ErrorState
          error={error}
          what="the consciousness graph"
          hint="The consciousness store did not answer. FERAL may well be mid-flow right now; this pane simply cannot see it."
          onRetry={refresh}
        />
      </div>
    );
  }

  if (loading && !data) {
    return (
      <div
        ref={containerRef}
        className="v2-mindmap v2-mindmap--empty"
        data-testid="consciousness-mindmap"
      >
        <div className="v2-mindmap-empty v2-p v2-p--muted">
          Reading the consciousness store…
        </div>
      </div>
    );
  }

  // Empty state renders ONLY the centred message — no SVG centre dot,
  // no ambient orbs, no kind-ring guides. Painting the FERAL anchor
  // node when there is nothing to anchor used to overlap the prompt
  // text and looked like a stray bug.
  if (entities.length === 0) {
    return (
      <div
        ref={containerRef}
        className="v2-mindmap v2-mindmap--empty"
        data-testid="consciousness-mindmap"
      >
        <div className="v2-mindmap-empty v2-p v2-p--muted">
          No in-flight consciousness entities. Start a TaskFlow, intent, or chat to see the graph come alive.
        </div>
      </div>
    );
  }

  return (
    <div ref={containerRef} className="v2-mindmap" data-testid="consciousness-mindmap">
      <svg
        width={size.width}
        height={size.height}
        className="v2-mindmap-svg"
        role="img"
        aria-label="Consciousness mind map"
      >
        <defs>
          <radialGradient id="v2-mindmap-core" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="var(--v2-surface-1)" stopOpacity="0.95" />
            <stop offset="100%" stopColor="var(--v2-surface-0)" stopOpacity="0.0" />
          </radialGradient>
        </defs>
        {/* centre nebula */}
        <circle cx={cx} cy={cy} r={44} fill="url(#v2-mindmap-core)" />
        <circle
          cx={cx} cy={cy} r={10}
          fill="var(--v2-accent)"
          opacity={0.85}
        />
        <text x={cx} y={cy + 30} textAnchor="middle" className="v2-mindmap-label" opacity={0.65}>
          FERAL
        </text>

        {/* Kind ring guides so the structure is legible even with few nodes. */}
        {Object.entries(radii).map(([kind, r]) => (
          <circle
            key={`ring-${kind}`}
            cx={cx} cy={cy} r={r}
            fill="none"
            stroke="var(--v2-border-subtle)"
            strokeDasharray="4 6"
            strokeOpacity={0.3}
          />
        ))}

        {/* edges */}
        {edges.map((edge) => (
          <line
            key={edge.id}
            x1={edge.from.x} y1={edge.from.y}
            x2={edge.to.x} y2={edge.to.y}
            stroke={edge.faint ? 'var(--v2-border-subtle)' : 'var(--v2-accent)'}
            strokeOpacity={edge.faint ? 0.25 : 0.55}
            strokeWidth={edge.faint ? 1 : 1.5}
          />
        ))}

        {/* nodes */}
        {entities.map((e) => {
          const p = pos[e.id];
          if (!p) return null;
          const opacity = STATUS_OPACITY[e.status] ?? 0.5;
          const color = KIND_COLOR[e.kind] || 'var(--v2-text-primary)';
          const r = hovered === e.id ? 12 : 8;
          return (
            <g
              key={e.id}
              role="button"
              tabIndex={0}
              onMouseEnter={() => setHovered(e.id)}
              onMouseLeave={() => setHovered((h) => (h === e.id ? null : h))}
              onFocus={() => setHovered(e.id)}
              onBlur={() => setHovered((h) => (h === e.id ? null : h))}
              onClick={() => {
                const route = KIND_ROUTE[e.kind] || '/';
                navigate(route);
              }}
              style={{ cursor: 'pointer' }}
            >
              <circle
                cx={p.x} cy={p.y} r={r}
                fill={color}
                opacity={opacity}
              />
              {e.status === 'active' && (
                <circle
                  cx={p.x} cy={p.y} r={r + 4}
                  fill="none"
                  stroke={color}
                  strokeOpacity={0.35}
                >
                  <animate attributeName="r" from={r + 4} to={r + 14} dur="2s" repeatCount="indefinite" />
                  <animate attributeName="opacity" from="0.35" to="0" dur="2s" repeatCount="indefinite" />
                </circle>
              )}
              <text
                x={p.x} y={p.y - r - 4}
                textAnchor="middle"
                className="v2-mindmap-label"
                opacity={opacity}
              >
                {shortLabel(e)}
              </text>
            </g>
          );
        })}
      </svg>
      {hoveredEntity && (
        <div className="v2-mindmap-tooltip" role="status" aria-live="polite">
          <div className="v2-chip v2-chip--muted" style={{ marginBottom: 4 }}>
            {hoveredEntity.kind} · {hoveredEntity.status}
          </div>
          <div style={{ fontWeight: 600 }}>{hoveredEntity.summary || hoveredEntity.id}</div>
          {hoveredEntity.owner_session_id && (
            <div className="v2-p v2-p--muted" style={{ marginTop: 4, fontSize: 11 }}>
              session {hoveredEntity.owner_session_id.slice(0, 8)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
