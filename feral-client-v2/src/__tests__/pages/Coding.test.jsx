import React from 'react';
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import Coding, { PermissionDetails, observedActions } from '../../pages/Coding';
import { apiJson } from '../../lib/api';

vi.mock('../../lib/api', () => ({ apiJson: vi.fn() }));
const ready = { agents: [{ agent_id: 'opencode', available: true }], workspaces: ['/tmp/project'], provider: { model: 'qwen3:4b', base_url: 'http://localhost:11434/v1', prepared: true }, live_sessions: [] };
beforeEach(() => { apiJson.mockReset(); });
afterEach(cleanup);

describe('Coding workspace', () => {
  it('renders the real proposed diff and shell command before approval', () => {
    const diff = { path: '/tmp/project/hello.txt', oldText: 'old content', newText: 'ferrous', type: 'diff' };
    const { rerender } = render(<PermissionDetails permission={{ raw: { toolCall: { rawInput: { diff: '-old content\n+ferrous' }, content: [diff] } } }} />);
    expect(screen.getByText('Before')).toBeInTheDocument();
    expect(screen.getByText('old content', { exact: true })).toBeInTheDocument();
    expect(screen.getByText('After')).toBeInTheDocument();
    expect(screen.getByText('ferrous', { exact: true })).toBeInTheDocument();
    rerender(<PermissionDetails permission={{ details: { rawInput: { command: '/usr/bin/python3 test.py' } } }} />);
    expect(screen.getByText('Command to run')).toBeInTheDocument();
    expect(screen.getByText('/usr/bin/python3 test.py')).toBeInTheDocument();
  });

  it('does not treat a narrated test claim as observed execution', async () => {
    apiJson.mockImplementation(async url => url === '/api/coding' ? ready : { status: 'completed', session_handle: 'handle', text: 'Tests passed: CHECK_PASSED', tool_calls: [], pending_permissions: [] });
    render(<Coding />);
    await screen.findByText('OpenCode · Ready');
    fireEvent.change(screen.getByLabelText('What should we build or fix?'), { target: { value: 'Test project' } });
    fireEvent.click(screen.getByRole('button', { name: 'Start task' }));
    await screen.findByText('Agent finished');
    expect(screen.getByText(/0 completed tool actions observed · No command execution observed/)).toBeInTheDocument();
    expect(screen.getByText('Agent message')).toBeInTheDocument();
    expect(screen.queryByText('Observed tool results')).not.toBeInTheDocument();
  });

  it('deduplicates observed tool updates and preserves execute classification', () => {
    const actions = observedActions({ tool_calls: [{ tool_call_id: 'run', action_kind: 'execute', status: 'pending' }, { tool_call_id: 'run', action_kind: '', status: 'completed', text: 'CHECK_PASSED' }] });
    expect(actions).toHaveLength(1);
    expect(actions[0]).toMatchObject({ action_kind: 'execute', status: 'completed', text: 'CHECK_PASSED' });
  });
  it('blocks a task until explicit model setup and folder grant exist', async () => {
    apiJson.mockResolvedValue({ agents: [{ agent_id: 'opencode', available: true }], workspaces: [], provider: null });
    render(<Coding />);
    await screen.findByText('OpenCode · Ready');
    fireEvent.change(screen.getByLabelText('What should we build or fix?'), { target: { value: 'Edit it' } });
    expect(screen.getByRole('button', { name: 'Start task' })).toBeDisabled();
    expect(screen.getByText(/Chat provider credentials are not shared/)).toBeInTheDocument();
  });

  it('starts through REST and answers only this action, then closes on stop', async () => {
    const waiting = { status: 'awaiting_permission', session_handle: 'handle', text: 'Ready to edit', tool_calls: [], pending_permissions: [{ request_id: 'permission', title: 'Write hello.txt', options: [{ kind: 'allow_once' }, { kind: 'reject_once' }, { kind: 'allow_always' }] }] };
    apiJson.mockImplementation(async (url, opts) => {
      if (url === '/api/coding') return ready;
      if (url === '/api/coding/tasks') return waiting;
      if (url === '/api/coding/permissions/permission') return { ...waiting, status: 'running', pending_permissions: [] };
      if (url === '/api/coding/sessions/handle/cancel') return { closed: true };
      throw new Error(`Unexpected ${url}`);
    });
    render(<Coding />);
    await screen.findByText('OpenCode · Ready');
    fireEvent.change(screen.getByLabelText('What should we build or fix?'), { target: { value: 'Create hello.txt' } });
    fireEvent.click(screen.getByRole('button', { name: 'Start task' }));
    await screen.findByText('Write hello.txt');
    expect(screen.getByLabelText('Project folder')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Choose folder' })).toBeDisabled();
    expect(screen.queryByText(/always/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Allow this action' }));
    await waitFor(() => expect(apiJson).toHaveBeenCalledWith('/api/coding/permissions/permission', expect.objectContaining({ body: JSON.stringify({ decision: 'allow_once' }) })));
    await waitFor(() => expect(screen.queryByText('Approval needed')).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Stop task' }));
    await screen.findByText('Task · interrupted');
    expect(apiJson).toHaveBeenCalledWith('/api/coding/sessions/handle/cancel', expect.anything());
  });

  it('shows backend failures and never invents a running task', async () => {
    apiJson.mockImplementation(async url => {
      if (url === '/api/coding') return ready;
      throw new Error('Ollama model was not found');
    });
    render(<Coding />);
    await screen.findByText('OpenCode · Ready');
    fireEvent.change(screen.getByLabelText('What should we build or fix?'), { target: { value: 'Fix project' } });
    fireEvent.click(screen.getByRole('button', { name: 'Start task' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Ollama model was not found');
    expect(screen.queryByRole('button', { name: 'Stop task' })).not.toBeInTheDocument();
  });
});
