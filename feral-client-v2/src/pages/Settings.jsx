import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import EmptyState from '../ui/EmptyState';
import StatusDot from '../ui/StatusDot';
import CodeEditor from '../ui/CodeEditor';
import { SelfWorkspace } from '../components/SelfEditors';
import { apiFetch, apiJson } from '../lib/api';
import { API_BASE } from '../lib/config';

/**
 * Settings — sixteen real sections. Self is the first section users
 * expect to find for "about me / my agent's personality" and it embeds
 * the same IDENTITY / SOUL / MEMORY editors that live at /identity so
 * users never have to hunt through the ⌘K hub to find them.
 *
 * `?section=Cost&call_site=chat` deeplink (S6 budget banner) lands the
 * user directly on the Cost panel with the relevant call-site
 * pre-selected.
 */

const SECTIONS = [
  'Self', 'General', 'Providers', 'Memory', 'Channels', 'Autonomy', 'Voice',
  'Access', 'Twin', 'Security', 'Integrations', 'Cost', 'Sync', 'Handoff', 'Push', 'MCP',
];

export default function Settings() {
  const [searchParams] = useSearchParams();
  const initialSection = (() => {
    const q = searchParams.get('section');
    if (q && SECTIONS.includes(q)) return q;
    return 'Self';
  })();
  const [section, setSection] = useState(initialSection);
  // Sync the URL `?section=` so the budget-banner deeplink + browser
  // back/forward never desync from the rendered panel.
  useEffect(() => {
    const q = searchParams.get('section');
    if (q && SECTIONS.includes(q) && q !== section) setSection(q);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  return (
    <div className="v2-page v2-page--split" data-testid="v2-marker">
      <aside className="v2-settings-nav">
        <Glass level={1} radius="lg" padding="sm">
          <ul className="v2-settings-list">
            {SECTIONS.map((s) => (
              <li key={s}>
                <button
                  type="button"
                  className={`v2-settings-btn${section === s ? ' is-active' : ''}`}
                  onClick={() => setSection(s)}
                  data-testid={`settings-tab-${s.toLowerCase()}`}
                >
                  {s}
                </button>
              </li>
            ))}
          </ul>
        </Glass>
      </aside>
      <Pane title={section}>
        {section === 'Self' && <SelfSection />}
        {section === 'General' && <GeneralSection />}
        {section === 'Providers' && <ProvidersSection />}
        {section === 'Memory' && <MemorySection />}
        {section === 'Channels' && <ChannelsSection />}
        {section === 'Autonomy' && <AutonomySection />}
        {section === 'Voice' && <VoiceSection />}
        {section === 'Access' && <AccessSection />}
        {section === 'Security' && <SecuritySection />}
        {section === 'Integrations' && <IntegrationsSection />}
        {section === 'Cost' && <CostSection initialCallSite={searchParams.get('call_site') || ''} />}
        {section === 'Sync' && <SyncSection />}
        {section === 'Handoff' && <HandoffSection />}
        {section === 'Push' && <PushSection />}
        {section === 'MCP' && <McpSection />}
        {section === 'Twin' && <TwinSection />}
      </Pane>
    </div>
  );
}

function SelfSection() {
  return (
    <>
      <p className="v2-p v2-p--muted">
        Your agent's personality + what it knows about you.
        Same editors you'll find at <code>/identity</code>.
      </p>
      <SelfWorkspace defaultTab="identity" showIntro={false} />
    </>
  );
}

// ── Shared primitives ─────────────────────────────────────────

function useConfig() {
  const [config, setConfig] = useState(null);
  const [error, setError] = useState(null);
  const refresh = useCallback(async () => {
    try { setConfig(await apiJson('/api/config')); } catch (e) { setError(e.message); }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);
  const update = useCallback(async (sec, key, value) => {
    const r = await apiFetch('/api/config/update', {
      method: 'POST',
      body: JSON.stringify({ section: sec, key, value }),
    });
    if (!r.ok) throw new Error(`${r.status}`);
    await refresh();
  }, [refresh]);
  return { config, error, refresh, update };
}

function Row({ label, hint, children }) {
  return (
    <div className="v2-setting-row">
      <div className="v2-setting-label">
        <div>{label}</div>
        {hint && <div className="v2-setting-hint">{hint}</div>}
      </div>
      <div className="v2-setting-control">{children}</div>
    </div>
  );
}

function Toggle({ checked, disabled, onChange }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`v2-toggle${checked ? ' is-on' : ''}`}
    >
      <span className="v2-toggle-thumb" />
    </button>
  );
}

function Select({ value, options, onChange, disabled }) {
  return (
    <select className="v2-select" value={value ?? ''} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  );
}

function Status({ tone = 'neutral', children }) {
  return <span className={`v2-chip v2-chip--${tone}`}>{children}</span>;
}

function formatApiDetail(body, fallback = 'request failed') {
  if (!body || typeof body !== 'object') return fallback;
  const detail = body.detail;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (detail && typeof detail === 'object') {
    const message = typeof detail.message === 'string' ? detail.message.trim() : '';
    const remediation = typeof detail.remediation === 'string' ? detail.remediation.trim() : '';
    const code = typeof detail.code === 'string' ? detail.code.trim() : '';
    if (message && remediation) return `${message} ${remediation}`;
    if (message) return message;
    if (remediation) return remediation;
    if (code) return code;
  }
  if (typeof body.error === 'string' && body.error.trim()) return body.error.trim();
  return fallback;
}

// ── Access ───────────────────────────────────────────────────

function AccessSection() {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState(null);
  const [message, setMessage] = useState('');

  const refresh = useCallback(async () => {
    try {
      const snap = await apiJson('/api/access/status');
      setStatus(snap);
      setError(null);
    } catch (e) {
      setError(e?.message || 'failed to fetch access status');
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const setMode = useCallback(async (mode) => {
    setBusy(mode);
    setError(null);
    setMessage('');
    try {
      const r = await apiFetch('/api/config/update', {
        method: 'POST',
        body: JSON.stringify({ section: 'access', key: 'pairing_mode', value: mode }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(formatApiDetail(body, `failed to set mode (${r.status})`));
      }
      setMessage(`Pairing mode set to ${mode}.`);
      await refresh();
    } catch (e) {
      setError(e?.message || 'failed to set pairing mode');
    } finally {
      setBusy('');
    }
  }, [refresh]);

  const remoteUp = useCallback(async () => {
    setBusy('remote-up');
    setError(null);
    setMessage('');
    try {
      const r = await apiFetch('/api/access/remote-up', { method: 'POST' });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(formatApiDetail(body, `failed to enable remote mode (${r.status})`));
      }
      const body = await r.json().catch(() => ({}));
      setMessage(`Anywhere mode enabled${body?.remote_url ? `: ${body.remote_url}` : ''}`);
      await refresh();
    } catch (e) {
      setError(e?.message || 'failed to enable remote mode');
    } finally {
      setBusy('');
    }
  }, [refresh]);

  const remoteDown = useCallback(async () => {
    setBusy('remote-down');
    setError(null);
    setMessage('');
    try {
      const r = await apiFetch('/api/access/remote-down', { method: 'POST' });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(formatApiDetail(body, `failed to disable remote mode (${r.status})`));
      }
      setMessage('Anywhere mode disabled. Pairing reverted to This Mac only.');
      await refresh();
    } catch (e) {
      setError(e?.message || 'failed to disable remote mode');
    } finally {
      setBusy('');
    }
  }, [refresh]);

  if (!status) {
    return (
      <div className="v2-setting-stack">
        {error ? <div className="v2-chip v2-chip--error">{error}</div> : <EmptyState title="Loading access mode…" />}
      </div>
    );
  }

  const mode = status.pairing_mode || 'localhost';
  const ts = status.tailscale || {};
  const funnel = status.funnel || {};
  const modeLabel = mode === 'local' ? 'Same WiFi' : mode === 'remote' ? 'Anywhere' : 'This Mac only';

  return (
    <div className="v2-setting-stack" data-testid="settings-access-section">
      <Row label="Current pairing mode" hint="How phones reach this brain">
        <Status tone={mode === 'remote' ? 'live' : mode === 'local' ? 'warn' : 'neutral'}>{modeLabel}</Status>
      </Row>

      <Row label="Quick switch" hint="Set LAN or local-only mode">
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button
            type="button"
            className="v2-btn"
            disabled={busy === 'local'}
            onClick={() => setMode('local')}
            data-testid="settings-access-mode-local"
          >
            Same WiFi
          </button>
          <button
            type="button"
            className="v2-btn"
            disabled={busy === 'localhost'}
            onClick={() => setMode('localhost')}
            data-testid="settings-access-mode-localhost"
          >
            This Mac only
          </button>
        </div>
      </Row>

      <Row label="Anywhere (Tailscale)" hint="Enable or disable remote tunnel mode">
        <div style={{ display: 'grid', gap: 8 }}>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button
              type="button"
              className="v2-btn v2-btn--primary"
              disabled={busy === 'remote-up'}
              onClick={remoteUp}
              data-testid="settings-access-remote-up"
            >
              Enable Anywhere
            </button>
            <button
              type="button"
              className="v2-btn"
              disabled={busy === 'remote-down'}
              onClick={remoteDown}
              data-testid="settings-access-remote-down"
            >
              Disable Anywhere
            </button>
            <button
              type="button"
              className="v2-btn v2-btn--ghost"
              disabled={busy === 'refresh'}
              onClick={async () => { setBusy('refresh'); try { await refresh(); } finally { setBusy(''); } }}
              data-testid="settings-access-refresh"
            >
              Refresh status
            </button>
          </div>
          <div className="v2-p v2-p--tiny v2-p--muted" data-testid="settings-access-remote-url">
            Remote URL: {status.remote_url || '(none)'}
          </div>
        </div>
      </Row>

      <Row label="Tailscale status" hint="Live daemon/login/funnel snapshot">
        <div style={{ display: 'grid', gap: 6 }}>
          <div className="v2-p v2-p--tiny">
            Installed: <strong>{ts.installed ? 'yes' : 'no'}</strong> · Running: <strong>{ts.running ? 'yes' : 'no'}</strong> · Logged in: <strong>{ts.logged_in ? 'yes' : 'no'}</strong>
          </div>
          <div className="v2-p v2-p--tiny">
            Funnel: <strong>{funnel.active ? 'active' : 'inactive'}</strong>{Array.isArray(funnel.ports) && funnel.ports.length > 0 ? ` (ports: ${funnel.ports.join(', ')})` : ''}
          </div>
          {!!ts.dns_name && <div className="v2-p v2-p--tiny">DNS: <code>{ts.dns_name}</code></div>}
          {!!ts.tailnet && <div className="v2-p v2-p--tiny">Tailnet: <code>{ts.tailnet}</code></div>}
          {!!ts.error && <div className="v2-chip v2-chip--warn">Tailscale status: {ts.error}</div>}
        </div>
      </Row>

      {message && <div className="v2-chip v2-chip--live" data-testid="settings-access-message">{message}</div>}
      {error && <div className="v2-chip v2-chip--error" data-testid="settings-access-error">{error}</div>}
    </div>
  );
}

// ── General ───────────────────────────────────────────────────

function GeneralSection() {
  const { config, update } = useConfig();
  const [busy, setBusy] = useState('');
  if (!config) return <EmptyState title="Loading config…" />;
  const features = config.features || {};
  const featureRow = (key, label, hint) => (
    <Row label={label} hint={hint} key={key}>
      <Toggle
        checked={!!features[key]}
        disabled={busy === key}
        onChange={async (next) => { setBusy(key); try { await update('features', key, next); } finally { setBusy(''); } }}
      />
    </Row>
  );
  return (
    <div className="v2-setting-stack">
      <Row label="Version" hint="Current feral-ai build"><code className="v2-code-inline">{config.version || '—'}</code></Row>
      {featureRow('streaming', 'Streaming replies', 'Token-by-token output')}
      {featureRow('proactive', 'Proactive alerts', 'Brain surfaces things without being asked')}
      {featureRow('self_learning', 'Self-learning', 'Enables Tool Genesis + pattern learning')}
      {featureRow('multi_agent', 'Multi-agent', 'Lets orchestrator spawn specialist sub-agents')}
      {featureRow('vision', 'Vision loop', 'Periodic screen-captioning for ambient context')}
    </div>
  );
}

// ── Providers ─────────────────────────────────────────────────

function ProvidersSection() {
  const [status, setStatus] = useState(null);
  const [providers, setProviders] = useState([]);
  const [presets, setPresets] = useState([]);
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null); // currently-edited provider card
  // Per-provider refresh counter bumped by ProviderKeysCard when the
  // user switches the active labeled key. Open ProviderForm instances
  // watch their entry and re-fetch models so the dropdown reflects
  // what the new key can actually see (Lane 3 U3 fix #4).
  const [keysRefreshTokens, setKeysRefreshTokens] = useState({});
  const bumpKeysRefresh = useCallback((pid) => {
    if (!pid) return;
    setKeysRefreshTokens((prev) => ({ ...prev, [pid]: (prev[pid] || 0) + 1 }));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [s, providersResp, presetsResp, h] = await Promise.all([
        apiJson('/api/llm/status').catch(() => null),
        apiJson('/api/llm/providers').catch(() => ({ providers: [] })),
        apiJson('/api/llm/presets').catch(() => ({ presets: [] })),
        apiJson('/api/llm/health').catch(() => null),
      ]);
      if (s) setStatus(s);
      setProviders(providersResp.providers || providersResp || []);
      setPresets(presetsResp.presets || []);
      setHealth(h);
      setError(null);
    } catch (e) {
      setError(e?.message || 'failed to load provider catalog');
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const applyPreset = async (preset) => {
    await apiFetch('/api/llm/presets/apply', {
      method: 'POST',
      body: JSON.stringify({ preset }),
    });
    refresh();
  };

  return (
    <div className="v2-providers">
      {error && <div className="v2-chip v2-chip--error">{error}</div>}

      <div className="v2-providers-current">
        <div>
          <div className="v2-stat-label">Current provider</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 4 }}>
            <StatusDot tone={status?.available ? 'live' : 'warn'} pulse={!!status?.available} />
            <strong>{status?.provider || 'none'}</strong>
            <span className="v2-p v2-p--muted">{status?.model || ''}</span>
          </div>
          <div className="v2-p v2-p--muted v2-p--tiny">
            {status?.available
              ? 'Live inference backend — reconfigure via any card below.'
              : 'No available provider. Configure one below.'}
          </div>
        </div>
        <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}>Refresh</button>
      </div>

      {presets.length > 0 && (
        <div className="v2-providers-presets">
          <div className="v2-stat-label" style={{ marginRight: 6 }}>Presets</div>
          {presets.map((p) => (
            <button
              key={p.id || p.preset}
              type="button"
              className="v2-btn"
              onClick={() => applyPreset(p.id || p.preset)}
            >
              {p.label || p.id || p.preset}
            </button>
          ))}
        </div>
      )}

      {health && (
        <FallbacksCard
          health={health}
          onChange={async (nextList) => {
            await apiFetch('/api/config/update', {
              method: 'POST',
              body: JSON.stringify({
                section: 'llm',
                key: 'fallback_providers',
                value: nextList,
              }),
            });
            refresh();
          }}
        />
      )}

      <div className="v2-providers-grid">
        {providers.map((p) => {
          const pid = p.id || p.provider_id;
          const isCurrent = (status?.provider || '').toLowerCase() === (pid || '').toLowerCase();
          return (
            <ProviderCard
              key={pid}
              provider={{ ...p, provider_id: pid }}
              isCurrent={isCurrent}
              // Seed the Reconfigure form with the runtime model (not
              // the cloud descriptor's empty default_model). Lane 3 U3
              // fix #1: previously selectedModel fell back to list[0]
              // of the recommended catalog and Save & apply silently
              // swapped the brain model on key rotation.
              activeModel={isCurrent ? (status?.model || '') : ''}
              keysRefreshToken={keysRefreshTokens[pid] || 0}
              isEditing={selected === pid}
              onEdit={() => setSelected(pid)}
              onCancel={() => setSelected(null)}
              onSaved={() => { setSelected(null); refresh(); }}
            />
          );
        })}
      </div>

      {/*
       * Multi-key + tier router (Lane 09 Wave 2 consumer).
       * Lists every labeled key per provider with per-key probe,
       * "Make active" + "Remove", and a row to add a new label. Below
       * that, a Tier picker per call-site (chat / routing / vision /
       * embedding) hits `/api/llm/route?call_site=X&tier=Y` so users
       * can pre-flight what provider + model would be selected for
       * each tier without sending a chat.
       */}
      <ProviderKeysCard providers={providers} onActiveChange={bumpKeysRefresh} />
      <TierRouteCard />
    </div>
  );
}

// ── Multi-key + tier (Lane 09 consumer) ──────────────────────

const TIER_OPTIONS = [
  { id: 'cheap', label: 'Cheap' },
  { id: 'balanced', label: 'Balanced' },
  { id: 'premium', label: 'Premium' },
];
const CALL_SITES = [
  { id: 'chat', label: 'Chat' },
  { id: 'routing', label: 'Routing' },
  { id: 'vision', label: 'Vision' },
  { id: 'embedding', label: 'Embedding' },
];

function ProviderKeysCard({ providers, onActiveChange }) {
  const supportedIds = (providers || [])
    .map((p) => p.id || p.provider_id)
    .filter(Boolean);
  const [pid, setPid] = useState(supportedIds[0] || '');
  useEffect(() => {
    if (supportedIds.length && !supportedIds.includes(pid)) setPid(supportedIds[0]);
  }, [supportedIds.join(','), pid]);

  const [data, setData] = useState(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState('');
  const [adding, setAdding] = useState(false);
  const [newLabel, setNewLabel] = useState('');
  const [newKey, setNewKey] = useState('');
  const [setActive, setSetActive] = useState(true);

  const refresh = useCallback(async () => {
    if (!pid) return;
    setErr('');
    try {
      const d = await apiJson(`/api/llm/providers/${encodeURIComponent(pid)}/keys`);
      setData(d);
    } catch (e) {
      setErr(e?.message || 'failed to load keys');
    }
  }, [pid]);

  useEffect(() => { refresh(); }, [refresh]);

  const probe = async (label) => {
    setBusy(`probe:${label}`);
    setErr('');
    try {
      await apiJson(`/api/llm/providers/${encodeURIComponent(pid)}/keys/${encodeURIComponent(label)}/probe`, { method: 'POST' });
      await refresh();
    } catch (e) {
      setErr(e?.message || 'probe failed');
    } finally {
      setBusy('');
    }
  };

  const makeActive = async (label) => {
    setBusy(`active:${label}`);
    try {
      await apiJson(`/api/llm/providers/${encodeURIComponent(pid)}/keys/active`, {
        method: 'POST',
        body: JSON.stringify({ label }),
      });
      await refresh();
      // Lane 3 U3 fix #4: tell any open ProviderForm for this
      // provider that its credential basis just changed so it
      // re-fetches /models with force=true. Backend already
      // invalidates the catalog cache (catalog.py:configure), but the
      // open form doesn't poll keys and would otherwise keep showing
      // models visible to the previous key.
      if (typeof onActiveChange === 'function') onActiveChange(pid);
    } catch (e) {
      setErr(e?.message || 'set-active failed');
    } finally {
      setBusy('');
    }
  };

  const remove = async (label) => {
    if (!window.confirm(`Remove ${pid} key ${label}? The runtime will fall back to the default credential.`)) return;
    setBusy(`del:${label}`);
    try {
      await apiFetch(`/api/llm/providers/${encodeURIComponent(pid)}/keys/${encodeURIComponent(label)}`, {
        method: 'DELETE',
      });
      await refresh();
    } catch (e) {
      setErr(e?.message || 'delete failed');
    } finally {
      setBusy('');
    }
  };

  const add = async () => {
    if (!pid || !newLabel.trim() || !newKey.trim()) {
      setErr('Both label and API key are required.');
      return;
    }
    setBusy('add');
    try {
      await apiJson(`/api/llm/providers/${encodeURIComponent(pid)}/keys`, {
        method: 'POST',
        body: JSON.stringify({
          label: newLabel.trim(),
          api_key: newKey.trim(),
          set_active: setActive,
        }),
      });
      setNewLabel('');
      setNewKey('');
      setAdding(false);
      await refresh();
      // Immediately probe the freshly-added key so the row renders
      // its real verdict (not "not probed yet").
      try { await probe(newLabel.trim()); } catch { /* swallow */ }
    } catch (e) {
      setErr(e?.message || 'add failed');
    } finally {
      setBusy('');
    }
  };

  const keys = data?.keys || [];
  const activeLabel = data?.active_label || '';

  return (
    <Glass level={1} radius="md" padding="md" className="v2-providers-keys" data-testid="provider-keys-card">
      <div className="v2-flow-card-head" style={{ marginBottom: 8 }}>
        <strong>Labeled keys</strong>
        <select className="v2-select" value={pid} onChange={(e) => setPid(e.target.value)} aria-label="Provider">
          {supportedIds.map((id) => <option key={id} value={id}>{id}</option>)}
        </select>
        <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}>Refresh</button>
        <button type="button" className="v2-btn v2-btn--primary" onClick={() => setAdding((v) => !v)}>
          {adding ? 'Cancel' : '+ Add key'}
        </button>
      </div>
      <p className="v2-p v2-p--muted v2-p--tiny" style={{ marginBottom: 8 }}>
        Stash multiple keys per provider (dev / prod / team). The active label is what the next chat turn uses.
        Per-key Test runs the brain's probe and renders the real verdict — no more "configured" lies.
      </p>

      {err && <div className="v2-chip v2-chip--error" role="alert">{err}</div>}

      {adding && (
        <Glass level={0} radius="sm" padding="sm" style={{ marginBottom: 8 }}>
          <Row label="Label">
            <input className="v2-input" placeholder="prod / dev / team" value={newLabel} onChange={(e) => setNewLabel(e.target.value)} />
          </Row>
          <Row label="API key">
            <input type="password" className="v2-input" placeholder="sk-…" value={newKey} onChange={(e) => setNewKey(e.target.value)} autoComplete="off" />
          </Row>
          <Row label="Make active">
            <Toggle checked={setActive} onChange={setSetActive} />
          </Row>
          <div className="v2-forge-actions" style={{ marginTop: 6 }}>
            <button type="button" className="v2-btn v2-btn--primary" onClick={add} disabled={busy === 'add'}>
              {busy === 'add' ? 'Adding…' : 'Save key'}
            </button>
          </div>
        </Glass>
      )}

      <div className="v2-keylist" data-testid="provider-keys-list">
        {keys.length === 0 && !adding && <EmptyState title="No labeled keys yet" hint="Add at least one to enable per-key probes + tier routing." />}
        {keys.map((k) => {
          const label = k.label;
          const status = k.probe?.status || k.probe_status || 'unknown';
          const cls = status === 'ok' ? 'v2-keylist__probe--ok'
            : status === 'invalid' || status === 'unauthorized' || status === 'forbidden' ? 'v2-keylist__probe--err'
            : 'v2-keylist__probe--warn';
          const isActive = label === activeLabel;
          return (
            <div key={label} className="v2-keylist__row" data-testid={`provider-key-row-${label}`}>
              <div>
                <div className="v2-keylist__label">{label}{isActive && <span className="v2-chip v2-chip--live" style={{ marginLeft: 8 }}>active</span>}</div>
                <div className="v2-keylist__fp">{k.fingerprint || '—'}</div>
              </div>
              <span className={`v2-keylist__probe ${cls}`}>{status}</span>
              <button type="button" className="v2-btn" onClick={() => probe(label)} disabled={busy === `probe:${label}`}>
                {busy === `probe:${label}` ? 'Probing…' : 'Test'}
              </button>
              <div style={{ display: 'flex', gap: 4 }}>
                {!isActive && (
                  <button type="button" className="v2-btn" onClick={() => makeActive(label)} disabled={busy === `active:${label}`}>
                    {busy === `active:${label}` ? 'Switching…' : 'Make active'}
                  </button>
                )}
                <button type="button" className="v2-btn v2-btn--ghost" onClick={() => remove(label)} disabled={busy === `del:${label}`}>
                  {busy === `del:${label}` ? '…' : 'Remove'}
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </Glass>
  );
}

function TierRouteCard() {
  const [tier, setTier] = useState('balanced');
  const [results, setResults] = useState({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const route = useCallback(async () => {
    setBusy(true);
    setErr('');
    const out = {};
    try {
      for (const cs of CALL_SITES) {
        try {
          const d = await apiJson(`/api/llm/route?call_site=${encodeURIComponent(cs.id)}&tier=${encodeURIComponent(tier)}`, { silent: true });
          out[cs.id] = d;
        } catch (e) {
          out[cs.id] = { error: e?.message || 'route failed' };
        }
      }
      setResults(out);
    } finally {
      setBusy(false);
    }
  }, [tier]);

  useEffect(() => { route(); }, [route]);

  return (
    <Glass level={1} radius="md" padding="md" data-testid="tier-route-card">
      <div className="v2-flow-card-head" style={{ marginBottom: 8 }}>
        <strong>Per call-site routing</strong>
        <select className="v2-select" value={tier} onChange={(e) => setTier(e.target.value)} aria-label="Tier">
          {TIER_OPTIONS.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
        </select>
        <button type="button" className="v2-btn v2-btn--ghost" onClick={route} disabled={busy}>
          {busy ? 'Routing…' : 'Refresh'}
        </button>
      </div>
      <p className="v2-p v2-p--muted v2-p--tiny" style={{ marginBottom: 8 }}>
        Pre-flight which provider + model Lane 08's orchestrator would pick for each call-site at the selected tier. Hits <code>/api/llm/route</code>.
      </p>
      {err && <div className="v2-chip v2-chip--error">{err}</div>}
      <div className="v2-setting-stack">
        {CALL_SITES.map((cs) => {
          const r = results[cs.id];
          if (!r) return <Row key={cs.id} label={cs.label}><span className="v2-p v2-p--muted">—</span></Row>;
          if (r.error) {
            return (
              <Row key={cs.id} label={cs.label}>
                <Status tone="error">{r.error}</Status>
              </Row>
            );
          }
          return (
            <Row key={cs.id} label={cs.label}>
              <span>
                <strong>{r.provider || '—'}</strong>
                <span className="v2-p v2-p--muted" style={{ marginLeft: 6 }}>{r.model || ''}</span>
                {r.label && <span className="v2-chip" style={{ marginLeft: 6 }}>{r.label}</span>}
                {r.reason && <span className="v2-p v2-p--muted v2-p--tiny" style={{ display: 'block' }}>{r.reason}</span>}
              </span>
            </Row>
          );
        })}
      </div>
    </Glass>
  );
}

function FallbacksCard({ health, onChange }) {
  const fallbacks = health.fallback_providers || [];
  const candidates = health.candidates || [];
  const active = health.active?.provider || '';
  // Candidate list excluding the active one — these are what can be
  // added as fallbacks. We render every catalog provider the user has
  // configured (has_key: true).
  const pool = candidates
    .map((c) => c.provider)
    .filter((p) => p !== active && !fallbacks.includes(p));

  const setList = (next) => {
    const deduped = [];
    for (const p of next) if (p && !deduped.includes(p)) deduped.push(p);
    onChange(deduped);
  };
  const move = (idx, delta) => {
    const next = [...fallbacks];
    const target = idx + delta;
    if (target < 0 || target >= next.length) return;
    [next[idx], next[target]] = [next[target], next[idx]];
    setList(next);
  };
  const remove = (p) => setList(fallbacks.filter((x) => x !== p));
  const add = (p) => setList([...fallbacks, p]);

  return (
    <Glass level={1} radius="md" padding="sm" className="v2-providers-fallbacks">
      <div className="v2-providers-fallbacks-head">
        <div>
          <div className="v2-stat-label">Fallbacks</div>
          <div className="v2-p v2-p--tiny v2-p--muted">
            When the active provider returns 401 / 429 / 5xx, the Brain
            falls through this list in order. The previous primary is
            auto-added here when you switch.
          </div>
        </div>
        <span className="v2-chip v2-chip--muted">
          {health.total_available || 0} of {candidates.length} live
        </span>
      </div>

      <ul className="v2-fallback-list">
        {fallbacks.length === 0 && (
          <li className="v2-p v2-p--muted v2-p--tiny">No fallbacks set.</li>
        )}
        {fallbacks.map((p, idx) => {
          const cand = candidates.find((c) => c.provider === p) || {};
          const tone = cand.in_cooldown ? 'warn'
            : cand.has_key ? 'live'
            : 'off';
          return (
            <li key={p} className="v2-fallback-row">
              <StatusDot tone={tone} />
              <code>{p}</code>
              <span className="v2-p v2-p--tiny v2-p--muted" style={{ flex: 1 }}>
                {cand.in_cooldown
                  ? `cooling down ${Math.ceil(cand.cooldown_remaining || 0)}s`
                  : (cand.has_key ? 'ready' : 'no key')}
              </span>
              <button type="button" className="v2-btn v2-btn--ghost" onClick={() => move(idx, -1)} disabled={idx === 0}>↑</button>
              <button type="button" className="v2-btn v2-btn--ghost" onClick={() => move(idx, 1)} disabled={idx === fallbacks.length - 1}>↓</button>
              <button type="button" className="v2-btn v2-btn--ghost" onClick={() => remove(p)} aria-label="Remove">×</button>
            </li>
          );
        })}
      </ul>

      {pool.length > 0 && (
        <div className="v2-fallback-add">
          <span className="v2-p v2-p--tiny v2-p--muted">Add:</span>
          {pool.slice(0, 8).map((p) => (
            <button key={p} type="button" className="v2-btn v2-btn--ghost" onClick={() => add(p)}>
              + {p}
            </button>
          ))}
        </div>
      )}
    </Glass>
  );
}

function ProviderCard({ provider, isCurrent, activeModel, keysRefreshToken, isEditing, onEdit, onCancel, onSaved }) {
  const supportsLocal = !!provider.supports_local;
  const requiresKey = !!provider.requires_api_key;
  // `reachable` is null until the user probes; `configured` = has a key
  // (or no-key-required). Show that cleanly in the dot + label.
  const reachable = provider.reachable;
  const configured = !!provider.configured;
  const statusTone =
    reachable === true ? 'live'
      : reachable === false ? 'off'
      : configured ? 'warn'
      : requiresKey ? 'off'
      : 'neutral';
  const statusLabel =
    reachable === true ? 'ready'
      : reachable === false ? 'unreachable'
      : configured ? 'configured'
      : requiresKey ? 'needs key'
      : 'unconfigured';

  return (
    <Glass level={0} radius="md" padding="sm" className={`v2-provider-card${isCurrent ? ' is-current' : ''}`}>
      <div className="v2-provider-head">
        <div>
          <div className="v2-provider-name">{provider.display_name || provider.provider_id}</div>
          <div className="v2-p v2-p--tiny v2-p--muted">
            <code>{provider.provider_id}</code>
            {supportsLocal && <> · local</>}
            {!requiresKey && <> · no key required</>}
            {isCurrent && <> · <span style={{ color: 'var(--v2-accent)' }}>current</span></>}
          </div>
        </div>
        <div className="v2-provider-status">
          <StatusDot tone={statusTone} />
          <span className="v2-p v2-p--tiny">{statusLabel}</span>
        </div>
      </div>

      {isEditing ? (
        <ProviderForm
          provider={provider}
          isCurrent={isCurrent}
          activeModel={activeModel}
          keysRefreshToken={keysRefreshToken}
          onCancel={onCancel}
          onSaved={onSaved}
        />
      ) : (
        <div className="v2-provider-actions">
          <button type="button" className="v2-btn v2-btn--primary" onClick={onEdit}>
            {isCurrent ? 'Reconfigure' : 'Use this provider'}
          </button>
        </div>
      )}
    </Glass>
  );
}

function ProviderForm({ provider, isCurrent, activeModel, keysRefreshToken, onCancel, onSaved }) {
  const [models, setModels] = useState([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [modelError, setModelError] = useState(null);
  // `modelWarning` is the backend-reported warning for the current cache row
  // (e.g. "provider rejected the API key (HTTP 401)") so the picker can
  // honestly tell the user the dropdown is a fallback list, not live data.
  const [modelWarning, setModelWarning] = useState('');
  const [modelSource, setModelSource] = useState('');
  // Unix-seconds timestamp of the catalog's most recent live fetch
  // (0 when the row is the bundled fallback list). Drives the
  // Live/Cached/Stale badge below.
  const [modelLastRefresh, setModelLastRefresh] = useState(0);
  const [modelFilter, setModelFilter] = useState('');
  // Lane 3 U3 fix #1: seed from `activeModel` (the runtime model
  // surfaced by /api/llm/status) when reconfiguring the current
  // provider. Cloud descriptors ship `default_model=""` so the old
  // initializer let `loadModels` auto-pick `list[0]` of the
  // recommended catalog, and Save & apply then silently swapped the
  // brain model whenever the user only intended to rotate keys.
  const [selectedModel, setSelectedModel] = useState(activeModel || provider.default_model || '');
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState(provider.default_base_url || '');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);

  // `force=true` is what the explicit "Refresh models" button hits — it
  // bypasses the 6h disk cache so the user always gets a wire fetch.
  // Returns the parsed response so the initial-mount effect can decide
  // whether the cache is stale enough to warrant an immediate force
  // refresh (Roadmap §3.5 P0 / Appendix A.1 — "GPT-5.5 missing" was a
  // 24h+ cache that the picker silently served).
  const loadModels = useCallback(async ({ force = false } = {}) => {
    setLoadingModels(true);
    setModelError(null);
    try {
      // Default to the conductor-curated chat-ready shortlist
      // (recommended=true, model_class=chat) so the Settings picker
      // surfaces the handful of 2026-era chat models users actually
      // care about instead of the full /v1/models dump. Filter is
      // projection-only on the catalog side — the raw cache still
      // has every id the provider advertised.
      const base = force ? 'live=true&force=true' : 'live=true';
      const qs = `${base}&recommended=true&model_class=chat`;
      const d = await apiJson(`/api/llm/providers/${encodeURIComponent(provider.provider_id)}/models?${qs}`);
      const list = d.models || d || [];
      setModels(list);
      setModelWarning(d.warning || '');
      setModelSource(d.source || '');
      // ``last_refresh`` is the unix-seconds timestamp of the catalog's
      // most recent successful live fetch (0 when the response is the
      // bundled fallback list). Used by the freshness badge below.
      setModelLastRefresh(typeof d.last_refresh === 'number' ? d.last_refresh : 0);
      // Functional setter so we don't re-run the effect every time
      // the user picks a model — without this, the auto-stale-refresh
      // useEffect below would be cancelled mid-flight by the
      // setSelectedModel state churn.
      //
      // Lane 3 U3 fix #1: prefer the runtime `activeModel` over the
      // first recommended entry. Only auto-pick `list[0]` when no
      // model is currently selected AND no runtime model is known.
      if (list.length > 0) {
        setSelectedModel((current) => {
          if (current) return current;
          if (activeModel) return activeModel;
          return list[0].id || list[0];
        });
      }
      return d;
    } catch (e) {
      setModelError(e?.message || 'failed to fetch models');
      return null;
    } finally {
      setLoadingModels(false);
    }
  }, [provider.provider_id, activeModel]);

  useEffect(() => {
    // Initial mount: do the cheap cached fetch first, then force a
    // live refresh if the catalog says the row is stale (>24h) OR the
    // dropdown would otherwise be empty. Without this the v2 picker
    // happily served pre-2026 model lists for as long as the brain
    // had been up — that was the bug in Appendix A.1.
    let cancelled = false;
    (async () => {
      const d = await loadModels();
      if (cancelled || !d) return;
      const lastRefresh = typeof d.last_refresh === 'number' ? d.last_refresh : 0;
      const ageSec = lastRefresh > 0 ? (Date.now() / 1000) - lastRefresh : Infinity;
      const STALE_AFTER_SEC = 24 * 3600;
      const noModels = !Array.isArray(d.models) || d.models.length === 0;
      if (noModels || ageSec > STALE_AFTER_SEC) {
        await loadModels({ force: true });
      }
    })();
    return () => { cancelled = true; };
  }, [loadModels]);

  // Lane 3 U3 fix #4: when the parent ProviderKeysCard switches the
  // active labeled key, our credential basis changes — force a live
  // re-fetch so the dropdown reflects what the new key can see, not
  // what the previous key could.
  const keysRefreshRef = useRef(keysRefreshToken);
  useEffect(() => {
    if (keysRefreshToken === keysRefreshRef.current) return;
    keysRefreshRef.current = keysRefreshToken;
    loadModels({ force: true }).catch(() => { /* swallow */ });
  }, [keysRefreshToken, loadModels]);

  // Save credentials for this provider WITHOUT switching the active
  // provider/model. Hits the provider-scoped
  // ``/api/llm/providers/{id}/configure`` route which re-binds the
  // adapter + persists the key into vault/env without touching
  // ``llm.provider`` / ``llm.model`` in settings. Used when the user
  // is adding a key for a provider that is not currently active, so
  // a "second provider" key paste doesn't churn the active session.
  const saveCredentialsOnly = async () => {
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      const r = await apiFetch(`/api/llm/providers/${encodeURIComponent(provider.provider_id)}/configure`, {
        method: 'POST',
        body: JSON.stringify({
          api_key: apiKey || undefined,
          base_url: baseUrl || undefined,
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok || body?.success === false) {
        setErr(body?.detail || body?.error || `${r.status}`);
        return;
      }
      const p = body.persisted || {};
      const warn = p.warnings || [];
      if (warn.length) {
        setMsg(`Saved — warning: ${warn.join('; ')}`);
      } else {
        setMsg('Saved ✓');
      }
      if (apiKey) {
        try { await loadModels({ force: true }); } catch (_) { /* swallow */ }
      }
      setTimeout(() => onSaved(), 600);
    } catch (e) {
      setErr(e?.message || 'failed');
    } finally {
      setBusy(false);
    }
  };

  // Save credentials AND make this provider/model the active one. Hits
  // ``/api/llm/config`` which persists llm.provider / llm.model /
  // llm.base_url, stores the key, and hot-swaps the running LLM.
  // Explicit user intent — used for the "Save & switch" button and as
  // the single action on the current provider's reconfigure form.
  const save = async () => {
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      const r = await apiFetch('/api/llm/config', {
        method: 'POST',
        body: JSON.stringify({
          provider: provider.provider_id,
          model: selectedModel || provider.default_model || (models[0] && (typeof models[0] === 'string' ? models[0] : models[0].id)) || '',
          api_key: apiKey || undefined,
          base_url: baseUrl || undefined,
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok || body?.success === false) {
        setErr(body?.detail || body?.error || `${r.status}`);
        return;
      }
      // AUDIT-r14 finding 01 fix #2: the brain returns
      // `{success: true, reconfigured: {ok: false, reason: "..."}}`
      // on hot-swap failure (e.g. invalid key, unsupported model).
      // Pre-fix this UI lit a green "Saved and switched ✓" even when
      // the next chat turn would have failed. Now: if reconfigured.ok
      // is explicitly false, surface the reason as the error path —
      // don't pretend success.
      const reconfigured = body?.reconfigured;
      if (reconfigured && reconfigured.ok === false) {
        setErr(reconfigured.reason || reconfigured.detail || 'Hot-swap failed; key persisted but provider did not switch.');
        return;
      }
      const p = body.persisted || {};
      const warn = p.warnings || [];
      if (warn.length) {
        setMsg(`Saved — warning: ${warn.join('; ')}`);
      } else {
        setMsg('Saved and switched ✓');
      }
      // If the user pasted a new key, immediately re-fetch the model
      // list with force=true so the picker reflects what the new key
      // can actually see (the whole point of "settings honesty").
      if (apiKey) {
        try { await loadModels({ force: true }); } catch (_) { /* swallow */ }
      }
      setTimeout(() => onSaved(), 600);
    } catch (e) {
      setErr(e?.message || 'failed');
    } finally {
      setBusy(false);
    }
  };

  // Typeahead filter only kicks in when the list is large — avoids
  // adding chrome the user doesn't need for a 5-model provider.
  const SEARCHABLE_THRESHOLD = 20;
  const normalisedFilter = modelFilter.trim().toLowerCase();
  const visibleModels = (() => {
    if (!normalisedFilter) return models;
    return models.filter((m) => {
      const id = typeof m === 'string' ? m : (m.id || m.name || '');
      return id.toLowerCase().includes(normalisedFilter);
    });
  })();
  const showFilter = models.length > SEARCHABLE_THRESHOLD;

  // Lane 3 U3 fix #5: the recommended chat-class filter hides models
  // that are valid (e.g. older snapshots, custom fine-tunes) but not
  // on the curated shortlist. If the user's active model or the value
  // currently typed in the input is one of those, it would otherwise
  // be absent from the datalist suggestions. Smaller fix than a "Show
  // all" toggle: always merge them in so the user can re-select them
  // without typing the full id by hand.
  const datalistOptions = (() => {
    const ids = visibleModels.map((m) => (typeof m === 'string' ? m : (m.id || m.name || '')));
    const merged = ids.slice();
    const seen = new Set(ids.filter(Boolean));
    for (const extra of [activeModel, selectedModel]) {
      if (extra && !seen.has(extra)) {
        merged.push(extra);
        seen.add(extra);
      }
    }
    return merged;
  })();

  return (
    <div className="v2-provider-form">
      {provider.requires_api_key && (
        <label className="v2-identity-field">
          <span className="v2-identity-field-label">{provider.credential_env_var || 'API key'}</span>
          <input
            className="v2-input"
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={provider.configured ? 'leave blank to keep existing key' : 'paste new key'}
            autoComplete="off"
          />
        </label>
      )}

      {provider.supports_local && (
        <label className="v2-identity-field">
          <span className="v2-identity-field-label">Base URL</span>
          <input
            className="v2-input"
            type="text"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder={provider.default_base_url || 'http://localhost:1234/v1'}
          />
        </label>
      )}

      <div className="v2-identity-field">
        <span className="v2-identity-field-label">Model</span>
        {showFilter && (
          <input
            className="v2-input"
            type="search"
            value={modelFilter}
            onChange={(e) => setModelFilter(e.target.value)}
            placeholder={`Filter ${models.length} models…`}
            aria-label={`Filter ${provider.provider_id} models`}
            data-testid={`model-filter-${provider.provider_id}`}
          />
        )}
        <div className="v2-provider-model-row">
          <input
            className="v2-input"
            value={selectedModel}
            onChange={(e) => setSelectedModel(e.target.value)}
            placeholder={provider.default_model || 'pick a model'}
            list={`models-${provider.provider_id}`}
          />
          {(() => {
            // Live/Cached/Stale freshness badge.
            // green dot < 2h, yellow dot < 24h, red dot otherwise (or
            // no successful live refresh yet). Source: ProviderCatalog
            // CachedModelList.last_refresh round-tripped through
            // /api/llm/providers/{id}/models.
            const ageSec = modelLastRefresh > 0
              ? (Date.now() / 1000) - modelLastRefresh
              : Infinity;
            let tone = 'stale';
            let label = 'Stale';
            if (modelLastRefresh > 0 && ageSec < 2 * 3600) { tone = 'live'; label = 'Live'; }
            else if (modelLastRefresh > 0 && ageSec < 24 * 3600) { tone = 'cached'; label = 'Cached'; }
            // Lane 3 U3 fix #3: never lie. If the backend reported a
            // warning (e.g. "provider rejected the API key (HTTP
            // 401)") the dropdown is the cached row, not live —
            // force error tone regardless of `last_refresh`. Same for
            // explicit non-live sources (`cache` / `fallback`): the
            // tone must not read `Live` next to a yellow warning chip.
            if (modelWarning) {
              tone = 'error';
              label = 'Stale';
            } else if (modelSource && modelSource !== 'live') {
              tone = 'stale';
              label = 'Stale';
            }
            const ageHuman = (() => {
              if (!isFinite(ageSec)) return 'never refreshed';
              if (ageSec < 60) return 'just now';
              if (ageSec < 3600) return `${Math.round(ageSec / 60)}m ago`;
              if (ageSec < 86400) return `${Math.round(ageSec / 3600)}h ago`;
              return `${Math.round(ageSec / 86400)}d ago`;
            })();
            return (
              <span
                className={`v2-chip v2-chip--${tone === 'live' ? 'live' : tone === 'cached' ? 'warn' : 'error'}`}
                data-testid={`model-age-${provider.provider_id}`}
                data-age-tone={tone}
                title={`Model list age: ${ageHuman}`}
              >
                {label} · {ageHuman}
              </span>
            );
          })()}
          <button
            type="button"
            className="v2-btn v2-btn--ghost"
            onClick={() => loadModels({ force: true })}
            disabled={loadingModels}
          >
            {loadingModels ? 'Loading…' : 'Refresh models'}
          </button>
          <datalist id={`models-${provider.provider_id}`} data-testid={`models-datalist-${provider.provider_id}`}>
            {datalistOptions.map((id, i) => (
              <option key={id || i} value={id}>{id}</option>
            ))}
          </datalist>
        </div>
        {loadingModels && <span className="v2-p v2-p--muted v2-p--tiny">Probing /models…</span>}
        {!loadingModels && models.length > 0 && (
          <span className="v2-p v2-p--muted v2-p--tiny">
            {showFilter ? `${visibleModels.length} of ${models.length}` : `${models.length} model${models.length === 1 ? '' : 's'}`}
            {modelSource ? ` · ${modelSource}` : ''}
          </span>
        )}
        {!loadingModels && models.length === 0 && !modelError && (
          <span className="v2-p v2-p--muted v2-p--tiny">No models returned — type any model id above to use.</span>
        )}
        {modelWarning && !modelError && (
          <span className="v2-chip v2-chip--warn" data-testid={`model-warning-${provider.provider_id}`}>
            {modelWarning}
          </span>
        )}
        {modelError && (
          <span className="v2-chip v2-chip--warn">{modelError}</span>
        )}
      </div>

      <div className="v2-provider-actions" style={{ justifyContent: 'flex-end' }}>
        <button type="button" className="v2-btn" onClick={onCancel} disabled={busy}>Cancel</button>
        {isCurrent ? (
          <>
            {/*
             * Lane 3 U3 fix #2: split the current-provider action into
             * Save key (configure-only, no model swap) and Save & apply
             * (full /api/llm/config POST). Before this split, key
             * rotation always sent the picker's `model:` field, which
             * — combined with fix #1's seeding bug — silently swapped
             * the running model whenever the user only meant to paste
             * a fresh key. `saveCredentialsOnly` hits
             * /api/llm/providers/{id}/configure which re-binds the
             * adapter without touching llm.provider / llm.model.
             */}
            <button
              type="button"
              className="v2-btn"
              onClick={saveCredentialsOnly}
              disabled={busy}
              data-testid={`provider-save-key-${provider.provider_id}`}
              title="Persist credentials without changing the active model. Use this for key rotation."
            >
              {busy ? 'Saving…' : 'Save key'}
            </button>
            <button
              type="button"
              className="v2-btn v2-btn--primary"
              onClick={save}
              disabled={busy || !selectedModel}
              data-testid={`provider-save-apply-${provider.provider_id}`}
              title="Persist credentials and apply the currently selected model now."
            >
              {busy ? 'Saving…' : 'Save & apply'}
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              className="v2-btn v2-btn--primary"
              onClick={saveCredentialsOnly}
              disabled={busy}
              data-testid={`provider-save-key-${provider.provider_id}`}
              title="Persist key and base URL without changing the active provider."
            >
              {busy ? 'Saving…' : 'Save key'}
            </button>
            <button
              type="button"
              className="v2-btn"
              onClick={save}
              disabled={busy || !selectedModel}
              data-testid={`provider-save-switch-${provider.provider_id}`}
              title="Persist credentials AND make this provider/model active now."
            >
              {busy ? 'Saving…' : 'Save & switch'}
            </button>
          </>
        )}
      </div>
      {msg && <div className="v2-chip v2-chip--live">{msg}</div>}
      {err && <div className="v2-chip v2-chip--error">{err}</div>}
    </div>
  );
}

// ── Memory ────────────────────────────────────────────────────

function MemorySection() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try { setData(await apiJson('/api/memory/backend')); } catch (e) { setError(e.message); }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);

  const switchTo = async (next) => {
    setBusy(true);
    try {
      const r = await apiFetch('/api/memory/backend', {
        method: 'POST',
        body: JSON.stringify({ backend: next }),
      });
      const body = await r.json();
      if (!body?.ok) setError(body?.error || 'switch failed');
      await refresh();
    } finally { setBusy(false); }
  };

  if (!data) return <EmptyState title={error || 'Loading memory status…'} />;

  return (
    <div className="v2-setting-stack">
      <Row label="Active backend"><Status tone="live">{data.backend}</Status></Row>
      {Object.entries(data.available || {}).map(([name, installed]) => (
        <Row
          key={name}
          label={name}
          hint={installed ? 'Installed' : `Run: pip install feral-ai[memory-${name}]`}
        >
          <button
            type="button"
            className={`v2-btn ${data.backend === name ? 'v2-btn--primary' : ''}`}
            disabled={busy || !installed || data.backend === name}
            onClick={() => switchTo(name)}
          >
            {data.backend === name ? 'In use' : installed ? 'Switch' : 'Not installed'}
          </button>
        </Row>
      ))}
      {error && <div className="v2-chip v2-chip--error">{error}</div>}
    </div>
  );
}

// ── Channels ──────────────────────────────────────────────────

function ChannelsSection() {
  const [stats, setStats] = useState(null);
  const [creds, setCreds] = useState({});
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => { apiJson('/api/channels').then(setStats).catch((e) => setError(e.message)); }, []);

  if (!stats) return <EmptyState title={error || 'Loading channels…'} />;
  const entries = Object.entries(stats.status_by_channel || stats.channels || {});

  const save = async (channel) => {
    setBusy(channel);
    try {
      const envKey = {
        telegram: 'FERAL_TELEGRAM_BOT_TOKEN',
        discord: 'FERAL_DISCORD_BOT_TOKEN',
        slack: 'FERAL_SLACK_BOT_TOKEN',
      }[channel] || `FERAL_${channel.toUpperCase()}_BOT_TOKEN`;
      await apiFetch('/api/config/credentials', {
        method: 'POST',
        body: JSON.stringify({ [envKey]: creds[channel] }),
      });
      await apiFetch('/api/channels/start', {
        method: 'POST',
        body: JSON.stringify({ type: channel, config: { bot_token: creds[channel], enabled: true } }),
      });
    } finally { setBusy(null); }
  };

  return (
    <div className="v2-setting-stack">
      <Row label="Active channels"><Status>{stats.active ?? entries.length}</Status></Row>
      {entries.map(([name, info]) => (
        <Row key={name} label={name} hint={info?.description || ''}>
          <Status tone={info?.connected ? 'live' : 'warn'}>{info?.connected ? 'connected' : 'disabled'}</Status>
        </Row>
      ))}
      {['telegram', 'discord', 'slack'].map((c) => (
        <Row key={c} label={`${c} token`} hint="Paste bot token and save to enable.">
          <input type="password" className="v2-input" value={creds[c] || ''} onChange={(e) => setCreds((s) => ({ ...s, [c]: e.target.value }))} placeholder="Bot token" />
          <button type="button" className="v2-btn" onClick={() => save(c)} disabled={busy === c || !creds[c]}>
            {busy === c ? 'Saving…' : 'Save + enable'}
          </button>
        </Row>
      ))}
    </div>
  );
}

// ── Autonomy ──────────────────────────────────────────────────

function AutonomySection() {
  const [mode, setMode] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { apiJson('/api/autonomy').then((d) => setMode(d.mode)); }, []);
  if (!mode) return <EmptyState title="Loading…" />;
  const tiers = [
    { id: 'strict', label: 'Strict', desc: 'Approval for every tool call. Safest, slowest.' },
    { id: 'hybrid', label: 'Hybrid', desc: 'Surfaces drafts for approval. Default.' },
    { id: 'loose', label: 'Loose', desc: 'Auto-promotes + auto-runs. Use for trusted environments.' },
  ];
  const set = async (next) => {
    setBusy(true);
    try {
      const r = await apiFetch('/api/autonomy', { method: 'POST', body: JSON.stringify({ mode: next }) });
      if (r.ok) setMode(next);
    } finally { setBusy(false); }
  };
  return (
    <div className="v2-setting-stack">
      <Row label="Current tier"><Status tone={mode === 'loose' ? 'warn' : 'live'}>{mode}</Status></Row>
      {tiers.map((t) => (
        <Row key={t.id} label={t.label} hint={t.desc}>
          <button type="button" className={`v2-btn ${mode === t.id ? 'v2-btn--primary' : ''}`} disabled={busy || mode === t.id} onClick={() => set(t.id)}>
            {mode === t.id ? 'Active' : 'Select'}
          </button>
        </Row>
      ))}
    </div>
  );
}

// ── Voice ─────────────────────────────────────────────────────

function VoiceSection() {
  // Lane 05 Wave 2 surface — `/api/voice/providers` returns an 8-entry
  // catalogue keyed by `kind` (realtime | stt | tts) with cached probe
  // status. The Settings panel groups them into three pickers
  // (realtime / chained STT / chained TTS) and persists the operator's
  // choice into `audio.realtime_providers` / `audio.chained_providers`
  // via `/api/config/update` (the same lists the audio router reads
  // on every voice session).
  //
  // Phone Settings → Voice mirrors this exact shape via SettingsPanel.jsx
  // so a user pairing from iPhone sees identical labels and status.
  const [status, setStatus] = useState(null);
  const [config, setConfig] = useState(null);
  const [providers, setProviders] = useState([]);
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');
  const [wakeWord, setWakeWord] = useState({ enabled: false, supported: true });

  const refresh = useCallback(async () => {
    setErr('');
    const [s, c, ww, vp] = await Promise.allSettled([
      apiJson('/api/voice/status'),
      apiJson('/api/config'),
      apiJson('/api/ambient/wake_word/status'),
      apiJson('/api/voice/providers'),
    ]);
    if (s.status === 'fulfilled') setStatus(s.value);
    if (c.status === 'fulfilled') setConfig(c.value);
    if (ww.status === 'fulfilled') setWakeWord({ enabled: !!ww.value?.enabled, supported: ww.value?.supported !== false });
    if (vp.status === 'fulfilled') setProviders(vp.value?.providers || []);
    else setErr(vp.reason?.message || 'voice catalogue unavailable');
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const updateAudio = async (key, value) => {
    setBusy(key);
    setErr('');
    try {
      const r = await apiFetch('/api/config/update', { method: 'POST', body: JSON.stringify({ section: 'audio', key, value }) });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(formatApiDetail(body, `failed to update ${key}`));
      }
      await refresh();
    } catch (e) {
      setErr(e?.message || `update ${key} failed`);
    } finally { setBusy(''); }
  };

  const toggleWake = async () => {
    const next = !wakeWord.enabled;
    setBusy('wake');
    try {
      await apiFetch('/api/ambient/wake_word/toggle', { method: 'POST', body: JSON.stringify({ enabled: next }) });
      await refresh();
    } catch (e) {
      setErr(e?.message || 'wake-word toggle failed');
    } finally {
      setBusy('');
    }
  };

  const probeProvider = async (providerId) => {
    setBusy(`probe:${providerId}`);
    setErr('');
    try {
      const r = await apiJson('/api/voice/providers/probe', {
        method: 'POST',
        body: JSON.stringify({ provider_id: providerId }),
      });
      // Update the inline row immediately so the user sees the verdict.
      setProviders((prev) => prev.map((p) => (
        p.id === providerId ? { ...p, probe_status: r.reason, configured: r.ok, latency_ms: r.latency_ms } : p
      )));
    } catch (e) {
      setErr(e?.message || `probe ${providerId} failed`);
    } finally {
      setBusy('');
    }
  };

  if (!status || !config) return <EmptyState title="Loading voice status…" />;
  const audio = config.audio || {};
  const realtimeList = Array.isArray(audio.realtime_providers) && audio.realtime_providers.length
    ? audio.realtime_providers
    : (audio.realtime_provider ? [audio.realtime_provider] : []);
  const chainedList = Array.isArray(audio.chained_providers) && audio.chained_providers.length
    ? audio.chained_providers
    : [];

  const byKind = { realtime: [], stt: [], tts: [] };
  for (const p of providers) {
    if (byKind[p.kind]) byKind[p.kind].push(p);
  }

  const toggleInList = async (key, currentList, providerId) => {
    const next = currentList.includes(providerId)
      ? currentList.filter((p) => p !== providerId)
      : [providerId, ...currentList.filter((p) => p !== providerId)];
    await updateAudio(key, next);
  };

  const renderGroup = (kind, title, listKey, currentList) => {
    const rows = byKind[kind] || [];
    return (
      <div className="v2-voice-group" data-testid={`voice-group-${kind}`}>
        <div className="v2-voice-group__title">{title}</div>
        {rows.length === 0 && <EmptyState title="No providers in catalogue" />}
        {rows.map((p) => {
          const active = currentList.includes(p.id);
          const status = p.probe_status || (p.configured ? 'ok' : 'no_key');
          const tone = status === 'ok' ? 'v2-keylist__probe--ok'
            : status === 'no_key' || status === 'not_configured' ? 'v2-keylist__probe--warn'
            : 'v2-keylist__probe--err';
          // Lane U2 — when /api/voice/providers attaches a realtime
          // model list (today: only openai_realtime) and the card is
          // active, render an in-list <select> instead of falling
          // back to the LLM picker's free-text behaviour. Entries
          // that omit ``models`` keep the original Use/Test layout.
          const showRealtimeModelPicker = (
            p.id === 'openai_realtime'
            && active
            && Array.isArray(p.models)
            && p.models.length > 0
          );
          const currentModel = audio.realtime_model
            || p.default_model
            || (showRealtimeModelPicker ? p.models[0] : '');
          return (
            <div key={p.id} className={`v2-voice-card${active ? ' v2-voice-card--active' : ''}`} data-testid={`voice-card-${p.id}`}>
              <div>
                <div className="v2-voice-card__name">{p.name || p.id}</div>
                <div className="v2-voice-card__sub">
                  <span className={`v2-keylist__probe ${tone}`}>{status}</span>
                  {p.probe_detail && <span style={{ marginLeft: 6 }}>{p.probe_detail}</span>}
                </div>
                {showRealtimeModelPicker && (
                  <div className="v2-voice-card__sub" style={{ marginTop: 6 }}>
                    <label htmlFor={`realtime-model-${p.id}`} style={{ marginRight: 6 }}>Model</label>
                    <select
                      id={`realtime-model-${p.id}`}
                      data-testid="openai-realtime-model-picker"
                      className="v2-input"
                      value={currentModel}
                      disabled={busy === 'realtime_model'}
                      onChange={(e) => updateAudio('realtime_model', e.target.value)}
                    >
                      {p.models.map((m) => (
                        <option key={m} value={m}>{m}</option>
                      ))}
                    </select>
                  </div>
                )}
              </div>
              <span className="v2-voice-card__lat">{p.latency_ms != null ? `${Math.round(p.latency_ms)}ms` : ''}</span>
              <button type="button" className="v2-btn" onClick={() => probeProvider(p.id)} disabled={busy === `probe:${p.id}`}>
                {busy === `probe:${p.id}` ? 'Testing…' : 'Test'}
              </button>
              <button
                type="button"
                className={`v2-btn ${active ? '' : 'v2-btn--primary'}`}
                onClick={() => toggleInList(listKey, currentList, p.id)}
                disabled={busy === listKey}
              >
                {active ? 'Remove' : 'Use'}
              </button>
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div className="v2-setting-stack">
      <Row label="Realtime voice"><Status tone={status.realtime_available ? 'live' : 'warn'}>{status.realtime_available ? 'ready' : 'unavailable'}</Status></Row>
      <Row label="Local TTS/STT"><Status tone={status.audio_available ? 'live' : 'warn'}>{status.audio_available ? 'ready' : 'unavailable'}</Status></Row>
      <Row label="Active sessions"><Status>{status.active_realtime_sessions ?? 0}</Status></Row>
      <Row label="Wake word" hint={wakeWord.supported ? '' : 'Install feral-ai[wake] to enable.'}>
        <Toggle checked={!!wakeWord.enabled} disabled={!wakeWord.supported || busy === 'wake'} onChange={toggleWake} />
      </Row>

      {err && <div className="v2-chip v2-chip--error" role="alert">{err}</div>}

      <Glass level={0} radius="md" padding="md" data-testid="voice-pickers">
        <p className="v2-p v2-p--muted v2-p--tiny" style={{ marginBottom: 6 }}>
          Pick providers per pipeline. Realtime is a single full-duplex endpoint (OpenAI Realtime / Gemini Live).
          Chained pipelines compose Speech-to-Text → LLM → Text-to-Speech; the audio router walks the lists in
          order and falls back automatically if one provider returns no_key or unauthorized.
        </p>
        {renderGroup('realtime', 'Realtime', 'realtime_providers', realtimeList)}
        {renderGroup('stt', 'Chained — Speech-to-Text', 'chained_providers', chainedList)}
        {renderGroup('tts', 'Chained — Text-to-Speech', 'chained_providers', chainedList)}
      </Glass>

      <Row label="TTS voice" hint="Default voice name passed to the active TTS provider (provider-specific).">
        <input
          className="v2-input"
          defaultValue={audio.tts_voice || ''}
          onBlur={(e) => { if (e.target.value !== (audio.tts_voice || '')) updateAudio('tts_voice', e.target.value); }}
          placeholder="nova, alloy, shimmer, …"
        />
      </Row>
    </div>
  );
}

// ── Security ──────────────────────────────────────────────────

function SecuritySection() {
  return (
    <div className="v2-setting-stack" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <VaultSub />
      <PermissionsSub />
      <AuditSub />
      <PolicySub />
    </div>
  );
}

function VaultSub() {
  const [items, setItems] = useState([]);
  const [key, setKey] = useState('');
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const d = await apiJson('/api/security/vault');
      // Brain returns {keys: {NAME: {stored, fingerprint}, ...}} as a dict.
      // Also tolerate legacy array shapes.
      let entries = [];
      if (d?.keys && typeof d.keys === 'object' && !Array.isArray(d.keys)) {
        entries = Object.entries(d.keys).map(([name, meta]) => ({
          name,
          stored: meta?.stored ?? true,
          fingerprint: meta?.fingerprint || '',
        }));
      } else if (Array.isArray(d?.keys)) {
        entries = d.keys.map((k) => typeof k === 'string' ? { name: k } : k);
      } else if (Array.isArray(d)) {
        entries = d.map((k) => typeof k === 'string' ? { name: k } : k);
      }
      setItems(entries);
    } catch { setItems([]); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const store = async () => {
    setBusy(true);
    try {
      await apiFetch('/api/security/vault/store', {
        method: 'POST',
        body: JSON.stringify({ key_name: key, value }),
      });
      setKey(''); setValue('');
      refresh();
    } finally { setBusy(false); }
  };

  const remove = async (name) => {
    if (!window.confirm(`Remove ${name}? This deletes the stored secret.`)) return;
    await apiFetch(`/api/security/vault/${encodeURIComponent(name)}`, { method: 'DELETE' });
    refresh();
  };

  return (
    <Glass level={1} radius="md" padding="lg">
      <h3>Vault</h3>
      <p className="v2-p v2-p--muted">
        Encrypted at-rest storage for API keys + secrets. Values never leave
        the Brain or render in the UI — only the key name + a fingerprint.
      </p>
      <div className="v2-vault-list">
        {items.map((it) => (
          <div key={it.name} className="v2-vault-row">
            <code className="v2-vault-name">{it.name}</code>
            {it.fingerprint && (
              <code className="v2-vault-fp" title="Fingerprint">{it.fingerprint.slice(0, 12)}</code>
            )}
            <Status tone={it.stored ? 'live' : 'off'}>{it.stored ? 'stored' : 'empty'}</Status>
            <button
              type="button"
              className="v2-btn v2-btn--ghost"
              onClick={() => remove(it.name)}
            >
              Remove
            </button>
          </div>
        ))}
        {items.length === 0 && <div className="v2-p v2-p--muted">No stored keys yet.</div>}
      </div>
      <div className="v2-setting-stack" style={{ marginTop: 16 }}>
        <Row label="Key name"><input className="v2-input" value={key} onChange={(e) => setKey(e.target.value)} placeholder="OPENWEATHER_API_KEY" /></Row>
        <Row label="Value"><input type="password" className="v2-input" value={value} onChange={(e) => setValue(e.target.value)} /></Row>
        <Row label=""><button type="button" className="v2-btn v2-btn--primary" onClick={store} disabled={busy || !key || !value}>Store</button></Row>
      </div>
    </Glass>
  );
}

function PermissionsSub() {
  const [perms, setPerms] = useState(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try { setPerms(await apiJson('/api/security/permissions')); }
    catch { setPerms({}); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const setTier = async (tier) => {
    setBusy(true);
    try {
      await apiFetch('/api/security/permissions/update', {
        method: 'POST',
        body: JSON.stringify({ max_tier: tier }),
      });
      await refresh();
    } finally { setBusy(false); }
  };

  if (!perms) return <Glass level={1} radius="md" padding="lg"><EmptyState title="Loading…" /></Glass>;

  const tiers = Array.isArray(perms.tiers) ? perms.tiers : ['passive', 'active', 'privileged', 'dangerous'];
  const descs = perms.tier_descriptions || {};
  const current = perms.max_tier;

  return (
    <Glass level={1} radius="md" padding="lg">
      <h3>Permissions</h3>
      <p className="v2-p v2-p--muted">
        Max tier caps what every tool call can do. Lower tiers are safer — tools
        above this level are blocked until you raise it.
      </p>
      <div className="v2-setting-stack">
        <Row label="Current max tier">
          <Status tone={current === 'dangerous' ? 'error' : current === 'privileged' ? 'warn' : 'live'}>
            {current}
          </Status>
        </Row>
        {tiers.map((t) => (
          <Row key={t} label={t} hint={descs[t] || ''}>
            <button
              type="button"
              className={`v2-btn ${current === t ? 'v2-btn--primary' : ''}`}
              disabled={busy || current === t}
              onClick={() => setTier(t)}
            >
              {current === t ? 'Active' : 'Set'}
            </button>
          </Row>
        ))}
      </div>
    </Glass>
  );
}

function AuditSub() {
  const [log, setLog] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiJson('/api/security/audit')
      .then((d) => setLog(d?.entries || d?.log || (Array.isArray(d) ? d : [])))
      .catch(() => setLog([]))
      .finally(() => setLoading(false));
  }, []);

  return (
    <Glass level={1} radius="md" padding="lg">
      <h3>Audit log</h3>
      <p className="v2-p v2-p--muted">Every vault retrieve / store / delete, timestamped.</p>
      {loading && <EmptyState title="Loading…" />}
      {!loading && log.length === 0 && <EmptyState title="No audit entries" />}
      {!loading && log.length > 0 && (
        <ul className="v2-audit-list">
          {log.slice(-40).reverse().map((e, i) => {
            const when = e.ts ? new Date(e.ts * 1000).toLocaleString() : '';
            const tone = e.action === 'store' ? 'live' : e.action === 'delete' ? 'error' : 'neutral';
            return (
              <li key={i} className="v2-audit-row">
                <Status tone={tone}>{e.action || 'event'}</Status>
                <code className="v2-audit-key">{e.key || '—'}</code>
                <span className="v2-audit-actor">{e.actor || ''}</span>
                <span className="v2-audit-time">{when}</span>
              </li>
            );
          })}
        </ul>
      )}
    </Glass>
  );
}

function PolicySub() {
  const [policy, setPolicy] = useState('');
  const [dirty, setDirty] = useState(false);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    apiJson('/api/policy').then((d) => setPolicy(JSON.stringify(d, null, 2))).catch(() => setPolicy('{}'));
  }, []);
  const save = async () => {
    try {
      const parsed = JSON.parse(policy);
      await apiFetch('/api/policy/update', { method: 'POST', body: JSON.stringify(parsed) });
      setSaved(true);
      setDirty(false);
      setTimeout(() => setSaved(false), 2000);
    } catch { /* silent */ }
  };
  return (
    <Glass level={1} radius="md" padding="lg">
      <h3>Policy {dirty && <span className="v2-chip v2-chip--warn">unsaved</span>}{saved && <span className="v2-chip v2-chip--live">saved</span>}</h3>
      <p className="v2-p v2-p--muted">
        The Brain's safety policy as JSON — network allowlists, auto-approve
        categories, tier gates. Saves to the running Brain immediately.
      </p>
      <CodeEditor value={policy} onChange={(v) => { setPolicy(v); setDirty(true); }} rows={12} language="json" />
      <div className="v2-forge-actions"><button type="button" className="v2-btn v2-btn--primary" onClick={save} disabled={!dirty}>Save policy</button></div>
    </Glass>
  );
}

// ── Integrations ──────────────────────────────────────────────

// ── Integrations (R-PROD-001/002 + Lane 10) ────────────────────
//
// Three integration shapes, one row per provider:
//
//   1. R-PROD-001 — Gmail = inline App Password walkthrough.
//      5 steps with copy-buttons; the app name copy is literally
//      "FERAL". Save POSTs to `/api/integrations/token` (the same
//      surface HA tokens use) with `{provider_id: "gmail",
//      address: ..., app_password: ...}`. Backend IMAP+SMTP probe
//      determines `connected`. NO OAuth flow.
//
//   2. R-PROD-002 — Google Calendar / Drive / Contacts / Notion /
//      Spotify / Microsoft 365 = "Use your own OAuth app" expand-
//      card. Shows the redirect URI (`{API_BASE}/api/oauth/callback`)
//      with a copy-button, links to the vendor's create-app docs
//      from the brain's `setup_doc_url`, takes client_id +
//      client_secret in two fields, persists via
//      `/api/integrations/token` with `{client_id, client_secret}`,
//      then opens the OAuth popup. Existing Lane 10 OAuthManager
//      handles the rest.
//
//   3. Home Assistant = long-lived token paste field, exactly the
//      same `/api/integrations/token` POST.
//
// Probe-based `connected` comes from `/api/integrations` (Lane 10).
// Disconnect revokes vault entries via `/api/integrations/disconnect`.

const GMAIL_STEPS = [
  { n: 1, body: <>Open <a href="https://myaccount.google.com/security" target="_blank" rel="noopener noreferrer">Google Account → Security</a> and turn on 2-Step Verification (required to mint App Passwords).</> },
  { n: 2, body: <>Go to <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener noreferrer">App passwords</a>.</> },
  { n: 3, body: <>For "App", choose "Mail". For "Device", choose "Other (Custom name)" and paste the app name below.</>, copyText: 'FERAL' },
  { n: 4, body: <>Click <strong>Generate</strong>. Copy the 16-character password Google shows.</> },
  { n: 5, body: <>Paste your Gmail address + that 16-char password below and click Save. The brain runs an IMAP+SMTP probe; green = working.</> },
];

const GMAIL_PROVIDER_ID = 'gmail';
const OAUTH_SELF_SERVE = new Set([
  'google_calendar', 'google_drive', 'google_contacts', 'google',
  'notion', 'spotify', 'microsoft', 'microsoft_365', 'm365',
]);
const HA_PROVIDER_ID = 'home_assistant';

function CopyButton({ value, label }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* swallow — clipboard may be disabled */
    }
  };
  return (
    <button type="button" className="v2-int-copy" onClick={copy} title="Copy to clipboard" data-testid={`copy-${label || value}`}>
      <code>{value}</code>
      <span>{copied ? '✓' : '⎘'}</span>
    </button>
  );
}

function GmailWalkthrough({ provider, onSaved, onDisconnect, connected }) {
  const [address, setAddress] = useState('');
  const [pw, setPw] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [msg, setMsg] = useState('');

  const save = async () => {
    if (!address.trim() || !pw.trim()) {
      setErr('Email and 16-character App Password are both required.');
      return;
    }
    setBusy(true);
    setErr('');
    setMsg('');
    try {
      const r = await apiFetch('/api/integrations/token', {
        method: 'POST',
        body: JSON.stringify({
          provider_id: GMAIL_PROVIDER_ID,
          token: pw.replace(/\s+/g, ''),
          address: address.trim(),
          app_password: pw.replace(/\s+/g, ''),
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok || body?.error) {
        throw new Error(formatApiDetail(body, `save failed (${r.status})`));
      }
      setMsg('Saved. IMAP+SMTP probe pending — refresh to see live status.');
      setPw('');
      await onSaved();
    } catch (e) {
      setErr(e?.message || 'save failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Glass level={0} radius="md" padding="md" data-testid="gmail-walkthrough">
      <div className="v2-flow-card-head" style={{ marginBottom: 6 }}>
        <strong>Gmail (App Password)</strong>
        <Status tone={connected ? 'live' : 'off'}>{connected ? 'connected' : 'disconnected'}</Status>
        {connected && (
          <button type="button" className="v2-btn" onClick={onDisconnect}>Disconnect</button>
        )}
      </div>
      <p className="v2-p v2-p--muted v2-p--tiny" style={{ marginBottom: 8 }}>
        FERAL connects to Gmail via IMAP+SMTP with a Google App Password — no OAuth, no Google Cloud project.
        Takes ~30 seconds. {provider?.description}
      </p>
      <div className="v2-int-expand">
        {GMAIL_STEPS.map((s) => (
          <div key={s.n} className="v2-int-walkstep">
            <span className="v2-int-walkstep__num">{s.n}</span>
            <div>
              {s.body}
              {s.copyText && (
                <div style={{ marginTop: 4 }}>
                  Copy the app name: <CopyButton value={s.copyText} label="gmail-app-name" />
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
      <div style={{ marginTop: 10, display: 'grid', gap: 8 }}>
        <Row label="Gmail address">
          <input
            type="email"
            className="v2-input"
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="you@gmail.com"
            autoComplete="username"
            data-testid="gmail-address"
          />
        </Row>
        <Row label="16-char App Password">
          <input
            type="password"
            className="v2-input"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            placeholder="xxxx xxxx xxxx xxxx"
            autoComplete="new-password"
            data-testid="gmail-app-password"
          />
        </Row>
        {err && <div className="v2-chip v2-chip--error" role="alert">{err}</div>}
        {msg && <div className="v2-chip v2-chip--live">{msg}</div>}
        <div>
          <button
            type="button"
            className="v2-btn v2-btn--primary"
            onClick={save}
            disabled={busy}
            data-testid="gmail-save"
          >
            {busy ? 'Saving + probing…' : 'Save and probe'}
          </button>
        </div>
      </div>
    </Glass>
  );
}

function OAuthSelfServeCard({ provider, onSaved, onDisconnect, connected }) {
  const pid = provider.id || provider.provider_id;
  const [expanded, setExpanded] = useState(false);
  const [clientId, setClientId] = useState(provider.client_id || '');
  const [clientSecret, setClientSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [msg, setMsg] = useState('');
  const redirectUri = `${API_BASE}/api/oauth/callback`;
  const hasClientId = !!(provider.has_client_id || provider.client_id);

  const saveCreds = async () => {
    if (!clientId.trim() || !clientSecret.trim()) {
      setErr('Both client_id and client_secret are required.');
      return;
    }
    setBusy(true);
    setErr('');
    setMsg('');
    try {
      const r = await apiFetch('/api/integrations/token', {
        method: 'POST',
        body: JSON.stringify({
          provider_id: pid,
          token: clientSecret.trim(),
          client_id: clientId.trim(),
          client_secret: clientSecret.trim(),
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok || body?.error) throw new Error(formatApiDetail(body, `save failed (${r.status})`));
      setMsg('Credentials saved. Click "Authorize" to complete OAuth.');
      setClientSecret('');
      await onSaved();
    } catch (e) {
      setErr(e?.message || 'save failed');
    } finally {
      setBusy(false);
    }
  };

  const authorize = () => {
    window.open(`${API_BASE}/api/oauth/authorize/${encodeURIComponent(pid)}`, '_blank', 'width=520,height=640');
  };

  return (
    <Glass level={0} radius="md" padding="md" data-testid={`oauth-card-${pid}`}>
      <div className="v2-flow-card-head" style={{ marginBottom: 6 }}>
        <strong>{provider.name || pid}</strong>
        <Status tone={connected ? 'live' : (hasClientId ? 'warn' : 'off')}>
          {connected ? 'connected' : (hasClientId ? 'configured' : 'needs-setup')}
        </Status>
        {connected ? (
          <button type="button" className="v2-btn" onClick={onDisconnect}>Disconnect</button>
        ) : hasClientId ? (
          <button type="button" className="v2-btn v2-btn--primary" onClick={authorize}>Authorize</button>
        ) : (
          <button type="button" className="v2-btn v2-btn--primary" onClick={() => setExpanded((v) => !v)}>
            {expanded ? 'Cancel' : 'Use your own OAuth app'}
          </button>
        )}
      </div>
      {provider.setup_doc_summary && (
        <p className="v2-p v2-p--muted v2-p--tiny" style={{ marginBottom: 6 }}>{provider.setup_doc_summary}</p>
      )}
      {expanded && (
        <div className="v2-int-expand">
          <div className="v2-int-walkstep">
            <span className="v2-int-walkstep__num">1</span>
            <div>
              Open the vendor's OAuth console and create a new app.
              {provider.setup_doc_url && (
                <> See: <a href={provider.setup_doc_url} target="_blank" rel="noopener noreferrer">setup guide</a>.</>
              )}
            </div>
          </div>
          <div className="v2-int-walkstep">
            <span className="v2-int-walkstep__num">2</span>
            <div>
              Set the OAuth redirect URI to: <CopyButton value={redirectUri} label={`${pid}-redirect`} />
            </div>
          </div>
          <div className="v2-int-walkstep">
            <span className="v2-int-walkstep__num">3</span>
            <div>
              Paste the resulting Client ID and Client Secret below and click Save.
              FERAL persists them to the local vault — they never leave this machine.
            </div>
          </div>
          <div style={{ marginTop: 10, display: 'grid', gap: 8 }}>
            <Row label="Client ID">
              <input
                className="v2-input"
                value={clientId}
                onChange={(e) => setClientId(e.target.value)}
                placeholder="…apps.googleusercontent.com"
                data-testid={`${pid}-client-id`}
              />
            </Row>
            <Row label="Client secret">
              <input
                type="password"
                className="v2-input"
                value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)}
                placeholder="GOCSPX-…"
                autoComplete="off"
                data-testid={`${pid}-client-secret`}
              />
            </Row>
            {err && <div className="v2-chip v2-chip--error" role="alert">{err}</div>}
            {msg && <div className="v2-chip v2-chip--live">{msg}</div>}
            <div className="v2-forge-actions">
              <button type="button" className="v2-btn v2-btn--primary" onClick={saveCreds} disabled={busy}>
                {busy ? 'Saving…' : 'Save credentials'}
              </button>
            </div>
          </div>
        </div>
      )}
    </Glass>
  );
}

function HomeAssistantCard({ provider, onSaved, onDisconnect, connected }) {
  const pid = HA_PROVIDER_ID;
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [msg, setMsg] = useState('');

  const save = async () => {
    if (!token.trim()) { setErr('Long-lived token is required.'); return; }
    setBusy(true);
    setErr('');
    setMsg('');
    try {
      const r = await apiFetch('/api/integrations/token', {
        method: 'POST',
        body: JSON.stringify({ provider_id: pid, token: token.trim() }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok || body?.error) throw new Error(formatApiDetail(body, `save failed (${r.status})`));
      setMsg('Saved. Probing Home Assistant…');
      setToken('');
      await onSaved();
    } catch (e) {
      setErr(e?.message || 'save failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Glass level={0} radius="md" padding="md" data-testid="ha-card">
      <div className="v2-flow-card-head" style={{ marginBottom: 6 }}>
        <strong>{provider?.name || 'Home Assistant'}</strong>
        <Status tone={connected ? 'live' : 'off'}>{connected ? 'connected' : 'disconnected'}</Status>
        {connected && <button type="button" className="v2-btn" onClick={onDisconnect}>Disconnect</button>}
      </div>
      <p className="v2-p v2-p--muted v2-p--tiny" style={{ marginBottom: 8 }}>
        In Home Assistant: <strong>Profile → Security → Long-lived access tokens → Create token</strong> →
        paste it below. No OAuth.
      </p>
      <Row label="Long-lived token">
        <input
          type="password"
          className="v2-input"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="eyJ0eXAi…"
          autoComplete="off"
          data-testid="ha-token"
        />
      </Row>
      {err && <div className="v2-chip v2-chip--error" role="alert">{err}</div>}
      {msg && <div className="v2-chip v2-chip--live">{msg}</div>}
      <div className="v2-forge-actions" style={{ marginTop: 8 }}>
        <button type="button" className="v2-btn v2-btn--primary" onClick={save} disabled={busy} data-testid="ha-save">
          {busy ? 'Saving + probing…' : 'Save and probe'}
        </button>
      </div>
    </Glass>
  );
}

function IntegrationsSection() {
  const [providers, setProviders] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');

  const refresh = useCallback(async () => {
    setErr('');
    try {
      const d = await apiJson('/api/integrations');
      const rows = d.providers || d.integrations || [];
      // Some legacy fields surface alongside the providers list
      // (`spotify_connected`, etc.); normalise into proper rows so
      // every tile renders the right card.
      setProviders(Array.isArray(rows) ? rows : []);
    } catch (e) {
      setErr(e?.message || 'failed to load integrations');
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const disconnect = async (id) => {
    if (!window.confirm(`Disconnect ${id}? Vault credentials will be revoked.`)) return;
    try {
      await apiFetch(`/api/integrations/disconnect/${encodeURIComponent(id)}`, { method: 'POST' });
      await refresh();
    } catch (e) {
      setErr(e?.message || 'disconnect failed');
    }
  };

  if (loading) return <EmptyState title="Loading integrations…" />;

  // Make sure gmail + home_assistant tiles always render even when the
  // backend hasn't yet produced an entry for them (e.g. first boot).
  const byId = new Map();
  for (const p of providers) byId.set(p.id || p.provider_id, p);
  if (!byId.has(GMAIL_PROVIDER_ID)) {
    byId.set(GMAIL_PROVIDER_ID, { id: GMAIL_PROVIDER_ID, name: 'Gmail', connected: false });
  }
  if (!byId.has(HA_PROVIDER_ID)) {
    byId.set(HA_PROVIDER_ID, { id: HA_PROVIDER_ID, name: 'Home Assistant', connected: false });
  }

  const all = Array.from(byId.values());
  const gmail = byId.get(GMAIL_PROVIDER_ID);
  const ha = byId.get(HA_PROVIDER_ID);
  const oauthRows = all.filter((p) => {
    const pid = (p.id || p.provider_id || '').toLowerCase();
    if (pid === GMAIL_PROVIDER_ID || pid === HA_PROVIDER_ID) return false;
    if (OAUTH_SELF_SERVE.has(pid)) return true;
    if (p.auth_type === 'oauth2' || p.auth_type === 'oauth') return true;
    return false;
  });

  return (
    <div className="v2-setting-stack">
      {err && <div className="v2-chip v2-chip--error" role="alert">{err}</div>}
      <p className="v2-p v2-p--muted v2-p--tiny">
        Each integration's connection status comes from a real backend probe (Lane 10). Gmail uses an App Password
        (no OAuth); every other provider uses your own OAuth app — paste your client_id + client_secret once and
        FERAL handles the rest.
      </p>

      <GmailWalkthrough
        provider={gmail}
        connected={!!gmail.connected}
        onSaved={refresh}
        onDisconnect={() => disconnect(GMAIL_PROVIDER_ID)}
      />

      {oauthRows.length === 0 && <EmptyState title="No OAuth integrations available" />}
      {oauthRows.map((p) => (
        <OAuthSelfServeCard
          key={p.id || p.provider_id}
          provider={p}
          connected={!!p.connected}
          onSaved={refresh}
          onDisconnect={() => disconnect(p.id || p.provider_id)}
        />
      ))}

      <HomeAssistantCard
        provider={ha}
        connected={!!ha.connected}
        onSaved={refresh}
        onDisconnect={() => disconnect(HA_PROVIDER_ID)}
      />
    </div>
  );
}

// ── Cost (Lane 04 + Lane 06 consumer) ──────────────────────────
//
// Reads caps from `/api/config` (settings dict has
// `cost.per_call_site_caps` per the CostBudget._merged_settings
// contract). Writes via `/api/config/update`. Live spend is delivered
// by the `budget_exceeded` WS frame (Lane 08) when a cap is hit.
//
// Known gap: the brain doesn't yet expose `/api/cost/snapshot`
// returning per-window current spend. Filed as a parent follow-up in
// WORK_LOG; until then, the "Current spend" column reads from the
// last received WS frame, which is sufficient to prove the panel and
// the S6 banner deeplink work end-to-end.

const DEFAULT_CALL_SITES_FOR_COST = [
  'chat', 'vision', 'embedding', 'routing', 'screen_loop', 'proactive', 'learner', 'compaction',
];

function CostSection({ initialCallSite }) {
  const [config, setConfig] = useState(null);
  const [editing, setEditing] = useState({});
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');
  const [msg, setMsg] = useState('');
  const [spends, setSpends] = useState({});

  const refresh = useCallback(async () => {
    setErr('');
    try {
      const c = await apiJson('/api/config');
      setConfig(c);
    } catch (e) {
      setErr(e?.message || 'failed to load cost settings');
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // S6 — listen to the live `budget_exceeded` WS frame so the panel
  // updates spend + reset time without a poll. The same shared
  // FeralSocket the Chat panel uses.
  useEffect(() => {
    let unsub = null;
    let cancelled = false;
    (async () => {
      try {
        const { useFeralSocket } = await import('../hooks/useFeralSocket');
        // can't call hook outside React; instead grab the shared
        // socket via the test seam (or fall back to a fresh
        // subscription)
        const { _getSharedSocketForTesting } = await import('../hooks/useFeralSocket');
        const socket = _getSharedSocketForTesting();
        if (cancelled || !socket) return;
        unsub = socket.subscribe((msg) => {
          if (msg?.type !== 'budget_exceeded') return;
          const p = msg.payload || msg || {};
          const site = p.call_site || 'unknown';
          setSpends((prev) => ({
            ...prev,
            [site]: {
              current_dollars: Number(p.current_dollars || 0),
              cap_dollars: Number(p.cap_dollars || 0),
              reset_at: p.reset_at,
              at: Date.now(),
            },
          }));
        });
      } catch {
        /* the shared socket may not be initialised in test envs */
      }
    })();
    return () => { cancelled = true; if (unsub) unsub(); };
  }, []);

  if (!config) {
    return (
      <div className="v2-setting-stack">
        {err ? <div className="v2-chip v2-chip--error">{err}</div> : <EmptyState title="Loading cost caps…" />}
      </div>
    );
  }

  const costCfg = (config.cost && typeof config.cost === 'object') ? config.cost : {};
  const perSiteCaps = (costCfg.per_call_site_caps && typeof costCfg.per_call_site_caps === 'object') ? costCfg.per_call_site_caps : {};
  const globalDay = Number(costCfg.global_per_day_usd ?? 0);
  const globalHour = Number(costCfg.global_per_hour_usd ?? 5);

  const callSites = Array.from(new Set([
    ...DEFAULT_CALL_SITES_FOR_COST,
    ...Object.keys(perSiteCaps),
    ...(initialCallSite ? [initialCallSite] : []),
  ]));

  const capForSite = (site) => {
    const cfg = perSiteCaps[site];
    if (!cfg) return 0;
    return Number(cfg.per_hour_usd ?? 0);
  };

  const updateCap = async (site) => {
    const raw = editing[site];
    const value = Number(raw);
    if (!Number.isFinite(value) || value < 0) {
      setErr(`Cap for ${site} must be a non-negative number.`);
      return;
    }
    setBusy(`cap:${site}`);
    setErr('');
    setMsg('');
    try {
      const nextSiteCaps = { ...perSiteCaps, [site]: { ...(perSiteCaps[site] || {}), per_hour_usd: value } };
      const r = await apiFetch('/api/config/update', {
        method: 'POST',
        body: JSON.stringify({ section: 'cost', key: 'per_call_site_caps', value: nextSiteCaps }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(formatApiDetail(body, `failed to update ${site}`));
      }
      setMsg(`Updated ${site} cap to $${value.toFixed(2)}/hour.`);
      setEditing((prev) => { const n = { ...prev }; delete n[site]; return n; });
      await refresh();
    } catch (e) {
      setErr(e?.message || `update ${site} failed`);
    } finally {
      setBusy('');
    }
  };

  const updateGlobal = async (key, raw) => {
    const value = Number(raw);
    if (!Number.isFinite(value) || value < 0) {
      setErr(`Global cap must be non-negative.`);
      return;
    }
    setBusy(`global:${key}`);
    setErr('');
    setMsg('');
    try {
      const r = await apiFetch('/api/config/update', {
        method: 'POST',
        body: JSON.stringify({ section: 'cost', key, value }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(formatApiDetail(body, `failed to update ${key}`));
      }
      setMsg(`Updated ${key} to $${value.toFixed(2)}.`);
      await refresh();
    } catch (e) {
      setErr(e?.message || `update ${key} failed`);
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="v2-setting-stack" data-testid="cost-section">
      <p className="v2-p v2-p--muted v2-p--tiny">
        Per-call-site hourly caps in USD. When a cap is exceeded the chat receives a yellow inline banner
        (the same one rendered in Chat → S6). 0 disables the cap for that site.
      </p>
      {err && <div className="v2-chip v2-chip--error" role="alert">{err}</div>}
      {msg && <div className="v2-chip v2-chip--live">{msg}</div>}

      <Glass level={0} radius="md" padding="md">
        <div className="v2-voice-group__title">Per call-site (hourly)</div>
        {callSites.map((site) => {
          const cap = capForSite(site);
          const current = spends[site];
          const editingValue = editing[site];
          const inputValue = editingValue != null ? editingValue : String(cap);
          const resetStr = current?.reset_at ? new Date(
            typeof current.reset_at === 'string' ? Date.parse(current.reset_at) : Number(current.reset_at) * 1000,
          ).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
          return (
            <div key={site} className="v2-cost-cap-row" data-testid={`cost-row-${site}`}>
              <div className="v2-cost-cap-row__label">{site}{site === initialCallSite && <span className="v2-chip" style={{ marginLeft: 6 }}>from chat</span>}</div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span className="v2-p v2-p--muted">$</span>
                <input
                  type="number"
                  step="0.05"
                  min="0"
                  className="v2-input"
                  style={{ width: 90, textAlign: 'right' }}
                  value={inputValue}
                  onChange={(e) => setEditing((p) => ({ ...p, [site]: e.target.value }))}
                  data-testid={`cost-cap-${site}`}
                />
                <span className="v2-p v2-p--muted v2-p--tiny">/hr</span>
              </div>
              <span className="v2-cost-cap-row__spend">
                {current ? `$${current.current_dollars.toFixed(2)}` : '—'}
              </span>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, justifyContent: 'flex-end' }}>
                {resetStr && <span className="v2-cost-cap-row__reset">resets {resetStr}</span>}
                <button
                  type="button"
                  className="v2-btn v2-btn--primary"
                  onClick={() => updateCap(site)}
                  disabled={busy === `cap:${site}` || editingValue == null}
                  data-testid={`cost-save-${site}`}
                >
                  {busy === `cap:${site}` ? 'Saving…' : 'Save'}
                </button>
              </div>
            </div>
          );
        })}
      </Glass>

      <Glass level={0} radius="md" padding="md">
        <div className="v2-voice-group__title">Global limits</div>
        <Row label="Global per hour (USD)" hint="Stops every call-site once the combined hourly spend reaches this number.">
          <input
            type="number"
            step="0.10"
            min="0"
            className="v2-input"
            style={{ width: 100, textAlign: 'right' }}
            defaultValue={globalHour}
            onBlur={(e) => { if (Number(e.target.value) !== globalHour) updateGlobal('global_per_hour_usd', e.target.value); }}
            data-testid="cost-global-hour"
          />
        </Row>
        <Row label="Global per day (USD)" hint="0 = disabled. Resets at midnight local time.">
          <input
            type="number"
            step="0.50"
            min="0"
            className="v2-input"
            style={{ width: 100, textAlign: 'right' }}
            defaultValue={globalDay}
            onBlur={(e) => { if (Number(e.target.value) !== globalDay) updateGlobal('global_per_day_usd', e.target.value); }}
            data-testid="cost-global-day"
          />
        </Row>
      </Glass>
    </div>
  );
}

// ── Sync ──────────────────────────────────────────────────────

function SyncSection() {
  const [status, setStatus] = useState(null);
  const [importMsg, setImportMsg] = useState(null);

  useEffect(() => { apiJson('/api/sync/status').then(setStatus).catch(() => setStatus({})); }, []);

  const doImport = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setImportMsg('Uploading…');
    try {
      const text = await file.text();
      const body = JSON.parse(text);
      const r = await apiFetch('/api/sync/import', { method: 'POST', body: JSON.stringify(body) });
      setImportMsg(r.ok ? `Imported ${file.name}` : `Failed: ${r.status}`);
    } catch (err) {
      setImportMsg(`Failed: ${err.message}`);
    }
  };

  if (!status) return <EmptyState title="Loading sync status…" />;

  const peers = Array.isArray(status.peers) ? status.peers : [];

  return (
    <div className="v2-setting-stack">
      <p className="v2-p v2-p--muted">
        FERAL's memory replicates across your paired devices via a
        conflict-free data structure (CRDT). No central server — peers
        merge directly. Pair a second device to start syncing.
      </p>
      <Row label="Engine" hint="Sync subsystem status">
        <Status tone={status.enabled ? 'live' : 'off'}>
          {status.enabled ? 'enabled' : 'disabled'}
        </Status>
        <Status tone={status.running ? 'live' : 'off'}>
          {status.running ? 'running' : 'stopped'}
        </Status>
      </Row>
      <Row label="Node ID" hint="This device's stable identifier">
        <code className="v2-code-inline">{status.node_id || '—'}</code>
      </Row>
      <Row label="Peer count" hint={peers.length === 0 ? 'No other devices paired yet.' : undefined}>
        <Status tone={peers.length > 0 ? 'live' : 'neutral'}>{status.peer_count ?? peers.length}</Status>
      </Row>
      {peers.length > 0 && (
        <Row label="Peers">
          <div className="v2-skill-card-phrases">
            {peers.map((p) => (
              <span key={typeof p === 'string' ? p : p.id} className="v2-chip">
                {typeof p === 'string' ? p : (p.name || p.id)}
              </span>
            ))}
          </div>
        </Row>
      )}
      <Row label="WAL entries" hint="Write-ahead log — every change awaiting replication">
        <code className="v2-code-inline">{status.wal_entries ?? 0}</code>
      </Row>
      {status.vector_clock && Object.keys(status.vector_clock).length > 0 && (
        <Row label="Vector clock" hint="Causal ordering per peer">
          <details className="v2-vault-details">
            <summary>{Object.keys(status.vector_clock).length} entries</summary>
            <pre className="v2-code">{JSON.stringify(status.vector_clock, null, 2).slice(0, 800)}</pre>
          </details>
        </Row>
      )}
      <Row label="Export" hint="Download CRDT state for backup or manual sync">
        <a className="v2-btn" href={`${API_BASE}/api/sync/export`} target="_blank" rel="noreferrer">Download JSON</a>
      </Row>
      <Row label="Import" hint="Upload a previously exported CRDT state">
        <input type="file" accept="application/json" onChange={doImport} className="v2-input" />
      </Row>
      {importMsg && <div className="v2-chip v2-chip--live">{importMsg}</div>}
    </div>
  );
}

// ── Handoff ───────────────────────────────────────────────────

function HandoffSection() {
  const [devices, setDevices] = useState([]);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState(null);

  useEffect(() => {
    apiJson('/api/handoff/devices')
      .then((d) => setDevices(d?.devices || (Array.isArray(d) ? d : [])))
      .catch(() => setDevices([]))
      .finally(() => setLoading(false));
  }, []);

  const handoff = async (target) => {
    setMsg('Handing off…');
    try {
      const r = await apiFetch('/api/handoff', {
        method: 'POST',
        body: JSON.stringify({ target }),
      });
      setMsg(r.ok ? `Handed off to ${target}` : `Failed: ${r.status}`);
    } catch (err) {
      setMsg(`Failed: ${err.message}`);
    }
  };

  return (
    <div className="v2-setting-stack">
      <p className="v2-p v2-p--muted">
        Handoff transfers your active FERAL session to another paired device.
        Start a conversation on your Mac, hand it off to your phone when you
        leave — the conversation context, active skills, and pending tool
        calls follow you. Requires at least two devices paired to this Brain.
      </p>
      {loading && <EmptyState title="Loading targets…" />}
      {!loading && devices.length === 0 && (
        <EmptyState
          title="No other devices paired yet"
          hint="Pair your phone, tablet, or another laptop to unlock handoff."
          action={<a href="/v2/devices" className="v2-btn v2-btn--primary">Open Devices</a>}
        />
      )}
      {devices.map((d) => (
        <Row key={d.id || d.device_id} label={d.name || d.device_id} hint={d.last_seen ? `Last seen ${d.last_seen}` : ''}>
          <button
            type="button"
            className="v2-btn v2-btn--primary"
            onClick={() => handoff(d.id || d.device_id)}
          >
            Hand off
          </button>
        </Row>
      ))}
      {msg && <div className="v2-chip v2-chip--live">{msg}</div>}
    </div>
  );
}

// ── Push ──────────────────────────────────────────────────────

function PushSection() {
  const [platform, setPlatform] = useState('apns');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const register = async () => {
    setBusy(true);
    try {
      const r = await apiFetch('/api/push/register', {
        method: 'POST',
        body: JSON.stringify({ platform, token }),
      });
      setMsg(r.ok ? 'Registered ✓' : `Failed: ${r.status}`);
    } finally { setBusy(false); }
  };

  const testSend = async () => {
    setBusy(true);
    try {
      await apiFetch('/api/push/send', {
        method: 'POST',
        body: JSON.stringify({ title: 'FERAL', body: 'Test push from Settings', platform }),
      });
      setMsg('Test push sent.');
    } finally { setBusy(false); }
  };

  return (
    <div className="v2-setting-stack">
      <Row label="Platform">
        <Select value={platform} onChange={setPlatform} options={[
          { value: 'apns', label: 'APNs (iOS)' },
          { value: 'fcm', label: 'FCM (Android)' },
        ]} />
      </Row>
      <Row label="Device token">
        <input className="v2-input" value={token} onChange={(e) => setToken(e.target.value)} placeholder="Paste APNs / FCM token" />
      </Row>
      <Row label="">
        <button type="button" className="v2-btn v2-btn--primary" onClick={register} disabled={busy || !token}>Register</button>
        <button type="button" className="v2-btn" onClick={testSend} disabled={busy}>Send test</button>
      </Row>
      {msg && <div className="v2-chip v2-chip--live">{msg}</div>}
    </div>
  );
}

// ── MCP ───────────────────────────────────────────────────────

function McpSection() {
  const [status, setStatus] = useState(null);
  const [registry, setRegistry] = useState([]);
  const [tools, setTools] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);
  const [msg, setMsg] = useState(null);

  const refresh = useCallback(async () => {
    const [s, r, t] = await Promise.allSettled([
      apiJson('/api/mcp/status'),
      apiJson('/api/mcp/registry'),
      apiJson('/api/mcp/tools'),
    ]);
    if (s.status === 'fulfilled') setStatus(s.value);
    if (r.status === 'fulfilled') setRegistry(r.value?.servers || []);
    if (t.status === 'fulfilled') setTools(t.value?.tools || []);
    setLoading(false);
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const connect = async (server) => {
    setBusy(server.id);
    setMsg(null);
    try {
      const body = {
        name: server.id,
        command: server.command,
        args: server.args,
        env: server.env,
      };
      const r = await apiFetch('/api/mcp/connect', { method: 'POST', body: JSON.stringify(body) });
      const data = await r.json().catch(() => ({}));
      setMsg(data?.success ? `${server.name} connected — ${data.tools} tools` : (data?.error || `Failed: ${r.status}`));
      refresh();
    } finally { setBusy(null); }
  };

  const copy = async (text) => {
    try { await navigator.clipboard.writeText(text); setMsg('Copied'); setTimeout(() => setMsg(null), 1500); } catch { /* silent */ }
  };

  if (loading) return <EmptyState title="Loading MCP…" />;

  const server = status?.server || {};
  const client = status?.client || {};

  return (
    <div className="v2-setting-stack">
      <p className="v2-p v2-p--muted">
        Model Context Protocol lets FERAL consume tools from external apps
        (GitHub, Filesystem, Slack, etc.) and lets external apps consume
        FERAL's skills via <code className="v2-code-inline">POST /mcp</code>.
      </p>
      <Row label="Server" hint="Tools FERAL exposes to external MCP clients">
        <Status tone="live">{server.tools_exposed ?? 0} tools</Status>
      </Row>
      <Row label="Client" hint="External MCP servers FERAL is consuming">
        <Status tone={client.servers_connected > 0 ? 'live' : 'off'}>
          {client.servers_connected ?? 0} servers
        </Status>
        <Status tone="neutral">{client.total_tools ?? 0} tools</Status>
      </Row>

      <div className="v2-p" style={{ marginTop: 8, fontWeight: 600 }}>Registered servers</div>
      {registry.length === 0 && <EmptyState title="No MCP servers in registry" />}
      <div className="v2-mcp-grid">
        {registry.map((s) => (
          <Glass key={s.id} level={0} radius="md" padding="md" className="v2-mcp-card">
            <header className="v2-mcp-head">
              <h3 className="v2-mcp-name">{s.name}</h3>
              <span className="v2-chip v2-chip--muted">{s.category || '—'}</span>
            </header>
            <p className="v2-p v2-p--muted">{s.description}</p>
            <div className="v2-mcp-chips">
              <Status tone={s.installed ? 'live' : 'off'}>
                {s.installed ? 'installed' : 'not installed'}
              </Status>
              <Status tone={s.configured ? 'live' : 'warn'}>
                {s.configured ? 'configured' : 'unconfigured'}
              </Status>
              <Status tone={s.connected ? 'live' : 'off'}>
                {s.connected ? 'connected' : 'disconnected'}
              </Status>
              {s.ready && <Status tone="live">ready</Status>}
            </div>
            {!s.installed && s.install_hint && (
              <div className="v2-mcp-hint">
                <div className="v2-p v2-p--tiny">Install:</div>
                <button
                  type="button"
                  className="v2-code v2-code--copyable"
                  onClick={() => copy(s.install_hint)}
                  title="Click to copy"
                >
                  {s.install_hint}
                </button>
              </div>
            )}
            {s.env && Object.keys(s.env).length > 0 && (
              <div className="v2-mcp-env">
                <div className="v2-p v2-p--tiny">Env:</div>
                <div className="v2-skill-card-phrases">
                  {Object.keys(s.env).map((k) => (
                    <span key={k} className={`v2-chip ${s.env[k] ? 'v2-chip--live' : 'v2-chip--warn'}`}>{k}</span>
                  ))}
                </div>
              </div>
            )}
            <div className="v2-forge-actions">
              {s.connected ? (
                <Status tone="live">in use</Status>
              ) : s.installed && s.ready ? (
                <button
                  type="button"
                  className="v2-btn v2-btn--primary"
                  disabled={busy === s.id}
                  onClick={() => connect(s)}
                >
                  {busy === s.id ? 'Connecting…' : 'Connect'}
                </button>
              ) : (
                <Status tone="neutral">needs setup</Status>
              )}
            </div>
          </Glass>
        ))}
      </div>

      {tools.length > 0 && (
        <details className="v2-vault-details">
          <summary>Connected tools ({tools.length})</summary>
          <ul className="v2-mem-list" style={{ marginTop: 8 }}>
            {tools.map((t, i) => (
              <li key={t.name || i}>
                <Glass level={0} radius="sm" padding="sm">
                  <div className="v2-flow-card-head">
                    <code className="v2-flow-card-title">{t.name}</code>
                    <span className="v2-chip v2-chip--muted">{t.server || ''}</span>
                  </div>
                  {t.description && <div className="v2-p v2-p--muted">{t.description}</div>}
                </Glass>
              </li>
            ))}
          </ul>
        </details>
      )}

      {msg && <div className="v2-chip v2-chip--live">{msg}</div>}
    </div>
  );
}


// ── Twin & Delegation ─────────────────────────────────────────

// Pretty labels for domains the backend exposes as wired executors.
// The backend payload itself can carry a label; this map is just the
// fallback so a domain id like "reply_slack" still renders nicely if a
// channel adapter forgot to register one.
const TWIN_DOMAIN_LABELS = {
  respond_imessage: 'Respond to iMessage',
  draft_email: 'Draft email',
  reply_slack: 'Reply on Slack',
  reply_telegram: 'Reply on Telegram',
  reply_whatsapp: 'Reply on WhatsApp',
  schedule_meeting: 'Schedule meetings',
  buy_groceries: 'Buy groceries',
  summarise_reading: 'Summarise readings',
  post_journal: 'Post to journal',
};

function _twinLabel(domain, fallback = '') {
  return fallback || TWIN_DOMAIN_LABELS[domain] || domain;
}

function TwinSection() {
  const [policies, setPolicies] = useState([]);
  const [disconnected, setDisconnected] = useState([]);
  const [available, setAvailable] = useState([]);
  const [pending, setPending] = useState([]);
  const [paused, setPaused] = useState(false);
  const [showAvailable, setShowAvailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const refresh = useCallback(async () => {
    setBusy(true);
    setErr(null);
    try {
      const pols = await apiJson('/api/twin/policies').catch(() => ({}));
      setPolicies(Array.isArray(pols.policies) ? pols.policies : []);
      setDisconnected(Array.isArray(pols.disconnected) ? pols.disconnected : []);
      setAvailable(Array.isArray(pols.available) ? pols.available : []);
      const approvals = await apiJson('/api/twin/approvals?status=pending').catch(() => ({ approvals: [] }));
      setPending(approvals.approvals || []);
      const stats = await apiJson('/api/supervisor/stats').catch(() => null);
      if (stats) setPaused(!!stats.paused);
    } catch (e) {
      setErr(e?.message || 'failed to load twin state');
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const policyByDomain = (() => {
    const map = {};
    for (const p of policies) map[p.domain] = p;
    for (const p of disconnected) map[p.domain] = p;
    return map;
  })();

  const upsert = async (domain, patch) => {
    const current = policyByDomain[domain] || {
      domain,
      mode: 'draft_only',
      time_windows: [],
      max_per_day: 10,
      requires_user_online: false,
    };
    const body = { ...current, ...patch, domain };
    await apiFetch('/api/twin/policies', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    refresh();
  };

  const revoke = async (domain) => {
    await apiFetch(`/api/twin/policies/${encodeURIComponent(domain)}`, { method: 'DELETE' });
    refresh();
  };

  const resolveApproval = async (id, verdict) => {
    await apiFetch(`/api/twin/approvals/${encodeURIComponent(id)}/${verdict}`, { method: 'POST' });
    refresh();
  };

  const togglePause = async () => {
    await apiFetch('/api/supervisor/pause', {
      method: 'POST',
      body: JSON.stringify({ paused: !paused }),
    });
    refresh();
  };

  // Domains in `available` that don't yet have a stored policy — the
  // discovery list. Lets the UI surface "you could wire this" without
  // pretending the row is already configured.
  const policyDomains = new Set(policies.map((p) => p.domain));
  const availableUnconfigured = available.filter((a) => !policyDomains.has(a.domain));
  const hasActive = policies.length > 0;

  // A "configured executor" is anything the backend reports as wired
  // right now — a stored policy whose executor is still bound, or any
  // discovery entry in `available`. Disconnected entries are stale and
  // do NOT count, which is what removes the kill-switch theatre on a
  // brand-new install (Roadmap §A.5 / W2).
  const hasConfiguredExecutor = policies.length > 0 || available.length > 0;

  // Workaround: TwinSection cannot edit Settings.jsx lines outside its
  // own range (W2 contract), so it cannot take a real `setSection`
  // prop yet. The Connect button on each "Available executors" row
  // therefore clicks the matching settings nav button via the DOM.
  // Tracked in docs/AGENT_PROMPTS_FOLLOWUPS.md for lifting into props.
  const navigateToSettingsSection = (target) => {
    if (typeof document === 'undefined') return;
    const buttons = document.querySelectorAll('.v2-settings-btn');
    for (const b of buttons) {
      if ((b.textContent || '').trim() === target) {
        b.click();
        return;
      }
    }
  };

  // Channels-driven domains live under Settings → Channels; the
  // productivity-flavoured ones (mail, calendar, meetings, readings,
  // journal) live under Settings → Integrations. Anything else
  // defaults to Channels.
  const targetSectionForDomain = (domain) => (
    /email|calendar|meeting|reading|journal/i.test(domain) ? 'Integrations' : 'Channels'
  );

  return (
    <div className="v2-twin-section">
      <p className="v2-p v2-p--muted">
        Let the digital twin act for you — one toggle per domain. Draft-only keeps
        every action in the approval queue below; auto-send fires immediately
        within the window + daily cap. The big red button pauses every twin call
        and every orchestrator dispatch at once.
      </p>

      {hasConfiguredExecutor && (
        <div
          style={{ marginTop: 10, display: 'flex', gap: 8, alignItems: 'center' }}
          data-testid="twin-kill-switch"
        >
          <button
            type="button"
            className={`v2-btn ${paused ? 'v2-btn--primary' : ''}`}
            onClick={togglePause}
          >
            {paused ? 'Resume all actions' : 'Pause all actions'}
          </button>
          <span className="v2-p v2-p--muted v2-p--tiny">
            {hasActive
              ? 'Kill switch — pauses every wired twin action at once.'
              : 'Kill switch — pauses every executor below the moment you enable a policy.'}
          </span>
        </div>
      )}

      {err && <div className="v2-chip v2-chip--error" style={{ marginTop: 8 }}>{err}</div>}

      {!hasConfiguredExecutor && disconnected.length === 0 && (
        <div
          className="v2-twin-empty"
          data-testid="twin-empty-state"
          style={{ marginTop: 14 }}
        >
          <Glass level={0} radius="md" padding="md">
            <div style={{ fontWeight: 600, marginBottom: 4 }}>
              No twin executors configured.
            </div>
            <p className="v2-p v2-p--muted" style={{ margin: 0 }}>
              Connect iMessage / email / calendar in the Channels and
              Integrations sections to enable. Authorised twin actions will
              appear here as soon as a real executor is ready — until then
              the twin has nothing to act on.
            </p>
          </Glass>
        </div>
      )}

      {hasActive && (
        <div className="v2-twin-domains" data-testid="twin-active-domains">
          {policies.map((p) => {
            const id = p.domain;
            const mode = p.mode || 'draft_only';
            const windows = (p.time_windows || []).join(', ');
            const cap = p.max_per_day ?? 10;
            const label = _twinLabel(id, p.label);
            return (
              <Glass key={id} level={0} radius="md" padding="sm" className="v2-twin-domain">
                <div className="v2-twin-domain-head">
                  <div>
                    <div className="v2-twin-domain-label">{label}</div>
                    <div className="v2-p v2-p--tiny v2-p--muted">
                      <code>{id}</code> · {mode} · cap {cap}/day{windows ? ` · ${windows}` : ''}
                    </div>
                  </div>
                  <div className="v2-twin-domain-actions">
                    <button type="button" className={`v2-btn ${mode === 'draft_only' ? 'v2-btn--primary' : ''}`} onClick={() => upsert(id, { mode: 'draft_only' })} disabled={busy}>Draft</button>
                    <button type="button" className={`v2-btn ${mode === 'auto_send' ? 'v2-btn--primary' : ''}`} onClick={() => upsert(id, { mode: 'auto_send' })} disabled={busy}>Auto</button>
                    <button type="button" className={`v2-btn ${mode === 'disabled' ? 'v2-btn--primary' : ''}`} onClick={() => upsert(id, { mode: 'disabled' })} disabled={busy}>Off</button>
                    <button type="button" className="v2-btn v2-btn--ghost" onClick={() => revoke(id)} disabled={busy}>Clear</button>
                  </div>
                </div>
              </Glass>
            );
          })}
        </div>
      )}

      {disconnected.length > 0 && (
        <div className="v2-twin-domains" data-testid="twin-disconnected" style={{ marginTop: 12 }}>
          <div className="v2-p v2-p--muted v2-p--tiny" style={{ marginBottom: 6 }}>
            Disconnected — the channel that backed these is no longer wired.
          </div>
          {disconnected.map((p) => {
            const id = p.domain;
            const label = _twinLabel(id, p.label);
            return (
              <Glass
                key={id}
                level={0}
                radius="md"
                padding="sm"
                className="v2-twin-domain v2-twin-domain--disconnected"
                style={{ opacity: 0.6 }}
              >
                <div className="v2-twin-domain-head">
                  <div>
                    <div className="v2-twin-domain-label">{label}</div>
                    <div className="v2-p v2-p--tiny v2-p--muted">
                      <code>{id}</code> · disconnected · last mode: {p.mode}
                    </div>
                  </div>
                  <div className="v2-twin-domain-actions">
                    <span className="v2-chip v2-chip--warn">Disconnected</span>
                    <button type="button" className="v2-btn v2-btn--ghost" onClick={() => revoke(id)} disabled={busy}>Clear</button>
                  </div>
                </div>
              </Glass>
            );
          })}
        </div>
      )}

      {availableUnconfigured.length > 0 && (
        <div style={{ marginTop: 16 }} data-testid="twin-available-section">
          <button
            type="button"
            className="v2-btn v2-btn--ghost"
            onClick={() => setShowAvailable((v) => !v)}
            aria-expanded={showAvailable}
          >
            Available executors ({availableUnconfigured.length}) {showAvailable ? '▾' : '▸'}
          </button>
          {showAvailable && (
            <div
              className="v2-twin-domains"
              style={{ marginTop: 8 }}
              data-testid="twin-available-list"
            >
              {availableUnconfigured.map((a) => {
                const id = a.domain;
                const label = _twinLabel(id, a.label);
                const target = targetSectionForDomain(id);
                return (
                  <Glass
                    key={id}
                    level={0}
                    radius="md"
                    padding="sm"
                    className="v2-twin-domain v2-twin-domain--available"
                    data-testid={`twin-available-row-${id}`}
                  >
                    <div className="v2-twin-domain-head">
                      <div>
                        <div className="v2-twin-domain-label">{label}</div>
                        <div className="v2-p v2-p--tiny v2-p--muted">
                          <code>{id}</code> · off · not connected
                        </div>
                      </div>
                      <div className="v2-twin-domain-actions">
                        <button
                          type="button"
                          className="v2-btn v2-btn--primary"
                          onClick={() => navigateToSettingsSection(target)}
                          disabled={busy}
                          aria-label={`Connect ${label} in ${target}`}
                        >
                          Connect
                        </button>
                      </div>
                    </div>
                  </Glass>
                );
              })}
            </div>
          )}
        </div>
      )}

      <div style={{ marginTop: 18 }}>
        <h3 style={{ margin: 0, fontSize: 13, textTransform: 'uppercase', letterSpacing: '0.06em', color: 'var(--v2-text-secondary)' }}>
          Pending approvals
        </h3>
        {pending.length === 0 ? (
          <p className="v2-p v2-p--muted" style={{ marginTop: 6 }}>Queue is empty.</p>
        ) : (
          <ul className="v2-twin-approvals">
            {pending.map((row) => (
              <li key={row.approval_id}>
                <Glass level={0} radius="md" padding="sm">
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                      <div style={{ fontWeight: 600 }}>{row.domain} · {row.action}</div>
                      <div className="v2-p v2-p--tiny v2-p--muted">
                        queued {new Date((row.created_at || 0) * 1000).toLocaleString()}
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: 6 }}>
                      <button type="button" className="v2-btn v2-btn--primary" onClick={() => resolveApproval(row.approval_id, 'approve')}>Approve</button>
                      <button type="button" className="v2-btn" onClick={() => resolveApproval(row.approval_id, 'reject')}>Reject</button>
                    </div>
                  </div>
                  {row.context && Object.keys(row.context).length > 0 && (
                    <pre className="v2-publish-error" style={{ color: 'var(--v2-text-secondary)' }}>
                      {JSON.stringify(row.context, null, 2)}
                    </pre>
                  )}
                </Glass>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
