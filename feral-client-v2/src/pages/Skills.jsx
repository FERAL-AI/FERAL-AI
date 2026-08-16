import React, { useCallback, useState } from 'react';
import { RefreshCw, Play, Wrench } from 'lucide-react';
import { Link } from 'react-router-dom';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import EmptyState from '../ui/EmptyState';
import ErrorState from '../ui/ErrorState';
import { ApiError, apiFetch } from '../lib/api';
import { useResource, toApiError } from '../hooks/useResource';

function asList(value, key) {
  if (Array.isArray(value?.[key])) return value[key];
  if (Array.isArray(value)) return value;
  return [];
}

/**
 * Skills — shows every loaded skill. Reload button per skill.
 * Pending drafts banner links to Forge.
 */
export default function Skills() {
  // Was: one `Promise.allSettled` with no else branch and no error
  // state anywhere in the file, so a failed `/skills` left the list at
  // `[]` and the page told the user "No skills loaded / Check the Brain
  // boot log", sending them to debug a boot that was fine.
  const {
    data: skillRows, error: skillsError, loading, refresh: refreshSkills,
  } = useResource('/skills', { select: (d) => asList(d, 'skills') });
  // The drafts banner is additive: if only this call fails we simply do
  // not claim there are drafts, and we do not shout about it.
  const { data: pendingRows, refresh: refreshPending } = useResource(
    '/api/skills/pending', { select: (d) => asList(d, 'pending'), silent: true },
  );
  const [reloading, setReloading] = useState(null);
  const [reloadError, setReloadError] = useState(null);
  const [reloaded, setReloaded] = useState(null);
  const [filter, setFilter] = useState('');

  const skills = skillRows || [];
  const pending = pendingRows || [];

  const refresh = useCallback(() => {
    setReloadError(null);
    setReloaded(null);
    return Promise.all([refreshSkills(), refreshPending()]);
  }, [refreshSkills, refreshPending]);

  // A hot-reload used to look identical whether it worked or not: no
  // catch, no confirmation. Now both outcomes say so.
  //
  // The catch alone was not enough, and this is the part the previous
  // pass missed. `apiFetch` throws on a non-2xx status, and on a 2xx
  // whose body carries an `error` key. A brain that predates the status
  // fix answers a reload that did nothing with HTTP 200 and
  // `{"ok": false, "skill_id": "..."}` with no `error` key, so neither
  // trigger fires. `await apiFetch(...)` resolved, nothing threw, and
  // `setReloaded(id)` painted "Hot-reloaded <id>" over a skill whose
  // code had not moved. The response body was never read at all, which
  // is the same "report success without checking the result" shape the
  // rest of this file was rewritten to remove. So: read the body, and
  // treat `ok: false` as the failure it is regardless of the status.
  const reload = async (id) => {
    setReloading(id);
    setReloadError(null);
    setReloaded(null);
    const path = `/api/skills/reload?skill_id=${encodeURIComponent(id)}`;
    try {
      const response = await apiFetch(path, { method: 'POST' });
      const body = await response.json().catch(() => null);
      if (body && body.ok === false) {
        throw new ApiError({
          status: response.status,
          code: body.code || '',
          detail: body.error || `the brain did not reload ${id}, and did not say why`,
          raw: body,
          path,
        });
      }
      await refreshSkills();
      setReloaded(id);
    } catch (err) {
      setReloadError({ id, error: toApiError(err, path) });
    } finally {
      setReloading(null);
    }
  };

  const visible = skills.filter((s) => {
    if (!filter) return true;
    const q = filter.toLowerCase();
    return (
      (s.skill_id || '').toLowerCase().includes(q) ||
      (s.name || '').toLowerCase().includes(q) ||
      (s.description || '').toLowerCase().includes(q)
    );
  });

  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title={skillRows ? `Skills (${skills.length})` : 'Skills'}
        actions={(
          <>
            <input
              type="search"
              className="v2-input"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter…"
              style={{ minWidth: 160 }}
            />
            <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh} aria-label="Refresh"><RefreshCw size={13} /></button>
          </>
        )}
      >
        {pending.length > 0 && (
          <Glass level={1} radius="md" padding="md" className="v2-dash-alert">
            <Wrench size={14} aria-hidden="true" />
            <div>
              <div className="v2-dash-alert-title">{pending.length} draft skill{pending.length > 1 ? 's' : ''} pending approval</div>
              <div className="v2-dash-alert-msg">Tool Genesis proposed new capabilities from recent requests.</div>
            </div>
            <Link to="/forge" className="v2-btn v2-btn--primary">Open Forge</Link>
          </Glass>
        )}

        {reloadError && (
          <ErrorState
            error={reloadError.error}
            what={`the hot-reload of ${reloadError.id}`}
            hint="The skill was not reloaded. Whatever code the brain had loaded before is still what is running."
            compact
            onRetry={() => reload(reloadError.id)}
          />
        )}
        {reloaded && !reloadError && (
          <div className="v2-chip v2-chip--live" role="status">
            Hot-reloaded {reloaded}
          </div>
        )}

        {loading && !skillRows && <EmptyState title="Loading skills…" />}
        {skillsError && !skillRows && (
          <ErrorState
            error={skillsError}
            what="the skill list"
            onRetry={refresh}
          />
        )}
        {!loading && !skillsError && skillRows && skills.length === 0 && <EmptyState title="No skills loaded" hint="Check the Brain boot log." />}

        <div className="v2-skills-grid">
          {visible.map((s) => {
            const id = s.skill_id || s.id;
            return (
              <Glass key={id} level={0} radius="md" padding="md" className="v2-skill-card">
                <header className="v2-skill-card-head">
                  <h3 className="v2-skill-card-name">{s.name || id}</h3>
                  <code className="v2-skill-card-id">{id}</code>
                </header>
                {s.description && <p className="v2-p v2-p--muted">{s.description}</p>}
                <div className="v2-skill-card-meta">
                  {s.version && <span className="v2-chip">v{s.version}</span>}
                  {s.approval_mode && <span className={`v2-chip v2-chip--${s.approval_mode}`}>{s.approval_mode}</span>}
                  {Array.isArray(s.endpoints) && s.endpoints.length > 0 && (
                    <span className="v2-chip">{s.endpoints.length} endpoints</span>
                  )}
                </div>
                {Array.isArray(s.trigger_phrases) && s.trigger_phrases.length > 0 && (
                  <div className="v2-skill-card-phrases">
                    {s.trigger_phrases.slice(0, 4).map((p, i) => (
                      <span key={i} className="v2-chip v2-chip--muted">"{p}"</span>
                    ))}
                  </div>
                )}
                <div className="v2-forge-actions">
                  <button
                    type="button"
                    className="v2-btn"
                    onClick={() => reload(id)}
                    disabled={reloading === id}
                  >
                    <RefreshCw size={12} /> {reloading === id ? 'Reloading…' : 'Hot-reload'}
                  </button>
                </div>
              </Glass>
            );
          })}
        </div>
      </Pane>
    </div>
  );
}
