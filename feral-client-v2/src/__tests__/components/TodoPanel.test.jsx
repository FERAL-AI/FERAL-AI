/**
 * TodoPanel + the tool-card suppression that makes it worth having.
 *
 * The brain's `feral_workflows__todo_write` is full-list replacement and
 * the model rewrites the list on nearly every step, so the panel must be
 * pinned and replaced in place, and the per-write ToolCallCard must not
 * render at all.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import TodoPanel, { normalizeTodos } from '../../components/TodoPanel';
import { isSuppressedToolCard, SUPPRESSED_TOOL_CARDS } from '../../pages/Chat';

const LIST = [
  { id: '1', content: 'read the manifest', status: 'completed' },
  { id: '2', content: 'wire the dispatch gate', status: 'in_progress' },
  { id: '3', content: 'write the tests', status: 'pending' },
];

describe('TodoPanel', () => {
  it('renders nothing when the list is empty', () => {
    const { container } = render(<TodoPanel todos={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders one row per todo with its status', () => {
    render(<TodoPanel todos={LIST} />);
    const items = screen.getAllByTestId('todo-item');
    expect(items).toHaveLength(3);
    expect(screen.getByText('wire the dispatch gate')).toBeInTheDocument();
    expect(
      items.find((el) => el.textContent.includes('wire the dispatch gate')),
    ).toHaveAttribute('data-status', 'in_progress');
  });

  it('sorts the in-progress item to the top', () => {
    render(<TodoPanel todos={LIST} />);
    const first = screen.getAllByTestId('todo-item')[0];
    expect(first).toHaveAttribute('data-status', 'in_progress');
  });

  it('shows a completed/total counter', () => {
    render(<TodoPanel todos={LIST} />);
    expect(screen.getByTestId('todo-panel-count')).toHaveTextContent('1/3');
  });

  it('collapses to the current task', () => {
    render(<TodoPanel todos={LIST} />);
    fireEvent.click(screen.getByRole('button', { name: /tasks/i }));
    expect(screen.queryAllByTestId('todo-item')).toHaveLength(0);
    expect(screen.getByText('wire the dispatch gate')).toBeInTheDocument();
  });

  it('drops malformed rows instead of rendering blanks', () => {
    // Non-objects are dropped before the index-based id fallback is
    // applied, so the surviving row is index 1 of the object-only list.
    expect(normalizeTodos([{ content: '' }, null, 'nope', { content: 'ok' }]))
      .toEqual([{ id: 'todo-1', content: 'ok', status: 'pending' }]);
  });

  it('coerces an unknown status to pending rather than dropping the row', () => {
    expect(normalizeTodos([{ id: 'x', content: 'a', status: 'blocked' }]))
      .toEqual([{ id: 'x', content: 'a', status: 'pending' }]);
  });

  it('replaces the whole list, matching the brain contract', () => {
    const { rerender } = render(<TodoPanel todos={LIST} />);
    expect(screen.getAllByTestId('todo-item')).toHaveLength(3);
    rerender(<TodoPanel todos={[{ id: '9', content: 'only this', status: 'pending' }]} />);
    const items = screen.getAllByTestId('todo-item');
    expect(items).toHaveLength(1);
    expect(items[0]).toHaveTextContent('only this');
  });
});

describe('tool-card suppression', () => {
  it('suppresses the todo-write card by fully qualified tool name', () => {
    expect(isSuppressedToolCard({ tool: 'feral_workflows__todo_write' })).toBe(true);
  });

  it('suppresses it from the skill_id/endpoint_id fields too', () => {
    expect(isSuppressedToolCard({
      skill_id: 'feral_workflows',
      endpoint_id: 'todo_write',
    })).toBe(true);
  });

  it('leaves sibling workflow tools alone', () => {
    expect(isSuppressedToolCard({ tool: 'feral_workflows__create' })).toBe(false);
    expect(isSuppressedToolCard({ tool: 'coding_tools__read_file' })).toBe(false);
    expect(isSuppressedToolCard({})).toBe(false);
  });

  it('suppresses exactly one tool, so nothing else silently vanishes', () => {
    expect([...SUPPRESSED_TOOL_CARDS]).toEqual(['feral_workflows__todo_write']);
  });
});
