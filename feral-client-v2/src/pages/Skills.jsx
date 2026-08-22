import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw, Wrench, Store, Hammer, CheckCircle2, AlertTriangle } from 'lucide-react';
import { Link } from 'react-router-dom';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Modal from '../ui/Modal';
import EmptyState from '../ui/EmptyState';
import ErrorState from '../ui/ErrorState';
import { ApiError, apiFetch } from '../lib/api';
import { useResource, toApiError } from '../hooks/useResource';
import { skillIcon, oneLineSummary } from '../lib/skillIcon';

function asList(value, key) {
  if (Array.isArray(value?.[key])) return value[key];
  if (Array.isArray(value)) return value;
  return [];
}

/**
 * Plain-English answer to "what does Hot-reload do?", shown next to the
 * button rather than assumed. The reported defect was not that the word
 * is unusual, it is that the page never said what pressing it changes.
 */
const RELOAD_EXPLAINER = 'Re-reads this skill\'s manifest, and its impl.py if it has one, from disk and swaps it into the running brain. Nothing restarts, no other skill is touched, and the skill\'s stored settings and API keys are left alone. Use it after editing a skill\'s files by hand. A skill defined in the brain\'s own Python has no file to re-read, and says so instead of pretending.';

/**
 * Skills: every loaded skill as one card of a fixed size.
 *
 * Card contents used to be the whole manifest: the full `description`
 * (written for the LLM, up to ~2,000 characters for `macos_ax`) plus four
 * trigger phrases, inline, for all 42 skills. Measured against a live
 * brain before this change, cards in one row ran 527px to 1055px tall and
 * the page was a wall of text. The card now carries the icon, the name,
 * the id and one line; everything else moved into a detail sheet that
 * opens on click.
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
  // One outcome record, `{ id, ok, error }`, rendered where the button
  // that produced it lives. See the comment on `reload` below.
  const [outcome, setOutcome] = useState(null);
  const [filter, setFilter] = useState('');
  const [openId, setOpenId] = useState(null);

  const skills = skillRows || [];
  const pending = pendingRows || [];

  const refresh = useCallback(() => {
    setOutcome(null);
    return Promise.all([refreshSkills(), refreshPending()]);
  }, [refreshSkills, refreshPending]);

  // A hot-reload used to look identical whether it worked or not: no
  // catch, no confirmation. Then both outcomes were reported, but only in
  // one place: a banner at the top of the pane, above a grid of 42 cards.
  //
  // That is the "Hot-reload does nothing" the user reported, and it is
  // measurable rather than a matter of taste. Driving the real page on a
  // live brain, clicking Hot-reload on `weather_current` put its outcome
  // banner at y = -71px, and on `spotify_music` at y = -3365px, both
  // entirely off-screen above the viewport. Success and failure were
  // equally invisible; the button did report, into a part of the page the
  // user was not looking at and had no reason to scroll back to.
  //
  // So the outcome is rendered at the click site now. The only button is
  // inside the detail sheet, and the sheet shows what happened without
  // closing.
  //
  // `apiFetch` throws on a non-2xx status, and on a 2xx whose body carries
  // an `error` key. A brain that predates the status fix answers a reload
  // that did nothing with HTTP 200 and `{"ok": false, "skill_id": "..."}`
  // with no `error` key, so neither trigger fires and the response body
  // was never read at all. Read the body, and treat `ok: false` as the
  // failure it is regardless of the status.
  const reload = async (id) => {
    setReloading(id);
    setOutcome(null);
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
      setOutcome({ id, ok: true, error: null });
    } catch (err) {
      setOutcome({ id, ok: false, error: toApiError(err, path) });
    } finally {
      setReloading(null);
    }
  };

  const visible = useMemo(() => skills.filter((s) => {
    if (!filter) return true;
    const q = filter.toLowerCase();
    return (
      (s.skill_id || '').toLowerCase().includes(q) ||
      (s.name || '').toLowerCase().includes(q) ||
      (s.description || '').toLowerCase().includes(q) ||
      (Array.isArray(s.categories) ? s.categories : []).some((c) => String(c).toLowerCase().includes(q))
    );
  }), [skills, filter]);

  const open = skills.find((s) => (s.skill_id || s.id) === openId) || null;
  const openOutcome = outcome && outcome.id === openId ? outcome : null;

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
        {/*
          Two things nothing on this page used to say: where more skills
          come from, and how you make one. Both destinations already
          existed and are already in the nav (shell/navigation.js), they
          were simply not reachable from the page about skills. Neither
          link is invented: /marketplace is App.jsx's route for
          pages/Marketplace.jsx over `/api/marketplace/*`, and /forge is
          the route for pages/Forge.jsx over `/api/tool-genesis/*`.
        */}
        <div className="v2-skills-ways">
          <Link to="/marketplace" className="v2-btn v2-btn--primary">
            <Store size={13} aria-hidden="true" /> Install a skill
          </Link>
          <Link to="/forge" className="v2-btn">
            <Hammer size={13} aria-hidden="true" /> Create a skill
          </Link>
          <span className="v2-p v2-p--muted v2-skills-ways-note">
            Install pulls a packaged skill from the registry. Create asks the brain
            to draft one from a description, and nothing runs until you approve it.
          </span>
        </div>

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

        {loading && !skillRows && <EmptyState title="Loading skills…" />}
        {skillsError && !skillRows && (
          <ErrorState
            error={skillsError}
            what="the skill list"
            onRetry={refresh}
          />
        )}
        {!loading && !skillsError && skillRows && skills.length === 0 && <EmptyState title="No skills loaded" hint="Check the Brain boot log." />}
        {skills.length > 0 && visible.length === 0 && (
          <EmptyState title={`No skill matches "${filter}"`} hint="Filter matches the name, the id, the description and the categories." />
        )}

        <div className="v2-skills-grid">
          {visible.map((s) => {
            const id = s.skill_id || s.id;
            const Icon = skillIcon(s);
            const count = typeof s.endpoint_count === 'number'
              ? s.endpoint_count
              : (Array.isArray(s.endpoints) ? s.endpoints.length : null);
            const category = Array.isArray(s.categories) && s.categories.length > 0 ? s.categories[0] : null;
            return (
              <button
                key={id}
                type="button"
                className="v2-skill-card"
                data-testid="v2-skill-card"
                data-skill-id={id}
                onClick={() => setOpenId(id)}
                aria-label={`Open details for ${s.name || id}`}
              >
                <span className="v2-skill-card-icon" aria-hidden="true"><Icon size={18} /></span>
                <span className="v2-skill-card-name">{s.name || id}</span>
                <code className="v2-skill-card-id">{id}</code>
                {/*
                  Two elements, not one, and the reason is mechanical: a
                  direct child of a grid container is blockified, so
                  `display: -webkit-box` on it computes to `flow-root`
                  and `-webkit-line-clamp` stops applying. Measured in
                  Chrome, that rendered a three-line summary with a stray
                  ellipsis. The outer span is the grid item, the inner
                  one carries the clamp.
                */}
                <span className="v2-skill-card-summary">
                  <span className="v2-skill-card-summary-text">{oneLineSummary(s.description)}</span>
                </span>
                <span className="v2-skill-card-meta">
                  {count !== null && <span className="v2-chip">{count} endpoint{count === 1 ? '' : 's'}</span>}
                  {category && <span className="v2-chip v2-chip--muted">{category.replace(/_/g, ' ')}</span>}
                </span>
              </button>
            );
          })}
        </div>
      </Pane>

      <Modal
        open={!!open}
        onClose={() => setOpenId(null)}
        title={open ? (open.name || openId) : ''}
        size="lg"
      >
        {open && <SkillDetail
          skill={open}
          reloading={reloading === openId}
          outcome={openOutcome}
          onReload={() => reload(openId)}
        />}
      </Modal>
    </div>
  );
}

/**
 * The detail sheet. Everything the card used to dump inline lives here,
 * plus the endpoint list the card never had: `GET /skills` sent
 * `endpoints` as an integer while both readers guarded with
 * `Array.isArray(...)`, so the endpoint chip was dead code and no client
 * could show what a skill can actually do. The route sends the list now.
 */
export function SkillDetail({ skill, reloading, outcome, onReload }) {
  const id = skill.skill_id || skill.id;
  // The sheet is taller than the dialog for a skill with ten endpoints,
  // so the outcome can land below the fold of a scrolled panel. Being
  // one scroll away is the same failure as being 3,365px away, just
  // smaller, so the outcome brings itself into view.
  const outcomeRef = useRef(null);
  useEffect(() => {
    if (!outcome) return;
    const node = outcomeRef.current;
    if (node && typeof node.scrollIntoView === 'function') {
      node.scrollIntoView({ block: 'nearest' });
    }
  }, [outcome]);
  const endpoints = Array.isArray(skill.endpoints) ? skill.endpoints : [];
  const phrases = Array.isArray(skill.trigger_phrases) ? skill.trigger_phrases : [];
  const categories = Array.isArray(skill.categories) ? skill.categories : [];

  return (
    <div className="v2-skill-detail">
      <div className="v2-skill-detail-meta">
        <code className="v2-skill-card-id">{id}</code>
        {skill.version && <span className="v2-chip">v{skill.version}</span>}
        {categories.map((c) => (
          <span key={c} className="v2-chip v2-chip--muted">{String(c).replace(/_/g, ' ')}</span>
        ))}
      </div>

      {skill.description && (
        <section className="v2-skill-detail-section">
          <h3 className="v2-skill-detail-h">What it does</h3>
          <p className="v2-p v2-p--muted">{skill.description}</p>
        </section>
      )}

      {phrases.length > 0 && (
        <section className="v2-skill-detail-section">
          <h3 className="v2-skill-detail-h">Say one of these ({phrases.length})</h3>
          <div className="v2-skill-card-phrases">
            {phrases.map((p, i) => (
              <span key={i} className="v2-chip v2-chip--muted">&quot;{p}&quot;</span>
            ))}
          </div>
        </section>
      )}

      <section className="v2-skill-detail-section">
        <h3 className="v2-skill-detail-h">Endpoints ({endpoints.length})</h3>
        {endpoints.length === 0 ? (
          <p className="v2-p v2-p--muted">
            This brain reported no endpoint detail for {id}. A brain older than this
            page sends only the endpoint count, not the list.
          </p>
        ) : (
          <ul className="v2-skill-endpoints">
            {endpoints.map((e, i) => (
              <li key={e.id || i} className="v2-skill-endpoint">
                <div className="v2-skill-endpoint-head">
                  <code className="v2-skill-endpoint-id">{id}__{e.id}</code>
                  {e.read_only && <span className="v2-chip v2-chip--muted">read only</span>}
                </div>
                {e.description && <p className="v2-p v2-p--muted">{e.description}</p>}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="v2-skill-detail-section v2-skill-reload">
        <h3 className="v2-skill-detail-h">Hot-reload</h3>
        <p className="v2-p v2-p--muted">{RELOAD_EXPLAINER}</p>
        <button
          type="button"
          className="v2-btn"
          onClick={onReload}
          disabled={reloading}
          data-testid="v2-skill-reload"
        >
          <RefreshCw size={12} /> {reloading ? 'Reloading…' : `Re-read ${id} from disk`}
        </button>

        {/*
          The outcome renders here, directly under the button that caused
          it, because the page-level banner it used to render into sat up
          to 3,365px above the click.
        */}
        {outcome && outcome.ok && (
          <div ref={outcomeRef} className="v2-skill-reload-result v2-skill-reload-result--ok" role="status" data-testid="v2-skill-reload-ok">
            <CheckCircle2 size={14} aria-hidden="true" />
            <span>
              Re-read {outcome.id} from disk. The brain is now running whatever
              its manifest and impl.py say as of a moment ago.
            </span>
          </div>
        )}
        {outcome && !outcome.ok && (
          <div ref={outcomeRef} className="v2-skill-reload-result v2-skill-reload-result--bad" data-testid="v2-skill-reload-error">
            <AlertTriangle size={14} aria-hidden="true" />
            <div>
              <div className="v2-skill-reload-result-title">
                {outcome.id} was not reloaded. Whatever code the brain had loaded
                before is still what is running.
              </div>
              <ErrorState
                error={outcome.error}
                what={`the hot-reload of ${outcome.id}`}
                hint={outcome.error?.code === 'no_source'
                  ? 'This skill is defined in the brain\'s own Python, not in a file under ~/.feral/skills, so there is nothing on disk to re-read. Only a brain restart picks up a change to it.'
                  : undefined}
                compact
                onRetry={onReload}
              />
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
