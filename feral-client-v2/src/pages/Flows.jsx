import React, { useCallback, useEffect, useState } from 'react';
import { Play, X, RefreshCw, Plus, Pause } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Modal from '../ui/Modal';
import Tabs from '../ui/Tabs';
import StatusDot from '../ui/StatusDot';
import EmptyState from '../ui/EmptyState';
import StepBuilder from '../components/StepBuilder';
import { apiJson, apiFetch } from '../lib/api';

function statusTone(status) {
  return {
    running: 'live',
    waiting: 'warn',
    queued: 'warn',
    completed: 'neutral',
    failed: 'error',
    cancelled: 'off',
    paused: 'warn',
  }[status] || 'neutral';
}

export default function Flows() {
  const [tab, setTab] = useState('taskflows');
  const [skills, setSkills] = useState([]);

  useEffect(() => {
    apiJson('/skills').then((d) => setSkills(d.skills || d || [])).catch(() => setSkills([]));
  }, []);

  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane title="Automation" actions={(
        <Tabs
          value={tab}
          onChange={setTab}
          items={[
            { id: 'taskflows', label: 'TaskFlows' },
            { id: 'packs', label: 'Packs' },
            { id: 'routines', label: 'Routines' },
            { id: 'automations', label: 'Automations' },
          ]}
        />
      )}>
        <p className="v2-p v2-p--muted">
          TaskFlows are one-shot multi-step routines · Packs are curated templates you can instantiate as a TaskFlow · Routines run on cron schedules · Automations are event triggers that fire a skill.
        </p>
      </Pane>

      {tab === 'taskflows' && <TaskFlowsTab skills={skills} />}
      {tab === 'packs' && <PacksTab />}
      {tab === 'routines' && <RoutinesTab skills={skills} />}
      {tab === 'automations' && <AutomationsTab skills={skills} />}
    </div>
  );
}

function PacksTab() {
  const [packs, setPacks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState(null);
  const [lastCreated, setLastCreated] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const d = await apiJson('/api/workflows/packs');
      setPacks(d.packs || []);
    } catch (err) {
      setError(err?.message || 'Failed to load workflow packs');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const instantiate = async (pack) => {
    setBusyId(pack.workflow_id);
    setError(null);
    try {
      const r = await apiFetch(`/api/workflows/packs/${pack.workflow_id}/instantiate`, {
        method: 'POST',
        body: JSON.stringify({}),
      });
      if (!r.ok) {
        setError(`${r.status} ${await r.text()}`);
      } else {
        const body = await r.json();
        setLastCreated({ workflow_id: pack.workflow_id, flow: body?.flow });
      }
    } catch (err) {
      setError(err?.message || 'Instantiate failed');
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Pane
      title={`Workflow packs (${packs.length})`}
      actions={<button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}><RefreshCw size={13} /></button>}
    >
      {loading && <EmptyState title="Loading…" />}
      {!loading && packs.length === 0 && (
        <EmptyState
          title="No first-party workflow packs loaded"
          hint="The Brain reads feral-core/workflows/*.json at boot. Check the Brain log for 'Loaded N first-party workflow packs'."
        />
      )}
      {error && <div className="v2-chip v2-chip--error" style={{ marginBottom: 12 }}>{error}</div>}
      {lastCreated && (
        <div className="v2-chip v2-chip--live" style={{ marginBottom: 12 }}>
          Instantiated {lastCreated.workflow_id} as flow {lastCreated.flow?.id || 'unknown'}
        </div>
      )}
      <div className="v2-skills-grid">
        {packs.map((p) => (
          <Glass key={p.workflow_id} level={0} radius="md" padding="md" className="v2-skill-card">
            <header className="v2-skill-card-head">
              <h3 className="v2-skill-card-name">{p.name}</h3>
              <code className="v2-skill-card-id">{p.workflow_id}</code>
            </header>
            {p.description && <p className="v2-p v2-p--muted">{p.description}</p>}
            <div className="v2-skill-card-meta">
              {p.schedule && <span className="v2-chip v2-chip--muted">cron: {p.schedule}</span>}
              <span className="v2-chip v2-chip--muted">{Array.isArray(p.steps) ? p.steps.length : 0} step{p.steps?.length === 1 ? '' : 's'}</span>
              {Array.isArray(p.tags) && p.tags.slice(0, 3).map((t) => (
                <span key={t} className="v2-chip v2-chip--muted">{t}</span>
              ))}
            </div>
            <div className="v2-forge-actions">
              <button
                type="button"
                className="v2-btn v2-btn--primary"
                disabled={busyId === p.workflow_id}
                onClick={() => instantiate(p)}
              >
                <Plus size={12} /> {busyId === p.workflow_id ? 'Instantiating…' : 'Install as TaskFlow'}
              </button>
            </div>
          </Glass>
        ))}
      </div>
    </Pane>
  );
}

// ── TaskFlows ───────────────────────────────────────────────────

function TaskFlowsTab({ skills }) {
  const [flows, setFlows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [showCreate, setShowCreate] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const d = await apiJson('/api/taskflows?limit=100');
      setFlows(d.flows || []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  const action = async (id, which) => {
    await apiFetch(`/api/taskflows/${id}/${which}`, { method: 'POST' });
    refresh();
  };

  return (
    <>
      <Pane
        title={`TaskFlows (${flows.length})`}
        actions={(
          <>
            <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}><RefreshCw size={13} /></button>
            <button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowCreate(true)}>
              <Plus size={13} /> New flow
            </button>
          </>
        )}
      >
        {loading && <EmptyState title="Loading…" />}
        {!loading && flows.length === 0 && (
          <EmptyState
            title="No flows yet"
            hint="Create a multi-step flow: save a note, call a skill, prompt the LLM, branch, etc."
            action={<button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowCreate(true)}>Create your first flow</button>}
          />
        )}
        <div className="v2-flow-list">
          {flows.map((f) => (
            <Glass key={f.id} level={0} radius="md" padding="md" className="v2-flow-card">
              <div className="v2-flow-card-head">
                <StatusDot tone={statusTone(f.status)} pulse={f.status === 'running'} />
                <div className="v2-flow-card-title" onClick={() => setSelected(f)} role="button" tabIndex={0}>
                  {f.title || f.id}
                </div>
                <div className="v2-flow-card-status">{f.status}</div>
              </div>
              <div className="v2-flow-card-meta">
                <span>{f.current_step ?? 0} / {(f.steps || []).length || '?'} steps</span>
                {f.created_at && <span>· created {new Date(f.created_at * 1000).toLocaleString()}</span>}
              </div>
              <div className="v2-flow-card-actions">
                <button type="button" className="v2-btn" onClick={() => action(f.id, 'resume')} disabled={f.status === 'running' || f.status === 'completed'}>
                  <Play size={12} /> Run
                </button>
                <button type="button" className="v2-btn" onClick={() => action(f.id, 'cancel')} disabled={['completed', 'cancelled', 'failed'].includes(f.status)}>
                  <X size={12} /> Cancel
                </button>
                <button type="button" className="v2-btn v2-btn--ghost" onClick={() => setSelected(f)}>Details</button>
              </div>
            </Glass>
          ))}
        </div>
      </Pane>

      {showCreate && <CreateFlowModal skills={skills} onClose={() => setShowCreate(false)} onCreated={() => { setShowCreate(false); refresh(); }} />}
      {selected && <FlowDetailModal flow={selected} onClose={() => setSelected(null)} onAction={action} />}
    </>
  );
}

function CreateFlowModal({ skills, onClose, onCreated }) {
  const [title, setTitle] = useState('New TaskFlow');
  const [sessionId, setSessionId] = useState('');
  const [steps, setSteps] = useState([
    { type: 'note.save', content: 'TaskFlow started', tags: ['ui'] },
    { type: 'sleep', seconds: 3 },
  ]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    if (steps.length === 0) { setError('At least one step is required.'); return; }
    setBusy(true);
    try {
      const r = await apiFetch('/api/taskflows', {
        method: 'POST',
        body: JSON.stringify({
          title,
          session_id: sessionId || 'ui_session',
          steps,
        }),
      });
      if (!r.ok) {
        const body = await r.text();
        setError(`${r.status} ${body}`);
        return;
      }
      onCreated();
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
      title="New TaskFlow"
      size="lg"
      actions={(
        <>
          <button type="button" className="v2-btn" onClick={onClose}>Cancel</button>
          <button type="button" className="v2-btn v2-btn--primary" onClick={submit} disabled={busy}>
            {busy ? 'Creating…' : 'Create flow'}
          </button>
        </>
      )}
    >
      <div className="v2-setting-stack">
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Title</div></div>
          <div className="v2-setting-control"><input className="v2-input" value={title} onChange={(e) => setTitle(e.target.value)} /></div>
        </label>
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Session ID</div><div className="v2-setting-hint">Optional — for session-scoped flows</div></div>
          <div className="v2-setting-control"><input className="v2-input" value={sessionId} onChange={(e) => setSessionId(e.target.value)} placeholder="ui_session" /></div>
        </label>
      </div>
      <div className="v2-p v2-p--muted" style={{ marginTop: 16 }}>Steps</div>
      <StepBuilder steps={steps} onChange={setSteps} skills={skills} />
      {error && <div className="v2-chip v2-chip--error" style={{ marginTop: 12 }}>{error}</div>}
    </Modal>
  );
}

function FlowDetailModal({ flow, onClose, onAction }) {
  const [detail, setDetail] = useState(flow);

  useEffect(() => {
    apiJson(`/api/taskflows/${flow.id}`).then(setDetail).catch(() => {});
  }, [flow.id]);

  const steps = detail.steps || flow.steps || [];

  return (
    <Modal open onClose={onClose} title={detail.title || flow.title || flow.id} size="lg">
      <div className="v2-setting-stack">
        <div className="v2-setting-row">
          <div className="v2-setting-label"><div>Status</div></div>
          <div className="v2-setting-control"><StatusDot tone={statusTone(detail.status)} /> {detail.status}</div>
        </div>
        <div className="v2-setting-row">
          <div className="v2-setting-label"><div>ID</div></div>
          <div className="v2-setting-control"><code className="v2-code-inline">{detail.id}</code></div>
        </div>
        <div className="v2-setting-row">
          <div className="v2-setting-label"><div>Steps</div></div>
          <div className="v2-setting-control">{steps.length}</div>
        </div>
      </div>
      <ol className="v2-step-detail-list">
        {steps.map((s, i) => (
          <li key={i} className="v2-step-detail-row">
            <StatusDot tone={statusTone(s.status)} />
            <span className="v2-step-detail-type">{s.step_type || s.type || 'step'}</span>
            {s.status && <span className="v2-step-detail-status">{s.status}</span>}
          </li>
        ))}
      </ol>
      <div className="v2-forge-actions">
        <button type="button" className="v2-btn v2-btn--primary" onClick={() => onAction(flow.id, 'resume')}>
          <Play size={12} /> Run / Resume
        </button>
        <button type="button" className="v2-btn" onClick={() => onAction(flow.id, 'cancel')}>
          <X size={12} /> Cancel
        </button>
      </div>
    </Modal>
  );
}

// ── Routines ───────────────────────────────────────────────────

// AUDIT-r14 finding 06 fix for Routines:
//   Backend POST /api/routines reads
//     {cron_expr | schedule, description, payload, job_type,
//      session_id, skill, endpoint, prompt}
//   The UI used to send {name, cron, steps} so `cron`/`steps` were
//   silently ignored and routines never matched a real cron schedule.
//   Status used to read `r.paused` but the brain returns `enabled`.
//   Both fixed below — and we now derive the cron + description from
//   the modal's structured fields, while still letting power users
//   stash arbitrary payload via the JSON box.

function routineStatus(r) {
  // Backend returns `enabled`. Some older builds (and our test
  // fixtures) also surface `paused`. Treat them as mutually
  // exclusive: enabled=true and paused=false both mean "armed".
  if (r.enabled === false) return { paused: true, tone: 'warn', label: 'paused' };
  if (r.paused === true) return { paused: true, tone: 'warn', label: 'paused' };
  return { paused: false, tone: 'live', label: 'armed' };
}

function RoutinesTab({ skills }) {
  const [routines, setRoutines] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const [showCreate, setShowCreate] = useState(false);

  const refresh = useCallback(async () => {
    setErr('');
    try {
      const d = await apiJson('/api/routines');
      setRoutines(d.routines || []);
    } catch (e) {
      setErr(e?.message || 'failed to list routines');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const action = async (id, verb, method = 'POST') => {
    setErr('');
    try {
      const r = await apiFetch(`/api/routines/${id}${verb ? '/' + verb : ''}`, { method });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body?.detail || body?.error || `${verb || 'delete'} returned ${r.status}`);
      }
      await refresh();
    } catch (e) {
      setErr(e?.message || `${verb || 'delete'} failed`);
    }
  };

  return (
    <>
      <Pane
        title={`Routines (${routines.length})`}
        actions={(
          <button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowCreate(true)}>
            <Plus size={13} /> New routine
          </button>
        )}
      >
        {err && <div className="v2-chip v2-chip--error" role="alert">{err}</div>}
        {loading && <EmptyState title="Loading…" />}
        {!loading && routines.length === 0 && (
          <EmptyState title="No routines" hint="Routines run on a cron schedule and call a skill or shell prompt." />
        )}
        <div className="v2-flow-list">
          {routines.map((r) => {
            const st = routineStatus(r);
            return (
              <Glass key={r.id} level={0} radius="md" padding="md" className="v2-flow-card">
                <div className="v2-flow-card-head">
                  <StatusDot tone={st.tone} label={st.label} />
                  <div className="v2-flow-card-title">{r.description || r.name || r.id}</div>
                  <div className="v2-flow-card-status">{r.cron_expr || r.cron || '—'}</div>
                </div>
                <div className="v2-flow-card-actions">
                  {st.paused
                    ? <button type="button" className="v2-btn" onClick={() => action(r.id, 'resume')}><Play size={12} /> Resume</button>
                    : <button type="button" className="v2-btn" onClick={() => action(r.id, 'pause')}><Pause size={12} /> Pause</button>
                  }
                  <button type="button" className="v2-btn" onClick={() => action(r.id, '', 'DELETE')}>
                    <X size={12} /> Delete
                  </button>
                </div>
              </Glass>
            );
          })}
        </div>
      </Pane>

      {showCreate && <CreateRoutineModal skills={skills} onClose={() => setShowCreate(false)} onCreated={() => { setShowCreate(false); refresh(); }} />}
    </>
  );
}

function CreateRoutineModal({ skills, onClose, onCreated }) {
  const [description, setDescription] = useState('New routine');
  const [cronExpr, setCronExpr] = useState('0 9 * * 1-5');
  const [skillId, setSkillId] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [prompt, setPrompt] = useState('');
  const [payloadJson, setPayloadJson] = useState('{}');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      let payload = {};
      try { payload = JSON.parse(payloadJson || '{}'); }
      catch { setError('Payload JSON is malformed.'); setBusy(false); return; }
      const body = {
        cron_expr: cronExpr.trim(),
        description: description.trim(),
        payload,
        job_type: prompt.trim() ? 'prompt' : (skillId ? 'skill' : 'scheduled'),
      };
      if (skillId) body.skill = skillId;
      if (endpoint) body.endpoint = endpoint.trim();
      if (prompt.trim()) body.prompt = prompt.trim();
      const r = await apiFetch('/api/routines', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const e2 = await r.json().catch(() => ({}));
        setError(e2?.detail || e2?.error || `${r.status}`);
        return;
      }
      onCreated();
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
      title="New Routine"
      size="lg"
      actions={(
        <>
          <button type="button" className="v2-btn" onClick={onClose}>Cancel</button>
          <button type="button" className="v2-btn v2-btn--primary" onClick={submit} disabled={busy}>
            {busy ? 'Creating…' : 'Create routine'}
          </button>
        </>
      )}
    >
      <div className="v2-setting-stack">
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Description</div></div>
          <div className="v2-setting-control"><input className="v2-input" value={description} onChange={(e) => setDescription(e.target.value)} /></div>
        </label>
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Cron schedule</div><div className="v2-setting-hint">e.g. "0 9 * * 1-5" = weekdays 9am</div></div>
          <div className="v2-setting-control"><input className="v2-input" value={cronExpr} onChange={(e) => setCronExpr(e.target.value)} /></div>
        </label>
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Skill (optional)</div></div>
          <div className="v2-setting-control">
            <select className="v2-select" value={skillId} onChange={(e) => setSkillId(e.target.value)}>
              <option value="">— none —</option>
              {skills.map((s) => (
                <option key={s.skill_id || s.id} value={s.skill_id || s.id}>{s.name || s.skill_id || s.id}</option>
              ))}
            </select>
          </div>
        </label>
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Endpoint (optional)</div></div>
          <div className="v2-setting-control"><input className="v2-input" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="list_today" /></div>
        </label>
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Prompt (optional)</div><div className="v2-setting-hint">If set, the routine runs this as a chat prompt instead of calling a skill.</div></div>
          <div className="v2-setting-control" style={{ flex: 1, minWidth: 220 }}>
            <textarea className="v2-code-editor" rows={2} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
          </div>
        </label>
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Payload (JSON)</div></div>
          <div className="v2-setting-control" style={{ flex: 1, minWidth: 220 }}>
            <textarea className="v2-code-editor" rows={3} value={payloadJson} onChange={(e) => setPayloadJson(e.target.value)} />
          </div>
        </label>
      </div>
      {/* StepBuilder UI is retained as a hint for users who like the
       * v1 mental model: visualise steps then transpose to payload.
       * Backend doesn't read `steps` directly for routines (it reads
       * payload), so we just show a helper note. */}
      <p className="v2-p v2-p--muted v2-p--tiny" style={{ marginTop: 12 }}>
        Looking for a step builder? Steps for routines belong inside <code>payload</code> (the brain decides
        how to interpret them). TaskFlows have the visual step builder.
      </p>
      {/* eslint-disable-next-line no-unused-vars */}
      {false && <StepBuilder steps={[]} onChange={() => {}} skills={skills} />}
      {error && <div className="v2-chip v2-chip--error" style={{ marginTop: 12 }} role="alert">{error}</div>}
    </Modal>
  );
}

// ── Automations ────────────────────────────────────────────────

function AutomationsTab({ skills }) {
  const [autos, setAutos] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const d = await apiJson('/api/automations');
      setAutos(d.automations || []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const remove = async (id) => {
    await apiFetch(`/api/automations/${id}`, { method: 'DELETE' });
    refresh();
  };

  return (
    <>
      <Pane
        title={`Automations (${autos.length})`}
        actions={(
          <button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowCreate(true)}>
            <Plus size={13} /> New automation
          </button>
        )}
      >
        {loading && <EmptyState title="Loading…" />}
        {!loading && autos.length === 0 && (
          <EmptyState
            title="No automations"
            hint="Automations fire a skill when a trigger event occurs (cron, webhook, geofence, etc.)."
          />
        )}
        <div className="v2-flow-list">
          {autos.map((a) => {
            // Phase-1 truthfulness: the dot must reflect whether the
            // automation is actually armed, not "always green". The
            // brain's `/api/automations` payload returns `enabled` —
            // when true the row will fire on its trigger, when false
            // it has been paused / disabled. If the field is absent
            // (older brain or a shape variant), render a neutral dot
            // rather than invent green.
            const enabled = a.enabled;
            let tone = 'neutral';
            let label = 'status unknown';
            if (enabled === true) { tone = 'live'; label = 'armed'; }
            else if (enabled === false) { tone = 'off'; label = 'paused'; }
            return (
              <Glass key={a.id || a.job_id} level={0} radius="md" padding="md" className="v2-flow-card">
                <div className="v2-flow-card-head">
                  <StatusDot tone={tone} label={label} />
                  <div className="v2-flow-card-title">{a.name || a.description || a.trigger || a.id}</div>
                  <div className="v2-flow-card-status">{a.trigger_type || 'event'}</div>
                </div>
                <div className="v2-flow-card-meta">
                  {a.skill_id && <span>→ {a.skill_id}.{a.endpoint || 'default'}</span>}
                </div>
                <div className="v2-flow-card-actions">
                  <button type="button" className="v2-btn" onClick={() => remove(a.id || a.job_id)}>
                    <X size={12} /> Delete
                  </button>
                </div>
              </Glass>
            );
          })}
        </div>
      </Pane>

      {showCreate && <CreateAutomationModal skills={skills} onClose={() => setShowCreate(false)} onCreated={() => { setShowCreate(false); refresh(); }} />}
    </>
  );
}

// AUDIT-r14 finding 06 fix for Automations:
//   Backend `POST /api/automations` (api/routes/timeline.py:130) reads
//   `body.get("text")` — it parses natural language into a job. The
//   UI used to send structured fields (`{name, trigger_type, ...}`)
//   which the brain ignored, so the create call ALWAYS returned
//   "text is required". Now the modal lets the user paste plain
//   English (e.g. "every Monday at 9am, summarise my inbox") AND
//   provides a structured-builder fallback that we serialise into
//   the same `text` payload the backend expects.
function CreateAutomationModal({ skills, onClose, onCreated }) {
  const [text, setText] = useState('');
  const [trigger, setTrigger] = useState('event');
  const [triggerValue, setTriggerValue] = useState('');
  const [skillId, setSkillId] = useState('');
  const [endpoint, setEndpoint] = useState('');
  const [args, setArgs] = useState('{}');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const buildSerialisedText = () => {
    if (text.trim()) return text.trim();
    // Compose a natural-language string from the structured fields
    // so users who prefer the form still get an automation created.
    const parts = [];
    if (trigger === 'cron' && triggerValue) parts.push(`On schedule ${triggerValue}`);
    else if (trigger && triggerValue) parts.push(`On ${trigger} ${triggerValue}`);
    else if (trigger) parts.push(`On ${trigger}`);
    if (skillId) parts.push(`run skill ${skillId}${endpoint ? `.${endpoint}` : ''}`);
    if (args && args.trim() !== '{}') parts.push(`with args ${args.trim()}`);
    return parts.join(' ').trim();
  };

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const composed = buildSerialisedText();
      if (!composed) { setError('Describe the automation in natural language, or fill in the trigger + skill fields.'); setBusy(false); return; }
      const r = await apiFetch('/api/automations', {
        method: 'POST',
        body: JSON.stringify({ text: composed }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        setError(body?.detail || body?.error || `${r.status}`);
        return;
      }
      const body = await r.json().catch(() => ({}));
      if (body?.success === false) {
        setError(body?.error || 'Brain could not parse the automation. Try a more specific description.');
        return;
      }
      onCreated();
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
      title="New Automation"
      size="md"
      actions={(
        <>
          <button type="button" className="v2-btn" onClick={onClose}>Cancel</button>
          <button type="button" className="v2-btn v2-btn--primary" onClick={submit} disabled={busy}>
            {busy ? 'Creating…' : 'Create'}
          </button>
        </>
      )}
    >
      <div className="v2-setting-stack">
        <p className="v2-p v2-p--muted v2-p--tiny">
          Describe the automation in plain English — the brain parses it into a cron + skill call.
          Or fill the structured fields below and we'll compose the sentence for you.
        </p>
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Natural-language description</div></div>
          <div className="v2-setting-control" style={{ flex: 1, minWidth: 240 }}>
            <textarea
              className="v2-code-editor"
              rows={2}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder='Every weekday at 9am, summarise my inbox.'
              data-testid="automation-text"
            />
          </div>
        </label>
        <details>
          <summary className="v2-p v2-p--muted v2-p--tiny" style={{ cursor: 'pointer' }}>Structured builder (optional)</summary>
          <label className="v2-setting-row">
            <div className="v2-setting-label"><div>Trigger type</div></div>
            <div className="v2-setting-control">
              <select className="v2-select" value={trigger} onChange={(e) => setTrigger(e.target.value)}>
                <option value="event">Event (brain event name)</option>
                <option value="cron">Cron</option>
                <option value="webhook">Webhook</option>
                <option value="geofence">Geofence</option>
              </select>
            </div>
          </label>
          <label className="v2-setting-row">
            <div className="v2-setting-label"><div>Trigger value</div></div>
            <div className="v2-setting-control"><input className="v2-input" value={triggerValue} onChange={(e) => setTriggerValue(e.target.value)} /></div>
          </label>
          <label className="v2-setting-row">
            <div className="v2-setting-label"><div>Skill</div></div>
            <div className="v2-setting-control">
              <select className="v2-select" value={skillId} onChange={(e) => setSkillId(e.target.value)}>
                <option value="">-- pick a skill --</option>
                {skills.map((s) => (
                  <option key={s.skill_id || s.id} value={s.skill_id || s.id}>{s.name || s.skill_id || s.id}</option>
                ))}
              </select>
            </div>
          </label>
          <label className="v2-setting-row">
            <div className="v2-setting-label"><div>Endpoint</div></div>
            <div className="v2-setting-control"><input className="v2-input" value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="list_today" /></div>
          </label>
          <label className="v2-setting-row">
            <div className="v2-setting-label"><div>Args (JSON)</div></div>
            <div className="v2-setting-control" style={{ flex: 1, minWidth: 220 }}>
              <textarea className="v2-code-editor" rows={3} value={args} onChange={(e) => setArgs(e.target.value)} />
            </div>
          </label>
        </details>
      </div>
      {error && <div className="v2-chip v2-chip--error" style={{ marginTop: 12 }} role="alert">{error}</div>}
    </Modal>
  );
}
