/**
 * Memory: the four-tier store, one tab per tier.
 *
 * Every tab in this file used to turn a failed fetch into an empty
 * result: `.then(setItems).finally(() => setLoading(false))` with no
 * catch, or a `Promise.allSettled` whose rejected leg was simply not
 * read. The list state stayed `[]`, `loading` went false, and the page
 * asserted "No notes saved yet" / "No episodes yet" / "No tool calls
 * yet" / "Knowledge graph is empty" about a brain it had never
 * successfully reached, with a fabricated `(0)` in the pane title next
 * to it. All five tabs now go through useResource, which never
 * substitutes an empty value for an answer it does not have, and each
 * renders ErrorState instead of EmptyState when the ask itself failed.
 */
import React, { useCallback, useState } from 'react';
import { Search, Plus, Network, Database, Clock, ScrollText } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Tabs from '../ui/Tabs';
import EmptyState from '../ui/EmptyState';
import ErrorState from '../ui/ErrorState';
import Modal from '../ui/Modal';
import { apiFetch } from '../lib/api';
import { useResource } from '../hooks/useResource';

/**
 * Pull the first array-valued key out of a response, else the response
 * itself when it is already an array. Returns `[]` only for a shape we
 * genuinely cannot read, never for a failed request: useResource keeps
 * `data` null in that case and `select` does not run at all.
 */
function asList(value, ...keys) {
  for (const key of keys) {
    if (Array.isArray(value?.[key])) return value[key];
  }
  return Array.isArray(value) ? value : [];
}

export default function Memory() {
  const [tab, setTab] = useState('recent');
  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title="Memory"
        actions={(
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { id: 'recent', label: 'Recent' },
              { id: 'search', label: 'Search' },
              { id: 'episodes', label: 'Episodes' },
              { id: 'log', label: 'Exec log' },
              { id: 'graph', label: 'Knowledge' },
            ]}
          />
        )}
      >
        <p className="v2-p v2-p--muted">
          Four tiers: semantic notes, episodic memory, execution log, and a knowledge graph.
        </p>
      </Pane>
      {tab === 'recent' && <RecentTab />}
      {tab === 'search' && <SearchTab />}
      {tab === 'episodes' && <EpisodesTab />}
      {tab === 'log' && <ExecLogTab />}
      {tab === 'graph' && <KnowledgeTab />}
    </div>
  );
}

function RecentTab() {
  const [showNew, setShowNew] = useState(false);
  // AUDIT-r14 D-L fix: surface real per-tier totals from /api/memory/stats
  // so the tab title shows e.g. "Recent (4361 episodes · 12 notes)"
  // instead of always claiming 0. The recent-notes list still renders
  // the bounded `/internal/memory/recent` slice; the count above is
  // the truthful number.
  // RC polish: backend canonical key is ``knowledge_triples``; the
  // legacy ``knowledge`` alias is still accepted for back-compat with
  // older brains. A degraded (``ok: false``) or unreachable stats call
  // surfaces the chip instead of a misleading row of zeros, and the
  // title drops the counts entirely rather than printing zeros.
  const {
    data: notes, error: notesError, loading, refresh: refreshNotes,
  } = useResource('/internal/memory/recent', {
    select: (d) => asList(d, 'memories', 'notes'),
  });
  const {
    data: stats, error: statsError, refresh: refreshStats,
  } = useResource('/api/memory/stats', { silent: true });

  const items = notes || [];
  const refresh = useCallback(
    () => Promise.all([refreshNotes(), refreshStats()]),
    [refreshNotes, refreshStats],
  );

  const tiers = stats?.totals || {};
  const totals = {
    notes: Number(tiers.notes ?? 0),
    episodes: Number(tiers.episodes ?? 0),
    knowledge: Number(tiers.knowledge_triples ?? tiers.knowledge ?? 0),
  };
  // Two different failures, one chip: the brain answered "my stats are
  // degraded", or we never got an answer at all. Both mean the numbers
  // are unknown, and the reason distinguishes them.
  const statsOk = !statsError && !!stats && stats.ok !== false;
  const statsReason = statsError ? 'stats_unreachable' : (stats?.reason || '');

  const title = statsOk
    ? `Recent (${totals.episodes} episodes · ${totals.notes} notes · ${totals.knowledge} knowledge)`
    : 'Recent';

  return (
    <Pane
      title={title}
      actions={<button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowNew(true)}><Plus size={13} /> Save memory</button>}
    >
      {!statsOk && (stats || statsError) && (
        <span
          className="v2-chip v2-chip--warn"
          role="status"
          data-testid="memory-stats-degraded"
        >
          Memory stats unavailable ({statsReason || 'unknown'})
        </span>
      )}
      {loading && !notes && <EmptyState title="Loading…" />}
      {notesError && !notes && (
        <ErrorState
          error={notesError}
          what="your recent memories"
          onRetry={refresh}
        />
      )}
      {notes && items.length === 0 && <EmptyState title="No notes saved yet" hint="Episodes are written automatically per chat turn; notes are explicit Save Memory items." />}
      <ul className="v2-mem-list">
        {items.slice(0, 30).map((m, i) => (
          <li key={m.id || i}>
            <Glass level={0} radius="md" padding="md">
              <div className="v2-mem-content">{m.content || m.text || JSON.stringify(m).slice(0, 200)}</div>
              <div className="v2-mem-meta">
                {m.tags && Array.isArray(m.tags) && m.tags.map((t, ti) => (
                  <span key={ti} className="v2-chip v2-chip--muted">{t}</span>
                ))}
                {m.created_at && <span>· {new Date(m.created_at * 1000).toLocaleString()}</span>}
              </div>
            </Glass>
          </li>
        ))}
      </ul>
      {showNew && <SaveMemoryModal onClose={() => setShowNew(false)} onSaved={() => { setShowNew(false); refresh(); }} />}
    </Pane>
  );
}

function SaveMemoryModal({ onClose, onSaved }) {
  const [content, setContent] = useState('');
  const [tags, setTags] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const r = await apiFetch('/internal/memory/save', {
        method: 'POST',
        body: JSON.stringify({
          content,
          tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
        }),
      });
      if (!r.ok) {
        setError(`${r.status} ${await r.text()}`);
        return;
      }
      onSaved();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title="Save a memory"
      actions={(
        <>
          <button type="button" className="v2-btn" onClick={onClose}>Cancel</button>
          <button type="button" className="v2-btn v2-btn--primary" onClick={submit} disabled={busy || !content.trim()}>
            {busy ? 'Saving…' : 'Save'}
          </button>
        </>
      )}
    >
      <label className="v2-step-field">
        <span>Content</span>
        <textarea className="v2-code-editor" rows={5} value={content} onChange={(e) => setContent(e.target.value)} />
      </label>
      <label className="v2-step-field">
        <span>Tags</span>
        <input className="v2-input" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="idea, personal" />
      </label>
      {error && <div className="v2-chip v2-chip--error">{error}</div>}
    </Modal>
  );
}

function SearchTab() {
  const [q, setQ] = useState('');
  // The submitted query, not the input. A search that failed used to
  // land in the same `results = []` as a search that genuinely matched
  // nothing, and the page said "No results" for both.
  const [query, setQuery] = useState('');
  const {
    data: hits, error, loading, refresh,
  } = useResource(
    query ? `/internal/memory/search?q=${encodeURIComponent(query)}` : null,
    { select: (d) => asList(d, 'results', 'memories') },
  );

  const results = hits || [];
  const busy = !!query && loading && !hits;

  const go = (e) => {
    e.preventDefault();
    const next = q.trim();
    if (!next) return;
    // Re-submitting the same text has to re-ask, not no-op.
    if (next === query) refresh();
    else setQuery(next);
  };

  return (
    <Pane title="Semantic search">
      <form onSubmit={go} className="v2-twin-form">
        <input className="v2-input v2-twin-input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="What do I know about…" />
        <button type="submit" className="v2-btn v2-btn--primary" disabled={busy || !q.trim()}>
          <Search size={13} /> {busy ? 'Searching…' : 'Search'}
        </button>
      </form>
      {/* Unlike a polling surface, stale rows here answer a DIFFERENT
          question than the one on screen, so a failed search hides the
          previous search's hits instead of leaving them to be misread
          as results for this query. */}
      {error && (
        <ErrorState
          error={error}
          what={`search results for "${query}"`}
          onRetry={refresh}
        />
      )}
      {!error && (
        <ul className="v2-mem-list" style={{ marginTop: 12 }}>
          {results.map((r, i) => (
            <li key={r.id || i}>
              <Glass level={0} radius="md" padding="md">
                <div className="v2-mem-content">{r.content || r.text || JSON.stringify(r).slice(0, 200)}</div>
                {r.score != null && <div className="v2-mem-meta"><span className="v2-chip">score {(r.score).toFixed(3)}</span></div>}
              </Glass>
            </li>
          ))}
          {!busy && hits && results.length === 0 && <EmptyState title="No results" />}
        </ul>
      )}
    </Pane>
  );
}

function EpisodesTab() {
  const {
    data: episodes, error, loading, refresh,
  } = useResource('/internal/episodes/recent', {
    select: (d) => asList(d, 'episodes'),
  });
  const items = episodes || [];
  return (
    <Pane title={episodes ? `Episodes (${items.length})` : 'Episodes'}>
      {loading && !episodes && <EmptyState title="Loading…" />}
      {error && !episodes && (
        <ErrorState error={error} what="your episodes" onRetry={refresh} />
      )}
      {episodes && items.length === 0 && <EmptyState title="No episodes yet" />}
      <ul className="v2-mem-list">
        {items.slice(0, 50).map((e, i) => (
          <li key={e.id || i}>
            <Glass level={0} radius="sm" padding="sm">
              <div className="v2-mem-content">{e.summary || e.content || JSON.stringify(e).slice(0, 200)}</div>
              <div className="v2-mem-meta">
                {e.start_time && <span><Clock size={10} /> {new Date(e.start_time * 1000).toLocaleString()}</span>}
              </div>
            </Glass>
          </li>
        ))}
      </ul>
    </Pane>
  );
}

function ExecLogTab() {
  const {
    data: entries, error, loading, refresh,
  } = useResource('/internal/execution-log', {
    select: (d) => asList(d, 'entries', 'log'),
  });
  const items = entries || [];
  return (
    <Pane title={entries ? `Execution log (${items.length})` : 'Execution log'}>
      {loading && !entries && <EmptyState title="Loading…" />}
      {error && !entries && (
        <ErrorState error={error} what="the execution log" onRetry={refresh} />
      )}
      {entries && items.length === 0 && <EmptyState title="No tool calls yet" />}
      <ul className="v2-mem-list">
        {items.slice(0, 60).map((e, i) => (
          <li key={e.id || i}>
            <Glass level={0} radius="sm" padding="sm">
              <div className="v2-mem-meta"><ScrollText size={10} /> {e.tool || e.skill_id || '—'}{e.endpoint && ` · ${e.endpoint}`}</div>
              <div className="v2-mem-content">{(e.args && JSON.stringify(e.args).slice(0, 160)) || e.summary || ''}</div>
            </Glass>
          </li>
        ))}
      </ul>
    </Pane>
  );
}

function KnowledgeTab() {
  const [selected, setSelected] = useState(null);
  const {
    data: rows, error, loading, refresh,
  } = useResource('/api/knowledge/entities', {
    select: (d) => asList(d, 'entities'),
  });
  // The detail pane's `.catch(() => setAbout(null))` rendered exactly
  // the same blank space as "nothing selected", so a failed lookup was
  // indistinguishable from an entity the brain knows nothing about.
  const {
    data: about, error: aboutError, loading: aboutLoading, refresh: refreshAbout,
  } = useResource(
    selected ? `/internal/knowledge/about/${encodeURIComponent(selected)}` : null,
  );

  const entities = rows || [];

  return (
    <Pane title={rows ? `Knowledge graph (${entities.length})` : 'Knowledge graph'}>
      {loading && !rows && <EmptyState title="Loading…" />}
      {error && !rows && (
        <ErrorState error={error} what="the knowledge graph" onRetry={refresh} />
      )}
      {rows && entities.length === 0 && <EmptyState title="Knowledge graph is empty" hint="Entities get extracted as FERAL learns about you." />}
      <div className="v2-knowledge-layout">
        <div className="v2-knowledge-entities">
          {entities.map((e, i) => {
            const name = e.name || e.entity || e;
            return (
              <button
                key={i}
                type="button"
                className={`v2-settings-btn${selected === name ? ' is-active' : ''}`}
                onClick={() => setSelected(name)}
              >
                <Network size={12} /> {name}
              </button>
            );
          })}
        </div>
        <div className="v2-knowledge-detail">
          {!selected && <EmptyState title="Pick an entity" />}
          {selected && aboutLoading && !about && <EmptyState title="Loading…" />}
          {selected && aboutError && !about && (
            <ErrorState
              error={aboutError}
              what={`what the brain knows about ${selected}`}
              compact
              onRetry={refreshAbout}
            />
          )}
          {selected && about && (
            <Glass level={0} radius="md" padding="md">
              <h3>{selected}</h3>
              <pre className="v2-code">{JSON.stringify(about, null, 2).slice(0, 1600)}</pre>
            </Glass>
          )}
        </div>
      </div>
    </Pane>
  );
}
