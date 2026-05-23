import React, { useCallback, useEffect, useState } from 'react';
import { Check, Plus, RefreshCw, Target } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Tabs from '../ui/Tabs';
import Modal from '../ui/Modal';
import EmptyState from '../ui/EmptyState';
import { apiJson, apiFetch } from '../lib/api';

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
  const [actions, setActions] = useState([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const d = await apiJson('/api/intents/today');
      setActions(d.actions || d.items || []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const complete = async (planId, actionId) => {
    await apiFetch(`/api/intents/${encodeURIComponent(planId)}/complete/${encodeURIComponent(actionId)}`, { method: 'POST' });
    refresh();
  };

  return (
    <Pane title={`Today (${actions.length})`} actions={<button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}><RefreshCw size={13} /></button>}>
      {loading && <EmptyState title="Loading…" />}
      {!loading && actions.length === 0 && <EmptyState title="Nothing planned for today" hint="Compile a new plan from a goal." />}
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
  const [plans, setPlans] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiJson('/api/intents/list').then((d) => setPlans(d.plans || d || [])).finally(() => setLoading(false));
  }, []);

  return (
    <Pane title={`Plans (${plans.length})`}>
      {loading && <EmptyState title="Loading…" />}
      {!loading && plans.length === 0 && <EmptyState title="No plans yet" />}
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
  const [stats, setStats] = useState(null);
  useEffect(() => { apiJson('/api/intents/stats').then(setStats).catch(() => setStats({})); }, []);
  if (!stats) return <Pane title="Stats"><EmptyState title="Loading…" /></Pane>;
  return (
    <Pane title="Intent stats">
      <div className="v2-grid v2-grid--stats">
        {Object.entries(stats).map(([k, v]) => (
          <Glass key={k} level={1} radius="md" padding="md">
            <div className="v2-stat-label">{k.replace(/_/g, ' ')}</div>
            <div className="v2-stat-value">{typeof v === 'number' ? v : JSON.stringify(v)}</div>
          </Glass>
        ))}
      </div>
    </Pane>
  );
}
