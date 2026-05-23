/**
 * ToolCallCard — collapsible card that renders a single tool/skill
 * invocation: name, args preview, result preview, duration.
 *
 * The chat used to display tool traces as a one-line "used N tools"
 * chip with no details. This card surfaces the same data the brain
 * already emits (tool_start / tool_call / tool_result frames) in a
 * shape developers and power-users can actually inspect.
 *
 * Trace shape (matches Chat.jsx tool-state assembly):
 *   {
 *     key: string,
 *     label: string,        // friendlyToolLabel(payload)
 *     args_preview: string, // "{ city: 'sf' }" or short string repr
 *     result_preview?: string,
 *     success: boolean | null,
 *     error: string,
 *     latency_ms: number,
 *   }
 */
import React, { useState } from 'react';
import { ChevronRight, CheckCircle2, XCircle, Loader2 } from 'lucide-react';
import Glass from '../ui/Glass';

function statusIcon(success) {
  if (success === true) {
    return <CheckCircle2 size={13} aria-hidden="true" style={{ color: 'var(--v2-state-live)' }} />;
  }
  if (success === false) {
    return <XCircle size={13} aria-hidden="true" style={{ color: 'var(--v2-state-error)' }} />;
  }
  return <Loader2 size={13} aria-hidden="true" className="v2-spin" />;
}

function formatLatency(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n <= 0) return '';
  if (n < 1000) return `${Math.round(n)}ms`;
  return `${(n / 1000).toFixed(2)}s`;
}

function summarisePreview(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  try {
    const s = JSON.stringify(value, null, 0);
    return s.length > 400 ? `${s.slice(0, 400)}…` : s;
  } catch {
    return String(value);
  }
}

export default function ToolCallCard({ trace, defaultOpen = false }) {
  const [open, setOpen] = useState(!!defaultOpen);
  if (!trace) return null;
  const label = trace.label || trace.tool || trace.name || 'tool';
  const argsPreview = summarisePreview(trace.args_preview);
  const resultPreview = summarisePreview(trace.result_preview);
  const latency = formatLatency(trace.latency_ms);
  const error = trace.error || '';

  return (
    <Glass level={0} radius="md" padding="sm" className="v2-tool-card">
      <button
        type="button"
        className="v2-tool-card__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <ChevronRight size={13} className={`v2-tool-card__chev${open ? ' is-open' : ''}`} aria-hidden="true" />
        {statusIcon(trace.success)}
        <span className="v2-tool-card__label">{label}</span>
        {latency && <span className="v2-tool-card__lat">{latency}</span>}
      </button>
      {open && (
        <div className="v2-tool-card__body">
          {argsPreview && (
            <div className="v2-tool-card__row">
              <div className="v2-tool-card__row-label">args</div>
              <pre className="v2-tool-card__pre">{argsPreview}</pre>
            </div>
          )}
          {(resultPreview && trace.success !== false) && (
            <div className="v2-tool-card__row">
              <div className="v2-tool-card__row-label">result</div>
              <pre className="v2-tool-card__pre">{resultPreview}</pre>
            </div>
          )}
          {trace.success === false && (
            <div className="v2-tool-card__row v2-tool-card__row--error">
              <div className="v2-tool-card__row-label">error</div>
              <pre className="v2-tool-card__pre">{error || 'tool failed'}</pre>
            </div>
          )}
        </div>
      )}
    </Glass>
  );
}

export function ToolCallList({ traces }) {
  if (!Array.isArray(traces) || traces.length === 0) return null;
  return (
    <div className="v2-tool-card-stack">
      {traces.map((t) => (
        <ToolCallCard key={t.key || t.label} trace={t} />
      ))}
    </div>
  );
}
