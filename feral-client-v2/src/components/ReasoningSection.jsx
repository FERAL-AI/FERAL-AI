/**
 * ReasoningSection — collapsible disclosure for assistant chain-of-thought
 * tokens. Collapsed by default per S1 chat polish requirement.
 *
 * Brain emits reasoning via either:
 *   - `reasoning` field on text_response/chat_response payloads, OR
 *   - delta payloads with `kind: "reasoning"` (rare, used by Anthropic
 *     extended thinking + DeepSeek R1).
 *
 * The chat aggregator concatenates everything into a `reasoning` string
 * on the assistant message; this component renders it.
 */
import React, { useState } from 'react';
import { ChevronRight, Brain } from 'lucide-react';
import MarkdownMessage from '../lib/markdown.jsx';

export default function ReasoningSection({ text, defaultOpen = false }) {
  const [open, setOpen] = useState(!!defaultOpen);
  if (!text || typeof text !== 'string' || !text.trim()) return null;
  return (
    <div className={`v2-reasoning${open ? ' is-open' : ''}`} data-testid="reasoning-section">
      <button
        type="button"
        className="v2-reasoning__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <ChevronRight size={13} className={`v2-reasoning__chev${open ? ' is-open' : ''}`} aria-hidden="true" />
        <Brain size={13} aria-hidden="true" />
        <span>Reasoning</span>
        <span className="v2-reasoning__hint">{open ? 'hide' : 'show'}</span>
      </button>
      {open && (
        <div className="v2-reasoning__body">
          <MarkdownMessage text={text} className="v2-md--reasoning" />
        </div>
      )}
    </div>
  );
}
