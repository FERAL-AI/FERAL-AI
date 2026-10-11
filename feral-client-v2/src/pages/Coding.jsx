import React, { useCallback, useEffect, useRef, useState } from 'react';
import { FolderOpen, Code2, Square, Send, Plus, RefreshCw } from 'lucide-react';
import Pane from '../ui/Pane';
import { apiJson } from '../lib/api';
import { installDesktopFolderReceiver, requestDesktopFolder } from '../lib/desktopFolderPicker';
import './Coding.css';

const post = (path, body = {}) => apiJson(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

export function observedActions(turn) {
  const calls = new Map();
  for (const event of turn?.tool_calls || []) {
    if (!event.tool_call_id) continue;
    const previous = calls.get(event.tool_call_id) || {};
    calls.set(event.tool_call_id, { ...previous, ...event, action_kind: event.action_kind || previous.action_kind || '' });
  }
  return [...calls.values()];
}

export function PermissionDetails({ permission }) {
  const tool = permission.details || permission.raw?.toolCall;
  if (!tool) return <p>Action details were not provided. Deny this action if you cannot review its effect.</p>;
  const input = tool.rawInput || {};
  const diffs = (tool.content || []).filter(item => item.type === 'diff');
  return <div className="coding-permission-details">
    {input.command && <><h4>Command to run</h4><pre>{input.command}</pre></>}
    {input.diff && <><h4>Proposed change</h4><pre>{input.diff}</pre></>}
    {diffs.map((diff, index) => <div key={index}><p>{diff.path}</p><h4>Before</h4><pre>{diff.oldText || '(empty file)'}</pre><h4>After</h4><pre>{diff.newText || '(empty file)'}</pre></div>)}
    {!input.command && !input.diff && !diffs.length && <pre>{JSON.stringify(tool, null, 2)}</pre>}
  </div>;
}

export default function Coding() {
  const [overview, setOverview] = useState(null);
  const [path, setPath] = useState('');
  const [model, setModel] = useState('');
  const [base, setBase] = useState('http://127.0.0.1:11434/v1');
  const [prompt, setPrompt] = useState('');
  const [turn, setTurn] = useState(null);
  const [history, setHistory] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const pendingPicker = useRef(null);
  const load = useCallback(async () => {
    const data = await apiJson('/api/coding', { silent: true });
    setOverview(data);
    return data;
  }, []);
  useEffect(() => {
    load().then(data => {
      setPath(data.workspaces?.[0] || '');
      setModel(data.provider?.source_model || data.provider?.model || '');
      setBase(data.provider?.base_url || 'http://127.0.0.1:11434/v1');
    }).catch(e => setError(e.message));
  }, [load]);
  useEffect(() => {
    if (!turn?.session_handle || !['running', 'awaiting_permission'].includes(turn.status)) return undefined;
    let active = true;
    let polling = false;
    const timer = setInterval(async () => {
      if (polling) return;
      polling = true;
      try {
        const data = await apiJson(`/api/coding/sessions/${encodeURIComponent(turn.session_handle)}`, { silent: true });
        if (active) setTurn(data);
      } catch (e) { if (active) setError(e.message); }
      finally { polling = false; }
    }, 2000);
    return () => { active = false; clearInterval(timer); };
  }, [turn?.session_handle, turn?.status]);
  useEffect(() => {
    return installDesktopFolderReceiver(() => pendingPicker.current, data => {
      pendingPicker.current = null;
      if (data.error) setError(data.error);
      else if (data.path) { setPath(data.path); setNotice('Folder selected. Grant it below to start coding.'); }
      else setNotice('Folder selection canceled.');
    });
  }, []);
  const action = async fn => {
    setBusy(true); setError(''); setNotice('');
    try { await fn(); } catch (e) { setError(e.message || 'Coding request failed'); }
    finally { setBusy(false); }
  };
  const choose = () => {
    if (window.parent === window) { setNotice('Enter the project folder path on the brain computer, then grant it.'); return; }
    pendingPicker.current = crypto.randomUUID();
    if (requestDesktopFolder(pendingPicker.current)) {
      setNotice('Choose a folder in the desktop window, or enter its path below.');
    } else {
      pendingPicker.current = null;
      setError('The folder chooser could not open. You can enter a folder path instead.');
    }
  };
  const engine = overview?.agents?.find(item => item.agent_id === 'opencode');
  const working = ['running', 'awaiting_permission'].includes(turn?.status);
  const granted = overview?.workspaces?.includes(path);
  const canRun = engine?.available && overview?.provider?.prepared && granted && !overview?.paused && !working && !busy;
  const actions = observedActions(turn);
  const completedActions = actions.filter(tool => tool.status === 'completed');
  return <main className="coding-page">
    <header className="coding-heading"><div><h1><Code2 size={25} /> Coding</h1><p>Work on a project with OpenCode. Review actions as they happen.</p></div>
      <button onClick={() => action(load)} disabled={busy} aria-label="Refresh coding workspace"><RefreshCw size={17} /></button></header>
    {error && <div role="alert" className="coding-error">{error}</div>}
    {notice && <p role="status" className="coding-notice">{notice}</p>}
    <div className="coding-layout"><aside>
      <Pane title="Workspace">
        <p className="coding-engine">OpenCode · {engine ? (engine.available ? 'Ready' : 'Unavailable') : 'Checking…'}</p>
        {engine && !engine.available && <p>{engine.reason || engine.install_hint || 'Install the bundled coding engine and restart the app.'}</p>}
        <button onClick={choose} disabled={busy || working}><FolderOpen size={17} /> Choose folder</button>
        <label>Project folder<input aria-label="Project folder" value={path} disabled={working || busy} onChange={e => { setPath(e.target.value); setTurn(null); }} placeholder="/path/to/project" /></label>
        {!!overview?.workspaces?.length && <select aria-label="Granted project folders" disabled={working || busy} value={granted ? path : ''} onChange={e => { setPath(e.target.value); setTurn(null); }}>
          <option value="">Choose a granted project</option>{overview.workspaces.map(p => <option key={p} value={p}>{p}</option>)}</select>}
        <button disabled={!path.trim() || busy || working} onClick={() => action(async () => {
          const result = await post('/api/coding/workspaces', { path }); setPath(result.path); await load(); setNotice('Project folder granted.');
        })}>{granted ? 'Folder granted' : 'Grant project folder'}</button>
        <p className="coding-small">The engine runs on the brain computer. Folder selection authorizes this project; this is not an operating system sandbox. Projects containing .opencode settings or plugins are blocked for review.</p>
      </Pane>
      <Pane title="Coding model">
        <p className="coding-engine">{overview?.provider?.prepared ? 'Prepared · 16,384 context tokens' : 'Model preparation required'}</p>
        <p className="coding-small">Uses a separate local Ollama model. Chat provider credentials are not shared.</p>
        <label>Ollama endpoint<input aria-label="Ollama endpoint" value={base} onChange={e => setBase(e.target.value)} /></label>
        <label>Installed model<input aria-label="Installed coding model" value={model} onChange={e => setModel(e.target.value)} placeholder="qwen2.5:3b" /></label>
        <p className="coding-small">Start Ollama and install a tool-capable model on the brain computer, then enter its exact name. Preparing creates a separate local alias with 16,384 context tokens, reusing its downloaded weights. Cloud coding providers are not connected in this workspace.</p>
        <button disabled={busy || working || !model.trim()} onClick={() => action(async () => {
          await post('/api/coding/provider', { model, base_url: base, prepare: true }); setTurn(null); await load(); setNotice('Local coding model prepared with 16,384 context tokens.');
        })}>Prepare coding model</button>
      </Pane>
      <Pane title="Sessions">
        <button disabled={busy || working} onClick={() => { setTurn(null); setPrompt(''); }}><Plus size={16} /> New task</button>
        {(overview?.live_sessions || []).map(s => <button key={s.handle} className="coding-session" disabled={busy || working} onClick={() => action(async () => { setTurn(await apiJson(`/api/coding/sessions/${encodeURIComponent(s.handle)}`)); setPath(s.cwd); })}>{s.cwd} · {s.pending_permissions ? 'Active' : 'Session'}</button>)}
        <button disabled={busy} onClick={() => action(async () => setHistory(await apiJson('/api/coding/activity')))}>Recall coding activity</button>
      </Pane>
    </aside><section className="coding-work">
      <Pane title={turn ? (turn.status === 'completed' ? 'Agent finished' : `Task · ${turn.status.replaceAll('_', ' ')}`) : 'Start a coding task'} actions={working && <button disabled={busy} onClick={() => action(async () => {
        await post(`/api/coding/sessions/${encodeURIComponent(turn.session_handle)}/cancel`); setTurn(t => ({ ...t, status: 'interrupted', pending_permissions: [] })); await load();
      })}><Square size={15} /> Stop task</button>}>
        {!turn && <p>Choose a project and a local model, then describe the change. You can continue a session after a task finishes.</p>}
        {overview?.paused && <p role="status">The agent is paused. Resume it in Oversight before starting or approving work.</p>}
        {turn && <p className="coding-small">{completedActions.length} completed tool actions observed{completedActions.some(tool => tool.action_kind === 'execute') ? ' · Command execution observed' : ' · No command execution observed'}. Agent messages do not verify tests.</p>}
        {turn?.text && <><h3>Agent message</h3><pre className="coding-output">{turn.text}</pre></>}
        {turn?.error && <p role="alert" className="coding-error">{turn.error}</p>}
        {actions.length > 0 && <h3>Observed tool results</h3>}
        {actions.map(tool => <details key={tool.tool_call_id}><summary>{tool.title || tool.tool_name || 'Tool action'} · {tool.status || ''}</summary><pre>{tool.text || JSON.stringify(tool, null, 2)}</pre></details>)}
        {(turn?.pending_permissions || []).map(permission => <div className="coding-permission" key={permission.request_id}>
          <h3>Approval needed</h3><p>{permission.title || permission.tool_name}</p>
          <PermissionDetails permission={permission} />
          <div className="coding-actions">{['allow_once', 'reject_once'].filter(kind => permission.options?.some(o => o.kind === kind)).map(decision => <button key={decision} disabled={busy || (decision === 'allow_once' && overview?.paused)} onClick={() => action(async () => {
            setTurn(await post(`/api/coding/permissions/${encodeURIComponent(permission.request_id)}`, { decision })); await load();
          })}>{decision === 'allow_once' ? 'Allow this action' : 'Deny this action'}</button>)}</div>
        </div>)}
        {turn?.memory && <p className="coding-small">{turn.memory.recorded ? 'Activity saved to memory.' : 'Activity memory was not saved.'}</p>}
        <form onSubmit={e => { e.preventDefault(); action(async () => {
          const data = await post('/api/coding/tasks', { prompt, workspace_dir: path, session_handle: turn?.status === 'interrupted' ? '' : turn?.session_handle || '' });
          setTurn(data); setPrompt(''); await load();
        }); }}>
          <label htmlFor="coding-task">What should we build or fix?</label>
          <textarea id="coding-task" value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="Explain the change and how to check it…" rows={5} disabled={working} />
          <button type="submit" disabled={!canRun || !prompt.trim()}><Send size={16} /> {turn && turn.status !== 'interrupted' ? 'Continue session' : 'Start task'}</button>
        </form>
      </Pane>
      {history && <Pane title="Coding activity"><p className="coding-small">Recorded activity comes from the brain memory; session pointers do not guarantee restored agent history.</p><pre className="coding-output">{JSON.stringify(history, null, 2)}</pre></Pane>}
    </section></div>
  </main>;
}
