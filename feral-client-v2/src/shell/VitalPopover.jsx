import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { apiJson, apiFetch } from '../lib/api';

/**
 * What sits behind a vital in the system bar.
 *
 * The approved design is explicit that the bar is made of controls, not
 * readouts: "The system bar is back and carries real vitals, each one
 * clickable", and each of its extras opens a popover whose rows are
 * actionable in place. That is the difference between an instrument
 * panel and a status line.
 *
 * What shipped instead navigated. Clicking a number took you to a page,
 * which is the one thing the design says the rail and these popovers
 * exist to avoid.
 *
 * Every row here is read from a live endpoint when the popover opens.
 * Nothing is polled while closed, because these are opened rarely and
 * the shared vitals poller exists to paint the counters cheaply. And
 * nothing is invented: where the brain has no answer the popover says
 * so rather than rendering a plausible-looking zero.
 */

/** Sources, by vital. `null` means the popover builds from vitals alone. */
const SOURCES = {
  jobs: '/api/jobs?limit=40',
  needs: '/api/approvals',
  dev: '/api/devices',
  brain: '/api/llm/status',
  mem: null,
  cost: null,
  autonomy: null,
};

/** 12400 -> "12.4k". Counts run large and the bar is narrow. */
export function compact(n) {
  const v = Number(n || 0);
  if (!Number.isFinite(v) || v <= 0) return '0';
  if (v < 1000) return String(Math.round(v));
  // One decimal up to 100k, because the design renders 12,410 as
  // "12.4k" and a bare "12k" throws away the digit that distinguishes
  // one week's memory from the next. Whole numbers above that, where
  // the decimal stops carrying anything.
  if (v < 1_000_000) return `${(v / 1000).toFixed(v < 100_000 ? 1 : 0)}k`;
  return `${(v / 1_000_000).toFixed(1)}M`;
}

/** Cost as the design renders it: $1.84. */
export function money(n) {
  const v = Number(n || 0);
  if (!Number.isFinite(v)) return '$0.00';
  return `$${v.toFixed(2)}`;
}

/** Uptime as the design's Brain popover renders it: "4h 12m". */
export function uptimeLabel(seconds) {
  const s = Math.floor(Number(seconds) || 0);
  if (s <= 0) return '';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${s}s`;
}

/** Elapsed seconds as the design renders it: 4m12s. */
export function elapsed(startedAt, now = Date.now() / 1000) {
  const s = Math.floor(now - Number(startedAt || 0));
  if (!Number.isFinite(s) || s < 0 || s > 60 * 60 * 24 * 365) return '';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60}s`;
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
}

/**
 * Rows for one vital, from live data plus the shared vitals snapshot.
 *
 * Exported because the shaping is the part worth testing: every one of
 * these branches is a claim about a payload the brain actually sends.
 */
export function rowsFor(kind, data, vitals) {
  if (kind === 'jobs') {
    const items = Array.isArray(data?.items) ? data.items : [];
    return items
      .filter((i) => i.status === 'running' || i.status === 'connected')
      .slice(0, 8)
      .map((i) => ({
        id: i.id,
        title: i.name || i.kind || 'job',
        sub: [i.kind, elapsed(i.started_at)].filter(Boolean).join(' · '),
        // Only offer kill when the brain names a route for it. A button
        // that 404s is worse than no button.
        verb: /^(POST|DELETE)\s+\S+$/.test(String(i.cancellable_via || '')) ? 'kill' : '',
        act: i.cancellable_via,
        tone: 'err',
      }));
  }

  if (kind === 'needs') {
    const list = Array.isArray(data?.approvals) ? data.approvals : [];
    return list.slice(0, 8).map((a) => ({
      id: a.request_id,
      title: a.tool_name || 'tool call',
      sub: String(a.args?.path || a.args?.command || a.args?.url || a.safety_level || ''),
      verb: 'approve',
      act: `POST /api/approvals/${encodeURIComponent(a.request_id)}/approve`,
      tone: 'warn',
    }));
  }

  if (kind === 'dev') {
    const list = Array.isArray(data?.devices) ? data.devices : [];
    return list.slice(0, 8).map((d, i) => ({
      id: d.device_id || d.id || `d${i}`,
      title: d.name || d.device_id || 'device',
      sub: [d.kind || d.type, d.online === false ? 'offline' : 'online']
        .filter(Boolean).join(' · '),
      verb: '',
    }));
  }

  if (kind === 'mem') {
    const m = data?.memory || {};
    return [
      { id: 'ep', title: 'Episodes', sub: 'conversations remembered', value: compact(m.episodes) },
      { id: 'no', title: 'Notes', sub: 'written down', value: compact(m.notes) },
      { id: 'kg', title: 'Knowledge', sub: 'triples in the graph', value: compact(m.knowledge_triples) },
      { id: 'em', title: 'Embedded', sub: `chunks · ${m.vec_index_mode || 'index'}`, value: compact(m.embedded_chunks) },
    ];
  }

  if (kind === 'brain') {
    // The design's Brain popover leads with uptime and then names the
    // model. What shipped led with "LLM / unavailable", which is both
    // less useful and, on a brain that is answering, misleading: it read
    // `llm_available` off a payload where that field is the only LLM
    // fact, so it could not say WHICH model.
    //
    // Context and Last turn are in the design and are not here: the
    // brain records neither. Inventing plausible values for them would
    // be worse than leaving them out.
    const provider = String(data?.provider || '');
    const model = String(data?.model || '');
    const ok = data?.available !== false && (provider || model);
    return [
      {
        id: 'model',
        title: 'Model',
        sub: provider && model ? `${provider} / ${model}` : 'not configured',
        value: ok ? 'ok' : 'unavailable',
        tone: ok ? '' : 'warn',
      },
      // Context and Last turn were left out of this popover because the
      // brain measured neither. It records both now, off paths that
      // already run per turn, so the popover carries the four rows the
      // design specifies. Both are omitted rather than shown as zero
      // when the brain has not answered: "0% used" and "no turn yet"
      // are different facts from "we do not know".
      vitals.contextPct > 0 && {
        id: 'ctx', title: 'Context', sub: 'of the conversation budget',
        value: `${vitals.contextPct}%`,
        tone: vitals.contextPct >= 85 ? 'warn' : '',
      },
      vitals.lastTurnAt > 0 && {
        id: 'turn', title: 'Last turn', sub: 'since the brain last answered',
        value: elapsed(vitals.lastTurnAt),
      },
      { id: 'sk', title: 'Skills', sub: 'loaded and callable', value: String(vitals.skills || 0) },
      { id: 'run', title: 'Running', sub: 'work in flight now', value: String(vitals.running || 0) },
      {
        id: 'dev', title: 'Devices', sub: 'paired and online',
        value: String(vitals.devices || 0),
      },
    ].filter(Boolean);
  }

  if (kind === 'cost') {
    if (!vitals.costKnown) {
      return [{ id: 'x', title: 'Not available', sub: 'the brain did not report a budget', value: '' }];
    }
    return [
      { id: 'sp', title: 'Spent today', sub: 'across every provider', value: money(vitals.cost) },
      vitals.budgetOn
        ? { id: 'cap', title: 'Daily cap', sub: 'set in Settings, LLM', value: money(vitals.budget) }
        : { id: 'cap', title: 'No daily cap', sub: 'nothing throttles spend', value: 'off' },
    ];
  }

  return [];
}

/** The three autonomy tiers, with what each one actually means. */
export const AUTONOMY_TIERS = [
  ['strict', 'ask before anything'],
  ['hybrid', 'ask for risky actions'],
  ['loose', 'never ask'],
];

export default function VitalPopover({ kind, title, vitals, onClose }) {
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState('');
  const [failed, setFailed] = useState('');
  const [mode, setMode] = useState(vitals.autonomy || '');
  const ref = useRef(null);
  const source = SOURCES[kind];

  useEffect(() => {
    let alive = true;
    if (kind === 'mem') {
      apiJson('/api/dashboard').then(
        (d) => alive && setData(d), () => alive && setData({}),
      );
      return () => { alive = false; };
    }
    if (!source) { setData({}); return undefined; }
    apiJson(source).then(
      (d) => alive && setData(d), () => alive && setData({}),
    );
    return () => { alive = false; };
  }, [kind, source]);

  // Escape and any click outside close it, the way a menu should.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    const onDown = (e) => { if (ref.current && !ref.current.contains(e.target)) onClose(); };
    window.addEventListener('keydown', onKey);
    window.addEventListener('pointerdown', onDown);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('pointerdown', onDown);
    };
  }, [onClose]);

  const run = useCallback(async (row) => {
    if (!row.act) return;
    setBusy(row.id);
    setFailed('');
    try {
      const [method, path] = String(row.act).split(/\s+/);
      await apiFetch(path, {
        method, silent: true,
        ...(method === 'POST' ? { body: JSON.stringify({}) } : {}),
      });
      setData((d) => {
        if (kind === 'needs') {
          return { ...d, approvals: (d?.approvals || []).filter((a) => a.request_id !== row.id) };
        }
        return { ...d, items: (d?.items || []).filter((i) => i.id !== row.id) };
      });
    } catch (e) {
      // Say so on the popover. `silent: true` suppresses the global
      // error surface on purpose, and without this the row simply does
      // not move, which reads as a click that missed.
      setFailed(e?.message || 'That did not go through.');
    } finally {
      setBusy('');
    }
  }, [kind]);

  const setAutonomy = useCallback(async (next) => {
    setBusy(next);
    setFailed('');
    try {
      await apiFetch('/api/autonomy', {
        method: 'POST', silent: true, body: JSON.stringify({ mode: next }),
      });
      setMode(next);
    } catch (e) {
      setFailed(e?.message || 'Could not change autonomy.');
    } finally {
      setBusy('');
    }
  }, []);

  const rows = kind === 'autonomy' ? [] : rowsFor(kind, data, vitals);

  return (
    <div className="v2-pop" ref={ref} role="dialog" aria-label={`${title} details`}>
      <header className="v2-pop-h">{title}</header>

      {/* The design leads this one with a single large number. It is the
          question a person actually has about a background process. */}
      {kind === 'brain' && uptimeLabel(vitals.uptime) && (
        <div className="v2-pop-big">
          <b>{uptimeLabel(vitals.uptime)}</b>
          <span>uptime</span>
        </div>
      )}

      {data === null && <p className="v2-pop-quiet">Reading…</p>}

      {kind === 'autonomy' && AUTONOMY_TIERS.map(([tier, meaning]) => (
        <button
          type="button"
          key={tier}
          className="v2-pop-r"
          disabled={busy === tier}
          onClick={() => (tier === mode ? onClose() : setAutonomy(tier))}
        >
          <span className="v2-pop-body">
            <span className="v2-pop-t">{tier}</span>
            <span className="v2-pop-s">{meaning}</span>
          </span>
          <span className={`v2-pop-v${tier === mode ? ' is-current' : ''}`}>
            {tier === mode ? 'current' : 'set'}
          </span>
        </button>
      ))}

      {data !== null && kind !== 'autonomy' && rows.length === 0 && (
        <p className="v2-pop-quiet">Nothing here right now.</p>
      )}

      {rows.map((r) => (
        <div key={r.id} className="v2-pop-r">
          <span className="v2-pop-body">
            <span className="v2-pop-t" title={r.title}>{r.title}</span>
            {r.sub && <span className="v2-pop-s">{r.sub}</span>}
          </span>
          {r.verb ? (
            <button
              type="button"
              className={`v2-pop-v v2-pop-v--${r.tone || 'plain'}`}
              disabled={busy === r.id}
              onClick={() => run(r)}
            >
              {r.verb}
            </button>
          ) : (
            <span className="v2-pop-v">{r.value || ''}</span>
          )}
        </div>
      ))}

      {failed && <p className="v2-pop-failed" role="alert">{failed}</p>}

      <button
        type="button"
        className="v2-pop-all"
        onClick={() => { onClose(); navigate(PAGE_FOR[kind] || '/console'); }}
      >
        Open {title}
      </button>
    </div>
  );
}

/** Where a vital's full surface lives, for the footer link. */
export const PAGE_FOR = {
  jobs: '/jobs',
  needs: '/approvals',
  dev: '/devices',
  mem: '/memory',
  brain: '/health',
  cost: '/settings',
  autonomy: '/oversight',
};
