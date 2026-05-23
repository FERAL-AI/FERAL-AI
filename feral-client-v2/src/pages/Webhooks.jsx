import React, { useCallback, useEffect, useState } from 'react';
import { Copy, Plus, Trash2, RefreshCw, Send, AlertCircle } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Modal from '../ui/Modal';
import EmptyState from '../ui/EmptyState';
import { apiJson, apiFetch } from '../lib/api';
import { API_BASE } from '../lib/config';

/**
 * Webhooks — custom inbound webhooks.
 *
 * AUDIT-r14 finding 06 closes:
 *   - Persistence: Lane 10 shipped `/api/custom-webhooks/*` backed by a
 *     real SQLite store (integrations/webhook_store.py); the v1 page
 *     used to hit an in-memory `_webhooks` dict that emptied on
 *     restart. The UI now always uses the persistent route.
 *   - Copy URL: `{API_BASE}/api/custom-webhooks/{id}/receive` matches
 *     the canonical receive endpoint. The old `/api/webhooks/{app_id}`
 *     URL was actually the integration ingress and never accepted
 *     custom webhooks.
 *   - Create payload: backend reads `{name, secret, action,
 *     action_params}`. UI used to send `{name, app_id}` which the
 *     brain silently ignored.
 *   - Signature header: docs are honest about which headers the
 *     receiver actually checks (x-hub-signature-256 / stripe-signature
 *     / x-signature).
 */
export default function Webhooks() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState('');
  const [showNew, setShowNew] = useState(false);
  const [created, setCreated] = useState(null);

  const refresh = useCallback(async () => {
    setErr('');
    try {
      const d = await apiJson('/api/custom-webhooks/list');
      setItems(d.webhooks || []);
    } catch (e) {
      setErr(e?.message || 'failed to list webhooks');
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const remove = async (id) => {
    if (!window.confirm(`Delete webhook ${id}? Any external service POSTing here will start receiving 404s.`)) return;
    try {
      await apiFetch(`/api/custom-webhooks/${encodeURIComponent(id)}`, { method: 'DELETE' });
      await refresh();
    } catch (e) {
      setErr(e?.message || 'delete failed');
    }
  };

  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title={`Webhooks (${items.length})`}
        actions={(
          <>
            <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh}><RefreshCw size={13} /></button>
            <button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowNew(true)}>
              <Plus size={13} /> New webhook
            </button>
          </>
        )}
      >
        <p className="v2-p v2-p--muted">
          External services POST to these URLs to trigger FERAL. Each webhook has an optional secret —
          when set, requests must include a matching HMAC signature in one of:
          <code>x-hub-signature-256</code>, <code>stripe-signature</code>, or <code>x-signature</code>.
          Unsigned requests are rejected with 401; bad signatures with 403.
        </p>
        {err && <div className="v2-chip v2-chip--error" role="alert">{err}</div>}
        {loading && <EmptyState title="Loading…" />}
        {!loading && items.length === 0 && (
          <EmptyState title="No webhooks yet" hint="Create one and point Zapier, Stripe, GitHub, anywhere at the receive URL." />
        )}
        <ul className="v2-mem-list" data-testid="webhook-list">
          {items.map((w) => {
            const id = w.id || w.webhook_id;
            // Backend exposes the canonical receive URL on the record;
            // build the absolute version for the copy button.
            const path = w.url || `/api/custom-webhooks/${encodeURIComponent(id)}/receive`;
            const url = `${API_BASE}${path}`;
            return (
              <li key={id}>
                <Glass level={0} radius="md" padding="md">
                  <div className="v2-flow-card-head">
                    <div className="v2-flow-card-title">{w.name || id}</div>
                    {w.action && <span className="v2-chip">{w.action}</span>}
                    {w.secret && <span className="v2-chip v2-chip--live" title="HMAC signature required on inbound requests">secured</span>}
                    {!w.secret && <span className="v2-chip v2-chip--warn" title="Unsigned requests accepted — anyone with the URL can fire this">unsecured</span>}
                    <button type="button" className="v2-btn v2-btn--ghost" onClick={() => navigator.clipboard.writeText(url)} title="Copy URL"><Copy size={12} /></button>
                    <button type="button" className="v2-btn v2-btn--ghost" onClick={() => remove(id)} title="Delete" data-testid={`webhook-delete-${id}`}><Trash2 size={12} /></button>
                  </div>
                  <code className="v2-code-inline" style={{ display: 'block', marginTop: 6 }}>{url}</code>
                  {w.trigger_count != null && (
                    <div className="v2-mem-meta" style={{ marginTop: 4 }}>
                      <Send size={10} /> {w.trigger_count} trigger{w.trigger_count === 1 ? '' : 's'}
                      {w.last_triggered && <span> · last {new Date(w.last_triggered * 1000).toLocaleString()}</span>}
                    </div>
                  )}
                </Glass>
              </li>
            );
          })}
        </ul>
      </Pane>

      {showNew && (
        <NewWebhookModal
          onClose={() => setShowNew(false)}
          onCreated={(rec) => { setShowNew(false); setCreated(rec); refresh(); }}
        />
      )}
      {created && (
        <CreatedReceiptModal record={created} onClose={() => setCreated(null)} />
      )}
    </div>
  );
}

function NewWebhookModal({ onClose, onCreated }) {
  const [name, setName] = useState('');
  const [secret, setSecret] = useState('');
  const [action, setAction] = useState('chat');
  const [prefix, setPrefix] = useState('Webhook received: ');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const body = {
        name: name.trim() || 'Untitled Webhook',
        secret: secret.trim(),
        action,
        action_params: action === 'chat' ? { prefix } : {},
      };
      const result = await apiJson('/api/custom-webhooks/create', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      if (result?.success === false) {
        setError(result.error || 'Create failed');
        return;
      }
      onCreated(result.webhook || result);
    } catch (err) {
      setError(err?.message || 'create failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title="New webhook"
      actions={(
        <>
          <button type="button" className="v2-btn" onClick={onClose}>Cancel</button>
          <button type="button" className="v2-btn v2-btn--primary" onClick={submit} disabled={busy || !name.trim()} data-testid="webhook-create">
            {busy ? 'Creating…' : 'Create'}
          </button>
        </>
      )}
    >
      <div className="v2-setting-stack">
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Name</div></div>
          <div className="v2-setting-control">
            <input className="v2-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="zapier-weather" data-testid="webhook-name" />
          </div>
        </label>
        <label className="v2-setting-row">
          <div className="v2-setting-label">
            <div>Secret (optional)</div>
            <div className="v2-setting-hint">If set, inbound requests must include a matching HMAC-SHA256 signature.</div>
          </div>
          <div className="v2-setting-control">
            <input type="password" className="v2-input" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="off" data-testid="webhook-secret" />
          </div>
        </label>
        <label className="v2-setting-row">
          <div className="v2-setting-label"><div>Action</div></div>
          <div className="v2-setting-control">
            <select className="v2-select" value={action} onChange={(e) => setAction(e.target.value)} data-testid="webhook-action">
              <option value="chat">Send the payload to chat</option>
              <option value="event">Emit a brain event only</option>
            </select>
          </div>
        </label>
        {action === 'chat' && (
          <label className="v2-setting-row">
            <div className="v2-setting-label"><div>Chat prefix</div></div>
            <div className="v2-setting-control">
              <input className="v2-input" value={prefix} onChange={(e) => setPrefix(e.target.value)} />
            </div>
          </label>
        )}
      </div>
      {error && <div className="v2-chip v2-chip--error" role="alert" style={{ marginTop: 8 }}><AlertCircle size={12} /> {error}</div>}
    </Modal>
  );
}

function CreatedReceiptModal({ record, onClose }) {
  const id = record.id || record.webhook_id;
  const path = record.url || `/api/custom-webhooks/${encodeURIComponent(id)}/receive`;
  const url = `${API_BASE}${path}`;
  return (
    <Modal
      open
      onClose={onClose}
      title="Webhook created"
      actions={<button type="button" className="v2-btn v2-btn--primary" onClick={onClose}>Done</button>}
    >
      <div className="v2-setting-stack">
        <p className="v2-p">Point your external service at:</p>
        <Glass level={0} radius="md" padding="sm">
          <code className="v2-code-inline" style={{ display: 'block', wordBreak: 'break-all' }}>{url}</code>
        </Glass>
        <button type="button" className="v2-btn" onClick={() => navigator.clipboard.writeText(url)}>
          <Copy size={12} /> Copy URL
        </button>
        {record.secret && (
          <>
            <p className="v2-p v2-p--muted v2-p--tiny">
              Include the HMAC-SHA256 of the request body in one of the supported headers
              (<code>x-hub-signature-256</code> / <code>stripe-signature</code> / <code>x-signature</code>) using:
            </p>
            <Glass level={0} radius="md" padding="sm">
              <code className="v2-code-inline" style={{ display: 'block', wordBreak: 'break-all' }}>
                sha256={'<'}HMAC_SHA256(secret, body){'>'}
              </code>
            </Glass>
          </>
        )}
      </div>
    </Modal>
  );
}
