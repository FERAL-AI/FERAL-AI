import React, { useCallback, useEffect, useState } from 'react';
import { Download, Trash2, RefreshCw, ShieldCheck, ShieldAlert } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Tabs from '../ui/Tabs';
import Modal from '../ui/Modal';
import EmptyState from '../ui/EmptyState';
import { apiJson, apiFetch } from '../lib/api';

const KINDS = ['skill', 'app', 'daemon', 'mcp', 'channel', 'provider', 'memory', 'workflow', 'agent'];

/**
 * Colours come from styles/tokens.css only. These are inline because this
 * page must not add rules to a shared stylesheet (see Oversight.jsx for the
 * same pattern); every value is a var() lookup so light/dark and any future
 * palette change still apply.
 */
const S = {
  permList: {
    listStyle: 'none',
    margin: '0 0 12px',
    padding: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
  },
  permRow: {
    display: 'flex',
    gap: 10,
    alignItems: 'flex-start',
    padding: '10px 12px',
    borderRadius: 'var(--v2-radius-sm)',
    border: '1px solid var(--v2-hairline)',
    background: 'var(--v2-fill-subtle)',
  },
  permLabel: {
    color: 'var(--v2-text-primary)',
    fontSize: 'var(--v2-size-base)',
    fontWeight: 600,
    lineHeight: 1.35,
  },
  permDesc: {
    color: 'var(--v2-text-secondary)',
    fontSize: 'var(--v2-size-sm)',
    lineHeight: 1.45,
    marginTop: 2,
  },
  permDot: {
    width: 6,
    height: 6,
    borderRadius: 'var(--v2-radius-pill)',
    background: 'var(--v2-text-tertiary)',
    marginTop: 7,
    flex: '0 0 auto',
  },
  sigBox: (ok) => ({
    display: 'flex',
    gap: 10,
    alignItems: 'flex-start',
    padding: '10px 12px',
    marginBottom: 12,
    borderRadius: 'var(--v2-radius-sm)',
    border: `1px solid ${ok ? 'var(--v2-state-live-soft)' : 'var(--v2-state-error-soft)'}`,
    background: ok ? 'var(--v2-state-live-soft)' : 'var(--v2-state-error-soft)',
    color: ok ? 'var(--v2-state-live)' : 'var(--v2-state-error)',
    fontSize: 'var(--v2-size-sm)',
    lineHeight: 1.45,
  }),
  sigDetail: { color: 'var(--v2-text-secondary)', marginTop: 2 },
  lead: {
    color: 'var(--v2-text-secondary)',
    fontSize: 'var(--v2-size-sm)',
    lineHeight: 1.5,
    margin: '0 0 12px',
  },
  sectionLabel: {
    color: 'var(--v2-text-tertiary)',
    fontSize: 'var(--v2-size-xs)',
    letterSpacing: '0.06em',
    textTransform: 'uppercase',
    margin: '0 0 8px',
  },
  cardPerms: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: 4,
    marginTop: 6,
  },
  hash: {
    fontFamily: 'var(--v2-font-mono)',
    fontSize: 'var(--v2-size-xs)',
    color: 'var(--v2-text-tertiary)',
    wordBreak: 'break-all',
  },
};

export default function Marketplace() {
  const [tab, setTab] = useState('browse');
  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title="Marketplace"
        actions={(
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { id: 'browse', label: 'Browse' },
              { id: 'installed', label: 'Installed' },
            ]}
          />
        )}
      >
        <p className="v2-p v2-p--muted">
          Signed community registry at registry.feral.sh. Every install is checked against the
          publisher&apos;s signature and lists what the item can reach before it is installed.
        </p>
      </Pane>
      {tab === 'browse' && <BrowseTab />}
      {tab === 'installed' && <InstalledTab />}
    </div>
  );
}

/** Signature state + copy for one preview response. */
function readSignature(preview) {
  const sig = preview?.signature || {};
  const verified = sig.verified === true;
  return {
    verified,
    publisher: sig.publisher || preview?.publisher || '',
    sha256: sig.sha256 || '',
    reason: sig.reason || '',
  };
}

function PermissionList({ details, permissions }) {
  const rows = (details && details.length)
    ? details
    : (permissions || []).map((id) => ({ id, label: id, description: '' }));

  if (!rows.length) {
    return (
      <p style={S.permDesc} data-testid="v2-install-permissions-empty">
        This item declares no permissions. It can still run its own code inside FERAL, so only
        install it if you trust the publisher.
      </p>
    );
  }
  return (
    <ul style={S.permList} data-testid="v2-install-permissions">
      {rows.map((p) => (
        <li key={p.id} style={S.permRow}>
          <span style={S.permDot} aria-hidden="true" />
          <span>
            <span style={S.permLabel}>{p.label || p.id}</span>
            {p.description && <span style={{ ...S.permDesc, display: 'block' }}>{p.description}</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}

/**
 * The consent step. Removing a skill already asked; adding one now asks
 * too, and asks with the two facts that make the answer possible: what it
 * can reach, and whether the bytes are the publisher's.
 */
function InstallConsent({ pending, busy, onCancel, onConfirm }) {
  if (!pending) return null;
  const { item, preview, error } = pending;
  const sig = readSignature(preview);
  const name = preview?.name || item?.name || item?.id;
  const version = preview?.version || item?.version;
  const canInstall = sig.verified && !!preview?.install_token && !error;

  return (
    <Modal
      open
      onClose={onCancel}
      title={`Install ${name}${version ? ` v${version}` : ''}?`}
      actions={(
        <>
          <button
            type="button"
            className="v2-btn"
            data-testid="v2-install-cancel"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            type="button"
            className="v2-btn v2-btn--primary"
            data-testid="v2-install-confirm"
            onClick={onConfirm}
            disabled={!canInstall || busy}
          >
            <Download size={12} /> {busy ? 'Installing…' : 'Install'}
          </button>
        </>
      )}
    >
      <div style={S.sigBox(sig.verified)} data-testid="v2-install-signature">
        {sig.verified
          ? <ShieldCheck size={16} aria-hidden="true" style={{ flex: '0 0 auto', marginTop: 1 }} />
          : <ShieldAlert size={16} aria-hidden="true" style={{ flex: '0 0 auto', marginTop: 1 }} />}
        <span>
          {sig.verified ? (
            <>
              <strong>Signature verified.</strong>
              <span style={S.sigDetail}>
                {' '}
                These files were signed by {sig.publisher || 'the publisher'} and arrived unchanged.
              </span>
            </>
          ) : (
            <>
              <strong>Not verified. FERAL will not install this.</strong>
              <span style={S.sigDetail}>
                {' '}
                The download could not be matched to the publisher&apos;s signature, so there is no
                way to tell who wrote these files or whether they were altered.
                {(error || sig.reason) ? ` Reason: ${error || sig.reason}` : ''}
              </span>
            </>
          )}
        </span>
      </div>

      {sig.verified && (
        <>
          <p style={S.lead}>
            Installing gives {name} the access below on this computer, every time it runs, until
            you uninstall it.
          </p>
          <p style={S.sectionLabel}>What it can reach</p>
          <PermissionList
            details={preview?.permission_details}
            permissions={preview?.permissions}
          />
          {preview?.permissions_source === 'registry' && (
            <p style={S.permDesc}>
              This list comes from the registry listing rather than from the signed package,
              because this item type does not ship a FERAL manifest.
            </p>
          )}
          {sig.sha256 && (
            <p style={S.hash}>
              SHA-256 {sig.sha256}
            </p>
          )}
        </>
      )}
    </Modal>
  );
}

function BrowseTab() {
  const [kind, setKind] = useState('skill');
  const [query, setQuery] = useState('');
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [catalogError, setCatalogError] = useState(null);
  const [busy, setBusy] = useState(null);
  const [msg, setMsg] = useState(null);
  const [pending, setPending] = useState(null);
  const [installing, setInstalling] = useState(false);

  const fetchList = useCallback(async () => {
    setLoading(true);
    setCatalogError(null);
    try {
      const url = query.trim()
        ? `/api/marketplace/search?q=${encodeURIComponent(query)}&kind=${kind}`
        : `/api/marketplace/catalog?kind=${kind}`;
      const d = await apiJson(url);
      setItems(d.items || d.results || d || []);
      if (typeof d?.error === 'string' && d.error.trim()) {
        setCatalogError(d.error.trim());
      }
    } catch (err) {
      setItems([]);
      setCatalogError(err?.message || 'Catalog unavailable');
    }
    finally { setLoading(false); }
  }, [kind, query]);

  useEffect(() => { fetchList(); }, [fetchList]);

  const flash = (text) => {
    setMsg(text);
    setTimeout(() => setMsg(null), 4000);
  };

  const installApp = async (it, id) => {
    setBusy(id);
    try {
      const r = await apiFetch('/api/apps/install', {
        method: 'POST',
        body: JSON.stringify({ registry_id: id }),
        silent: true,
      });
      const data = await r.json().catch(() => ({}));
      flash(data?.error || `Installed ${it.name || id}`);
    } catch (err) {
      flash(err?.detail || err?.message || 'Install failed');
    } finally { setBusy(null); }
  };

  // Step one: verify and describe. Nothing is installed by this call, and
  // the install button in the dialog is dead without the token it returns.
  const openPreview = async (it) => {
    const id = it.id || it.item_id;
    const itKind = it.kind || kind;
    if (itKind === 'app') {
      // GenUI apps install through AppRegistry, which runs its own
      // signature check; there is no marketplace preview for them.
      return installApp(it, id);
    }
    setBusy(id);
    try {
      const preview = await apiJson('/api/marketplace/preview', {
        method: 'POST',
        body: JSON.stringify({ kind: itKind, id }),
        silent: true,
      });
      setPending({
        item: { ...it, id, kind: itKind },
        preview,
        error: preview?.success === false ? (preview.error || '') : '',
      });
    } catch (err) {
      // A refusal is still an answer: show it in the dialog the user
      // asked for rather than as a toast they have to correlate.
      setPending({
        item: { ...it, id, kind: itKind },
        preview: (err && typeof err.raw === 'object') ? err.raw : null,
        error: err?.detail || err?.message || 'Could not verify this package',
      });
    } finally { setBusy(null); }
  };

  // Step two: install exactly the package the dialog described.
  const confirmInstall = async () => {
    if (!pending?.preview?.install_token) return;
    const { item, preview } = pending;
    setInstalling(true);
    try {
      const r = await apiFetch('/api/marketplace/install', {
        method: 'POST',
        body: JSON.stringify({
          kind: item.kind,
          id: item.id,
          install_token: preview.install_token,
        }),
        silent: true,
      });
      const data = await r.json().catch(() => ({}));
      flash(data?.success === false
        ? (data.error || 'Install failed')
        : `Installed ${preview.name || item.name || item.id}`);
      setPending(null);
    } catch (err) {
      flash(err?.detail || err?.message || 'Install failed');
    } finally { setInstalling(false); }
  };

  return (
    <Pane
      title="Browse catalog"
      actions={(
        <>
          <input
            type="search"
            className="v2-input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search…"
            style={{ minWidth: 160 }}
          />
          <Tabs value={kind} onChange={setKind} items={KINDS.map((k) => ({ id: k, label: k }))} />
          <button type="button" className="v2-btn v2-btn--ghost" onClick={fetchList}><RefreshCw size={13} /></button>
        </>
      )}
    >
      {msg && <div className="v2-chip v2-chip--live">{msg}</div>}
      {catalogError && <div className="v2-chip v2-chip--error">{catalogError}</div>}
      {loading && <EmptyState title="Loading…" />}
      {!loading && items.length === 0 && (
        <EmptyState
          title={`Nothing in ${kind} yet`}
          hint={kind === 'app'
            ? 'No apps are published to the remote registry yet. Local starter apps appear under Apps, not Browse.'
            : undefined}
        />
      )}
      <div className="v2-skills-grid">
        {items.map((it) => {
          const id = it.id || it.item_id;
          const perms = it.permission_details || (it.permissions || []).map((p) => ({ id: p, label: p }));
          return (
            <Glass key={it.id || it.name} level={0} radius="md" padding="md" className="v2-skill-card">
              <header className="v2-skill-card-head">
                <h3 className="v2-skill-card-name">
                  {it.name || it.skill_id || it.id}
                  {it.verified && <span className="v2-chip v2-chip--live" style={{ marginLeft: 6 }}>verified</span>}
                </h3>
                <code className="v2-skill-card-id">v{it.version || '0.0.0'}</code>
              </header>
              <p className="v2-p v2-p--muted">{it.description || '—'}</p>
              <div className="v2-skill-card-meta">
                {it.publisher && <span className="v2-chip v2-chip--muted">by {it.publisher}</span>}
                {it.downloads != null && <span className="v2-chip">{it.downloads} installs</span>}
              </div>
              {perms.length > 0 && (
                <div style={S.cardPerms} data-testid="v2-card-permissions">
                  {perms.map((p) => (
                    <span key={p.id} className="v2-chip v2-chip--muted">{p.label || p.id}</span>
                  ))}
                </div>
              )}
              <div className="v2-forge-actions">
                <button
                  type="button"
                  className="v2-btn v2-btn--primary"
                  onClick={() => openPreview(it)}
                  disabled={busy === id}
                >
                  <Download size={12} /> {busy === id ? 'Checking…' : 'Install'}
                </button>
              </div>
            </Glass>
          );
        })}
      </div>
      <InstallConsent
        pending={pending}
        busy={installing}
        onCancel={() => setPending(null)}
        onConfirm={confirmInstall}
      />
    </Pane>
  );
}

function InstalledTab() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);

  const refresh = useCallback(async () => {
    try {
      // AUDIT-r14 finding 05 fix: `/api/marketplace/installed`
      // returns `{skills: [...]}`. The UI was reading `installed`,
      // which never existed, so the tab always rendered "Nothing
      // installed yet" no matter how many skills the user had
      // installed. Read `skills` first; keep `installed`/`items` as
      // back-compat fallbacks.
      const d = await apiJson('/api/marketplace/installed');
      const rows = d.skills || d.installed || d.items || (Array.isArray(d) ? d : []);
      setItems(Array.isArray(rows) ? rows : []);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const uninstall = async (id) => {
    if (!window.confirm(`Uninstall ${id}?`)) return;
    setBusy(id);
    try {
      await apiFetch(`/api/marketplace/uninstall/${encodeURIComponent(id)}`, { method: 'DELETE' });
      refresh();
    } finally { setBusy(null); }
  };

  const update = async (id) => {
    setBusy(id);
    try {
      await apiFetch(`/api/marketplace/update/${encodeURIComponent(id)}`, { method: 'POST' });
      refresh();
    } finally { setBusy(null); }
  };

  return (
    <Pane title={`Installed (${items.length})`} actions={<button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}><RefreshCw size={13} /></button>}>
      {loading && <EmptyState title="Loading…" />}
      {!loading && items.length === 0 && <EmptyState title="Nothing installed yet" hint="Browse the catalog and install anything." />}
      <ul className="v2-mem-list">
        {items.map((it) => {
          const id = it.skill_id || it.id;
          const perms = it.permission_details || (it.permissions || []).map((p) => ({ id: p, label: p }));
          return (
            <li key={id}>
              <Glass level={0} radius="md" padding="md">
                <div className="v2-flow-card-head">
                  <div className="v2-flow-card-title">{it.name || id}</div>
                  <div className="v2-flow-card-status">{it.kind} · v{it.version}</div>
                </div>
                {perms.length > 0 && (
                  <div style={S.cardPerms} data-testid="v2-installed-permissions">
                    {perms.map((p) => (
                      <span key={p.id} className="v2-chip v2-chip--muted">{p.label || p.id}</span>
                    ))}
                  </div>
                )}
                <div className="v2-forge-actions">
                  <button type="button" className="v2-btn" onClick={() => update(id)} disabled={busy === id}>Update</button>
                  <button type="button" className="v2-btn" onClick={() => uninstall(id)} disabled={busy === id}><Trash2 size={12} /> Uninstall</button>
                </div>
              </Glass>
            </li>
          );
        })}
      </ul>
    </Pane>
  );
}
