import React, { useCallback, useState } from 'react';
import { BookOpen, Upload, Link as LinkIcon, RefreshCw } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Tabs from '../ui/Tabs';
import EmptyState from '../ui/EmptyState';
import ErrorState from '../ui/ErrorState';
import { apiFetch } from '../lib/api';
import { useResource } from '../hooks/useResource';
import { API_BASE } from '../lib/config';

export default function Wiki() {
  const [tab, setTab] = useState('pages');
  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title="Wiki"
        actions={(
          <Tabs
            value={tab}
            onChange={setTab}
            items={[
              { id: 'pages', label: 'Pages' },
              { id: 'ingest', label: 'Ingest' },
              { id: 'compile', label: 'Compile' },
            ]}
          />
        )}
      >
        <p className="v2-p v2-p--muted">
          Long-form structured knowledge. Pages are compiled from memories, transcripts,
          docs, and repos you feed in.
        </p>
      </Pane>
      {tab === 'pages' && <PagesTab />}
      {tab === 'ingest' && <IngestTab />}
      {tab === 'compile' && <CompileTab />}
    </div>
  );
}

/**
 * PagesTab: the false-empty-state conversion.
 *
 * Was: one `Promise.allSettled` whose rejected legs were never read,
 * with `loading` flipped false in a `finally`. A dropped
 * `/api/wiki/pages` therefore left `pages` at its initial `[]` and the
 * pane rendered "Pages (0)" over "No pages yet / Compile or ingest
 * content to populate the wiki", sending the user to re-ingest a wiki
 * that may well be full. The open-page fetch had the mirrored problem:
 * `.catch(() => setContent(null))` renders exactly what "no page
 * selected" renders.
 */
function PagesTab() {
  const [open, setOpen] = useState(null);

  const {
    data: pageRows, error: pagesError, loading, refresh: refreshPages,
  } = useResource('/api/wiki/pages', {
    select: (d) => (Array.isArray(d?.pages) ? d.pages : (Array.isArray(d) ? d : [])),
  });
  // Additive: the total chip is a nice-to-have next to a list that
  // already rendered, so its failure stays quiet rather than stacking a
  // second ErrorState on the same pane.
  const { data: stats, refresh: refreshStats } = useResource('/api/wiki/stats', { silent: true });
  const {
    data: content, error: contentError, loading: contentLoading, refresh: refreshContent,
  } = useResource(open ? `/api/wiki/pages/${encodeURIComponent(open)}` : null);

  const pages = pageRows || [];
  const refresh = useCallback(
    () => Promise.all([refreshPages(), refreshStats()]),
    [refreshPages, refreshStats],
  );

  return (
    <Pane
      title={pageRows ? `Pages (${pages.length})` : 'Pages'}
      actions={(
        <>
          {stats?.pages != null && <span className="v2-chip">{stats.pages} total</span>}
          <button type="button" className="v2-btn v2-btn--ghost" onClick={refresh} aria-label="Refresh pages"><RefreshCw size={13} /></button>
        </>
      )}
    >
      {loading && !pageRows && <EmptyState title="Loading…" />}
      {pagesError && !pageRows && (
        <ErrorState error={pagesError} what="the wiki page list" onRetry={refresh} />
      )}
      {pageRows && pages.length === 0 && <EmptyState title="No pages yet" hint="Compile or ingest content to populate the wiki." />}
      <div className="v2-knowledge-layout">
        <div className="v2-knowledge-entities">
          {pages.map((p, i) => {
            const id = p.id || p.page_id || p.slug || p;
            return (
              <button
                key={i}
                type="button"
                className={`v2-settings-btn${open === id ? ' is-active' : ''}`}
                onClick={() => setOpen(id)}
              >
                <BookOpen size={12} /> {p.title || id}
              </button>
            );
          })}
        </div>
        <div className="v2-knowledge-detail">
          {!open && <EmptyState title="Pick a page" />}
          {open && contentLoading && !content && <EmptyState title="Loading…" />}
          {open && contentError && !content && (
            <ErrorState
              error={contentError}
              what={`the page "${open}"`}
              compact
              onRetry={refreshContent}
            />
          )}
          {open && content && (
            <Glass level={0} radius="md" padding="md">
              <h3>{content.title || open}</h3>
              <div className="v2-wiki-body">{content.content || content.body || JSON.stringify(content, null, 2).slice(0, 2000)}</div>
            </Glass>
          )}
        </div>
      </div>
    </Pane>
  );
}

function IngestTab() {
  const [text, setText] = useState('');
  const [repo, setRepo] = useState('');
  const [busy, setBusy] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const send = async (kind, body) => {
    setBusy(kind);
    setResult(null);
    setError(null);
    try {
      const r = await apiFetch(`/api/wiki/ingest/${kind}`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) setError(data?.error || `${r.status}`);
      else setResult(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  const sendText = (e) => { e.preventDefault(); if (text.trim()) send('text', { content: text, text }); };
  // AUDIT-r14 finding 04 fix: backend at `memory.py:679` reads
  // `body.get("path", "")` (a filesystem path or git URL). The UI
  // used to send `{url}`, which the brain silently ignored and the
  // ingest never happened. Now we send `{path}` and also keep `url`
  // as a fallback for any older brain build still on the WS.
  const sendRepo = (e) => { e.preventDefault(); if (repo.trim()) send('repo', { path: repo.trim(), url: repo.trim() }); };

  const uploadPdf = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy('pdf');
    setResult(null);
    setError(null);
    try {
      const form = new FormData();
      form.append('file', file);
      const r = await fetch(`${API_BASE}/api/wiki/ingest/pdf`, {
        method: 'POST',
        body: form,
        headers: {
          // apiFetch adds auth; here we hand-build so we keep multipart.
          Authorization: `Bearer ${typeof localStorage !== 'undefined' ? (localStorage.getItem('feral_api_key') || '') : ''}`,
        },
        credentials: 'same-origin',
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) setError(data?.error || `${r.status}`);
      else setResult(data);
    } catch (err) {
      // Same shape as the other two ingest paths. Without this the
      // upload rejected, the spinner cleared, and nothing on screen
      // distinguished a dropped upload from one that never started.
      setError(err?.message || 'upload failed');
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <Pane title="Ingest text">
        <form onSubmit={sendText}>
          <textarea className="v2-code-editor" rows={6} value={text} onChange={(e) => setText(e.target.value)} placeholder="Paste docs, transcripts, notes, anything…" />
          <div className="v2-forge-actions">
            <button type="submit" className="v2-btn v2-btn--primary" disabled={busy === 'text' || !text.trim()}>
              <Upload size={13} /> {busy === 'text' ? 'Ingesting…' : 'Ingest text'}
            </button>
          </div>
        </form>
      </Pane>

      <Pane title="Ingest PDF">
        <input type="file" accept="application/pdf" onChange={uploadPdf} className="v2-input" />
        {busy === 'pdf' && <div className="v2-chip">Uploading…</div>}
      </Pane>

      <Pane title="Ingest repo">
        <form onSubmit={sendRepo}>
          <label className="v2-step-field">
            <span>Repo URL</span>
            <input className="v2-input" value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="https://github.com/…" />
          </label>
          <div className="v2-forge-actions">
            <button type="submit" className="v2-btn v2-btn--primary" disabled={busy === 'repo' || !repo.trim()}>
              <LinkIcon size={13} /> {busy === 'repo' ? 'Cloning…' : 'Ingest repo'}
            </button>
          </div>
        </form>
      </Pane>

      {result && <Glass level={0} radius="md" padding="md"><pre className="v2-code">{JSON.stringify(result, null, 2).slice(0, 1200)}</pre></Glass>}
      {error && <div className="v2-chip v2-chip--error">{error}</div>}
    </>
  );
}

function CompileTab() {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const run = async () => {
    setBusy(true);
    setResult(null);
    setError(null);
    try {
      const r = await apiFetch('/api/wiki/compile', { method: 'POST' });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) setError(data?.error || `${r.status}`);
      else setResult(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Pane title="Compile / rebuild wiki">
      <p className="v2-p v2-p--muted">Re-extracts entities + rebuilds page summaries from memories + episodes. Safe to run anytime.</p>
      <div className="v2-forge-actions">
        <button type="button" className="v2-btn v2-btn--primary" onClick={run} disabled={busy}>
          <RefreshCw size={13} /> {busy ? 'Compiling…' : 'Compile now'}
        </button>
      </div>
      {result && <Glass level={0} radius="md" padding="md"><pre className="v2-code">{JSON.stringify(result, null, 2).slice(0, 800)}</pre></Glass>}
      {error && <div className="v2-chip v2-chip--error">{error}</div>}
    </Pane>
  );
}
