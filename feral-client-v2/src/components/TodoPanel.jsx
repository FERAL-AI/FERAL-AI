/**
 * TodoPanel renders the agent's own task list for the active thread.
 *
 * Pinned, not appended. The brain's `feral_workflows__todo_write` is a
 * FULL-LIST REPLACEMENT endpoint and the model rewrites the whole list
 * on nearly every step, so rendering a ToolCallCard per write would bury
 * the conversation under near-identical cards. Chat.jsx therefore
 * suppresses the tool card for that one tool name and feeds the latest
 * `todo_update` frame here instead, where each write replaces the panel
 * in place.
 *
 * Frame shape (agents/orchestrator.py::_maybe_emit_todo_frame):
 *   { session_id, todos: [{id, content, status}], counts: {...} }
 *
 * `status` is one of pending | in_progress | completed, and the brain
 * guarantees at most one `in_progress` item (rejecting writes with more),
 * so the panel can treat it as "the current focus" rather than as a set.
 */
import React, { useMemo, useState } from 'react';
import { ChevronRight, Circle, CircleDot, CheckCircle2 } from 'lucide-react';
import Glass from '../ui/Glass';

const STATUS_ORDER = { in_progress: 0, pending: 1, completed: 2 };

function StatusIcon({ status }) {
  if (status === 'completed') {
    return (
      <CheckCircle2
        size={13}
        aria-hidden="true"
        className="v2-todo__icon v2-todo__icon--done"
      />
    );
  }
  if (status === 'in_progress') {
    return (
      <CircleDot
        size={13}
        aria-hidden="true"
        className="v2-todo__icon v2-todo__icon--active"
      />
    );
  }
  return (
    <Circle
      size={13}
      aria-hidden="true"
      className="v2-todo__icon v2-todo__icon--pending"
    />
  );
}

export function normalizeTodos(raw) {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((t) => t && typeof t === 'object')
    .map((t, i) => ({
      id: String(t.id || `todo-${i}`),
      content: String(t.content || ''),
      status: ['pending', 'in_progress', 'completed'].includes(t.status)
        ? t.status
        : 'pending',
    }))
    .filter((t) => t.content);
}

export default function TodoPanel({ todos, defaultOpen = true }) {
  const list = useMemo(() => normalizeTodos(todos), [todos]);
  const [open, setOpen] = useState(!!defaultOpen);

  // Sorted for reading, not reordered in the store: the brain owns the
  // canonical order and the panel must not teach the user an order the
  // model does not see.
  const ordered = useMemo(
    () => [...list].sort(
      (a, b) => (STATUS_ORDER[a.status] ?? 1) - (STATUS_ORDER[b.status] ?? 1),
    ),
    [list],
  );

  if (list.length === 0) return null;

  const done = list.filter((t) => t.status === 'completed').length;
  const current = list.find((t) => t.status === 'in_progress');

  return (
    <Glass
      level={0}
      radius="md"
      padding="sm"
      className="v2-todo-panel"
      data-testid="todo-panel"
    >
      <button
        type="button"
        className="v2-todo-panel__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <ChevronRight
          size={13}
          className={`v2-tool-card__chev${open ? ' is-open' : ''}`}
          aria-hidden="true"
        />
        <span className="v2-todo-panel__title">Tasks</span>
        <span className="v2-todo-panel__meta" data-testid="todo-panel-count">
          {done}/{list.length}
        </span>
        {!open && current && (
          <span className="v2-todo-panel__now" title={current.content}>
            {current.content}
          </span>
        )}
      </button>

      {open && (
        <ul className="v2-todo-panel__list">
          {ordered.map((todo) => (
            <li
              key={todo.id}
              className={`v2-todo v2-todo--${todo.status}`}
              data-status={todo.status}
              data-testid="todo-item"
            >
              <StatusIcon status={todo.status} />
              <span className="v2-todo__text">{todo.content}</span>
            </li>
          ))}
        </ul>
      )}
    </Glass>
  );
}
