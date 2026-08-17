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
import { Trash2, RefreshCw, Store, Rocket, AlertTriangle } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import EmptyState from '../ui/EmptyState';
import { apiJson, apiFetch } from '../lib/api';

/**
 * Colours come from styles/tokens.css only, inline, per the Oversight.jsx
 * precedent. This page must not add rules to a shared stylesheet.
 */
const S = {
  missingBox: {
    marginBottom: 8,
    padding: '8px 10px',
    borderRadius: 'var(--v2-radius-sm)',
    border: '1px solid var(--v2-state-warn-soft)',
    background: 'var(--v2-state-warn-soft)',
  },
  missingHead: {
    display: 'flex',
    gap: 8,
    alignItems: 'flex-start',
    color: 'var(--v2-state-warn)',
    fontSize: 'var(--v2-size-sm)',
    fontWeight: 600,
    lineHeight: 1.35,
  },
  missingBody: {
    color: 'var(--v2-text-secondary)',
    fontSize: 'var(--v2-size-xs)',
    lineHeight: 1.45,
    marginTop: 4,
  },
  missingCmd: {
    display: 'inline-block',
    marginTop: 4,
    padding: '3px 6px',
    borderRadius: 'var(--v2-radius-xs)',
    border: '1px solid var(--v2-hairline)',
    background: 'var(--v2-well)',
    fontFamily: 'var(--v2-font-mono)',
    fontSize: 'var(--v2-size-xs)',
    color: 'var(--v2-text-primary)',
    wordBreak: 'break-all',
  },
};

/**
 * An app may be installed without one of the skills it declared, when
 * FERAL could not verify that skill. Refusing the install outright would
 * leave the user with nothing and no next step, so the app installs and
 * the shortfall is carried here instead: what is missing, what it costs,
 * and how to fix it. The brain recomputes this on every listing, so it
 * disappears by itself once the skill is installed.
 */
function MissingDependencies({ app }) {
  const missing = app.missing_skill_dependencies || [];
  if (!missing.length) return null;
  return (
    <div style={S.missingBox} data-testid={`v2-apps-missing-${app.app_id}`}>
      {missing.map((m) => (
        <div key={m.skill_id}>
          <div style={S.missingHead}>
            <AlertTriangle size={14} aria-hidden="true" style={{ flex: '0 0 auto', marginTop: 1 }} />
            <span>Missing skill: {m.skill_id}</span>
          </div>
          {m.impact?.length > 0 && (
            <div style={S.missingBody}>
              {m.impact.map((a) => a.description || a.action_id).join(', ')} will not work.
            </div>
          )}
          {m.remediation?.action && <div style={S.missingBody}>{m.remediation.action}</div>}
          {m.remediation?.command && <code style={S.missingCmd}>{m.remediation.command}</code>}
        </div>
      ))}
    </div>
  );
}

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
                <MissingDependencies app={app} />
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
