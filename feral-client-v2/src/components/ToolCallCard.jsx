/**
 * ToolCallCard renders one structured, collapsible card per tool/skill
 * invocation.
 *
 * Collapsed (default) the card is a single dense line:
 *
 *   ▸ ⟳  Read file     src/pages/Chat.jsx            1.24s
 *   ▸ ✓  Search web    latest tahoe release notes     412ms
 *   ▸ ✕  Run command   pytest -q                      failed
 *
 * Expanded it shows pretty-printed arguments and the result rendered
 * by shape (`ToolResultView`): code/diff highlighted, row lists as a
 * table, images inline, long stdout clamped and scrollable.
 *
 * Trace shape (assembled in pages/Chat.jsx from tool_start /
 * tool_result frames):
 *   {
 *     key: string,
 *     label: string,            // friendlyToolLabel(payload)
 *     args_preview: any,        // string | object
 *     result_preview?: any,
 *     success: boolean | null,  // null === still running
 *     error: string,
 *     error_code: string,       // '' unless the brain declined the call
 *     latency_ms: number,
 *     started_at?: number,      // Date.now() at tool_start
 *   }
 *
 * `success === null` means in-flight: the card shows a spinner and a
 * live elapsed counter so a hung tool is visible rather than silent.
 */
import React, { useEffect, useId, useMemo, useRef, useState } from 'react';
import { ChevronRight, CheckCircle2, XCircle, Loader2, ShieldAlert } from 'lucide-react';
import Glass from '../ui/Glass';
import ToolResultView from './ToolResultView';
import {
  formatArgs,
  formatDuration,
  guessLanguageFromPath,
  summariseArgs,
  summariseGroup,
  tryParseJson,
} from '../lib/toolResult';

const TICK_MS = 500;

/**
 * Tool results the brain declined rather than attempted.
 *
 * A refusal is not a failure. The tool never ran, nothing was written,
 * and there is nothing to retry until the user changes something. Both
 * used to render as a red ✕ "failed", which reads as a malfunction and
 * hides the one thing the user needs to know: FERAL held a boundary on
 * purpose. Keyed off `error_code` rather than matching the prose,
 * because the prose is user-facing copy and will change.
 *
 * Populated by `_emit_tool_result` in agents/orchestrator.py.
 */
export const REFUSAL_CODES = Object.freeze({
  plan_mode_blocked: 'blocked by plan mode',
  policy_denied: 'blocked by policy',
  pending_approval: 'waiting for your approval',
});

export function isRefusal(trace) {
  return Object.hasOwn(REFUSAL_CODES, String(trace?.error_code || ''));
}

function statusOf(trace) {
  // Checked before `success`, since a refusal also carries success:false.
  if (isRefusal(trace)) return 'refused';
  if (trace?.success === true) return 'ok';
  if (trace?.success === false) return 'failed';
  return 'running';
}

function StatusIcon({ status }) {
  if (status === 'ok') {
    return <CheckCircle2 size={13} aria-hidden="true" className="v2-tool-card__icon v2-tool-card__icon--ok" />;
  }
  if (status === 'refused') {
    return <ShieldAlert size={13} aria-hidden="true" className="v2-tool-card__icon v2-tool-card__icon--refused" />;
  }
  if (status === 'failed') {
    return <XCircle size={13} aria-hidden="true" className="v2-tool-card__icon v2-tool-card__icon--error" />;
  }
  return <Loader2 size={13} aria-hidden="true" className="v2-tool-card__icon v2-spin" />;
}

/**
 * Live elapsed milliseconds for a running call. Returns 0 (and never
 * schedules a timer) once the call settles or when the brain didn't
 * send a start timestamp, so settled transcripts stay static.
 */
function useElapsed(startedAt, running) {
  const [, setTick] = useState(0);
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  useEffect(() => {
    if (!running || !startedAt) return undefined;
    const id = setInterval(() => {
      if (mounted.current) setTick((t) => t + 1);
    }, TICK_MS);
    return () => clearInterval(id);
  }, [running, startedAt]);
  if (!running || !startedAt) return 0;
  return Math.max(0, Date.now() - startedAt);
}

/** Derive a highlight language for the result from the call's args. */
function languageHint(trace) {
  const raw = trace?.args_preview;
  const args = typeof raw === 'string' ? tryParseJson(raw) : raw;
  if (!args || typeof args !== 'object') return '';
  const path = args.path || args.file_path || args.file || args.filename || '';
  return guessLanguageFromPath(path);
}

export default function ToolCallCard({ trace, defaultOpen = false }) {
  const [open, setOpen] = useState(!!defaultOpen);
  const bodyId = useId();
  const status = statusOf(trace);
  const running = status === 'running';
  const elapsed = useElapsed(trace?.started_at, running);

  const argsText = useMemo(() => formatArgs(trace?.args_preview), [trace?.args_preview]);
  const argsSummary = useMemo(() => summariseArgs(trace?.args_preview), [trace?.args_preview]);
  const language = useMemo(() => languageHint(trace), [trace]);

  if (!trace) return null;

  const label = trace.label || trace.tool || trace.name || 'tool';
  const duration = running
    ? formatDuration(elapsed)
    : formatDuration(trace.latency_ms);
  const error = trace.error || '';
  const refused = status === 'refused';
  const refusalLabel = REFUSAL_CODES[String(trace.error_code || '')] || 'refused';
  const hasResult = trace.result_preview != null && trace.result_preview !== '';

  return (
    <Glass
      level={0}
      radius="md"
      padding="sm"
      className={`v2-tool-card v2-tool-card--${status}`}
      data-testid="tool-call-card"
      data-status={status}
    >
      <button
        type="button"
        className="v2-tool-card__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={bodyId}
      >
        <ChevronRight size={13} className={`v2-tool-card__chev${open ? ' is-open' : ''}`} aria-hidden="true" />
        <StatusIcon status={status} />
        <span className="v2-tool-card__label">{label}</span>
        {argsSummary && (
          <span className="v2-tool-card__summary" title={argsSummary}>{argsSummary}</span>
        )}
        <span className="v2-tool-card__status-text">
          {running ? 'running' : status === 'refused' ? refusalLabel : status === 'failed' ? 'failed' : ''}
        </span>
        {duration && <span className="v2-tool-card__lat">{duration}</span>}
      </button>

      <div id={bodyId} className="v2-tool-card__body" hidden={!open}>
        {open && (
          <>
            {argsText && (
              <div className="v2-tool-card__row">
                <div className="v2-tool-card__row-label">args</div>
                <pre className="v2-tool-card__pre">{argsText}</pre>
              </div>
            )}
            {refused && (
              <div className="v2-tool-card__row v2-tool-card__row--refused">
                <div className="v2-tool-card__row-label">refused</div>
                <pre className="v2-tool-card__pre">
                  {error || `This tool was ${refusalLabel}. It did not run.`}
                </pre>
              </div>
            )}
            {status === 'failed' && (
              <div className="v2-tool-card__row v2-tool-card__row--error">
                <div className="v2-tool-card__row-label">error</div>
                <pre className="v2-tool-card__pre">{error || 'tool failed'}</pre>
              </div>
            )}
            {hasResult && status !== 'failed' && !refused && (
              <div className="v2-tool-card__row">
                <div className="v2-tool-card__row-label">result</div>
                <ToolResultView value={trace.result_preview} language={language} />
              </div>
            )}
            {running && (
              <div className="v2-tool-card__row v2-tool-card__row--pending">
                <span className="v2-tool-card__pending">Waiting for the tool to return…</span>
              </div>
            )}
            {!running && !hasResult && status !== 'failed' && !refused && (
              <div className="v2-tool-card__row v2-tool-card__row--pending">
                <span className="v2-tool-card__pending">Completed with no returned output.</span>
              </div>
            )}
          </>
        )}
      </div>
    </Glass>
  );
}

/**
 * ToolCallList groups the tool calls belonging to one assistant
 * turn. A single call renders bare (no chrome to read past); two or
 * more get a collapsible group header with an aggregate status so a
 * fan-out of parallel calls reads as one block instead of a stack of
 * unrelated cards.
 */
export function ToolCallList({ traces, label = 'Tool calls' }) {
  const list = Array.isArray(traces) ? traces.filter(Boolean) : [];
  const [open, setOpen] = useState(true);
  const groupId = useId();
  const summary = useMemo(() => summariseGroup(list), [list]);

  if (list.length === 0) return null;

  if (list.length === 1) {
    return (
      <div className="v2-tool-card-stack" data-testid="tool-call-list">
        <ToolCallCard trace={list[0]} />
      </div>
    );
  }

  const parts = [];
  if (summary.running) parts.push(`${summary.running} running`);
  if (summary.ok) parts.push(`${summary.ok} succeeded`);
  if (summary.failed) parts.push(`${summary.failed} failed`);
  const total = formatDuration(summary.totalMs);

  return (
    <div
      className={`v2-tool-group v2-tool-group--${summary.status}`}
      data-testid="tool-call-list"
      data-status={summary.status}
    >
      <button
        type="button"
        className="v2-tool-group__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={groupId}
      >
        <ChevronRight size={13} className={`v2-tool-card__chev${open ? ' is-open' : ''}`} aria-hidden="true" />
        <StatusIcon status={summary.status} />
        <span className="v2-tool-group__label">
          {list.length} {label.toLowerCase()}
        </span>
        <span className="v2-tool-group__meta">{parts.join(' · ')}</span>
        {total && <span className="v2-tool-card__lat">{total}</span>}
      </button>
      <div id={groupId} className="v2-tool-card-stack" hidden={!open}>
        {open && list.map((t, i) => (
          <ToolCallCard key={t.key || `${t.label || 'tool'}-${i}`} trace={t} />
        ))}
      </div>
    </div>
  );
}
