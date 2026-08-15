/**
 * ForYouToday — compact "what I'd suggest today" pane on Home.
 *
 * Renders up to 5 Idea rows from /api/ideas/today with accept +
 * dismiss buttons. Real-time updates arrive on the `ideas_updated`
 * WebSocket event from the IdeasEngine.
 *
 * Action types this surface knows how to handle:
 *   route                     → navigate to action.route
 *   install_routine           → POST /api/routines install
 *   confirm_about_me_fact     → POST /api/about-me/{fact_id}/confirm
 *   resume_consciousness      → POST /api/consciousness/resume
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Lightbulb, Heart, Briefcase, Sparkles, Sun, RefreshCw,
  CheckCircle2, XCircle, ChevronRight,
} from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import EmptyState from '../ui/EmptyState';
import StatusDot from '../ui/StatusDot';
import { apiJson, apiFetch } from '../lib/api';
import { useBrainEvents } from '../hooks/useBrainEvents';

const KIND_ICON = {
  morning: Sun,
  health: Heart,
  work: Briefcase,
  about: Sparkles,
  focus: Lightbulb,
};

const SEVERITY_TONE = {
  info: 'neutral',
  warning: 'warn',
  critical: 'error',
};

const MAX_VISIBLE = 5;

function useIdeas() {
  const [ideas, setIdeas] = useState([]);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const d = await apiJson('/api/ideas/today');
      setIdeas(d?.ideas || []);
      setError(null);
    } catch (e) {
      setError(e?.message || 'failed to fetch ideas');
    }
  }, []);

  const pullRefresh = useCallback(async () => {
    setBusy(true);
    try {
      const r = await apiFetch('/api/ideas/refresh', { method: 'POST' });
      if (r.ok) {
        const d = await r.json();
        setIdeas(d?.today || []);
      }
    } catch (e) {
      setError(e?.message || 'refresh failed');
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  return { ideas, error, busy, refresh, pullRefresh, setIdeas };
}

export default function ForYouToday() {
  const navigate = useNavigate();
  const { ideas, error, busy, refresh, pullRefresh, setIdeas } = useIdeas();
  const [acting, setActing] = useState(() => new Set());

  const wsPushes = useBrainEvents({ types: ['ideas_updated', 'state_push'], limit: 5 });
  useEffect(() => {
    if (!wsPushes || wsPushes.length === 0) return;
    refresh();
  }, [wsPushes, refresh]);

  const runAction = useCallback(async (idea) => {
    const a = idea.action;
    if (!a) return;
    switch (a.kind) {
      case 'route':
        if (a.route) navigate(a.route);
        return;
      case 'install_routine':
        try {
          await apiFetch('/api/routines', {
            method: 'POST',
            body: JSON.stringify({
              routine_id: a.payload?.routine_id || 'wind_down',
              source: 'idea',
            }),
          });
        } catch { /* best-effort, brain may not have routines wired */ }
        return;
      case 'confirm_about_me_fact': {
        const fid = a.payload?.fact_id;
        if (!fid) return;
        await apiFetch(`/api/about-me/${encodeURIComponent(fid)}/confirm`, { method: 'POST' });
        return;
      }
      case 'resume_consciousness': {
        const cid = a.payload?.consciousness_id;
        if (!cid) return;
        await apiFetch('/api/consciousness/resume', {
          method: 'POST', body: JSON.stringify({ id: cid }),
        });
        return;
      }
      default:
        return;
    }
  }, [navigate]);

  const accept = useCallback(async (idea) => {
    const next = new Set(acting); next.add(idea.id); setActing(next);
    try {
      await runAction(idea);
      await apiFetch(`/api/ideas/${encodeURIComponent(idea.id)}/accept`, { method: 'POST' });
      setIdeas((prev) => prev.filter((i) => i.id !== idea.id));
    } finally {
      const done = new Set(acting); done.delete(idea.id); setActing(done);
    }
  }, [acting, runAction, setIdeas]);

  const dismiss = useCallback(async (idea) => {
    const next = new Set(acting); next.add(idea.id); setActing(next);
    try {
      await apiFetch(`/api/ideas/${encodeURIComponent(idea.id)}/dismiss`, { method: 'POST' });
      setIdeas((prev) => prev.filter((i) => i.id !== idea.id));
    } finally {
      const done = new Set(acting); done.delete(idea.id); setActing(done);
    }
  }, [acting, setIdeas]);

  const visible = useMemo(() => ideas.slice(0, MAX_VISIBLE), [ideas]);
  const overflow = Math.max(0, ideas.length - MAX_VISIBLE);

  return (
    <Pane
      title={`For you today${ideas.length ? ` · ${ideas.length}` : ''}`}
      actions={(
        <>
          {overflow > 0 && (
            <Link to="/memory" className="v2-btn v2-btn--ghost">
              + {overflow} more <ChevronRight size={12} />
            </Link>
          )}
          <button
            type="button"
            className="v2-btn v2-btn--ghost"
            onClick={pullRefresh}
            disabled={busy}
            aria-label="Refresh ideas"
            title="Ask the engine to regenerate ideas now"
          >
            <RefreshCw size={13} />
          </button>
        </>
      )}
    >
      <p className="v2-p v2-p--muted">
        Deterministic suggestions from your baselines, paused work, and things you mentioned in chat.
        Accept runs the action; dismiss tells me to weight this signal lower.
      </p>
      {error && <div className="v2-chip v2-chip--error">{error}</div>}

      {visible.length === 0 && !error && (
        <EmptyState
          title="Nothing to suggest yet"
          hint="Keep chatting, let baselines build up, and I'll have ideas for you here by tomorrow."
        />
      )}

      <div className="v2-foryou-list" style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {visible.map((idea) => {
          const Icon = KIND_ICON[idea.kind] || Lightbulb;
          const tone = SEVERITY_TONE[idea.severity] || 'neutral';
          const busyOne = acting.has(idea.id);
          return (
            <Glass key={idea.id} level={0} radius="md" padding="sm" className="v2-foryou-row">
              <header
                style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}
                className="v2-foryou-row-head"
              >
                <Icon size={14} aria-hidden="true" />
                <StatusDot tone={tone} label={`Severity: ${idea.severity || 'unspecified'}`} />
                <span className="v2-chip v2-chip--muted" style={{ textTransform: 'capitalize' }}>
                  {idea.kind}
                </span>
              </header>
              <div className="v2-p" style={{ marginBottom: 8 }}>{idea.text}</div>
              <div className="v2-forge-actions" style={{ display: 'flex', gap: 6 }}>
                <button
                  type="button"
                  className="v2-btn v2-btn--primary"
                  disabled={busyOne}
                  onClick={() => accept(idea)}
                  data-testid={`foryou-accept-${idea.id}`}
                >
                  <CheckCircle2 size={12} /> {idea.action?.verb ? idea.action.verb.replace(/_/g, ' ') : 'Accept'}
                </button>
                <button
                  type="button"
                  className="v2-btn"
                  disabled={busyOne}
                  onClick={() => dismiss(idea)}
                  data-testid={`foryou-dismiss-${idea.id}`}
                >
                  <XCircle size={12} /> Dismiss
                </button>
              </div>
            </Glass>
          );
        })}
      </div>
    </Pane>
  );
}
