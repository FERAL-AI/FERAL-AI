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

  it('degrades to a single Events section when only entries[] provided', () => {
    render(<TimelineCard timeline={{ entries: [{ time: '08:00', text: 'one' }] }} />);
    expect(screen.getByText('Events')).toBeInTheDocument();
    expect(screen.getByText('one')).toBeInTheDocument();
  });
});
