import React, { useCallback, useEffect, useState } from 'react';
import { Users, Plus, ThumbsUp, ThumbsDown, RefreshCw, Sparkles } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Modal from '../ui/Modal';
import Tabs from '../ui/Tabs';
import EmptyState from '../ui/EmptyState';
import ErrorState from '../ui/ErrorState';
import { apiJson, apiFetch } from '../lib/api';
import { useResource, firstRejection, toApiError } from '../hooks/useResource';

/** Normalise `{key: [...]}` / a bare array / anything else into a list. */
function asList(value, ...keys) {
  for (const key of keys) {
    if (Array.isArray(value?.[key])) return value[key];
  }
  if (Array.isArray(value)) return value;
  return [];
}

/**
 * Agents page. Two catalogs + one timeline:
 *  - Personas   → first-party curated archetypes (GET /api/agents/personas)
 *  - Specialists → user/Mitosis-spawned live agents (GET /api/agents/list)
 *  - Proposals  → Mitosis-detected recurring patterns
 *  - Stats      → Mitosis runtime stats
 */
export default function Agents() {
  const [tab, setTab] = useState('personas');
  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title="Agents"
        actions={(
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { id: 'personas', label: 'Personas' },
              { id: 'specialists', label: 'Specialists' },
              { id: 'proposals', label: 'Proposals' },
              { id: 'stats', label: 'Stats' },
            ]}
          />
        )}
      >
        <p className="v2-p v2-p--muted">
          Personas are the curated first-party agent archetypes shipped with FERAL.
          Specialists are Mitosis-spawned permanent sub-agents with their own tool permissions.
          Proposals surface when FERAL sees a recurring task pattern.
        </p>
      </Pane>

      {tab === 'personas' && <PersonasTab />}
      {tab === 'specialists' && <SpecialistsTab />}
      {tab === 'proposals' && <ProposalsTab />}
      {tab === 'stats' && <StatsTab />}
    </div>
  );
}

function PersonasTab() {
  // The catch here did set an error chip, but it left `personas` at
  // `[]`, so the pane rendered the chip AND "No first-party personas
  // loaded / Check the Brain log for 'Loaded N first-party personas'".
  // That second sentence sent the user to debug a boot that was fine.
  const {
    data: personaRows, error: loadError, loading, refresh,
  } = useResource('/api/agents/personas', { select: (d) => asList(d, 'personas') });
  const personas = personaRows || [];
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState(null);

  const spawnFromPersona = async (p) => {
    setBusyId(p.agent_id);
    setError(null);
    try {
      const r = await apiFetch('/api/agents/spawn', {
        method: 'POST',
        body: JSON.stringify({
          name: p.name,
          description: p.description,
          system_prompt: p.system_prompt,
          tool_permissions: p.tool_permissions || [],
          source_pattern: p.source_pattern,
        }),
      });
      if (!r.ok) {
        setError(`${r.status} ${await r.text()}`);
      }
    } catch (err) {
      setError(err?.message || 'Spawn failed');
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Pane
      title={personaRows ? `Personas (${personas.length})` : 'Personas'}
      actions={<button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}><RefreshCw size={13} /></button>}
    >
      {loading && !personaRows && <EmptyState title="Loading…" />}
      {loadError && !personaRows && (
        <ErrorState error={loadError} what="the persona catalog" onRetry={refresh} />
      )}
      {!loading && !loadError && personaRows && personas.length === 0 && (
        <EmptyState
          title="No first-party personas loaded"
          hint="The Brain looks for feral-core/agents/personas/*.json at boot. Check the Brain log for 'Loaded N first-party personas'."
        />
      )}
      {error && <div className="v2-chip v2-chip--error" style={{ marginBottom: 12 }}>{error}</div>}
      <div className="v2-skills-grid">
        {personas.map((p) => (
          <Glass key={p.agent_id} level={0} radius="md" padding="md" className="v2-skill-card">
            <header className="v2-skill-card-head">
              <h3 className="v2-skill-card-name">{p.name}</h3>
              <code className="v2-skill-card-id">{p.agent_id}</code>
            </header>
            {p.description && <p className="v2-p v2-p--muted">{p.description}</p>}
            <div className="v2-skill-card-meta">
              {p.schedule && <span className="v2-chip v2-chip--muted">{p.schedule}</span>}
              {p.memory_filter && <span className="v2-chip v2-chip--muted">memory: {p.memory_filter}</span>}
              {Array.isArray(p.tags) && p.tags.slice(0, 3).map((t) => (
                <span key={t} className="v2-chip v2-chip--muted">{t}</span>
              ))}
            </div>
            {Array.isArray(p.tool_permissions) && p.tool_permissions.length > 0 && (
              <div className="v2-skill-card-phrases">
                {p.tool_permissions.slice(0, 6).map((perm) => (
                  <span key={perm} className="v2-chip v2-chip--muted">{perm}</span>
                ))}
              </div>
            )}
            <div className="v2-forge-actions">
              <button
                type="button"
                className="v2-btn v2-btn--primary"
                disabled={busyId === p.agent_id}
                onClick={() => spawnFromPersona(p)}
              >
                <Sparkles size={12} /> {busyId === p.agent_id ? 'Spawning…' : 'Spawn specialist'}
              </button>
            </div>
          </Glass>
        ))}
      </div>
    </Pane>
  );
}

function SpecialistsTab() {
  // `Promise.allSettled` never rejects, so the `try { … } finally` had
  // nothing to catch and the two `if (status === 'fulfilled')` guards
  // had no else. With the brain down `agents` stayed `[]` and the pane
  // rendered "No specialists yet" beside a "Spawn first specialist"
  // button, which is a claim about agents the client never read.
  const [agents, setAgents] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [showSpawn, setShowSpawn] = useState(false);
  const [skills, setSkills] = useState(null);
  const [feedbackError, setFeedbackError] = useState(null);

  const refresh = useCallback(async () => {
    const [a, s] = await Promise.allSettled([
      apiJson('/api/agents/list'),
      apiJson('/skills'),
    ]);
    if (a.status === 'fulfilled') setAgents(asList(a.value, 'agents', 'specialists'));
    if (s.status === 'fulfilled') setSkills(asList(s.value, 'skills'));
    // Only the agent list speaks for the "no specialists" empty state.
    // `/skills` fills the spawn modal's permission chips and is
    // reported inside that modal instead.
    setLoadError(firstRejection([a]));
    setLoading(false);
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // Was `catch { /* silent */ }`: a rating that never reached the brain
  // looked exactly like one that did.
  const feedback = async (id, score) => {
    setFeedbackError(null);
    try {
      await apiFetch('/api/agents/feedback', {
        method: 'POST',
        body: JSON.stringify({ agent_id: id, score }),
        silent: true,
      });
      await refresh();
    } catch (err) {
      setFeedbackError({ id, error: toApiError(err) });
    }
  };

  const rows = agents || [];

  return (
    <>
      <Pane
        title={agents ? `Specialists (${rows.length})` : 'Specialists'}
        actions={(
          <>
            <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}><RefreshCw size={13} /></button>
            <button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowSpawn(true)}>
              <Plus size={13} /> Spawn specialist
            </button>
          </>
        )}
      >
        {loading && !agents && !loadError && <EmptyState title="Loading…" />}
        {loadError && !agents && (
          <ErrorState error={loadError} what="the specialist list" onRetry={refresh} />
        )}
        {feedbackError && (
          <ErrorState
            error={feedbackError.error}
            what={`the rating of ${feedbackError.id}`}
            hint="The rating was not recorded. Mitosis has not seen it."
            compact
          />
        )}
        {!loading && !loadError && agents && rows.length === 0 && (
          <EmptyState
            title="No specialists yet"
            hint="Spawn one manually or wait for Agent Mitosis to propose one based on recurring task patterns."
            action={<button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowSpawn(true)}>Spawn first specialist</button>}
          />
        )}
        <div className="v2-skills-grid">
          {rows.map((a) => (
            <Glass key={a.agent_id || a.id} level={0} radius="md" padding="md" className="v2-skill-card">
              <header className="v2-skill-card-head">
                <h3 className="v2-skill-card-name">{a.name || a.agent_id}</h3>
                <code className="v2-skill-card-id">{a.agent_id || a.id}</code>
              </header>
              {a.description && <p className="v2-p v2-p--muted">{a.description}</p>}
              <div className="v2-skill-card-meta">
                {a.tasks_completed != null && <span className="v2-chip">{a.tasks_completed} tasks</span>}
                {a.satisfaction_score != null && <span className="v2-chip">{(a.satisfaction_score * 100).toFixed(0)}% satisfaction</span>}
                {a.schedule && <span className="v2-chip v2-chip--muted">{a.schedule}</span>}
              </div>
              {a.tool_permissions && Array.isArray(a.tool_permissions) && (
                <div className="v2-skill-card-phrases">
                  {a.tool_permissions.slice(0, 5).map((p) => (
                    <span key={p} className="v2-chip v2-chip--muted">{p}</span>
                  ))}
                </div>
              )}
              <div className="v2-forge-actions">
                <button type="button" className="v2-btn" onClick={() => feedback(a.agent_id || a.id, 1)}><ThumbsUp size={12} /></button>
                <button type="button" className="v2-btn" onClick={() => feedback(a.agent_id || a.id, -1)}><ThumbsDown size={12} /></button>
              </div>
            </Glass>
          ))}
        </div>
      </Pane>

      {showSpawn && <SpawnModal skills={skills} onClose={() => setShowSpawn(false)} onSpawned={() => { setShowSpawn(false); refresh(); }} />}
    </>
  );
}

function SpawnModal({ skills, onClose, onSpawned }) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [tools, setTools] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const toggle = (id) => setTools((prev) => prev.includes(id) ? prev.filter((t) => t !== id) : [...prev, id]);

  const submit = async () => {
    setBusy(true);
    try {
      const r = await apiFetch('/api/agents/spawn', {
        method: 'POST',
        body: JSON.stringify({
          name,
          description,
          system_prompt: systemPrompt,
          tool_permissions: tools,
        }),
      });
      if (!r.ok) setError(`${r.status} ${await r.text()}`);
      else onSpawned();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title="Spawn a specialist"
      size="lg"
      actions={(
        <>
          <button type="button" className="v2-btn" onClick={onClose}>Cancel</button>
          <button type="button" className="v2-btn v2-btn--primary" onClick={submit} disabled={busy || !name.trim()}>
            {busy ? 'Spawning…' : 'Spawn'}
          </button>
        </>
      )}
    >
      <label className="v2-step-field">
        <span>Name</span>
        <input className="v2-input" value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="v2-step-field">
        <span>Description</span>
        <input className="v2-input" value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <label className="v2-step-field">
        <span>System prompt</span>
        <textarea className="v2-code-editor" rows={6} value={systemPrompt} onChange={(e) => setSystemPrompt(e.target.value)} />
      </label>
      <div className="v2-p v2-p--muted" style={{ marginTop: 12 }}>Tool permissions</div>
      {/* `skills` is null when `/skills` did not answer. An empty chip
       * row would read as "this brain has no tools to grant", which is
       * a different and much more alarming claim. */}
      {!skills && (
        <p className="v2-p v2-p--muted v2-p--tiny">
          The skill list could not be loaded, so no tool permissions can be listed here.
          This is a failed request, not a brain with no skills. Spawning still works;
          grant permissions later from the specialist card.
        </p>
      )}
      <div className="v2-skill-card-phrases">
        {(skills || []).map((s) => {
          const id = s.skill_id || s.id;
          return (
            <button
              key={id}
              type="button"
              className={`v2-chip${tools.includes(id) ? ' v2-chip--live' : ' v2-chip--muted'}`}
              onClick={() => toggle(id)}
            >
              {id}
            </button>
          );
        })}
      </div>
      {error && <div className="v2-chip v2-chip--error" style={{ marginTop: 12 }}>{error}</div>}
    </Modal>
  );
}

function ProposalsTab() {
  // `.then(…).finally(…)` with no catch: a failure rendered "No
  // recurring patterns yet", i.e. a claim about what Mitosis has
  // observed across the user's whole history.
  const { data, error, loading, refresh } = useResource('/api/agents/proposals', {
    select: (d) => asList(d, 'proposals'),
  });
  const proposals = data || [];

  return (
    <Pane title={data ? `Proposals (${proposals.length})` : 'Proposals'}>
      {loading && !data && <EmptyState title="Loading…" />}
      {error && !data && (
        <ErrorState error={error} what="the Mitosis proposals" onRetry={refresh} />
      )}
      {!loading && !error && data && proposals.length === 0 && <EmptyState title="No recurring patterns yet" hint="Mitosis watches your recent turns for 5+ uses of the same tool set." />}
      <ul className="v2-mem-list">
        {proposals.map((p, i) => (
          <li key={p.pattern_id || i}>
            <Glass level={0} radius="md" padding="md">
              <div className="v2-mem-content">{p.topic_cluster || p.pattern_id}</div>
              <div className="v2-mem-meta">
                {p.occurrence_count != null && <span>seen {p.occurrence_count}×</span>}
                {Array.isArray(p.tool_affinities) && <span>· tools: {p.tool_affinities.slice(0, 5).join(', ')}</span>}
              </div>
            </Glass>
          </li>
        ))}
      </ul>
    </Pane>
  );
}

function StatsTab() {
  // `.catch(() => setStats({}))` swapped a failure for an answer: the
  // page left its loading branch and rendered the "Mitosis stats"
  // heading over a stat grid the brain never filled in, so every
  // counter it would have reported silently ceased to exist.
  const { data: stats, error, loading, refresh } = useResource('/api/agents/stats');
  if (loading && !stats) return <Pane title="Stats"><EmptyState title="Loading…" /></Pane>;
  if (error && !stats) {
    return (
      <Pane title="Stats">
        <ErrorState
          error={error}
          what="the Mitosis stats"
          hint="No counters are shown rather than zeroes. The client never received these numbers, and a zero here would be an invented measurement."
          onRetry={refresh}
        />
      </Pane>
    );
  }
  const entries = Object.entries(stats && typeof stats === 'object' ? stats : {});
  return (
    <Pane title="Mitosis stats">
      {entries.length === 0 && (
        <EmptyState
          title="The brain reports no Mitosis counters"
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
