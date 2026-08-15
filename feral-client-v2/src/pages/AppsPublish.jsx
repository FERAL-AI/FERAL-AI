/**
 * AppsPublish — the developer flow for publishing a GenUI app to this brain
 * (and, eventually, the public FERAL registry).
 *
 * This is the page that actually explains what a publisher has to do — not a
 * two-field modal jammed into /canvas. It's organised as a linear wizard:
 *
 *   1. Scaffold   — show the CLI that initialises a new app bundle.
 *   2. Author     — quick tour of AppManifest + surfaces + actions.
 *   3. Validate   — paste a manifest (YAML/JSON) and check it locally.
 *   4. Install    — install from a local folder, a git URL, or a registry id.
 *   5. Publish    — show the CLI that signs + publishes to the registry.
 *
 * Each step is a separate Pane so the visual rhythm matches the rest of the
 * app. No fake fields; every input/CLI example corresponds to a real route
 * on the brain or a real `feral app …` command.
 */
import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowLeft, CheckCircle2, XCircle, Terminal, Package, GitBranch, Folder,
  FileCheck2, Rocket, BookOpen,
} from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import CodeEditor from '../ui/CodeEditor';
import CopyButton from '../ui/CopyButton';
import ErrorState from '../ui/ErrorState';
import { apiFetch } from '../lib/api';
import { useResource } from '../hooks/useResource';

// Example manifest content, not FERAL chrome. The #6F4E37 below is the
// fictional Coffee Log app's own brand mark and is meant to show that a
// third-party app picks its own primary_color. It is deliberately not a
// design token.
const SAMPLE_MANIFEST = `app_id: coffee-log
version: 0.1.0
author: Jane Dev
description: A tiny GenUI app that logs and reviews espresso shots.

brand:
  name: Coffee Log
  primary_color: "#6F4E37"

permissions: ["storage.read", "storage.write"]

entry_surface_id: home

surfaces:
  - surface_id: home
    kind: authored
    title: Today
    template_root:
      type: stack
      children:
        - { type: heading, text: "Espresso shots today" }
        - { type: list, items: "$data.shots" }
        - { type: button, label: "Log a shot", action_id: log_shot }
    action_contract:
      - action_id: log_shot
        handler: emit_event
        target: coffee.shot_logged

  - surface_id: review
    kind: hybrid
    title: Weekly review
    data_schema_ref: shot_summary
    generation_prompt: >
      Summarise the user's espresso shots for the week. Highlight trends
      in extraction time and taste notes. Keep it under 120 words.
    template_root:
      type: stack
      children:
        - { type: heading, text: "Weekly review" }
        - { type: text, value: "Summary will be generated here." }

data_schemas:
  - schema_id: shot_summary
    schema: { type: object }
`;

// The local `useCopy` this file used to carry called
// `navigator.clipboard.writeText(text)` inside a synchronous `try` and
// then set the copied flag unconditionally. `writeText` returns a
// promise; on an insecure origin or a denied permission it rejects
// after the try block has already exited, so the checkmark appeared for
// a clipboard that was never written. ui/CopyButton awaits the write
// and confirms only what actually happened.
function CliBlock({ id, label, cmd }) {
  return (
    <div className="v2-publish-cli">
      {label && <div className="v2-publish-cli-label">{label}</div>}
      <div className="v2-publish-cli-row">
        <Terminal size={13} aria-hidden="true" />
        <code>{cmd}</code>
        <CopyButton value={cmd} label={`Copy command: ${cmd}`} testId={`cli-copy-${id}`} />
      </div>
    </div>
  );
}

export default function AppsPublish() {
  return (
    <div className="v2-page v2-page--stack v2-publish" data-testid="v2-marker">
      <Pane
        title="Publish a GenUI app"
        actions={(
          <Link to="/apps" className="v2-btn v2-btn--ghost">
            <ArrowLeft size={13} /> Back to apps
          </Link>
        )}
      >
        <p className="v2-p">
          A GenUI app is a manifest + a few surface templates. You describe your
          screens, actions, and data — FERAL generates, caches per user, and can
          regenerate any surface on demand. Nothing here is fake; every step
          below talks to a real <code>/api/apps/*</code> route or runs the real
          <code> feral app</code> CLI you already have installed.
        </p>
        <p className="v2-p v2-p--muted">
          Start at step 1 if you're new. Jump to step 3 to validate an existing
          manifest, or step 4 to install one you're actively developing.
        </p>
      </Pane>

      <ScaffoldStep />
      <AuthorStep />
      <ValidateStep />
      <InstallStep />
      <PublishStep />
      <DocsStep />
    </div>
  );
}

function ScaffoldStep() {
  return (
    <Pane
      title="1 · Scaffold a new app"
      actions={<Package size={14} aria-hidden="true" />}
    >
      <p className="v2-p v2-p--muted">
        The CLI lays out a folder with a minimal <code>manifest.yaml</code>, a
        <code> README.md</code>, and an example authored surface you can edit.
      </p>
      <CliBlock id="scaffold" cmd="feral app init coffee-log" />
      <p className="v2-p v2-p--muted v2-p--tiny" style={{ marginTop: 6 }}>
        The first argument is your <code>app_id</code> — a DNS-safe slug
        (lowercase letters, digits, hyphens; 3-64 chars; starts with a letter).
        It's how the registry de-duplicates apps.
      </p>
    </Pane>
  );
}

function AuthorStep() {
  return (
    <Pane
      title="2 · Author surfaces, actions, data"
      actions={<BookOpen size={14} aria-hidden="true" />}
    >
      <p className="v2-p v2-p--muted">
        An <code>AppManifest</code> has three moving parts:
      </p>
      <ul className="v2-publish-bullets">
        <li>
          <strong>Surfaces</strong> — the screens. Each is either
          <code> authored</code> (you ship the SDUI tree), <code>generated</code>{' '}
          (FERAL produces the tree from a prompt + data schema), or
          <code> hybrid</code> (authored template you allow the agent to
          re-author per user and cache).
        </li>
        <li>
          <strong>Action contract</strong> — every surface declares its
          <code> action_contract</code>: the buttons/forms a user can fire plus
          the handler (<code>app_event</code>, <code>emit_event</code>,
          <code> open_surface</code>, <code>tool_call</code>) and target the
          brain validates against. Any action fired at runtime is rejected
          if it isn't on the contract.
        </li>
        <li>
          <strong>Data schemas</strong> — named JSON Schemas surfaces hydrate
          against. A surface's <code>$data.*</code> placeholders are resolved
          from the schema and the live payload before rendering.
        </li>
      </ul>
      <p className="v2-p v2-p--muted" style={{ marginTop: 10 }}>
        Here's a minimal end-to-end example with one authored + one hybrid
        surface. Copy it into the next step and validate:
      </p>
      <CodeEditor
        value={SAMPLE_MANIFEST}
        readOnly
        language="yaml"
        rows={18}
        aria-label="Sample manifest"
      />
    </Pane>
  );
}

function ValidateStep() {
  const [text, setText] = useState(SAMPLE_MANIFEST);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  const validate = async () => {
    setBusy(true);
    setResult(null);
    try {
      const r = await apiFetch('/api/apps/validate', {
        method: 'POST',
        body: JSON.stringify({ manifest: text }),
      });
      const body = await r.json().catch(() => ({}));
      setResult({ ok: r.ok, body });
    } catch (e) {
      setResult({ ok: false, body: { error: e?.message || 'network error' } });
    } finally { setBusy(false); }
  };

  return (
    <Pane
      title="3 · Validate your manifest"
      actions={(
        <button
          type="button"
          className="v2-btn v2-btn--primary"
          onClick={validate}
          disabled={busy || !text.trim()}
        >
          <FileCheck2 size={13} /> {busy ? 'Validating…' : 'Validate'}
        </button>
      )}
    >
      <p className="v2-p v2-p--muted">
        Runs the exact same <code>AppManifest</code> pydantic validator the
        brain uses at install time. Catches missing cross-refs, unknown
        surface kinds, action targets without a surface, and duplicate ids.
      </p>
      <CodeEditor
        value={text}
        onChange={setText}
        language="yaml"
        rows={18}
        aria-label="Manifest to validate"
      />
      {result && (
        <Glass level={0} radius="md" padding="md" style={{ marginTop: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
            {result.ok
              ? <span className="v2-chip v2-chip--live"><CheckCircle2 size={12} /> manifest valid</span>
              : <span className="v2-chip v2-chip--error"><XCircle size={12} /> invalid</span>}
          </div>
          {!result.ok && (
            <pre className="v2-publish-error">
              {typeof result.body === 'string'
                ? result.body
                : JSON.stringify(result.body, null, 2)}
            </pre>
          )}
          {result.ok && result.body?.summary && (
            <dl className="v2-publish-summary">
              <dt>app_id</dt><dd>{result.body.summary.app_id}</dd>
              <dt>surfaces</dt><dd>{(result.body.summary.surfaces || []).join(', ') || '—'}</dd>
              <dt>actions</dt><dd>{(result.body.summary.actions || []).join(', ') || '—'}</dd>
              <dt>permissions</dt><dd>{(result.body.summary.permissions || []).join(', ') || '—'}</dd>
            </dl>
          )}
        </Glass>
      )}
    </Pane>
  );
}

function InstallStep() {
  const [tab, setTab] = useState('path');
  const [path, setPath] = useState('');
  const [gitUrl, setGitUrl] = useState('');
  const [registryId, setRegistryId] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);

  const install = async (payload) => {
    setBusy(true);
    setResult(null);
    try {
      const r = await apiFetch('/api/apps/install', {
        method: 'POST',
        body: JSON.stringify({ overwrite: true, ...payload }),
      });
      const body = await r.json().catch(() => ({}));
      setResult({ ok: r.ok, body });
    } catch (e) {
      setResult({ ok: false, body: { error: e?.message || 'network error' } });
    } finally { setBusy(false); }
  };

  return (
    <Pane
      title="4 · Install on this brain"
      actions={<Folder size={14} aria-hidden="true" />}
    >
      <p className="v2-p v2-p--muted">
        Installs straight from disk, a public git URL, or the FERAL registry
        once you've published. Installed apps show up on <Link to="/apps">/apps</Link>.
      </p>

      <div className="v2-publish-tabs">
        <button type="button" className={`v2-btn${tab === 'path' ? ' v2-btn--primary' : ' v2-btn--ghost'}`} onClick={() => setTab('path')}>
          <Folder size={13} /> Local folder
        </button>
        <button type="button" className={`v2-btn${tab === 'git' ? ' v2-btn--primary' : ' v2-btn--ghost'}`} onClick={() => setTab('git')}>
          <GitBranch size={13} /> Git URL
        </button>
        <button type="button" className={`v2-btn${tab === 'registry' ? ' v2-btn--primary' : ' v2-btn--ghost'}`} onClick={() => setTab('registry')}>
          <Package size={13} /> Registry id
        </button>
      </div>

      {tab === 'path' && (
        <div className="v2-publish-form">
          <label className="v2-identity-field">
            <span className="v2-identity-field-label">Absolute path to the app folder</span>
            <input
              type="text"
              className="v2-input"
              placeholder="/Users/you/projects/coffee-log"
              value={path}
              onChange={(e) => setPath(e.target.value)}
            />
          </label>
          <button type="button" className="v2-btn v2-btn--primary" disabled={busy || !path.trim()} onClick={() => install({ path: path.trim() })}>
            {busy ? 'Installing…' : 'Install from path'}
          </button>
        </div>
      )}

      {tab === 'git' && (
        <div className="v2-publish-form">
          <label className="v2-identity-field">
            <span className="v2-identity-field-label">Public git URL</span>
            <input
              type="text"
              className="v2-input"
              placeholder="https://github.com/you/coffee-log.git"
              value={gitUrl}
              onChange={(e) => setGitUrl(e.target.value)}
            />
          </label>
          <button type="button" className="v2-btn v2-btn--primary" disabled={busy || !gitUrl.trim()} onClick={() => install({ git_url: gitUrl.trim() })}>
            {busy ? 'Cloning + installing…' : 'Install from git'}
          </button>
        </div>
      )}

      {tab === 'registry' && (
        <div className="v2-publish-form">
          <label className="v2-identity-field">
            <span className="v2-identity-field-label">Registry id</span>
            <input
              type="text"
              className="v2-input"
              placeholder="com.example.coffee"
              value={registryId}
              onChange={(e) => setRegistryId(e.target.value)}
            />
          </label>
          <p className="v2-p v2-p--muted v2-p--tiny">
            Registry-backed installs require the public FERAL registry; the
            brain will return <code>501</code> if it isn't wired yet. Use local
            path or git URL in the meantime.
          </p>
          <button type="button" className="v2-btn v2-btn--primary" disabled={busy || !registryId.trim()} onClick={() => install({ registry_id: registryId.trim() })}>
            {busy ? 'Installing…' : 'Install from registry'}
          </button>
        </div>
      )}

      <p className="v2-p v2-p--muted v2-p--tiny" style={{ marginTop: 8 }}>
        Or from your terminal:
      </p>
      <CliBlock id="cli-install" cmd="feral app install ./coffee-log" />

      {result && (
        <Glass level={0} radius="md" padding="md" style={{ marginTop: 10 }}>
          {result.ok
            ? <span className="v2-chip v2-chip--live"><CheckCircle2 size={12} /> installed {result.body?.app?.app_id}</span>
            : <span className="v2-chip v2-chip--error"><XCircle size={12} /> {result.body?.detail || result.body?.error || 'install failed'}</span>}
          {result.ok && (
            <div style={{ marginTop: 8 }}>
              <Link to={`/apps/${encodeURIComponent(result.body.app.app_id)}`} className="v2-btn v2-btn--primary">
                Open {result.body.app.brand?.name || result.body.app.app_id}
              </Link>
            </div>
          )}
        </Glass>
      )}
    </Pane>
  );
}

function PublishStep() {
  return (
    <Pane
      title="5 · Publish to the registry"
      actions={<Rocket size={14} aria-hidden="true" />}
    >
      <p className="v2-p v2-p--muted">
        Signs your manifest with your publisher key and uploads the bundle
        to the registry at <code>registry.feral.sh</code>. Users install it
        by <code>app_id</code> from <Link to="/marketplace">/marketplace</Link>.
      </p>
      <CliBlock id="publisher-key" label="One-time: create a publisher key" cmd="feral publisher init" />
      <CliBlock id="build-bundle" label="Build a signed bundle" cmd="feral app build ./coffee-log" />
      <CliBlock id="publish-bundle" label="Publish to the registry" cmd="feral app publish ./coffee-log" />
      <p className="v2-p v2-p--muted v2-p--tiny" style={{ marginTop: 6 }}>
        The registry validates the manifest again server-side and rejects anything
        that fails the same pydantic checks step 3 runs locally.
      </p>
    </Pane>
  );
}

function DocsStep() {
  // Was: `.catch(() => setHealth(null))` feeding `{health ? count : '…'}`,
  // so a failed /api/apps left an ellipsis on screen forever. That is
  // the same lie as a false empty state wearing a spinner: it says the
  // answer is still coming when the request already failed.
  const {
    data: health, error, loading, refresh,
  } = useResource('/api/apps', {
    select: (d) => ({ count: d?.count ?? (d?.apps?.length || 0) }),
  });

  return (
    <Pane title="Live state">
      {loading && !health && (
        <p className="v2-p v2-p--muted">Counting the apps installed on this brain…</p>
      )}
      {error && !health && (
        <ErrorState
          error={error}
          what="the installed app count"
          hint="Your apps are unaffected; the client just could not read the count."
          compact
          onRetry={refresh}
        />
      )}
      {health && (
        <p className="v2-p v2-p--muted">
          Apps currently installed on this brain: <strong>{health.count}</strong>.
        </p>
      )}
      <p className="v2-p v2-p--muted">
        Open <Link to="/apps">/apps</Link> for the user-facing launcher.
      </p>
      <p className="v2-p v2-p--muted v2-p--tiny" style={{ marginTop: 6 }}>
        Developer references:
      </p>
      <ul className="v2-publish-bullets">
        <li><code>ASOS/feral-core/models/app_manifest.py</code> — manifest pydantic schema.</li>
        <li><code>ASOS/feral-core/agents/app_registry.py</code> — install / open / dispatch runtime.</li>
        <li><code>ASOS/feral-core/agents/hybrid_genui.py</code> — authored / generated / hybrid render.</li>
        <li><code>ASOS/examples/apps/feral-rides</code> — hybrid surface reference app.</li>
        <li><code>ASOS/examples/apps/feral-messages</code> — authored-only reference app.</li>
      </ul>
    </Pane>
  );
}
