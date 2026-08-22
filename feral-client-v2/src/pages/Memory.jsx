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

const TIER_LABEL = {
  episode: 'Episode',
  note: 'Note',
  knowledge: 'Knowledge',
  entity: 'Entity',
};

const TIER_ORDER = ['episode', 'note', 'knowledge', 'entity'];

/**
 * The text a result row is actually about, in the order the tiers
 * populate it. Episodes carry `summary` + `detail`, notes carry
 * `content`, knowledge rows are a subject/predicate/object triple that
 * `search_all` pre-renders into `summary`, entities carry a name.
 * `JSON.stringify(row)` is the last resort and never the first answer.
 */
function resultBody(r) {
  if (typeof r?.content === 'string' && r.content) return r.content;
  if (typeof r?.summary === 'string' && r.summary) return r.summary;
  if (typeof r?.text === 'string' && r.text) return r.text;
  if (r?.subject && r?.predicate) return `${r.subject} ${r.predicate} ${r.object ?? ''}`.trim();
  if (typeof r?.name === 'string' && r.name) return r.name;
  return JSON.stringify(r).slice(0, 200);
}

/** Secondary context under the body: whatever this tier can prove. */
function resultContext(r) {
  const bits = [];
  if (Array.isArray(r?.tags)) bits.push(...r.tags.map((t) => `#${t}`));
  if (r?.importance && r.importance !== 'normal') bits.push(r.importance);
  if (typeof r?.mentions === 'number') bits.push(`${r.mentions} mention${r.mentions === 1 ? '' : 's'}`);
  if (r?.type && r.tier === 'entity') bits.push(r.type);
  const ts = r?.created_at ?? r?.start_time ?? r?.timestamp;
  if (typeof ts === 'number' && ts > 0) {
    bits.push(new Date(ts * 1000).toLocaleString());
  }
  return bits;
}

/**
 * Memory search, against the brain's real hybrid recall.
 *
 * This tab used to GET `/internal/memory/search?q=<term>`. That route
 * declares its parameter as `query`, not `q`, so FastAPI bound `query`
 * to "" and the handler's `if not query: return []` fired on every
 * search this page has ever run. Measured against a live brain holding
 * two notes that both matched: `?q=quokka` returned `[]` and
 * `?query=quokka` returned both notes with scores. An empty result set
 * reads as "nothing matched", so the page rendered "No results" and the
 * store looked empty rather than un-queried. It also only ever searched
 * the notes tier, and rendered `r.score`, a key the notes route does not
 * return (it returns `relevance_score`), so the score chip was dead too.
 *
 * It now calls `/api/memory/search`, which runs `MemoryStore.search_all`
 * over all four tiers (episodes and notes via FTS5 + vector hybrid,
 * knowledge triples, and knowledge-graph entities) and reports the
 * per-tier degradations `search_all` records. A tier that failed is
 * named on screen, so a partial answer can never be read as an empty
 * store.
 */
/**
 * Below this cosine score a vector hit is a nearest neighbour, not an
 * answer. It matters because the vector leg ALWAYS returns its top-k:
 * measured on this brain, the nonsense query "zzzznotathing" came back
 * with ten rows scoring 0.39-0.42, while "Perth" scored 0.835 against a
 * note that really says Perth. Without the distinction, a user checking
 * whether recall works reads ten rows and concludes it does.
 */
const WEAK_SCORE = 0.5;

function SearchTab() {
  const [q, setQ] = useState('');
  // The submitted query, not the input. A search that failed used to
  // land in the same `results = []` as a search that genuinely matched
  // nothing, and the page said "No results" for both.
  const [query, setQuery] = useState('');
  const [tierFilter, setTierFilter] = useState('all');
  const {
    data: payload, error, loading, refresh,
  } = useResource(
    query ? `/api/memory/search?q=${encodeURIComponent(query)}&limit=50` : null,
  );
  // What actually answered: which embedding provider produced the query
  // vector and which index served it. Named so a weak result set can be
  // attributed rather than guessed at.
  const { data: engine } = useResource('/internal/memory/stats', { silent: true });
  const obs = engine?.observability || null;

  const allResults = Array.isArray(payload?.results) ? payload.results : [];
  const degradations = Array.isArray(payload?.degradations) ? payload.degradations : [];
  const tiers = payload?.tiers || {};
  const busy = !!query && loading && !payload;

  const results = tierFilter === 'all'
    ? allResults
    : allResults.filter((r) => (r.tier || 'unknown') === tierFilter);

  const scoreOf = (r) => (typeof r.score === 'number'
    ? r.score
    : (typeof r.relevance_score === 'number' ? r.relevance_score : null));
  const strongCount = allResults.filter((r) => {
    const s = scoreOf(r);
    return s == null || s >= WEAK_SCORE;
  }).length;

  const go = (e) => {
    e.preventDefault();
    const next = q.trim();
    if (!next) return;
    // Re-submitting the same text has to re-ask, not no-op.
    if (next === query) refresh();
    else setQuery(next);
  };

  return (
    <Pane
      title={payload
        ? `Search — ${strongCount} strong of ${payload.count} hit${payload.count === 1 ? '' : 's'} across ${Object.keys(tiers).length} tier${Object.keys(tiers).length === 1 ? '' : 's'}`
        : 'Search'}
    >
      <p className="v2-p v2-p--muted">
        Hybrid recall over every tier at once: episodes and notes through FTS5 plus
        vector cosine, knowledge triples, and knowledge-graph entities. Ranked by the
        brain, not re-sorted here. The vector leg always returns its nearest
        neighbours, so a score below {WEAK_SCORE.toFixed(2)} is marked weak rather
        than presented as a match.
      </p>
      {obs && (
        <p className="v2-p v2-p--tiny v2-p--muted" data-testid="memory-search-engine">
          {`Answered by: ${obs.embedding_provider || 'no embedding provider'} embeddings over ${obs.active_vector_store || 'unknown index'}`}
          {obs.chunk_count != null ? ` · ${obs.chunk_count} embedded chunk${obs.chunk_count === 1 ? '' : 's'}` : ''}
          {!obs.embedding_provider ? ' · no semantic leg, this is keyword search only' : ''}
        </p>
      )}
      <form onSubmit={go} className="v2-twin-form">
        <input
          className="v2-input v2-twin-input"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="What do I know about…"
          data-testid="memory-search-input"
        />
        <button type="submit" className="v2-btn v2-btn--primary" disabled={busy || !q.trim()} data-testid="memory-search-submit">
          <Search size={13} /> {busy ? 'Searching…' : 'Search'}
        </button>
      </form>

      {/* A tier that raised is NOT a tier that matched nothing. The
          brain distinguishes them; so does this. */}
      {degradations.length > 0 && (
        <div className="v2-chip v2-chip--warn" role="status" data-testid="memory-search-degraded" style={{ marginTop: 10 }}>
          {`Incomplete: ${degradations.map((d) => `${d.tier} tier failed (${d.error})`).join('; ')}. These results are partial, not empty.`}
        </div>
      )}

      {payload && allResults.length > 0 && (
        <div className="v2-device-caps" style={{ marginTop: 10 }} data-testid="memory-search-facets">
          <button
            type="button"
            className={`v2-btn v2-btn--ghost${tierFilter === 'all' ? ' is-active' : ''}`}
            onClick={() => setTierFilter('all')}
          >
            All ({allResults.length})
          </button>
          {TIER_ORDER.filter((t) => tiers[t]).map((t) => (
            <button
              key={t}
              type="button"
              className={`v2-btn v2-btn--ghost${tierFilter === t ? ' is-active' : ''}`}
              onClick={() => setTierFilter(t)}
            >
              {TIER_LABEL[t] || t} ({tiers[t]})
            </button>
          ))}
        </div>
      )}

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
          {results.map((r, i) => {
            const context = resultContext(r);
            const score = typeof r.score === 'number'
              ? r.score
              : (typeof r.relevance_score === 'number' ? r.relevance_score : null);
            return (
              <li key={`${r.tier || 't'}-${r.id || i}`}>
                <Glass level={0} radius="md" padding="md">
                  <div className="v2-mem-meta" style={{ marginBottom: 6 }}>
                    <span className="v2-chip" data-testid="memory-search-tier">{TIER_LABEL[r.tier] || r.tier || 'result'}</span>
                    {score != null && <span className="v2-chip v2-chip--muted">score {score.toFixed(3)}</span>}
                    {score != null && score < WEAK_SCORE && (
                      <span className="v2-chip v2-chip--warn" data-testid="memory-search-weak" title="Below the weak-match floor. The vector leg returns its nearest neighbours for any input, so this is proximity, not a match.">
                        weak
                      </span>
                    )}
                  </div>
                  <div className="v2-mem-content">{resultBody(r)}</div>
                  {r.detail && <div className="v2-p v2-p--tiny v2-p--muted" style={{ marginTop: 4 }}>{String(r.detail).slice(0, 400)}</div>}
                  {context.length > 0 && (
                    <div className="v2-mem-meta">
                      {context.map((c, ci) => <span key={ci} className="v2-chip v2-chip--muted">{c}</span>)}
                    </div>
                  )}
                </Glass>
              </li>
            );
          })}
          {!busy && payload && results.length === 0 && (
            <EmptyState
              title={tierFilter === 'all' ? 'No results' : `No ${TIER_LABEL[tierFilter] || tierFilter} results`}
              hint={degradations.length > 0
                ? 'Some tiers failed to answer, so this is not proof the brain knows nothing.'
                : 'Every tier answered and none of them matched.'}
            />
          )}
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
