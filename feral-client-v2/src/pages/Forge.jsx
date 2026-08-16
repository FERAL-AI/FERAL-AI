import React, { useCallback, useEffect, useState } from 'react';
import { RefreshCw, Sparkles, CheckCircle2, XCircle, Zap } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Tabs from '../ui/Tabs';
import EmptyState from '../ui/EmptyState';
import ErrorState from '../ui/ErrorState';
import StatusDot from '../ui/StatusDot';
import { ApiError, apiJson, apiFetch } from '../lib/api';
import { useResource, firstRejection, toApiError } from '../hooks/useResource';

/** Normalise `{key: [...]}` / a bare array / anything else into a list. */
function asList(value, key) {
  if (Array.isArray(value?.[key])) return value[key];
  if (Array.isArray(value)) return value;
  return [];
}

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
  // `null` means "no endpoint has answered yet", which is a different
  // thing from an answer of zero drafts. It used to start at `[]`, and
  // because `Promise.allSettled` never rejects the surrounding
  // `try/finally` had nothing to catch: with the brain unreachable the
  // list stayed `[]` and this pane asserted "No drafts pending".
  const [pending, setPending] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);
  const [decideError, setDecideError] = useState(null);

  const refresh = useCallback(async () => {
    const [tg, sk] = await Promise.allSettled([
      apiJson('/api/tool-genesis/pending'),
      apiJson('/api/skills/pending'),
    ]);
    const merged = [];
    if (tg.status === 'fulfilled') merged.push(...asList(tg.value, 'pending'));
    if (sk.status === 'fulfilled') {
      for (const item of asList(sk.value, 'pending')) {
        if (!merged.some((m) => (m.id || m.skill_id) === (item.id || item.skill_id))) {
          merged.push(item);
        }
      }
    }
    // Both queues feed one list, so a rejection from either means we do
    // not know the list is complete and must not say it is empty.
    if (tg.status === 'fulfilled' || sk.status === 'fulfilled') setPending(merged);
    setLoadError(firstRejection([tg, sk]));
    setLoading(false);
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
  // `apiFetch` throws on every non-2xx, so the old `if (r.ok)` could
  // only ever be true and the `r.json()` line under it was unreachable.
  // Worse, the `throw` that ended the loop had no catch above it: the
  // whole body was `try { … } finally`, so a draft that no endpoint
  // would approve produced an unhandled rejection and told the user
  // nothing. Each attempt is `silent` because trying the tool-genesis
  // route first for a plain-skill row is expected to 404, and toasting
  // that while the fallback succeeds is noise.
  // `await apiFetch(...)` was still not enough on its own, and this is
  // the part the previous pass missed. `apiFetch` raises on a non-2xx
  // status, or on a 2xx body carrying an `error` key. Both endpoints
  // behind these buttons could answer a failure with neither:
  // `/api/skills/approve` returned `{"ok": false, "skill_id": ...,
  // "registered": false}` at HTTP 200 when `SkillGenerator.approve_skill`
  // returned False, and `/api/tool-genesis/approve` still returns
  // `{"success": false, ...}` at HTTP 200 when the approved tool fails to
  // promote into the registry. Nothing threw, this loop `return`ed, and a
  // draft that was never registered rendered as promoted. The brain no
  // longer sends the first shape, but the client must not depend on that:
  // the desktop bundle ships its own copy of `api/routes/skills.py` and
  // an operator can point this UI at an older brain.
  const postDecision = async (path, id) => {
    const response = await apiFetch(path, {
      method: 'POST',
      body: JSON.stringify({ tool_id: id, id, skill_id: id }),
      silent: true,
    });
    const body = await response.json().catch(() => null);
    if (body && (body.ok === false || body.success === false)) {
      throw new ApiError({
        status: response.status,
        code: body.code || '',
        detail: body.error || body.detail
          || `the brain did not act on ${id}, and did not say why`,
        raw: body,
        path,
      });
    }
    return body;
  };

  const decide = async (id, approve) => {
    setBusy(id);
    setDecideError(null);
    const paths = approve
      ? ['/api/tool-genesis/approve', '/api/skills/approve']
      : ['/api/tool-genesis/reject', '/api/skills/reject'];
    let lastErr = null;
    try {
      for (const p of paths) {
        try {
          await postDecision(p, id);
          await refresh();
          return;
        } catch (e) {
          lastErr = toApiError(e, p);
        }
      }
      setDecideError({ id, approve, error: lastErr });
      // The list is refreshed on failure too, because a failed approval
      // does not always leave the queue alone: the brain pops a draft off
      // the pending queue before it registers it, so a failure after that
      // point has already discarded it. Leaving the row on screen would
      // be one more claim we cannot support.
      await refresh();
    } finally {
      setBusy(null);
    }
  };

  const rows = pending || [];
  // A count is a measurement. Only claim one when both queues answered.
  const counted = pending !== null && !loadError;

  return (
    <Pane title={counted ? `Pending (${rows.length})` : 'Pending'} actions={<button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}><RefreshCw size={13} /></button>}>
      {loading && pending === null && !loadError && <EmptyState title="Loading drafts…" />}
      {loadError && pending === null && (
        <ErrorState error={loadError} what="the pending drafts" onRetry={refresh} />
      )}
      {loadError && pending !== null && (
        <ErrorState
          error={loadError}
          what="one of the two draft queues"
          hint="The list below is whatever the queue that did answer returned. It may be missing drafts."
          compact
          onRetry={refresh}
        />
      )}
      {decideError && (
        <ErrorState
          error={decideError.error}
          what={`the ${decideError.approve ? 'approval' : 'rejection'} of ${decideError.id}`}
          hint={decideError.approve
            ? 'Nothing was registered, so no new skill is live. The brain takes a draft off the pending queue before it registers it, so if it failed after that point the draft is gone from the list above as well. The list has been refreshed to whatever the brain still has.'
            : 'Nothing was discarded. The draft is still whatever the refreshed list above says it is.'}
          compact
          onRetry={() => decide(decideError.id, decideError.approve)}
        />
      )}
      {!loading && !loadError && pending !== null && rows.length === 0 && (
        <EmptyState
          title="No drafts pending"
          hint="When you ask FERAL something none of the 25 skills can do, a draft appears here."
        />
      )}
      <ul className="v2-forge-list">
        {rows.map((d) => {
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
  // `.then(…).finally(…)` with no catch: a failure left `proposals` at
  // its initial `[]` and the pane said "No capability gaps tracked",
  // which is a claim about the orchestrator's findings.
  const { data, error, loading, refresh } = useResource('/api/tool-genesis/proposals', {
    select: (d) => asList(d, 'proposals'),
  });
  const proposals = data || [];

  return (
    <Pane title={data ? `Proposals (${proposals.length})` : 'Proposals'}>
      <p className="v2-p v2-p--muted">Capability gaps the orchestrator detected but hasn't drafted yet.</p>
      {loading && !data && <EmptyState title="Loading…" />}
      {error && !data && (
        <ErrorState error={error} what="the capability-gap proposals" onRetry={refresh} />
      )}
      {!loading && !error && data && proposals.length === 0 && <EmptyState title="No capability gaps tracked" />}
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
  // Same `.then(…).finally(…)` shape as ProposalsTab: a dropped request
  // used to render "Nothing generated yet", i.e. a statement about this
  // Brain's whole lifetime made without an answer.
  const { data, error, loading, refresh } = useResource('/api/tool-genesis/list', {
    select: (d) => asList(d, 'tools'),
  });
  const items = data || [];

  return (
    <Pane title={data ? `Generated skills (${items.length})` : 'Generated skills'}>
      <p className="v2-p v2-p--muted">Skills Tool Genesis created during this Brain's lifetime.</p>
      {loading && !data && <EmptyState title="Loading…" />}
      {error && !data && (
        <ErrorState error={error} what="the generated skill list" onRetry={refresh} />
      )}
      {!loading && !error && data && items.length === 0 && <EmptyState title="Nothing generated yet" />}
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
          <StatusDot tone="live" label="Draft queued" /> Draft queued — check Pending tab
          <pre className="v2-code">{JSON.stringify(result, null, 2).slice(0, 1200)}</pre>
        </Glass>
      )}
    </Pane>
  );
}

function StatsTab() {
  // The worst instance in this file. `.catch(() => setStats({}))` moved
  // the page out of its "loading" branch with a value the brain never
  // sent, so a failed request rendered the "Tool Genesis stats" heading
  // over a stat grid, and every counter the brain would have reported
  // silently ceased to exist. A statistics surface must never present a
  // number, or the absence of one, that it did not measure.
  const { data: stats, error, loading, refresh } = useResource('/api/tool-genesis/stats');

  if (loading && !stats) return <Pane title="Stats"><EmptyState title="Loading…" /></Pane>;

  if (error && !stats) {
    return (
      <Pane title="Stats">
        <ErrorState
          error={error}
          what="the Tool Genesis stats"
          hint="No counters are shown rather than zeroes. The client never received these numbers, and a zero here would be an invented measurement."
          onRetry={refresh}
        />
      </Pane>
    );
  }

  const entries = Object.entries(stats && typeof stats === 'object' ? stats : {});

  return (
    <Pane title="Tool Genesis stats">
      {entries.length === 0 && (
        <EmptyState
          title="The brain reports no Tool Genesis counters"
          hint="The stats endpoint answered, and the answer was empty."
        />
      )}
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
