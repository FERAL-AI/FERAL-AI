import React, { useState } from 'react';
import { Check, Plus, RefreshCw, Target } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Tabs from '../ui/Tabs';
import EmptyState from '../ui/EmptyState';
import ErrorState from '../ui/ErrorState';
import { apiFetch } from '../lib/api';
import { useResource, toApiError } from '../hooks/useResource';

/** Normalise `{key: [...]}` / a bare array / anything else into a list. */
function asList(value, ...keys) {
  for (const key of keys) {
    if (Array.isArray(value?.[key])) return value[key];
  }
  if (Array.isArray(value)) return value;
  return [];
}

/**
 * Intents — short-term plans the orchestrator compiles from user goals.
 * Brain:
 *   POST /api/intents/compile
 *   GET  /api/intents/list
 *   GET  /api/intents/today
 *   POST /api/intents/{plan_id}/complete/{action_id}
 *   GET  /api/intents/stats
 */
export default function Intents() {
  const [tab, setTab] = useState('today');
  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title="Intents"
        actions={(
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { id: 'today', label: 'Today' },
              { id: 'plans', label: 'All plans' },
              { id: 'new', label: 'New plan' },
              { id: 'stats', label: 'Stats' },
            ]}
          />
        )}
      >
        <p className="v2-p v2-p--muted">
          Intents are multi-action plans tied to a goal. Today shows actionable items with progress; Plans lists every active goal.
        </p>
      </Pane>

      {tab === 'today' && <TodayTab />}
      {tab === 'plans' && <PlansTab />}
      {tab === 'new' && <NewPlanTab />}
      {tab === 'stats' && <StatsTab />}
    </div>
  );
}

function TodayTab() {
  // `try { … } finally` with no catch: the rejection escaped as an
  // unhandled promise rejection while `actions` stayed at its initial
  // `[]`, so a dropped request told the user "Nothing planned for
  // today". That is a statement about their plans, made without one.
  const { data, error, loading, refresh } = useResource('/api/intents/today', {
    select: (d) => asList(d, 'actions', 'items'),
  });
  const actions = data || [];
  const [completeError, setCompleteError] = useState(null);

  const complete = async (planId, actionId) => {
    setCompleteError(null);
    try {
      await apiFetch(`/api/intents/${encodeURIComponent(planId)}/complete/${encodeURIComponent(actionId)}`, { method: 'POST' });
      await refresh();
    } catch (err) {
      setCompleteError({ actionId, error: toApiError(err) });
    }
  };

  return (
    <Pane title={data ? `Today (${actions.length})` : 'Today'} actions={<button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}><RefreshCw size={13} /></button>}>
      {loading && !data && <EmptyState title="Loading…" />}
      {error && !data && (
        <ErrorState error={error} what="today's planned actions" onRetry={refresh} />
      )}
      {completeError && (
        <ErrorState
          error={completeError.error}
          what={`the completion of ${completeError.actionId}`}
          hint="The action is still open. Nothing was marked done."
          compact
        />
      )}
      {!loading && !error && data && actions.length === 0 && <EmptyState title="Nothing planned for today" hint="Compile a new plan from a goal." />}
      <ul className="v2-mem-list">
        {actions.map((a) => (
          <li key={a.action_id || a.id}>
            <Glass level={0} radius="md" padding="md">
              <div className="v2-flow-card-head">
                <Target size={13} aria-hidden="true" />
                <div className="v2-flow-card-title">{a.title || a.action || a.text}</div>
                <button
                  type="button"
                  className="v2-btn v2-btn--primary"
                  onClick={() => complete(a.plan_id, a.action_id || a.id)}
                  disabled={a.completed}
                >
                  <Check size={12} /> {a.completed ? 'Done' : 'Mark done'}
                </button>
              </div>
              {a.goal && <div className="v2-mem-meta">Goal: {a.goal}</div>}
            </Glass>
          </li>
        ))}
      </ul>
    </Pane>
  );
}

function PlansTab() {
  // `.then(…).finally(…)` with no catch, so a failure rendered
  // "No plans yet" over every goal the user has ever compiled.
  const { data, error, loading, refresh } = useResource('/api/intents/list', {
    select: (d) => asList(d, 'plans'),
  });
  const plans = data || [];

  return (
    <Pane title={data ? `Plans (${plans.length})` : 'Plans'}>
      {loading && !data && <EmptyState title="Loading…" />}
      {error && !data && (
        <ErrorState error={error} what="the plan list" onRetry={refresh} />
      )}
      {!loading && !error && data && plans.length === 0 && <EmptyState title="No plans yet" />}
      <ul className="v2-mem-list">
        {plans.map((p) => (
          <li key={p.id}>
            <Glass level={0} radius="md" padding="md">
              <div className="v2-flow-card-head">
                <div className="v2-flow-card-title">{p.goal || p.title || p.id}</div>
                <span className="v2-chip">{Math.round((p.progress || 0) * 100)}%</span>
              </div>
              {Array.isArray(p.actions) && (
                <ul className="v2-ambient-list">
                  {p.actions.slice(0, 6).map((a, i) => (
                    <li key={i}>{a.title || a.action || JSON.stringify(a).slice(0, 120)}</li>
                  ))}
                </ul>
              )}
            </Glass>
          </li>
        ))}
      </ul>
    </Pane>
  );
}

function NewPlanTab() {
  const [goal, setGoal] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  // AUDIT-r14 finding 06 fix: backend `/api/intents/compile` reads
  // `body.get("intent")` (intents.py:11). Sending `{goal}` made every
  // compile call return "intent text is required". Now we send
  // `intent` and keep `goal` as a fallback for any older brain build.
  const compile = async (e) => {
    e.preventDefault();
    if (!goal.trim()) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const r = await apiFetch('/api/intents/compile', {
        // intentional spread of multi-shape body — backend will pick
        // whichever field its build understands.
        method: 'POST',
        body: JSON.stringify({ intent: goal.trim(), goal: goal.trim() }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) setError(body?.error || `${r.status}`);
      else setResult(body);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Pane title="New plan">
      <form onSubmit={compile}>
        <textarea
          className="v2-code-editor"
          rows={4}
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="Learn basic Japanese over the next 3 months"
        />
        <div className="v2-forge-actions">
          <button type="submit" className="v2-btn v2-btn--primary" disabled={busy || !goal.trim()}>
            <Plus size={13} /> {busy ? 'Compiling…' : 'Compile plan'}
          </button>
        </div>
      </form>
      {result && <pre className="v2-code">{JSON.stringify(result, null, 2).slice(0, 1600)}</pre>}
      {error && <div className="v2-chip v2-chip--error">{error}</div>}
    </Pane>
  );
}

function StatsTab() {
  // `.catch(() => setStats({}))` swapped a failure for an answer: the
  // page left its loading branch and rendered the "Intent stats"
  // heading over a stat grid the brain never filled in, so every
  // counter it would have reported silently ceased to exist.
  const { data: stats, error, loading, refresh } = useResource('/api/intents/stats');
  if (loading && !stats) return <Pane title="Stats"><EmptyState title="Loading…" /></Pane>;
  if (error && !stats) {
    return (
      <Pane title="Stats">
        <ErrorState
          error={error}
          what="the intent stats"
          hint="No counters are shown rather than zeroes. The client never received these numbers, and a zero here would be an invented measurement."
          onRetry={refresh}
        />
      </Pane>
    );
  }
  const entries = Object.entries(stats && typeof stats === 'object' ? stats : {});
  return (
    <Pane title="Intent stats">
      {entries.length === 0 && (
        <EmptyState
          title="The brain reports no intent counters"
          hint="The stats endpoint answered, and the answer was empty."
        />
      )}
      <div className="v2-grid v2-grid--stats">
        {entries.map(([k, v]) => (
          <Glass key={k} level={1} radius="md" padding="md">
            <div className="v2-stat-label">{k.replace(/_/g, ' ')}</div>
            <div className="v2-stat-value">{typeof v === 'number' ? v : JSON.stringify(v)}</div>
          </Glass>
        ))}
      </div>
    </Pane>
  );
}
