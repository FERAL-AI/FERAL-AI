/**
 * ReasoningSection: collapsible disclosure for assistant
 * chain-of-thought tokens. Collapsed by default and styled as a
 * subordinate rail so it never competes with the answer.
 *
 * Brain emits reasoning via either:
 *   - `reasoning` field on text_response/chat_response payloads, OR
 *   - delta payloads with `kind: "reasoning"` (Anthropic extended
 *     thinking, DeepSeek R1).
 *
 * While `streaming` is true the header animates and reads "Thinking…";
 * once the turn settles it becomes "Reasoning" plus, when the caller
 * passes `durationMs`, "thought for 4.2s". The header is a real
 * `<button>` with `aria-expanded` / `aria-controls`, so it is
 * keyboard-operable and announced correctly.
 */
import React, { useId, useState } from 'react';
import { ChevronRight, Brain } from 'lucide-react';
import MarkdownMessage from '../lib/markdown.jsx';
import { formatDuration } from '../lib/toolResult';

export default function ReasoningSection({
  text,
  defaultOpen = false,
  streaming = false,
  durationMs = 0,
}) {
  const [open, setOpen] = useState(!!defaultOpen);
  const bodyId = useId();
  if (!text || typeof text !== 'string' || !text.trim()) return null;
  const duration = formatDuration(durationMs);
  return (
    <div
      className={`v2-reasoning${open ? ' is-open' : ''}${streaming ? ' is-streaming' : ''}`}
      data-testid="reasoning-section"
    >
      <button
        type="button"
        className="v2-reasoning__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={bodyId}
      >
        <ChevronRight size={13} className={`v2-reasoning__chev${open ? ' is-open' : ''}`} aria-hidden="true" />
        <Brain size={13} aria-hidden="true" />
        <span className="v2-reasoning__title">{streaming ? 'Thinking' : 'Reasoning'}</span>
        {streaming && <span className="v2-reasoning__dots" aria-hidden="true"><i /><i /><i /></span>}
        {!streaming && duration && (
          <span className="v2-reasoning__dur">thought for {duration}</span>
        )}
        <span className="v2-reasoning__hint">{open ? 'hide' : 'show'}</span>
      </button>
      <div id={bodyId} className="v2-reasoning__body" hidden={!open}>
        {open && <MarkdownMessage text={text} className="v2-md--reasoning" />}
      </div>
    </div>
  );
}
