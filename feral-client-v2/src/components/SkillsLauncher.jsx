import React, { useEffect, useMemo, useState } from 'react';
import { Search, X, Pin, PinOff, ChevronDown, ChevronUp, RefreshCw } from 'lucide-react';
import Glass from '../ui/Glass';
import { ApiError, apiFetch } from '../lib/api';

/**
 * SkillsLauncher — full-surface popup showing every loaded skill.
 *
 *   - Search across name / skill_id / description
 *   - Click a row to expand description, trigger phrases, endpoint count
 *   - Pin / unpin — writes to ``localStorage.feral_pinned_skills`` so the
 *     Home page's compact strip is user-editable
 *   - Hot-reload a skill via POST /api/skills/reload
 */

/**
 * Inline reload outcome, styled from tokens.css only. `src/styles/` is
 * owned elsewhere and this component has no stylesheet of its own, so the
 * two states are inline styles reading `--v2-*`, the same way
 * `pages/Oversight.jsx` renders its local error box. Nothing here
 * hard-codes a colour.
 */
const RELOAD_NOTE_STYLE = {
  display: 'flex',
  alignItems: 'flex-start',
  gap: 6,
  marginTop: 6,
  padding: '6px 10px',
  borderRadius: 'var(--v2-radius-sm)',
  fontSize: 'var(--v2-size-sm)',
  lineHeight: 1.45,
};

const RELOAD_FAILED_STYLE = {
  ...RELOAD_NOTE_STYLE,
  border: '1px solid var(--v2-state-error-soft)',
  background: 'var(--v2-state-error-soft)',
  color: 'var(--v2-state-error)',
};

const RELOAD_OK_STYLE = {
  ...RELOAD_NOTE_STYLE,
  border: '1px solid var(--v2-state-live-soft)',
  background: 'var(--v2-state-live-soft)',
  color: 'var(--v2-state-live)',
};

const RETRY_STYLE = {
  marginLeft: 'auto',
  background: 'none',
  border: 0,
  padding: 0,
  color: 'inherit',
  font: 'inherit',
  textDecoration: 'underline',
  cursor: 'pointer',
};

export const PIN_STORAGE_KEY = 'feral_pinned_skills';
export const DEFAULT_PINNED = [
  'coding_tools', 'web_search', 'calendar_google', 'messaging_channels',
  'smart_home_hue', 'notes_memory', 'weather_current', 'self_introspection',
];
export const MAX_PINNED = 8;

export function readPinned() {
  if (typeof localStorage === 'undefined') return DEFAULT_PINNED;
  try {
    const raw = localStorage.getItem(PIN_STORAGE_KEY);
    if (!raw) return DEFAULT_PINNED;
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed) && parsed.every((s) => typeof s === 'string')) {
      return parsed.slice(0, MAX_PINNED);
    }
  } catch { /* fall through */ }
  return DEFAULT_PINNED;
}

export function writePinned(list) {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify(list.slice(0, MAX_PINNED)));
    window.dispatchEvent(new CustomEvent('feral_pinned_change'));
  } catch { /* silent */ }
}

export default function SkillsLauncher({ open, onClose, skills = [] }) {
  const [query, setQuery] = useState('');
  const [expanded, setExpanded] = useState(null);
  const [pinned, setPinned] = useState(readPinned());
  const [busy, setBusy] = useState(null);
  // `{ id, ok, detail }` for the last reload attempt, or null. One at a
  // time: only one reload can be in flight, and a note about a skill the
  // user has moved on from is noise.
  const [note, setNote] = useState(null);

  useEffect(() => {
    if (!open) { setQuery(''); setExpanded(null); setNote(null); return undefined; }
    const onKey = (e) => { if (e.key === 'Escape') onClose?.(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const list = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return skills;
    return skills.filter((s) => {
      const id = (s.skill_id || s.id || '').toLowerCase();
      const name = (s.name || '').toLowerCase();
      const desc = (s.description || '').toLowerCase();
      return id.includes(q) || name.includes(q) || desc.includes(q);
    });
  }, [skills, query]);

  const togglePin = (id) => {
    setPinned((prev) => {
      const next = prev.includes(id)
        ? prev.filter((x) => x !== id)
        : [id, ...prev.filter((x) => x !== id)].slice(0, MAX_PINNED);
      writePinned(next);
      return next;
    });
  };

  // This button used to be `await apiFetch(...)` inside a bare
  // `try/finally`: no body read, no catch, and no confirmation of any
  // kind. Every outcome looked the same, a spinner that stopped. Since
  // `apiFetch` raises on a non-2xx it now at least raises a global toast
  // against a current brain, but two cases still went nowhere: an
  // unhandled rejection on a thrown error, and a brain that predates the
  // reload-status fix answering HTTP 200 with `{"ok": false}` and no
  // `error` key, which `apiFetch` cannot see either. So: read the body,
  // treat `ok: false` as the failure it is regardless of status, and say
  // which of the two happened in the row itself. Same shape as
  // `pages/Skills.jsx`.
  const reload = async (id) => {
    setBusy(id);
    setNote(null);
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
      setNote({ id, ok: true });
    } catch (err) {
      setNote({ id, ok: false, detail: err?.detail || err?.message || 'the reload failed' });
    } finally { setBusy(null); }
  };

  if (!open) return null;

  return (
    <div
      className="v2-skills-launcher-backdrop"
      role="presentation"
      onClick={(e) => { if (e.target === e.currentTarget) onClose?.(); }}
    >
      <Glass
        as="section"
        level="elev"
        radius="lg"
        padding="none"
        className="v2-skills-launcher"
        role="dialog"
        aria-label="All skills"
        aria-modal="true"
      >
        <header className="v2-skills-launcher-head">
          <Search size={15} aria-hidden="true" />
          <input
            className="v2-hub-search"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={`Search ${skills.length} skill${skills.length === 1 ? '' : 's'}…`}
            aria-label="Search skills"
            autoFocus
          />
          <span className="v2-chip v2-chip--muted">{pinned.length}/{MAX_PINNED} pinned</span>
          <button
            type="button"
            className="v2-btn v2-btn--ghost"
            onClick={() => onClose?.()}
            aria-label="Close"
          >
            <X size={14} />
          </button>
        </header>

        <div className="v2-skills-launcher-body">
          {list.length === 0 && <div className="v2-p v2-p--muted">No matches.</div>}
          <ul className="v2-skills-launcher-list">
            {list.map((s) => {
              const id = s.skill_id || s.id;
              const isPinned = pinned.includes(id);
              const isExpanded = expanded === id;
              return (
                <li key={id} className={`v2-skill-row${isExpanded ? ' is-expanded' : ''}`}>
                  <button
                    type="button"
                    className="v2-skill-row-head"
                    onClick={() => setExpanded((prev) => (prev === id ? null : id))}
                  >
                    <span className="v2-skill-row-name">{s.name || id}</span>
                    <code className="v2-skill-row-id">{id}</code>
                    <span className="v2-skill-row-chevron" aria-hidden="true">
                      {isExpanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
                    </span>
                  </button>
                  <div className="v2-skill-row-actions">
                    <button
                      type="button"
                      className={`v2-btn v2-btn--ghost${isPinned ? ' is-on' : ''}`}
                      onClick={() => togglePin(id)}
                      aria-label={isPinned ? 'Unpin' : 'Pin'}
                      title={isPinned ? 'Unpin from Home' : 'Pin to Home'}
                    >
                      {isPinned ? <Pin size={13} /> : <PinOff size={13} />}
                    </button>
                    <button
                      type="button"
                      className="v2-btn v2-btn--ghost"
                      onClick={() => reload(id)}
                      disabled={busy === id}
                      aria-label="Hot-reload skill"
                      title="Hot-reload"
                    >
                      <RefreshCw size={13} />
                    </button>
                  </div>
                  {note && note.id === id && !note.ok && (
                    <div
                      style={RELOAD_FAILED_STYLE}
                      role="alert"
                      data-testid={`skill-reload-failed-${id}`}
                    >
                      <span>
                        {id} was not reloaded: {note.detail} Whatever code the brain had
                        loaded before is still what is running.
                      </span>
                      <button type="button" style={RETRY_STYLE} onClick={() => reload(id)}>
                        Retry
                      </button>
                    </div>
                  )}
                  {note && note.id === id && note.ok && (
                    <div
                      style={RELOAD_OK_STYLE}
                      role="status"
                      data-testid={`skill-reload-ok-${id}`}
                    >
                      Hot-reloaded {id}
                    </div>
                  )}
                  {isExpanded && (
                    <div className="v2-skill-row-detail">
                      {s.description && <p className="v2-p">{s.description}</p>}
                      {Array.isArray(s.trigger_phrases) && s.trigger_phrases.length > 0 && (
                        <div className="v2-skill-card-phrases">
                          {s.trigger_phrases.slice(0, 6).map((p, i) => (
                            <span key={i} className="v2-chip v2-chip--muted">"{p}"</span>
                          ))}
                        </div>
                      )}
                      <div className="v2-skill-card-meta">
                        {s.version && <span className="v2-chip">v{s.version}</span>}
                        {Array.isArray(s.endpoints) && (
                          <span className="v2-chip">{s.endpoints.length} endpoints</span>
                        )}
                        {s.approval_mode && (
                          <span className={`v2-chip v2-chip--${s.approval_mode}`}>{s.approval_mode}</span>
                        )}
                      </div>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      </Glass>
    </div>
  );
}
