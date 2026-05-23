import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw, Sparkles, CheckCircle2, XCircle, Zap } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Tabs from '../ui/Tabs';
import EmptyState from '../ui/EmptyState';
import StatusDot from '../ui/StatusDot';
import { apiJson, apiFetch } from '../lib/api';

/**
 * Forge — Tool Genesis full surface.
 *   Proposals | Pending | Generated | Stats + a Generate panel.
 */
export default function Forge() {
  const [tab, setTab] = useState('pending');
  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title="Forge · Tool Genesis"
        actions={(
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { id: 'pending', label: 'Pending' },
              { id: 'proposals', label: 'Proposals' },
              { id: 'generated', label: 'Generated' },
              { id: 'generate', label: 'Generate' },
              { id: 'stats', label: 'Stats' },
            ]}
          />
        )}
      >
        <p className="v2-p v2-p--muted">
          When no existing skill fits a request, FERAL drafts one, sandbox-runs it, and surfaces it here.
          Approve to promote permanently, reject to discard. Stats show runtime growth over time.
        </p>
      </Pane>
      {tab === 'pending' && <PendingTab />}
      {tab === 'proposals' && <ProposalsTab />}
      {tab === 'generated' && <GeneratedTab />}
      {tab === 'generate' && <GenerateTab />}
      {tab === 'stats' && <StatsTab />}
    </div>
  );
}

function PendingTab() {
  const [pending, setPending] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const [tg, sk] = await Promise.allSettled([
        apiJson('/api/tool-genesis/pending'),
        apiJson('/api/skills/pending'),
      ]);
      const merged = [];
      if (tg.status === 'fulfilled') merged.push(...(tg.value?.pending || tg.value || []));
      if (sk.status === 'fulfilled') {
        const arr = sk.value?.pending || sk.value || [];
        for (const item of arr) {
          if (!merged.some((m) => (m.id || m.skill_id) === (item.id || item.skill_id))) {
            merged.push(item);
          }
        }
      }
      setPending(merged);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // AUDIT-r14 finding 05 fix: tool-genesis backend reads
  // `body.get("tool_id")` (api/routes/tool_genesis.py:73), not `id` /
  // `skill_id`. The reject ordering used to try `/api/skills/reject`
  // FIRST which accepted the wrong-shape payload and returned ok,
  // so tool-genesis drafts were never actually rejected on the
  // tool_genesis side. Reordered to try the tool-genesis route first,
  // and send all three shapes so the legacy skills approve/reject
  // still works for plain-skill rows.
  const decide = async (id, approve) => {
    setBusy(id);
    try {
      const path = approve
        ? ['/api/tool-genesis/approve', '/api/skills/approve']
        : ['/api/tool-genesis/reject', '/api/skills/reject'];
      let ok = false;
      let lastErr = '';
      for (const p of path) {
        try {
          const r = await apiFetch(p, {
            method: 'POST',
            body: JSON.stringify({ tool_id: id, id, skill_id: id }),
          });
          if (r.ok) { ok = true; break; }
          const body = await r.json().catch(() => ({}));
          lastErr = body?.detail || body?.error || `${r.status}`;
        } catch (e) {
          lastErr = e?.message || 'request failed';
        }
      }
      if (!ok) throw new Error(lastErr || 'No approve endpoint accepted the id');
      await refresh();
    } finally {
      setBusy(null);
    }
  };

  return (
    <Pane title={`Pending (${pending.length})`} actions={<button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}><RefreshCw size={13} /></button>}>
      {loading && <EmptyState title="Loading drafts…" />}
      {!loading && pending.length === 0 && (
        <EmptyState
          title="No drafts pending"
          hint="When you ask FERAL something none of the 25 skills can do, a draft appears here."
        />
      )}
      <ul className="v2-forge-list">
        {pending.map((d) => {
          const id = d.id || d.skill_id || d.draft_id;
          return (
            <li key={id} className="v2-forge-item">
              <Glass level={1} radius="md" padding="md">
                <header className="v2-forge-head">
                  <h3 className="v2-forge-title">{d.name || d.skill_id || id || 'Untitled'}</h3>
                  <span className={`v2-chip v2-chip--${d.autonomy_tier || 'hybrid'}`}>
                    {d.autonomy_tier || d.approval_mode || 'hybrid'}
                  </span>
                </header>
                <p className="v2-p v2-p--muted">{d.description || d.reason || '—'}</p>
                {d.ast_gate && (
                  <div className="v2-forge-meta">
                    AST gate: <strong>{d.ast_gate.passed ? 'passed' : 'failed'}</strong>
                    {d.ast_gate.issues && <span> · {d.ast_gate.issues.length} issues</span>}
                  </div>
                )}
                {(d.code || d.manifest?.code) && (
                  <pre className="v2-code">{String(d.code || d.manifest.code).slice(0, 2000)}</pre>
                )}
                {d.sandbox_result && (
                  <pre className="v2-code">{typeof d.sandbox_result === 'string' ? d.sandbox_result.slice(0, 1200) : JSON.stringify(d.sandbox_result, null, 2).slice(0, 1200)}</pre>
                )}
                <div className="v2-forge-actions">
                  <button type="button" className="v2-btn v2-btn--primary" onClick={() => decide(id, true)} disabled={busy === id}>
                    <CheckCircle2 size={13} /> Approve
                  </button>
                  <button type="button" className="v2-btn" onClick={() => decide(id, false)} disabled={busy === id}>
                    <XCircle size={13} /> Reject
                  </button>
                </div>
              </Glass>
            </li>
          );
        })}
      </ul>
    </Pane>
  );
}

function ProposalsTab() {
  const [proposals, setProposals] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiJson('/api/tool-genesis/proposals')
      .then((d) => setProposals(d.proposals || d || []))
      .finally(() => setLoading(false));
  }, []);

  return (
    <Pane title={`Proposals (${proposals.length})`}>
      <p className="v2-p v2-p--muted">Capability gaps the orchestrator detected but hasn't drafted yet.</p>
      {loading && <EmptyState title="Loading…" />}
      {!loading && proposals.length === 0 && <EmptyState title="No capability gaps tracked" />}
      <ul className="v2-forge-list">
        {proposals.map((p, i) => (
          <li key={p.id || i}>
            <Glass level={0} radius="md" padding="md">
              <header className="v2-forge-head">
                <h3 className="v2-forge-title">{p.pattern || p.name || `Proposal ${i + 1}`}</h3>
                {p.count && <span className="v2-chip">{p.count}×</span>}
              </header>
              <p className="v2-p v2-p--muted">{p.description || p.sample_prompt || '—'}</p>
            </Glass>
          </li>
        ))}
      </ul>
    </Pane>
  );
}

function GeneratedTab() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiJson('/api/tool-genesis/list')
      .then((d) => setItems(d.tools || d || []))
      .finally(() => setLoading(false));
  }, []);

  return (
    <Pane title={`Generated skills (${items.length})`}>
      <p className="v2-p v2-p--muted">Skills Tool Genesis created during this Brain's lifetime.</p>
      {loading && <EmptyState title="Loading…" />}
      {!loading && items.length === 0 && <EmptyState title="Nothing generated yet" />}
      <ul className="v2-forge-list">
        {items.map((t, i) => (
          <li key={t.id || i}>
            <Glass level={0} radius="md" padding="md">
              <header className="v2-forge-head">
                <h3 className="v2-forge-title">{t.name || t.skill_id || t.id}</h3>
                {t.uses != null && <span className="v2-chip">{t.uses} uses</span>}
              </header>
              <p className="v2-p v2-p--muted">{t.description || '—'}</p>
            </Glass>
          </li>
        ))}
      </ul>
    </Pane>
  );
}

function GenerateTab() {
  const [prompt, setPrompt] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  // AUDIT-r14 finding 05 fix: `/api/tool-genesis/generate` reads
  // `body.get("sequence_id")` and only works when wired up to an
  // existing sequence record — it never accepted a free-form prompt.
  // The natural-language "Generate" flow is actually
  // `/api/tool-genesis/propose` which reads `body.get("intent", "")`
  // (tool_genesis.py:51-67). Switch to it and surface the returned
  // `tool_id` so the user sees the draft id straight away.
  const go = async (e) => {
    e.preventDefault();
    if (!prompt.trim()) return;
    setBusy(true);
    setResult(null);
    setError(null);
    try {
      const r = await apiFetch('/api/tool-genesis/propose', {
        method: 'POST',
        body: JSON.stringify({ intent: prompt.trim() }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        setError(body?.detail || body?.error || `${r.status}`);
        return;
      }
      if (body?.tool_id) {
        setResult(body);
      } else if (body?.error) {
        setError(body.error);
      } else {
        setError('Brain rejected the proposal (no tool_id returned). Try a more specific intent.');
      }
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Pane title="Generate a new skill">
      <p className="v2-p v2-p--muted">
        Describe the capability. FERAL will draft code, AST-gate it, sandbox-run it, and promote it to Pending for your review.
      </p>
      <form onSubmit={go}>
        <textarea
          className="v2-code-editor"
          rows={5}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="Fetch current weather for my location and summarise in 1 sentence"
        />
        <div className="v2-forge-actions">
          <button type="submit" className="v2-btn v2-btn--primary" disabled={busy || !prompt.trim()}>
            <Sparkles size={13} /> {busy ? 'Drafting…' : 'Generate'}
          </button>
        </div>
      </form>
      {error && <div className="v2-chip v2-chip--error">{error}</div>}
      {result && (
        <Glass level={0} radius="md" padding="md">
          <StatusDot tone="live" /> Draft queued — check Pending tab
          <pre className="v2-code">{JSON.stringify(result, null, 2).slice(0, 1200)}</pre>
        </Glass>
      )}
    </Pane>
  );
}

function StatsTab() {
  const [stats, setStats] = useState(null);
  useEffect(() => {
    apiJson('/api/tool-genesis/stats').then(setStats).catch(() => setStats({}));
  }, []);

  if (!stats) return <Pane title="Stats"><EmptyState title="Loading…" /></Pane>;

  const entries = Object.entries(stats);

  return (
    <Pane title="Tool Genesis stats">
      <div className="v2-grid v2-grid--stats">
        {entries.map(([key, value]) => (
          <Glass key={key} level={1} radius="md" padding="md">
            <div className="v2-stat-label">{key.replace(/_/g, ' ')}</div>
            <div className="v2-stat-value">{typeof value === 'number' ? value : JSON.stringify(value)}</div>
          </Glass>
        ))}
      </div>
    </Pane>
  );
}
