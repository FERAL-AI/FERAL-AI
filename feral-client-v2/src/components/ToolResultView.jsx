/**
 * ToolResultView renders a tool result according to its detected
 * shape instead of dumping every payload into one `<pre>`.
 *
 *   file read / diff  → fenced code block (highlighted, copyable)
 *   row list          → real <table> inside a scroll container
 *   image / data URI  → inline <img>, size-capped
 *   JSON body         → pretty-printed, copyable
 *   long stdout       → clamped with an explicit "Show all N lines"
 *
 * Everything renders inside `.v2-toolres` which owns its own scroll,
 * so a 4000-line stdout never stretches the chat column and never
 * introduces horizontal page scroll.
 *
 * Classification lives in `lib/toolResult.js` (pure, unit tested).
 */
import React, { useMemo, useState } from 'react';
import { ChevronDown } from 'lucide-react';
import MarkdownMessage from '../lib/markdown.jsx';
import CopyButton from '../ui/CopyButton';
import { cellText, detectResultShape, formatJson } from '../lib/toolResult';

const DEFAULT_CLAMP_LINES = 16;

/** Pick a fence long enough that the payload can't terminate it early. */
function fenceFor(text) {
  const runs = String(text).match(/`{3,}/g) || [];
  const longest = runs.reduce((n, r) => Math.max(n, r.length), 2);
  return '`'.repeat(Math.max(3, longest + 1));
}

function ClampedPre({ text, clampLines, testId }) {
  const [expanded, setExpanded] = useState(false);
  const lines = useMemo(() => String(text).split('\n'), [text]);
  const overflows = lines.length > clampLines;
  const shown = expanded || !overflows ? text : lines.slice(0, clampLines).join('\n');
  return (
    <>
      <pre className="v2-toolres__pre" data-testid={testId}>{shown}</pre>
      {overflows && (
        <button
          type="button"
          className="v2-toolres__more"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          <ChevronDown size={12} aria-hidden="true" className={expanded ? 'is-open' : ''} />
          {expanded ? 'Show less' : `Show all ${lines.length} lines`}
        </button>
      )}
    </>
  );
}

function ResultTable({ table }) {
  const { columns, rows } = table;
  return (
    <div className="v2-toolres__table-scroll">
      <table className="v2-md-table v2-toolres__table">
        <thead>
          <tr>
            {columns.map((c) => <th key={c} scope="col">{c}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            // Row objects have no stable id; index is the only key available.
            // eslint-disable-next-line react/no-array-index-key
            <tr key={i}>
              {columns.map((c) => <td key={c}>{cellText(row[c])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ToolResultView({
  value,
  language = '',
  clampLines = DEFAULT_CLAMP_LINES,
  copyable = true,
  className = '',
}) {
  const shape = useMemo(
    () => detectResultShape(value, { language }),
    [value, language],
  );

  const copyText = useMemo(() => {
    if (shape.kind === 'empty') return '';
    if (shape.kind === 'table') return formatJson(shape.data.rows);
    if (shape.kind === 'image') return shape.data;
    if (typeof shape.data === 'string') return shape.data;
    return formatJson(shape.data);
  }, [shape]);

  if (shape.kind === 'empty') return null;

  const wrap = (children, extra = '') => (
    <div className={`v2-toolres v2-toolres--${shape.kind}${extra}${className ? ` ${className}` : ''}`} data-shape={shape.kind}>
      {copyable && copyText && (
        <div className="v2-toolres__actions">
          <CopyButton value={copyText} label="Copy result" />
        </div>
      )}
      {children}
    </div>
  );

  if (shape.kind === 'image') {
    return wrap(
      <img
        className="v2-toolres__img"
        src={shape.data}
        alt="Tool result"
        loading="lazy"
      />,
    );
  }

  if (shape.kind === 'table') {
    const { rows } = shape.data;
    return wrap(
      <>
        <ResultTable table={shape.data} />
        <div className="v2-toolres__meta">
          {rows.length} row{rows.length === 1 ? '' : 's'}
          {shape.envelope ? ` · ${shape.envelope}` : ''}
        </div>
      </>,
    );
  }

  if (shape.kind === 'code') {
    const fence = fenceFor(shape.data);
    return wrap(
      <MarkdownMessage
        className="v2-md--toolres"
        text={`${fence}${shape.language || ''}\n${shape.data}\n${fence}`}
      />,
    );
  }

  if (shape.kind === 'markdown') {
    return wrap(<MarkdownMessage className="v2-md--toolres" text={shape.data} />);
  }

  // json | text: both land in a clamped, scrollable <pre>.
  const text = shape.kind === 'json' ? formatJson(shape.data) : String(shape.data);
  return wrap(<ClampedPre text={text} clampLines={clampLines} testId="toolres-pre" />);
}
