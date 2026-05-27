import React, { useCallback, useEffect, useState } from 'react';
import { Search, Plus, Network, Database, Clock, ScrollText } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Tabs from '../ui/Tabs';
import EmptyState from '../ui/EmptyState';
import Modal from '../ui/Modal';
import { apiJson, apiFetch } from '../lib/api';

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
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showNew, setShowNew] = useState(false);
  // AUDIT-r14 D-L fix: surface real per-tier totals from /api/memory/stats
  // so the tab title shows e.g. "Recent (4361 episodes · 12 notes)"
  // instead of always claiming 0. The recent-notes list still renders
  // the bounded `/internal/memory/recent` slice — the count above is
  // the truthful number.
  // RC polish: backend canonical key is ``knowledge_triples``; the
  // legacy ``knowledge`` alias is still accepted for back-compat with
  // older brains. ``statsState.ok === false`` surfaces the degraded
  // chip instead of a misleading row of zeros.
  const [totals, setTotals] = useState({ notes: 0, episodes: 0, knowledge: 0 });
  const [statsState, setStatsState] = useState({ ok: true, reason: '' });

  const refresh = useCallback(async () => {
    try {
      const [recent, stats] = await Promise.allSettled([
        apiJson('/internal/memory/recent'),
        apiJson('/api/memory/stats', { silent: true }),
      ]);
      if (recent.status === 'fulfilled') {
        const d = recent.value;
        setItems(d.memories || d.notes || d || []);
      }
      if (stats.status === 'fulfilled') {
        const t = stats.value?.totals || {};
        const knowledge = t.knowledge_triples ?? t.knowledge ?? 0;
        setTotals({
          notes: Number(t.notes ?? 0),
          episodes: Number(t.episodes ?? 0),
          knowledge: Number(knowledge),
        });
        setStatsState({
          ok: stats.value?.ok !== false,
          reason: stats.value?.reason || '',
        });
      } else {
        setStatsState({ ok: false, reason: 'stats_unreachable' });
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const title = `Recent (${totals.episodes} episodes · ${totals.notes} notes · ${totals.knowledge} knowledge)`;

  return (
    <Pane
      title={title}
      actions={<button type="button" className="v2-btn v2-btn--primary" onClick={() => setShowNew(true)}><Plus size={13} /> Save memory</button>}
    >
      {!statsState.ok && (
        <span
          className="v2-chip v2-chip--warn"
          role="status"
          data-testid="memory-stats-degraded"
        >
          Memory stats unavailable ({statsState.reason || 'unknown'})
        </span>
      )}
      {loading && <EmptyState title="Loading…" />}
      {!loading && items.length === 0 && <EmptyState title="No notes saved yet" hint="Episodes are written automatically per chat turn; notes are explicit Save Memory items." />}
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
  const [results, setResults] = useState([]);
  const [busy, setBusy] = useState(false);

  const go = async (e) => {
    e.preventDefault();
    if (!q.trim()) return;
    setBusy(true);
    try {
      const d = await apiJson(`/internal/memory/search?q=${encodeURIComponent(q)}`);
      setResults(d.results || d.memories || d || []);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Pane title="Semantic search">
      <form onSubmit={go} className="v2-twin-form">
        <input className="v2-input v2-twin-input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="What do I know about…" />
        <button type="submit" className="v2-btn v2-btn--primary" disabled={busy || !q.trim()}>
          <Search size={13} /> {busy ? 'Searching…' : 'Search'}
        </button>
      </form>
      <ul className="v2-mem-list" style={{ marginTop: 12 }}>
        {results.map((r, i) => (
          <li key={r.id || i}>
            <Glass level={0} radius="md" padding="md">
              <div className="v2-mem-content">{r.content || r.text || JSON.stringify(r).slice(0, 200)}</div>
              {r.score != null && <div className="v2-mem-meta"><span className="v2-chip">score {(r.score).toFixed(3)}</span></div>}
            </Glass>
          </li>
        ))}
        {!busy && q && results.length === 0 && <EmptyState title="No results" />}
      </ul>
    </Pane>
  );
}

function EpisodesTab() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    apiJson('/internal/episodes/recent')
      .then((d) => setItems(d.episodes || d || []))
      .finally(() => setLoading(false));
  }, []);
  return (
    <Pane title={`Episodes (${items.length})`}>
      {loading && <EmptyState title="Loading…" />}
      {!loading && items.length === 0 && <EmptyState title="No episodes yet" />}
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
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    apiJson('/internal/execution-log')
      .then((d) => setItems(d.entries || d.log || d || []))
      .finally(() => setLoading(false));
  }, []);
  return (
    <Pane title={`Execution log (${items.length})`}>
      {loading && <EmptyState title="Loading…" />}
      {!loading && items.length === 0 && <EmptyState title="No tool calls yet" />}
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
  const [entities, setEntities] = useState([]);
  const [selected, setSelected] = useState(null);
  const [about, setAbout] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiJson('/api/knowledge/entities')
      .then((d) => setEntities(d.entities || d || []))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selected) { setAbout(null); return; }
    apiJson(`/internal/knowledge/about/${encodeURIComponent(selected)}`).then(setAbout).catch(() => setAbout(null));
  }, [selected]);

  return (
    <Pane title={`Knowledge graph (${entities.length})`}>
      {loading && <EmptyState title="Loading…" />}
      {!loading && entities.length === 0 && <EmptyState title="Knowledge graph is empty" hint="Entities get extracted as FERAL learns about you." />}
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
