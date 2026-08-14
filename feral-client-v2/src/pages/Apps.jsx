/**
 * Apps — installed third-party GenUI app launcher.
 *
 * Each installed app is shown as a branded tile (brand name + logo if
 * present + short description). Tapping a tile navigates to
 * /apps/<app_id> which mounts <AppSurface /> and opens the entry
 * surface. Uninstall fires DELETE /api/apps/<app_id>.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Trash2, RefreshCw, Store, Rocket } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import EmptyState from '../ui/EmptyState';
import { apiJson, apiFetch } from '../lib/api';

export default function Apps() {
  const [apps, setApps] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(null);
  const navigate = useNavigate();

  const refresh = useCallback(async () => {
    try {
      const data = await apiJson('/api/apps');
      setApps(data?.apps || []);
      setError(null);
    } catch (e) {
      setError(e?.message || 'failed to fetch apps');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const uninstall = async (app_id) => {
    if (!window.confirm(`Uninstall ${app_id}?`)) return;
    setBusy(app_id);
    try {
      const r = await apiFetch(`/api/apps/${encodeURIComponent(app_id)}`, {
        method: 'DELETE',
      });
      if (r.ok) refresh();
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title={`Apps${apps.length ? ` · ${apps.length}` : ''}`}
        actions={(
          <>
            <Link to="/apps/publish" className="v2-btn v2-btn--ghost" title="Publish a GenUI app">
              <Rocket size={13} /> Publish
            </Link>
            <Link to="/marketplace" className="v2-btn v2-btn--primary">
              <Store size={13} /> Browse marketplace
            </Link>
            <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh} aria-label="Refresh">
              <RefreshCw size={13} />
            </button>
          </>
        )}
      >
        <p className="v2-p v2-p--muted">
          Third-party apps installed on this brain. Every action they fire is validated against the
          publisher's declared action contract before the agent runs anything.
        </p>
        {error && <div className="v2-chip v2-chip--error">{error}</div>}
        {loading && <EmptyState title="Loading…" />}
        {!loading && apps.length === 0 && (
          <EmptyState
            title="No apps installed yet"
            hint="Head to the marketplace and install one, or use `feral app install ./` on any folder with a manifest."
            action={<Link to="/marketplace" className="v2-btn v2-btn--primary">Open marketplace</Link>}
          />
        )}
      </Pane>

      {apps.length > 0 && (
        <div className="v2-skills-grid" data-testid="v2-apps-grid">
          {apps.map((app) => {
            const brand = app.brand || {};
            // An app's own primary_color is third-party brand data and is used
            // verbatim. The fallback, when an app declares no brand, is FERAL
            // chrome and so must be the FERAL accent, not a stray violet.
            const accent = brand.primary_color || 'var(--v2-accent)';
            return (
              <Glass key={app.app_id} level={1} radius="md" padding="md">
                <header
                  style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}
                >
                  <div
                    aria-hidden="true"
                    style={{
                      width: 28, height: 28, borderRadius: 8, flexShrink: 0,
                      background: accent,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontSize: 13, fontWeight: 700, color: 'var(--v2-on-accent)',
                    }}
                  >
                    {(brand.name || app.app_id).charAt(0).toUpperCase()}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600 }}>{brand.name || app.app_id}</div>
                    <div className="v2-p v2-p--muted v2-p--tiny">v{app.version} · {app.author || 'unknown'}</div>
                  </div>
                </header>
                {app.description ? (
                  <p className="v2-p v2-p--muted" style={{ marginBottom: 8 }}>{app.description}</p>
                ) : null}
                <div className="v2-forge-actions" style={{ display: 'flex', gap: 6 }}>
                  <button
                    type="button"
                    className="v2-btn v2-btn--primary"
                    onClick={() => navigate(`/apps/${encodeURIComponent(app.app_id)}`)}
                    data-testid={`v2-apps-open-${app.app_id}`}
                  >
                    Open
                  </button>
                  <button
                    type="button"
                    className="v2-btn"
                    disabled={busy === app.app_id}
                    onClick={() => uninstall(app.app_id)}
                  >
                    <Trash2 size={12} /> Uninstall
                  </button>
                </div>
              </Glass>
            );
          })}
        </div>
      )}
    </div>
  );
}
