/**
 * TimelineCard — inline chat card for S1 "what did I do yesterday?"
 *
 * v2026.5.43 (S1 closer): now accepts both legacy and canonical input
 * shapes.
 *
 * Canonical shape — sent by the brain's ``timeline`` WS frame
 * (``models.protocol.TimelinePayload``), the contract pinned by
 * cut-list item #8:
 *
 *   {
 *     query: "what did I do yesterday?",
 *     window: { from: iso, to: iso, label: "yesterday" },
 *     entries: [
 *       { type, source, timestamp, title, content, metadata },
 *       ...
 *     ],
 *     summary?: string,
 *     sources_queried: ["episode", "note", "calendar", ...],
 *     degraded_sources: [
 *       { source: "calendar", reason: "no_token" },
 *       { source: "screen_loop", reason: "no_query_api" }
 *     ],
 *   }
 *
 * Legacy shape — older brains (Wave 3 Lane 12) and the chat_response
 * inline-timeline path used:
 *
 *   {
 *     date: "2026-05-21",
 *     sections: [ { source, title, items: [...] } ],
 *     summary?: string,
 *   }
 *
 * Both shapes are accepted. ``entries`` is auto-grouped by ``source``
 * into collapsible sections; ``degraded_sources`` renders as small
 * chips above the sections so the user knows which surfaces couldn't
 * be queried (e.g. "Calendar unavailable: no token").
 */
import React, { useMemo, useState } from 'react';
import { ChevronDown, Calendar, MessageSquare, Mail, Monitor, FileText, Hash, Heart, Brain, AlertCircle } from 'lucide-react';
import Glass from '../ui/Glass';
import MarkdownMessage from '../lib/markdown.jsx';

const SOURCE_ICON = {
  chat: MessageSquare,
  episode: MessageSquare,
  calendar: Calendar,
  event: Calendar,
  email: Mail,
  screen: Monitor,
  screen_loop: Monitor,
  screenloop: Monitor,
  notes: FileText,
  note: FileText,
  memory: FileText,
  knowledge: Brain,
  health: Heart,
};

const SOURCE_TITLE = {
  episode: 'Chat',
  chat: 'Chat',
  note: 'Notes',
  notes: 'Notes',
  memory: 'Memory',
  knowledge: 'Knowledge',
  calendar: 'Calendar',
  event: 'Calendar',
  health: 'Health',
  screen: 'Screen activity',
  screen_loop: 'Screen activity',
  screenloop: 'Screen activity',
  email: 'Email',
};

function humanizeReason(reason) {
  if (!reason) return 'unavailable';
  const r = String(reason).toLowerCase();
  if (r === 'no_token') return 'no token configured';
  if (r === 'no_provider') return 'no provider configured';
  if (r === 'no_memory') return 'memory unavailable';
  if (r === 'no_kg_query') return 'knowledge query unavailable';
  if (r === 'no_query_api') return 'query API not implemented';
  if (r.includes('_failed')) return r.replace(/_/g, ' ');
  return r.replace(/_/g, ' ');
}

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

function groupEntriesBySource(entries) {
  const grouped = new Map();
  for (const e of entries) {
    const src = String(e.source || e.type || 'memory').toLowerCase();
    if (!grouped.has(src)) grouped.set(src, []);
    grouped.get(src).push({
      time: e.timestamp || e.time || e.at,
      title: e.title,
      text: e.content || e.text,
      ref: e.metadata?.html_link || e.ref,
      ref_label: e.ref_label,
      id: e.metadata?.id || e.id,
    });
  }
  const out = [];
  for (const [source, items] of grouped.entries()) {
    items.sort((a, b) => (Number(a.time) || 0) - (Number(b.time) || 0));
    out.push({
      source,
      title: SOURCE_TITLE[source] || source.charAt(0).toUpperCase() + source.slice(1),
      items,
    });
  }
  return out;
}

function topbarLabel(data) {
  if (data.window?.label) {
    const cap = data.window.label.charAt(0).toUpperCase() + data.window.label.slice(1).replace(/_/g, ' ');
    return cap;
  }
  if (data.date) return fmtDate(data.date);
  return 'Timeline';
}

export default function TimelineCard({ timeline }) {
  const data = useMemo(() => {
    if (!timeline || typeof timeline !== 'object') return null;
    // Canonical: entries[] + window{} — group by source on the fly.
    if (Array.isArray(timeline.entries) && timeline.entries.length > 0
        && !Array.isArray(timeline.sections)) {
      return { ...timeline, sections: groupEntriesBySource(timeline.entries) };
    }
    if (Array.isArray(timeline.sections) && timeline.sections.length > 0) return timeline;
    // Empty entries[] with degraded chips is still a valid "I tried
    // but everything was off" card — render the chips, no sections.
    if (Array.isArray(timeline.entries)) {
      return { ...timeline, sections: [] };
    }
    return null;
  }, [timeline]);

  if (!data) return null;
  const degraded = Array.isArray(data.degraded_sources) ? data.degraded_sources : [];

  return (
    <Glass level={1} radius="md" padding="md" className="v2-timeline-card" data-testid="timeline-card">
      <div className="v2-timeline-card__topbar">
        <Calendar size={14} aria-hidden="true" />
        <strong>{topbarLabel(data)}</strong>
      </div>
      {data.summary && (
        <div className="v2-timeline-card__summary">
          <MarkdownMessage text={data.summary} />
        </div>
      )}
      {degraded.length > 0 && (
        <div className="v2-timeline-card__degraded" role="status" aria-live="polite">
          {degraded.map((d, i) => {
            const src = String(d.source || 'unknown');
            const label = SOURCE_TITLE[src] || src.charAt(0).toUpperCase() + src.slice(1);
            return (
              <span key={`${src}-${i}`} className="v2-timeline-card__chip" data-testid="timeline-degraded-chip">
                <AlertCircle size={11} aria-hidden="true" />
                {label} unavailable: {humanizeReason(d.reason)}
              </span>
            );
          })}
        </div>
      )}
      {data.sections.length > 0 && (
        <div className="v2-timeline-card__sections">
          {data.sections.map((s, idx) => (
            <Section key={s.source || idx} section={s} defaultOpen={idx === 0} />
          ))}
        </div>
      )}
    </Glass>
  );
}
