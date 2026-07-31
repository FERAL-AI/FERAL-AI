import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import ToolCallCard, { ToolCallList } from '../../components/ToolCallCard';

describe('ToolCallCard', () => {
  it('starts collapsed; shows label + latency', () => {
    render(<ToolCallCard trace={{ key: 'k', label: 'calendar.list_events', latency_ms: 432, success: true }} />);
    expect(screen.getByText('calendar.list_events')).toBeInTheDocument();
    expect(screen.getByText('432ms')).toBeInTheDocument();
    // body is hidden until clicked
    expect(screen.queryByText('args')).toBeNull();
    expect(screen.getByRole('button')).toHaveAttribute('aria-expanded', 'false');
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
    fireEvent.click(screen.getByRole('button', { name: /tool/i }));
    expect(screen.getByText('args')).toBeInTheDocument();
    expect(screen.getByText('result')).toBeInTheDocument();
    // Args + JSON results are pretty-printed (2-space indent), not
    // dumped as a single minified line.
    expect(screen.getByText(/"city": "sf"/)).toBeInTheDocument();
    expect(screen.getByText(/"temp_f": 64/)).toBeInTheDocument();
  });

  it('renders failure path with error preview and a failed status', () => {
    render(
      <ToolCallCard trace={{ key: 'k', label: 't', success: false, error: 'rate_limit', latency_ms: 100 }} defaultOpen />,
    );
    expect(screen.getByText('error')).toBeInTheDocument();
    expect(screen.getByText('rate_limit')).toBeInTheDocument();
    expect(screen.getByTestId('tool-call-card')).toHaveAttribute('data-status', 'failed');
    expect(screen.getByText('failed')).toBeInTheDocument();
  });

  it('shows a running state with no latency until the result lands', () => {
    render(
      <ToolCallCard trace={{ key: 'k', label: 'Run local command', success: null, args_preview: { command: 'pytest -q' } }} />,
    );
    const card = screen.getByTestId('tool-call-card');
    expect(card).toHaveAttribute('data-status', 'running');
    expect(screen.getByText('running')).toBeInTheDocument();
    // The compact head summarises args on one line.
    expect(screen.getByText('pytest -q')).toBeInTheDocument();
  });

  it('summarises the head from the most meaningful arg', () => {
    render(
      <ToolCallCard trace={{ key: 'k', label: 'Read file', success: true, args_preview: '{"path":"src/App.jsx","limit":40}' }} />,
    );
    expect(screen.getByText('src/App.jsx')).toBeInTheDocument();
  });

  it('exposes an aria-controls disclosure contract', () => {
    render(<ToolCallCard trace={{ key: 'k', label: 'tool', success: true }} />);
    const head = screen.getByRole('button');
    const controls = head.getAttribute('aria-controls');
    expect(controls).toBeTruthy();
    fireEvent.click(head);
    expect(head).toHaveAttribute('aria-expanded', 'true');
    expect(document.getElementById(controls)).toBeTruthy();
  });
});

describe('ToolCallList', () => {
  it('renders nothing for empty traces', () => {
    const { container } = render(<ToolCallList traces={[]} />);
    expect(container.innerHTML).toBe('');
  });

  it('renders a single call bare (no group chrome)', () => {
    render(<ToolCallList traces={[{ key: 'a', label: 'Search web', success: true, latency_ms: 12 }]} />);
    expect(screen.getByTestId('tool-call-list')).toHaveClass('v2-tool-card-stack');
    expect(screen.getAllByTestId('tool-call-card')).toHaveLength(1);
  });

  it('groups parallel calls under one collapsible header', () => {
    render(
      <ToolCallList traces={[
        { key: 'a', label: 'Search web', success: true, latency_ms: 100 },
        { key: 'b', label: 'Read file', success: false, error: 'ENOENT', latency_ms: 20 },
        { key: 'c', label: 'Run local command', success: null },
      ]} />,
    );
    const group = screen.getByTestId('tool-call-list');
    expect(group).toHaveAttribute('data-status', 'running');
    expect(within(group).getByText('3 tool calls')).toBeInTheDocument();
    expect(within(group).getByText(/1 running/)).toBeInTheDocument();
    expect(within(group).getByText(/1 succeeded/)).toBeInTheDocument();
    expect(within(group).getByText(/1 failed/)).toBeInTheDocument();
    expect(screen.getAllByTestId('tool-call-card')).toHaveLength(3);

    // Group header collapses the whole fan-out.
    fireEvent.click(within(group).getByRole('button', { name: /3 tool calls/i }));
    expect(screen.queryAllByTestId('tool-call-card')).toHaveLength(0);
  });

  it('reports failed as the group status when nothing is still running', () => {
    render(
      <ToolCallList traces={[
        { key: 'a', label: 'Search web', success: true, latency_ms: 100 },
        { key: 'b', label: 'Read file', success: false, error: 'ENOENT', latency_ms: 20 },
      ]} />,
    );
    expect(screen.getByTestId('tool-call-list')).toHaveAttribute('data-status', 'failed');
  });
});
