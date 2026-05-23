/**
 * TimelineCard — inline chat card for S1 "what did I do yesterday?"
 *
 * When the orchestrator returns a timeline-style result (memory.episodes
 * + calendar + screen-loop summaries fused for a day), it emits a
 * `chat_response` whose payload includes a `timeline` field shaped as:
 *
 *   {
 *     date: "2026-05-21",
 *     sections: [
 *       { source: "chat", title: "Chat",       items: [{time, text, ref?}] },
 *       { source: "calendar", title: "Calendar", items: [...] },
 *       { source: "screen", title: "Screen activity", items: [...] },
 *       { source: "email", title: "Email", items: [...] },
 *       ...
 *     ],
 *     summary?: string,
 *   }
 *
 * Each section is collapsible with a count chip. The summary (if any)
 * is rendered as markdown above the sections.
 *
 * This is intentionally schema-strict but field-tolerant — older brains
 * may emit `entries` instead of `sections`, or omit `date`. The card
 * degrades to a flat list when sections are missing.
 */
import React, { useMemo, useState } from 'react';
import { ChevronDown, Calendar, MessageSquare, Mail, Monitor, FileText, Hash } from 'lucide-react';
import Glass from '../ui/Glass';
import MarkdownMessage from '../lib/markdown.jsx';

const SOURCE_ICON = {
  chat: MessageSquare,
  calendar: Calendar,
  email: Mail,
  screen: Monitor,
  screenloop: Monitor,
  notes: FileText,
  memory: FileText,
};

function iconFor(source) {
  const Cmp = SOURCE_ICON[(source || '').toLowerCase()] || Hash;
  return <Cmp size={13} aria-hidden="true" />;
}

function fmtTime(t) {
  if (!t) return '';
  if (typeof t === 'string' && /^\d{1,2}:\d{2}/.test(t)) return t;
  const epoch = typeof t === 'string' ? Date.parse(t) : Number(t) * (Number(t) < 1e12 ? 1000 : 1);
  if (!Number.isFinite(epoch) || epoch <= 0) return '';
  return new Date(epoch).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function fmtDate(d) {
  if (!d) return '';
  const epoch = typeof d === 'string' ? Date.parse(d) : Number(d) * (Number(d) < 1e12 ? 1000 : 1);
  if (!Number.isFinite(epoch) || epoch <= 0) return String(d);
  return new Date(epoch).toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
}

function Section({ section, defaultOpen }) {
  const items = section.items || section.entries || [];
  const [open, setOpen] = useState(defaultOpen);
  if (items.length === 0) return null;
  return (
    <div className={`v2-timeline-card__section${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="v2-timeline-card__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {iconFor(section.source)}
        <span className="v2-timeline-card__title">{section.title || section.source || 'Section'}</span>
        <span className="v2-timeline-card__count">{items.length}</span>
        <ChevronDown size={14} className={`v2-timeline-card__chev${open ? ' is-open' : ''}`} aria-hidden="true" />
      </button>
      {open && (
        <ul className="v2-timeline-card__list">
          {items.map((item, i) => (
            <li key={item.id || item.ref || i}>
              <div className="v2-timeline-card__row">
                <span className="v2-timeline-card__time">{fmtTime(item.time || item.timestamp || item.at)}</span>
                <div className="v2-timeline-card__text">
                  {item.title && <div className="v2-timeline-card__rowtitle">{item.title}</div>}
                  {item.text && <MarkdownMessage text={item.text} className="v2-md--inline" />}
                  {item.ref && (
                    <a
                      className="v2-timeline-card__ref"
                      href={item.ref}
                      target={/^https?:/i.test(item.ref) ? '_blank' : undefined}
                      rel="noopener noreferrer"
                    >
                      {item.ref_label || 'source'}
                    </a>
                  )}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function TimelineCard({ timeline }) {
  const data = useMemo(() => {
    if (!timeline || typeof timeline !== 'object') return null;
    if (Array.isArray(timeline.sections) && timeline.sections.length > 0) return timeline;
    if (Array.isArray(timeline.entries)) {
      return { ...timeline, sections: [{ source: 'memory', title: 'Events', items: timeline.entries }] };
    }
    return null;
  }, [timeline]);

  if (!data) return null;

  return (
    <Glass level={1} radius="md" padding="md" className="v2-timeline-card">
      <div className="v2-timeline-card__topbar">
        <Calendar size={14} aria-hidden="true" />
        <strong>{data.date ? fmtDate(data.date) : 'Timeline'}</strong>
      </div>
      {data.summary && (
        <div className="v2-timeline-card__summary">
          <MarkdownMessage text={data.summary} />
        </div>
      )}
      <div className="v2-timeline-card__sections">
        {data.sections.map((s, idx) => (
          <Section key={s.source || idx} section={s} defaultOpen={idx === 0} />
        ))}
      </div>
    </Glass>
  );
}
