import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import ToolCallCard, { ToolCallList } from '../../components/ToolCallCard';

describe('ToolCallCard', () => {
  it('starts collapsed; shows label + latency', () => {
    render(<ToolCallCard trace={{ key: 'k', label: 'calendar.list_events', latency_ms: 432, success: true }} />);
    expect(screen.getByText('calendar.list_events')).toBeInTheDocument();
    expect(screen.getByText('432ms')).toBeInTheDocument();
    // body is hidden until clicked
    expect(screen.queryByText('args')).toBeNull();
  });

  it('expands on click and renders args + result', () => {
    render(
      <ToolCallCard trace={{
        key: 'k',
        label: 'tool',
        args_preview: { city: 'sf' },
        result_preview: { ok: true, temp_f: 64 },
        success: true,
        latency_ms: 200,
      }} />,
    );
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText('args')).toBeInTheDocument();
    expect(screen.getByText('result')).toBeInTheDocument();
    expect(screen.getByText(/"city":"sf"/)).toBeInTheDocument();
    expect(screen.getByText(/"temp_f":64/)).toBeInTheDocument();
  });

  it('renders failure path with error preview', () => {
    render(
      <ToolCallCard trace={{ key: 'k', label: 't', success: false, error: 'rate_limit', latency_ms: 100 }} defaultOpen />,
    );
    expect(screen.getByText('error')).toBeInTheDocument();
    expect(screen.getByText('rate_limit')).toBeInTheDocument();
  });

  it('ToolCallList renders nothing for empty traces', () => {
    const { container } = render(<ToolCallList traces={[]} />);
    expect(container.innerHTML).toBe('');
  });
});
