import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ShieldAlert, Check, X, RefreshCw } from 'lucide-react';
import Pane from '../ui/Pane';
import EmptyState from '../ui/EmptyState';
import { apiFetch, apiJson } from '../lib/api';

/**
 * Approvals: the tool calls the brain is blocked on, across every surface.
 *
 * `GET /api/approvals` has existed with approve and reject since the
 * autonomy work landed, and this client never called it. Confirmed by
 * grepping the whole of `src/` for `api/approvals`: zero hits.
 *
 * The only approval UI was a live `permission_request` WebSocket frame
 * handled in `Chat.jsx`, which has three consequences:
 *
 *   1. it renders only if `/chat` happens to be mounted at that instant,
 *   2. Chat never rehydrates on mount, so arriving later shows nothing, and
 *   3. `permission_request` is deliberately absent from `CHAT_FRAME_TYPES`,
 *      so unlike every other frame it is not session-filtered.
 *
 * The result is that a tool call raised by a cron job, a Discord message
 * or the phone blocks in `ToolRunner._pending_approvals` with nothing on
 * screen anywhere, and the operator has no way to find it. This page is
 * that missing surface.
 *
 * Origin is derived from `session_id` rather than a new backend field,
 * because the prefix already encodes it: `channel_<type>_<user>` for a
 * messaging channel, `phone-<node>` and `voice-<node>` for a device.
 */

const POLL_MS = 4000;

/** Where a request came from, read off the session id prefix. */
export function originOf(sessionId) {
  const sid = String(sessionId || '');
  if (!sid) return { label: 'this brain', kind: 'local' };
  if (sid.startsWith('channel_')) {
    const parts = sid.split('_');
    const channel = parts[1] || 'a channel';
    return { label: `${channel} message`, kind: 'channel' };
  }
  if (sid.startsWith('voice-')) return { label: 'a voice session', kind: 'voice' };
  if (sid.startsWith('phone-')) return { label: 'your phone', kind: 'phone' };
  if (sid.startsWith('cron_') || sid.startsWith('routine_')) {
    return { label: 'a scheduled routine', kind: 'cron' };
  }
  return { label: 'this chat', kind: 'chat' };
}

/** A one-line human summary of what the call will do. */
export function describeCall(approval) {
  const tool = String(approval?.tool_name || 'a tool');
  const args = approval?.args || {};
  const first =
    args.path || args.command || args.url || args.script || args.query || '';
  return first ? `${tool}: ${String(first).slice(0, 160)}` : tool;
}

/** Seconds since the request was raised, as a short human string. */
export function waitedFor(createdAt, now = Date.now() / 1000) {
  const secs = Math.max(0, Math.floor(now - Number(createdAt || 0)));
  if (!Number.isFinite(secs) || secs > 60 * 60 * 24 * 365) return '';
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  return `${Math.floor(secs / 3600)}h`;
}

/**
 * Turn `policy_sources` into short lines an operator can act on.
 *
 * The shape is not uniform, and this was written against what the real
 * resolver emits rather than a guess. Measured by running
 * `resolve_policy('browser__evaluate', {}, surface='api', registry=...)`
 * against the real 42-skill builtin registry:
 *
 *   {
 *     manifest: { safety_tier, read_only_hint, requires_user_approval },
 *     danger_map: 'critical',
 *     legacy_substring: 'legacy_substring:unknown_default',
 *   }
 *
 * plus `{ surface_deny: true }`, which returns early and is the only key
 * present when it fires. So `manifest` is an object, `danger_map` is a
 * string, `surface_deny` is a boolean, and rendering all of them as
 * `${k}: ${v}` puts "[object Object]" on screen.
 */
export function explainSources(sources) {
  const out = [];
  const s = sources || {};

  if (s.surface_deny) out.push('Blocked for this surface');

  const m = s.manifest;
  if (m && typeof m === 'object') {
    const bits = [];
    if (m.safety_tier) bits.push(String(m.safety_tier));
    if (m.requires_user_approval) bits.push('author asks every time');
    if (m.read_only_hint) bits.push('read only');
    out.push(bits.length ? `Skill declares: ${bits.join(', ')}` : 'Skill declares nothing');
  }

  if (s.danger_map && s.danger_map !== 'safe') out.push(`Danger map: ${s.danger_map}`);

  // `legacy_substring:unknown_default` is the resolver's way of saying the
  // fallback heuristic matched nothing. It is on essentially every row and
  // tells the operator nothing, so it is not worth a line.
  const legacy = s.legacy_substring;
  if (legacy && !String(legacy).endsWith('unknown_default')) {
    out.push(`Name heuristic: ${String(legacy).replace(/^legacy_substring:/, '')}`);
  }

  return out;
}

export default function Approvals() {
  const [approvals, setApprovals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const timer = useRef(null);

  const load = useCallback(async () => {
    try {
      const data = await apiJson('/api/approvals');
      setApprovals(Array.isArray(data?.approvals) ? data.approvals : []);
      setError('');
    } catch (e) {
      // A brain that is not running is not an error worth a red banner on
      // a page whose whole job is to be checked when something is stuck.
      setError(e?.message || 'could not reach the brain');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    timer.current = setInterval(load, POLL_MS);
    return () => clearInterval(timer.current);
  }, [load]);

  const decide = useCallback(async (requestId, approved) => {
    setBusy(requestId);
    try {
      const verb = approved ? 'approve' : 'reject';
      const r = await apiFetch(`/api/approvals/${encodeURIComponent(requestId)}/${verb}`, {
        method: 'POST',
        body: JSON.stringify({}),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body?.detail || `failed to ${verb} (${r.status})`);
      }
      // Drop it locally rather than waiting for the next poll, so the row
      // does not sit there looking un-actioned for up to four seconds.
      setApprovals((rows) => rows.filter((a) => a.request_id !== requestId));
    } catch (e) {
      setError(e?.message || 'decision failed');
    } finally {
      setBusy('');
    }
  }, []);

  return (
    <div className="v2-page v2-approvals">
      <Pane
        title="Needs you"
        leading={<ShieldAlert size={16} aria-hidden="true" />}
        actions={
          <button
            type="button"
            className="v2-btn v2-btn--ghost"
            onClick={load}
            aria-label="Refresh approvals"
          >
            <RefreshCw size={14} aria-hidden="true" />
          </button>
        }
      >
        {error && (
          <div className="v2-approvals-error" role="status">{error}</div>
        )}

        {!loading && approvals.length === 0 && !error && (
          <EmptyState
            icon={<ShieldAlert size={22} aria-hidden="true" />}
            title="Nothing is waiting on you"
            hint="Tool calls that need your decision show up here, whichever surface raised them."
          />
        )}

        {approvals.length > 0 && (
          <ul className="v2-approvals-list" aria-label="Pending approvals">
            {approvals.map((a) => {
              const origin = originOf(a.session_id);
              const waited = waitedFor(a.created_at);
              const reasons = explainSources(a.policy_sources);
              return (
                <li key={a.request_id} className="v2-approval" data-level={a.safety_level || 'confirm'}>
                  <div className="v2-approval-head">
                    <span className="v2-approval-tool">{describeCall(a)}</span>
                    <span className="v2-approval-meta">
                      {`from ${origin.label}`}
                      {waited ? ` · waiting ${waited}` : ''}
                      {a.safety_level ? ` · ${a.safety_level}` : ''}
                    </span>
                  </div>

                  {reasons.length > 0 && (
                    <div className="v2-approval-why">
                      {reasons.map((r) => (
                        <span key={r} className="v2-approval-source">{r}</span>
                      ))}
                    </div>
                  )}

                  <div className="v2-approval-actions">
                    <button
                      type="button"
                      className="v2-btn v2-btn--primary"
                      disabled={busy === a.request_id}
                      onClick={() => decide(a.request_id, true)}
                    >
                      <Check size={13} aria-hidden="true" /> Approve
                    </button>
                    <button
                      type="button"
                      className="v2-btn"
                      disabled={busy === a.request_id}
                      onClick={() => decide(a.request_id, false)}
                    >
                      <X size={13} aria-hidden="true" /> Decline
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Pane>
    </div>
  );
}
