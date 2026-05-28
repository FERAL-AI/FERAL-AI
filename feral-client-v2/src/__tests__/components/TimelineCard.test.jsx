import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import TimelineCard from '../../components/TimelineCard';

describe('TimelineCard (S1)', () => {
  const sample = {
    date: '2026-05-21',
    summary: 'Quiet day, lots of memory writes.',
    sections: [
      { source: 'chat', title: 'Chat', items: [{ time: '09:00', text: 'said hi' }] },
      { source: 'calendar', title: 'Calendar', items: [{ time: '10:00', title: '1:1 with Mahmoud' }] },
    ],
  };

  it('renders header + summary + section counts', () => {
    render(<TimelineCard timeline={sample} />);
    expect(screen.getByText(/Quiet day/)).toBeInTheDocument();
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(screen.getByText('Calendar')).toBeInTheDocument();
    // counts
    const counts = screen.getAllByText('1');
    expect(counts.length).toBeGreaterThan(0);
  });

  it('first section is expanded by default; others collapse/expand', () => {
    render(<TimelineCard timeline={sample} />);
    expect(screen.getByText(/said hi/)).toBeInTheDocument();
    expect(screen.queryByText('1:1 with Mahmoud')).toBeNull();
    fireEvent.click(screen.getByText('Calendar'));
    expect(screen.getByText('1:1 with Mahmoud')).toBeInTheDocument();
  });

  it('returns null for malformed input', () => {
    const { container } = render(<TimelineCard timeline={null} />);
    expect(container.innerHTML).toBe('');
  });

  it('groups entries[] by source when sections are missing', () => {
    render(<TimelineCard timeline={{ entries: [{ source: 'memory', time: '08:00', text: 'one' }] }} />);
    expect(screen.getByText('Memory')).toBeInTheDocument();
    expect(screen.getByText('one')).toBeInTheDocument();
  });
});

describe('TimelineCard — canonical WS payload (cut-list #8 / S1)', () => {
  const wsPayload = {
    query: 'what did I do yesterday?',
    window: { from: '2026-05-26T00:00:00', to: '2026-05-27T00:00:00', label: 'yesterday' },
    summary: '',
    entries: [
      {
        source: 'episode', type: 'episode',
        timestamp: 1716700000,
        title: 'standup',
        content: 'said 9am works',
        metadata: { id: 'ep-1' },
      },
      {
        source: 'note', type: 'note',
        timestamp: 1716710000,
        title: 'pick up groceries',
        content: 'milk, eggs',
        metadata: { id: 'note-1' },
      },
      {
        source: 'episode', type: 'episode',
        timestamp: 1716720000,
        title: 'afternoon recap',
        content: 'shipped timeline closer',
        metadata: { id: 'ep-2' },
      },
    ],
    sources_queried: ['episode', 'note', 'calendar', 'health', 'screen_loop'],
    degraded_sources: [
      { source: 'calendar', reason: 'no_token' },
      { source: 'screen_loop', reason: 'no_query_api' },
    ],
  };

  it('renders entries grouped by source with chronological order within group', () => {
    render(<TimelineCard timeline={wsPayload} />);
    // Group titles
    expect(screen.getByText('Chat')).toBeInTheDocument();   // episode → Chat
    expect(screen.getByText('Notes')).toBeInTheDocument();  // note → Notes

    // First group (Chat) is open by default — entries in chronological order
    const standup = screen.getByText('standup');
    const recap = screen.getByText('afternoon recap');
    expect(standup.compareDocumentPosition(recap) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('shows degraded-source chip with humanized reason', () => {
    render(<TimelineCard timeline={wsPayload} />);
    const chips = screen.getAllByTestId('timeline-degraded-chip');
    expect(chips.length).toBe(2);
    expect(chips[0].textContent).toMatch(/Calendar unavailable/i);
    expect(chips[0].textContent).toMatch(/no token configured/i);
    expect(chips[1].textContent).toMatch(/Screen activity unavailable/i);
  });

  it('hides summary section when summary is empty', () => {
    render(<TimelineCard timeline={wsPayload} />);
    // No paragraph with summary text — only the topbar + chips + sections.
    expect(screen.queryByText(/Quiet day/i)).toBeNull();
  });

  it('renders the window label as the topbar title', () => {
    render(<TimelineCard timeline={wsPayload} />);
    expect(screen.getByText('Yesterday')).toBeInTheDocument();
  });

  it('renders only degraded chips when entries[] is empty', () => {
    render(<TimelineCard timeline={{
      query: 'q',
      window: { from: '', to: '', label: 'today' },
      entries: [],
      summary: '',
      sources_queried: ['episode'],
      degraded_sources: [{ source: 'episode', reason: 'no_memory' }],
    }} />);
    expect(screen.getByText(/Chat unavailable/i)).toBeInTheDocument();
  });
});
