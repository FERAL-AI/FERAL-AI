import React, { useState } from 'react';
import { Play, X, RefreshCw, Plus, Pause } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Modal from '../ui/Modal';
import Tabs from '../ui/Tabs';
import StatusDot from '../ui/StatusDot';
import EmptyState from '../ui/EmptyState';
import ErrorState from '../ui/ErrorState';
import StepBuilder from '../components/StepBuilder';
import { apiFetch } from '../lib/api';
import { useResource, toApiError } from '../hooks/useResource';

/** Normalise `{key: [...]}` / a bare array / anything else into a list. */
function asList(value, key) {
  if (Array.isArray(value?.[key])) return value[key];
  if (Array.isArray(value)) return value;
  return [];
}

/**
 * Every create modal on this page offers a skill picker fed by
 * `/skills`. When that call fails the picker used to render with no
 * options, which reads as "this brain has no skills". `skills === null`
 * means we never got the list, and each picker says so instead.
 */
function SkillPickerNote({ error }) {
  if (!error) return null;
  return (
    <ErrorState
      error={error}
      what="the skill list"
      hint="The picker below is empty because the list never arrived, not because this brain has no skills."
      compact
    />
  );
}

/**
 * Enter / Space activation for the `role="button" tabIndex={0}` flow
 * title below. It was focusable with no key handler, so the flow detail
 * pane could not be opened from the keyboard at all. Not a real <button>
 * because it sits inside a card head alongside other controls and a
 * button element would inherit the card's flex sizing; the guard on
 * `e.currentTarget` keeps a nested control's own activation from also
 * opening the pane.
 */
function activateOnKey(fn) {
  return (e) => {
    if (e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
      e.preventDefault();
      fn();
    }
  };
}

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
  // Was `.catch(() => setSkills([]))`, which turned a dropped request
  // into an empty skill picker in all three create modals.
  const { data: skillRows, error: skillsError } = useResource('/skills', {
    select: (d) => asList(d, 'skills'),
  });
  const skills = skillRows || [];

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

      {tab === 'taskflows' && <TaskFlowsTab skills={skills} skillsError={skillsError} />}
      {tab === 'packs' && <PacksTab />}
      {tab === 'routines' && <RoutinesTab skills={skills} skillsError={skillsError} />}
      {tab === 'automations' && <AutomationsTab skills={skills} skillsError={skillsError} />}
    </div>
  );
}

function PacksTab() {
  // The catch here did set an error chip, but it left `packs` at `[]`,
  // so the pane rendered the chip AND "No first-party workflow packs
  // loaded / Check the Brain log for 'Loaded N first-party workflow
  // packs'". That second sentence sent the user to debug a boot that
  // was fine.
  const {
    data: packRows, error: loadError, loading, refresh,
  } = useResource('/api/workflows/packs', { select: (d) => asList(d, 'packs') });
  const packs = packRows || [];
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState(null);
  const [lastCreated, setLastCreated] = useState(null);

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
      title={packRows ? `Workflow packs (${packs.length})` : 'Workflow packs'}
      actions={<button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}><RefreshCw size={13} /></button>}
    >
      {loading && !packRows && <EmptyState title="Loading…" />}
      {loadError && !packRows && (
        <ErrorState error={loadError} what="the workflow packs" onRetry={refresh} />
      )}
      {!loading && !loadError && packRows && packs.length === 0 && (
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

function TaskFlowsTab({ skills, skillsError }) {
  // `try { … } finally` with no catch: the rejection escaped as an
  // unhandled promise rejection every 5s while `flows` stayed `[]`, so
  // an unreachable brain rendered "No flows yet / Create your first
  // flow" over however many flows were actually queued or running.
  // `silent` because a permanent inline ErrorState beats a toast loop
  // on a 5s poll.
  const {
    data: flowRows, error: loadError, loading, refresh,
  } = useResource('/api/taskflows?limit=100', {
    select: (d) => asList(d, 'flows'),
    pollMs: 5000,
    silent: true,
  });
  const flows = flowRows || [];
  const [selected, setSelected] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const [actionError, setActionError] = useState(null);

  const action = async (id, which) => {
    setActionError(null);
    try {
      await apiFetch(`/api/taskflows/${id}/${which}`, { method: 'POST' });
      await refresh();
    } catch (err) {
      setActionError({ id, which, error: toApiError(err) });
    }
  };

  return (
    <>
      <Pane
        title={flowRows ? `TaskFlows (${flows.length})` : 'TaskFlows'}
        actions={(
          <>
            <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}><RefreshCw size={13} /></button>
            <button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowCreate(true)}>
              <Plus size={13} /> New flow
            </button>
          </>
        )}
      >
        {loading && !flowRows && <EmptyState title="Loading…" />}
        {loadError && !flowRows && (
          <ErrorState error={loadError} what="the TaskFlow list" onRetry={refresh} />
        )}
        {/* The poll is silent, so a mid-session outage would otherwise
         * leave a frozen list on screen with nothing to say it is
         * frozen. The rows below are real, just no longer current. */}
        {loadError && flowRows && (
          <ErrorState
            error={loadError}
            what="the latest TaskFlow statuses"
            hint="The rows below are the last successful read and have stopped updating. They are real, but they may be out of date."
            compact
            onRetry={refresh}
          />
        )}
        {actionError && (
          <ErrorState
            error={actionError.error}
            what={`the ${actionError.which} of ${actionError.id}`}
            hint="The flow was left in whatever state it was already in."
            compact
            onRetry={() => action(actionError.id, actionError.which)}
          />
        )}
        {!loading && !loadError && flowRows && flows.length === 0 && (
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
                <StatusDot
                  tone={statusTone(f.status)}
                  pulse={f.status === 'running'}
                  label={`Flow ${f.title || f.id}: ${f.status || 'unknown'}`}
                />
                <div
                  className="v2-flow-card-title"
                  onClick={() => setSelected(f)}
                  onKeyDown={activateOnKey(() => setSelected(f))}
                  role="button"
                  tabIndex={0}
                >
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

      {showCreate && <CreateFlowModal skills={skills} skillsError={skillsError} onClose={() => setShowCreate(false)} onCreated={() => { setShowCreate(false); refresh(); }} />}
      {selected && <FlowDetailModal flow={selected} onClose={() => setSelected(null)} onAction={action} />}
    </>
  );
}

function CreateFlowModal({ skills, skillsError, onClose, onCreated }) {
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
      <SkillPickerNote error={skillsError} />
      <StepBuilder steps={steps} onChange={setSteps} skills={skills} />
      {error && <div className="v2-chip v2-chip--error" style={{ marginTop: 12 }}>{error}</div>}
    </Modal>
  );
}

function FlowDetailModal({ flow, onClose, onAction }) {
  // Was `.catch(() => {})`. The fallback to the list row is genuinely
  // useful, so it stays: the row is real data we already have. What was
  // missing is any sign that the detailed read failed, so the "Steps"
  // count silently reported the row's step count (often 0 for a row the
  // list endpoint summarised) as if it were the flow's real one.
  const {
    data: detail, error, refresh,
  } = useResource(`/api/taskflows/${flow.id}`, { initialData: flow, silent: true });
  const current = detail || flow;
  const steps = current.steps || flow.steps || [];

  return (
    <Modal open onClose={onClose} title={current.title || flow.title || flow.id} size="lg">
      {error && (
        <ErrorState
          error={error}
          what="the full detail for this flow"
          hint="Everything below is the summary row from the list, which may be less complete than the flow itself."
          compact
          onRetry={refresh}
        />
      )}
      <div className="v2-setting-stack">
        <div className="v2-setting-row">
          <div className="v2-setting-label"><div>Status</div></div>
          <div className="v2-setting-control"><StatusDot tone={statusTone(current.status)} label={`Flow status: ${current.status || 'unknown'}`} /> {current.status}</div>
        </div>
        <div className="v2-setting-row">
          <div className="v2-setting-label"><div>ID</div></div>
          <div className="v2-setting-control"><code className="v2-code-inline">{current.id}</code></div>
        </div>
        <div className="v2-setting-row">
          <div className="v2-setting-label"><div>Steps</div></div>
          <div className="v2-setting-control">{steps.length}</div>
        </div>
      </div>
      <ol className="v2-step-detail-list">
        {steps.map((s, i) => (
          <li key={i} className="v2-step-detail-row">
            <StatusDot
              tone={statusTone(s.status)}
              label={`Step ${i + 1} ${s.step_type || s.type || ''}: ${s.status || 'unknown'}`}
            />
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

function RoutinesTab({ skills, skillsError }) {
  // The catch here did set an error chip, but it left `routines` at
  // `[]`, so the pane rendered the chip AND "No routines / Routines run
  // on a cron schedule", i.e. an assertion that nothing is scheduled.
  const {
    data: routineRows, error: loadError, loading, refresh,
  } = useResource('/api/routines', { select: (d) => asList(d, 'routines') });
  const routines = routineRows || [];
  const [err, setErr] = useState('');
  const [showCreate, setShowCreate] = useState(false);

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
        title={routineRows ? `Routines (${routines.length})` : 'Routines'}
        actions={(
          <button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowCreate(true)}>
            <Plus size={13} /> New routine
          </button>
        )}
      >
        {err && <div className="v2-chip v2-chip--error" role="alert">{err}</div>}
        {loading && !routineRows && <EmptyState title="Loading…" />}
        {loadError && !routineRows && (
          <ErrorState error={loadError} what="the routine list" onRetry={refresh} />
        )}
        {!loading && !loadError && routineRows && routines.length === 0 && (
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

      {showCreate && <CreateRoutineModal skills={skills} skillsError={skillsError} onClose={() => setShowCreate(false)} onCreated={() => { setShowCreate(false); refresh(); }} />}
    </>
  );
}

function CreateRoutineModal({ skills, skillsError, onClose, onCreated }) {
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
      <SkillPickerNote error={skillsError} />
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

function AutomationsTab({ skills, skillsError }) {
  // `try { … } finally` with no catch: the rejection escaped as an
  // unhandled promise rejection while `autos` stayed `[]`, so an
  // unreachable brain rendered "No automations" over however many
  // triggers the user has actually armed.
  const {
    data: autoRows, error: loadError, loading, refresh,
  } = useResource('/api/automations', { select: (d) => asList(d, 'automations') });
  const autos = autoRows || [];
  const [showCreate, setShowCreate] = useState(false);
  const [removeError, setRemoveError] = useState(null);

  const remove = async (id) => {
    setRemoveError(null);
    try {
      await apiFetch(`/api/automations/${id}`, { method: 'DELETE' });
      await refresh();
    } catch (err) {
      setRemoveError({ id, error: toApiError(err) });
    }
  };

  return (
    <>
      <Pane
        title={autoRows ? `Automations (${autos.length})` : 'Automations'}
        actions={(
          <button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowCreate(true)}>
            <Plus size={13} /> New automation
          </button>
        )}
      >
        {loading && !autoRows && <EmptyState title="Loading…" />}
        {loadError && !autoRows && (
          <ErrorState error={loadError} what="the automation list" onRetry={refresh} />
        )}
        {removeError && (
          <ErrorState
            error={removeError.error}
            what={`the deletion of ${removeError.id}`}
            hint="The automation is still armed and will still fire on its trigger."
            compact
            onRetry={() => remove(removeError.id)}
          />
        )}
        {!loading && !loadError && autoRows && autos.length === 0 && (
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

      {showCreate && <CreateAutomationModal skills={skills} skillsError={skillsError} onClose={() => setShowCreate(false)} onCreated={() => { setShowCreate(false); refresh(); }} />}
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
function CreateAutomationModal({ skills, skillsError, onClose, onCreated }) {
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
          <SkillPickerNote error={skillsError} />
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
