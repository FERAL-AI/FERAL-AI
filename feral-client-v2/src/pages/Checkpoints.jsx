import React, { useCallback, useEffect, useState } from 'react';
import { Undo2, FileWarning, ChevronRight, AlertTriangle } from 'lucide-react';
import Pane from '../ui/Pane';
import EmptyState from '../ui/EmptyState';
import { apiJson, apiFetch } from '../lib/api';

/**
 * Checkpoints: undo the files a turn wrote.
 *
 * `skills/checkpoints.py` has stored content-addressed pre-write blobs
 * for a long time, and three routes have exposed them
 * (`/api/checkpoints/turns`, `/turns/{id}`, and `POST /revert`). Grepping
 * src/ for "checkpoints" returned zero hits: no client had ever called
 * any of them. Undo existed and was reachable only from a terminal.
 *
 * Every response shape below was confirmed by running the real store
 * against a real temp FERAL_HOME, not read off the route docstring:
 *
 *   plan/dry run   -> {success: true, refused: false, error_code: '',
 *                      files: [{path, status, action, detail}], drifted: []}
 *   refused revert -> {success: false, refused: true,
 *                      error_code: 'revert_refused_drift', dry_run: false}
 *   forced revert  -> {success: true, forced: true, reverted_count: 2}
 *
 * Two traps the route docstring calls out and the measurements confirmed:
 *
 *   1. A refusal and a preview are distinguished by `refused`, never by
 *      `dry_run` and never by parsing `error` prose.
 *   2. `skipped` is not where drifted files go. Drift lands in `drifted`
 *      and keeps `action: 'restore'`, so a UI reading `skipped` to find
 *      what is at risk finds an empty array and says nothing is.
 *
 * Refusal is all-or-nothing by design: a turn with one drifted file
 * restores none of its files, which was verified directly (the
 * untouched, restorable file was still on disk after the refusal).
 *
 * A turn can also contain things undone by a compensating call rather
 * than by restoring bytes: a calendar event, a reminder, a routine.
 * Those arrive under `actions`, files stay under `files`, and `entries`
 * is both. Two consequences for this page:
 *
 *   3. A revert is no longer all-or-nothing once actions are involved. A
 *      failed compensation and a successful file restore come back
 *      together as {success: false, partial: true, error_code:
 *      'revert_incomplete'}. That is a 200 carrying `error`, so apiFetch
 *      throws it like the refusal does, and it has to be read as an
 *      answer rather than a failed request.
 *   4. There is no drift check for actions and there cannot be: the
 *      store never held a copy of the created object. An object the user
 *      already deleted comes back as `already_reverted`, not an error.
 */

/** A stable list key for an entry, file or action. */
export function entryKey(entry) {
  if (entry?.kind === 'action') return `${entry.inverse_tool}:${entry.target}`;
  return entry?.path || '';
}

/** What a revert will do to one entry, in words rather than an enum. */
export function describeAction(file) {
  if (file?.kind === 'action') {
    const name = `${file?.label || 'action'} ${file?.target || ''}`.trim();
    if (file?.action === 'compensate') return `${name} will be deleted (the turn created it)`;
    if (file?.status === 'already_reverted') return `${name} is already gone; nothing to undo`;
    return `${name} will be left alone${file?.detail ? `: ${file.detail}` : ''}`;
  }
  const name = String(file?.path || '').split('/').pop() || file?.path || '';
  if (file?.action === 'delete') return `${name} will be deleted (the turn created it)`;
  if (file?.action === 'skip') return `${name} will be left alone${file?.detail ? `: ${file.detail}` : ''}`;
  return `${name} will be restored to what it was before the turn`;
}

/** "2 files restored, 1 action undone", counted from the envelope's own lists. */
export function describeUndone(body) {
  const files = Array.isArray(body?.reverted) ? body.reverted.length : 0;
  const actions = Array.isArray(body?.reverted_actions) ? body.reverted_actions.length : 0;
  const parts = [`${files} file(s) restored`];
  if (actions) parts.push(`${actions} action(s) undone`);
  return parts.join(', ');
}

/** Files a revert would overwrite, from a plan or a refusal alike. */
export function driftedPaths(result) {
  const rows = Array.isArray(result?.drifted) ? result.drifted : [];
  return rows.map((d) => String(d?.path || '')).filter(Boolean);
}

/** True only for the drift refusal, read off the documented fields. */
export function isDriftRefusal(result) {
  return Boolean(result?.refused) && result?.error_code === 'revert_refused_drift';
}

/** Last 8 characters of an id, which is what distinguishes two turns.
 *
 * Turn ids share a long common prefix, so the leading characters are
 * the part that carries no information. */
export function shortId(id) {
  const s = String(id || '');
  if (!s) return '';
  return s.length <= 10 ? s : `…${s.slice(-8)}`;
}

export function formatWhen(ts) {
  const n = Number(ts || 0);
  if (!n || !Number.isFinite(n)) return '';
  try {
    return new Date(n * 1000).toLocaleString();
  } catch {
    return '';
  }
}

export default function Checkpoints() {
  const [turns, setTurns] = useState([]);
  const [note, setNote] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [openTurn, setOpenTurn] = useState('');
  const [detail, setDetail] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState('');

  const loadTurns = useCallback(async () => {
    try {
      const data = await apiJson('/api/checkpoints/turns?limit=50');
      setTurns(Array.isArray(data?.turns) ? data.turns : []);
      // The store returns this on every listing. It is the single most
      // important thing on the page: anything bash changed in the turn
      // is not tracked and will not come back.
      setNote(String(data?.note || ''));
      setError('');
    } catch (e) {
      setError(e?.message || 'could not reach the brain');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadTurns(); }, [loadTurns]);

  const openDetail = useCallback(async (turnId) => {
    setResult(null);
    if (openTurn === turnId) { setOpenTurn(''); setDetail(null); return; }
    setOpenTurn(turnId);
    setDetail(null);
    try {
      setDetail(await apiJson(`/api/checkpoints/turns/${encodeURIComponent(turnId)}`));
    } catch (e) {
      setError(e?.message || 'could not load that turn');
    }
  }, [openTurn]);

  const revert = useCallback(async (turnId, force) => {
    setBusy(true);
    setResult(null);
    setDone('');
    try {
      // `silent: true` matters here. apiFetch inspects even a 200 body
      // and throws an ApiError whenever it carries a non-empty `error`
      // string, pushing a global error toast on the way out. The drift
      // refusal is a 200 whose envelope carries exactly that, so without
      // this the designed refusal is reported to the user as a failed
      // request and never reaches the code below. Verified in Chrome:
      // the first version of this page rendered nothing at all on a
      // refusal because the throw was caught as a generic error.
      const r = await apiFetch('/api/checkpoints/revert', {
        method: 'POST',
        silent: true,
        body: JSON.stringify({ turn_id: turnId, force: Boolean(force) }),
      });
      const body = await r.json().catch(() => ({}));
      setResult(body);
      if (body?.success && !body?.refused) {
        // A successful undo collapses the open turn, which unmounts
        // anything rendered inside it. Verified in Chrome: the first
        // version put the confirmation there and it vanished on the
        // same tick it was created, so a successful undo gave the user
        // no feedback at all. It lives at page level instead.
        setDone(`Undone. ${describeUndone(body)}.`);
        await loadTurns();
        setOpenTurn('');
        setDetail(null);
      }
    } catch (e) {
      // A refusal arrives here as a thrown ApiError carrying the whole
      // envelope on `.raw`. It is an answer, not a failure.
      //
      // So is an incomplete revert. A turn can contain actions undone by
      // a compensating call, and one of those can fail while the file
      // restores succeed. That envelope is a 200 carrying `error`, so it
      // lands here too, and reporting it as a generic request failure
      // would tell the user nothing came back when half of it did.
      const envelope = e?.raw;
      const answered = envelope && typeof envelope === 'object'
        && (envelope.refused || envelope.error_code === 'revert_incomplete');
      if (answered) {
        setResult(envelope);
        if (envelope.partial) {
          setDone(`Partly undone. ${describeUndone(envelope)}. ${envelope.error || ''}`.trim());
          await loadTurns();
        } else if (!envelope.refused) {
          setError(envelope.error || 'nothing could be undone');
        }
      } else {
        setError(e?.message || 'revert failed');
      }
    } finally {
      setBusy(false);
    }
  }, [loadTurns]);

  const plan = detail?.plan;
  // `entries` is files plus the actions undone by a compensating call.
  // Falls back to `files` so an older brain still renders.
  const planFiles = Array.isArray(plan?.entries)
    ? plan.entries
    : (Array.isArray(plan?.files) ? plan.files : []);

  return (
    <div className="v2-page v2-checkpoints">
      <Pane title="Undo a turn" leading={<Undo2 size={16} aria-hidden="true" />}>
        {note && (
          <div className="v2-cp-note" role="note">
            <FileWarning size={13} aria-hidden="true" /> {note}
          </div>
        )}

        {error && <div className="v2-cp-error" role="status">{error}</div>}

        {done && <div className="v2-cp-done" role="status">{done}</div>}

        {!loading && turns.length === 0 && !error && (
          <EmptyState
            icon={<Undo2 size={22} aria-hidden="true" />}
            title="Nothing to undo"
            hint="Turns where FERAL wrote a file, or created a calendar event, reminder or routine, show up here."
          />
        )}

        {turns.length > 0 && (
          <ul className="v2-cp-list" aria-label="Turns that wrote files">
            {turns.map((t) => {
              const open = openTurn === t.turn_id;
              return (
                <li key={t.turn_id} className="v2-cp-turn">
                  <button
                    type="button"
                    className="v2-cp-turn-head"
                    aria-expanded={open}
                    onClick={() => openDetail(t.turn_id)}
                  >
                    <ChevronRight
                      size={13}
                      aria-hidden="true"
                      className={open ? 'v2-cp-caret is-open' : 'v2-cp-caret'}
                    />
                    <span className="v2-cp-turn-files">
                      {t.files === 1 ? '1 file' : `${t.files} files`}
                      {t.writes > t.files && (
                        <span className="v2-cp-turn-writes">
                          {` (${t.writes} writes)`}
                        </span>
                      )}
                    </span>
                    {t.actions > 0 && (
                      <span className="v2-cp-turn-actioncount">
                        {t.actions === 1 ? '1 action' : `${t.actions} actions`}
                      </span>
                    )}
                    <span className="v2-cp-turn-when">{formatWhen(t.started_at)}</span>
                    {/* Undo is destructive and irreversible, and the two
                        things rendered above it identify nothing: "1 file"
                        describes most turns, and two turns in the same
                        second render an identical timestamp. Both turn_id
                        and session_id were already in the payload and
                        thrown away, so on a busy minute you picked which
                        turn to revert blind. */}
                    <span
                      className="v2-cp-turn-id"
                      title={`turn ${t.turn_id}`}
                    >
                      {shortId(t.turn_id)}
                    </span>
                    {t.session_id && (
                      <span className="v2-cp-turn-session" title={`session ${t.session_id}`}>
                        {shortId(t.session_id)}
                      </span>
                    )}
                  </button>

                  {open && (
                    <div className="v2-cp-detail">
                      {!detail && <span className="v2-cp-muted">Loading what this would do...</span>}

                      {detail && planFiles.length > 0 && (
                        <ul className="v2-cp-files">
                          {planFiles.map((f) => (
                            <li
                              key={entryKey(f)}
                              className="v2-cp-file"
                              data-status={f.status}
                              data-kind={f.kind || 'file'}
                              title={f.path || f.target}
                            >
                              {describeAction(f)}
                            </li>
                          ))}
                        </ul>
                      )}

                      {detail && driftedPaths(plan).length > 0 && (
                        <div className="v2-cp-drift" role="note">
                          <AlertTriangle size={13} aria-hidden="true" />
                          {' '}
                          {driftedPaths(plan).length === 1
                            ? '1 file has changed since the turn. Undoing would discard that change.'
                            : `${driftedPaths(plan).length} files have changed since the turn. Undoing would discard those changes.`}
                        </div>
                      )}

                      <div className="v2-cp-actions">
                        <button
                          type="button"
                          className="v2-btn v2-btn--primary"
                          disabled={busy || !detail}
                          onClick={() => revert(t.turn_id, false)}
                        >
                          <Undo2 size={13} aria-hidden="true" /> Undo this turn
                        </button>
                      </div>

                      {result && isDriftRefusal(result) && (
                        <div className="v2-cp-refusal" role="alert">
                          <p className="v2-cp-refusal-head">
                            Nothing was undone.
                            {' '}
                            {driftedPaths(result).length === 1
                              ? 'One file has changed since the turn, so the whole undo was refused.'
                              : `${driftedPaths(result).length} files have changed since the turn, so the whole undo was refused.`}
                          </p>
                          <ul className="v2-cp-files">
                            {driftedPaths(result).map((p) => (
                              <li key={p} className="v2-cp-file" data-status="drifted">{p}</li>
                            ))}
                          </ul>
                          <button
                            type="button"
                            className="v2-btn v2-btn--danger"
                            disabled={busy}
                            onClick={() => revert(t.turn_id, true)}
                          >
                            Undo anyway and lose those changes
                          </button>
                        </div>
                      )}

                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </Pane>
    </div>
  );
}
