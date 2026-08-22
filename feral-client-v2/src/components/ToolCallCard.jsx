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
import {
  Bot,
  Braces,
  CalendarClock,
  ChevronRight,
  Cpu,
  Eye,
  FileText,
  Globe,
  HeartPulse,
  Image as ImageIcon,
  ListChecks,
  Loader2,
  MessageSquare,
  MousePointerClick,
  Search,
  Settings2,
  ShieldAlert,
  Wrench,
} from 'lucide-react';
import Glass from '../ui/Glass';
import ToolResultView from './ToolResultView';
import { TOOL_FAMILIES, toolFamily } from '../lib/toolDisplay';
import {
  REFUSAL_CODES,
  formatArgs,
  formatDuration,
  guessLanguageFromPath,
  isRefusal,
  shouldOpenByDefault,
  summariseArgs,
  summariseGroup,
  tryParseJson,
} from '../lib/toolResult';

const TICK_MS = 500;

/**
 * Glyph per skill family, not per outcome.
 *
 * Every key here is a family id from `lib/toolDisplay.TOOL_FAMILIES`,
 * and `__tests__/lib/toolDisplay.families.test.js` asserts the two
 * agree, so a family added there without a glyph here is a failing
 * test rather than a blank square.
 */
export const FAMILY_ICONS = Object.freeze({
  search: Search,
  browser: Globe,
  code: Braces,
  computer: MousePointerClick,
  vision: Eye,
  comms: MessageSquare,
  schedule: CalendarClock,
  tasks: ListChecks,
  notes: FileText,
  media: ImageIcon,
  hardware: Cpu,
  health: HeartPulse,
  system: Settings2,
  tool: Wrench,
});

/** Group headers are not one skill, so they get a neutral agent glyph. */
const GROUP_ICON = Bot;

/**
 * Refusal vocabulary. Defined in `lib/toolResult.js` so the group
 * summary can count refusals apart from failures without importing a
 * React component; re-exported here because this module has been the
 * public surface for it.
 */
export { REFUSAL_CODES, isRefusal };

function statusOf(trace) {
  // Checked before `success`, since a refusal also carries success:false.
  if (isRefusal(trace)) return 'refused';
  if (trace?.success === true) return 'ok';
  if (trace?.success === false) return 'failed';
  return 'running';
}

const STATUS_WORDS = Object.freeze({
  ok: 'succeeded',
  failed: 'failed',
  refused: 'was refused',
  running: 'is running',
});

/**
 * The head glyph.
 *
 * Outcome no longer lives here: it is carried by the card's tone (one
 * consistent treatment per outcome, see `.v2-tool-card--*` in
 * pages.css) plus the status word. A refusal keeps its shield, because
 * "FERAL declined this on purpose" is a claim about the SYSTEM rather
 * than about the tool and there is no family glyph that can say it. A
 * running call keeps its spinner, because motion is the only honest
 * way to render "not finished yet".
 */
function ToolGlyph({ status, family }) {
  if (status === 'running') {
    return <Loader2 size={13} aria-hidden="true" className="v2-tool-card__icon v2-spin" />;
  }
  if (status === 'refused') {
    return <ShieldAlert size={13} aria-hidden="true" className="v2-tool-card__icon v2-tool-card__icon--refused" />;
  }
  const Icon = FAMILY_ICONS[family] || FAMILY_ICONS.tool;
  return (
    <Icon
      size={13}
      aria-hidden="true"
      className="v2-tool-card__icon"
      data-family={family}
    />
  );
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

export default function ToolCallCard({ trace, defaultOpen, grouped = false }) {
  const bodyId = useId();
  const status = statusOf(trace);
  const running = status === 'running';
  const refused = status === 'refused';
  const elapsed = useElapsed(trace?.started_at, running);

  const argsText = useMemo(() => formatArgs(trace?.args_preview), [trace?.args_preview]);
  const argsSummary = useMemo(() => summariseArgs(trace?.args_preview), [trace?.args_preview]);
  const language = useMemo(() => languageHint(trace), [trace]);
  const family = useMemo(() => toolFamily(trace || {}), [trace]);

  // Whether this card wants to be open, recomputed as the call settles.
  const wantsOpen = useMemo(
    () => (defaultOpen === undefined
      ? shouldOpenByDefault(trace, { grouped, refused, language })
      : !!defaultOpen),
    [trace, grouped, refused, language, defaultOpen],
  );

  const [open, setOpen] = useState(wantsOpen);
  // A card mounts while the call is still running, so `wantsOpen` is
  // false on first paint and only becomes true when the result lands.
  // Adopt it then, but never after the user has touched the control:
  // a card the reader deliberately closed must stay closed.
  const touched = useRef(false);
  useEffect(() => {
    if (!touched.current) setOpen(wantsOpen);
  }, [wantsOpen]);

  if (!trace) return null;

  const label = trace.label || trace.tool || trace.name || 'tool';
  const duration = running
    ? formatDuration(elapsed)
    : formatDuration(trace.latency_ms);
  const error = trace.error || '';
  const refusalLabel = REFUSAL_CODES[String(trace.error_code || '')] || 'refused';
  const hasResult = trace.result_preview != null && trace.result_preview !== '';
  const statusText = running ? 'running' : refused ? refusalLabel : status === 'failed' ? 'failed' : 'done';
  // The glyph says which family; the tone says the outcome. Neither is
  // readable by a screen reader, so the accessible name carries both.
  const headLabel = `${label}${argsSummary ? ` ${argsSummary}` : ''}, `
    + `${TOOL_FAMILIES[family] || 'Tool'}, ${STATUS_WORDS[status] || status}`
    + `${duration ? ` in ${duration}` : ''}`;

  return (
    <Glass
      level={0}
      radius="md"
      /* The head and the body carry their own padding, exactly as the
         mockup's `.tbh` / `.tbb` do. Glass padding on top of that
         double-inset the card and stopped the body's caption strip and
         its top hairline from reaching the card's edges. */
      padding="none"
      className={`v2-tool-card v2-tool-card--${status}`}
      data-testid="tool-call-card"
      data-status={status}
      data-family={family}
    >
      <button
        type="button"
        className="v2-tool-card__head"
        onClick={() => { touched.current = true; setOpen((v) => !v); }}
        aria-expanded={open}
        aria-controls={bodyId}
        aria-label={headLabel}
      >
        <ChevronRight size={13} className={`v2-tool-card__chev${open ? ' is-open' : ''}`} aria-hidden="true" />
        <ToolGlyph status={status} family={family} />
        {/* Four grid cells, not six loose children. The head is a grid
            so a stack of calls lines up (chevron, glyph, name, meta);
            with the six pieces sitting directly in a three-column track
            they wrapped, which right-aligned every tool NAME into the
            duration column and squeezed the argument line down to two
            characters and an ellipsis. Name + argument share one cell on
            one baseline; status word + duration share the last. */}
        <span className="v2-tool-card__name">
          <span className="v2-tool-card__label">{label}</span>
          {/* Rendered even when empty: it is the spacer that keeps the
              name hard left while the meta column stays hard right. */}
          <span className="v2-tool-card__summary" title={argsSummary || undefined}>{argsSummary}</span>
        </span>
        <span className="v2-tool-card__meta">
          <span className="v2-tool-card__status-text" data-testid="tool-card-status">
            {statusText}
          </span>
          {/* Duration lives on the head so a slow call is visible without
              expanding anything. Absent (rather than "0ms") when the brain
              reported no latency and the card never saw the call start. */}
          {duration && (
            <span className="v2-tool-card__lat" data-testid="tool-card-duration">{duration}</span>
          )}
        </span>
      </button>

      <div id={bodyId} className="v2-tool-card__body" hidden={!open}>
        {open && (
          <>
            {argsText && (
              <div className="v2-tool-card__row">
                <div className="v2-tool-card__cap">args</div>
                <pre className="v2-tool-card__pre">{argsText}</pre>
              </div>
            )}
            {refused && (
              <div className="v2-tool-card__row v2-tool-card__row--refused">
                <div className="v2-tool-card__cap">refused</div>
                <pre className="v2-tool-card__pre">
                  {error || `This tool was ${refusalLabel}. It did not run.`}
                </pre>
              </div>
            )}
            {status === 'failed' && (
              <div className="v2-tool-card__row v2-tool-card__row--error">
                <div className="v2-tool-card__cap">error</div>
                <pre className="v2-tool-card__pre">{error || 'tool failed'}</pre>
              </div>
            )}
            {hasResult && status !== 'failed' && !refused && (
              <div className="v2-tool-card__row">
                <div className="v2-tool-card__cap">result</div>
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
  // Never folded into "failed": nothing ran and nothing broke.
  if (summary.refused) parts.push(`${summary.refused} refused`);
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
        {summary.status === 'running'
          ? <Loader2 size={13} aria-hidden="true" className="v2-tool-card__icon v2-spin" />
          : <GROUP_ICON size={13} aria-hidden="true" className="v2-tool-card__icon" />}
        <span className="v2-tool-group__label">
          {list.length} {label.toLowerCase()}
        </span>
        <span className="v2-tool-group__meta">{parts.join(' · ')}</span>
        {total && <span className="v2-tool-card__lat" data-testid="tool-group-duration">{total}</span>}
      </button>
      <div id={groupId} className="v2-tool-card-stack" hidden={!open}>
        {open && list.map((t, i) => (
          <ToolCallCard key={t.key || `${t.label || 'tool'}-${i}`} trace={t} grouped />
        ))}
      </div>
    </div>
  );
}
