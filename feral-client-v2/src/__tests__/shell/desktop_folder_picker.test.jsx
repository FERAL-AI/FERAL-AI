import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { registerFolderPicker } from '../../../../desktop/src/folderPicker';
import { installDesktopFolderReceiver, requestDesktopFolder } from '../../lib/desktopFolderPicker';
import Coding from '../../pages/Coding';
import { apiJson } from '../../lib/api';

vi.mock('../../lib/api', () => ({ apiJson: vi.fn() }));
const originalParent = window.parent;
const originalReferrer = Object.getOwnPropertyDescriptor(document, 'referrer');
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  Object.defineProperty(window, 'parent', { configurable: true, value: originalParent });
  if (originalReferrer) Object.defineProperty(document, 'referrer', originalReferrer);
  else delete document.referrer;
});

it('actual Coding chooser crosses native shell and updates the project field despite a loopback referrer', async () => {
  apiJson.mockResolvedValue({ agents: [{ agent_id: 'opencode', available: true }], workspaces: [], provider: null });
  let shellHandler;
  const invoke = vi.fn(async () => '/tmp/project with spaces');
  const frame = { src: 'http://127.0.0.1:9464/', isConnected: true, contentWindow: {
    postMessage(data, origin) {
      expect(origin).toBe('http://127.0.0.1:9464');
      window.dispatchEvent(new MessageEvent('message', { data, source: parent, origin: 'tauri://localhost' }));
    },
  } };
  const parent = { postMessage: vi.fn((data, origin) => {
    // Like a browser, deliver only the address matching the actual parent.
    if (origin === 'tauri://localhost') void shellHandler({ data, origin: 'http://127.0.0.1:9464', source: frame.contentWindow });
  }) };
  Object.defineProperty(window, 'parent', { configurable: true, value: parent });
  Object.defineProperty(document, 'referrer', { configurable: true, value: 'http://127.0.0.1:9464/setup' });
  registerFolderPicker(invoke, () => frame, { addEventListener: (_, fn) => { shellHandler = fn; }, removeEventListener() {} });
  render(<Coding />);
  await screen.findByText('OpenCode · Ready');
  fireEvent.click(screen.getByRole('button', { name: 'Choose folder' }));
  await waitFor(() => expect(screen.getByLabelText('Project folder')).toHaveValue('/tmp/project with spaces'));
  expect(invoke).toHaveBeenCalledExactlyOnceWith('pick_working_directory');
  expect(parent.postMessage.mock.calls.every(([, origin]) => origin !== '*' && origin !== 'http://127.0.0.1:9464')).toBe(true);
  expect(apiJson.mock.calls.every(([url]) => url === '/api/coding')).toBe(true);
  expect(screen.getByText('Folder selected. Grant it below to start coding.')).toBeInTheDocument();
});

it('receiver rejects arbitrary origins, other windows, stale ids and malformed replies, then cleans up', () => {
  let receive;
  const parent = {};
  const target = { parent, addEventListener: (_, fn) => { receive = fn; }, removeEventListener: vi.fn() };
  const result = vi.fn();
  const dispose = installDesktopFolderReceiver(() => 'pending', result, target);
  const event = { source: parent, origin: 'tauri://localhost', data: { type: 'feral:working-directory-picked', requestId: 'pending', path: '/tmp/project', error: null } };
  receive({ ...event, origin: 'http://127.0.0.1:9464' });
  receive({ ...event, origin: 'null' });
  receive({ ...event, source: {} });
  receive({ ...event, data: { ...event.data, requestId: 'stale' } });
  receive({ ...event, data: { ...event.data, command: 'arbitrary' } });
  receive({ ...event, data: { ...event.data, path: {} } });
  expect(result).not.toHaveBeenCalled();
  receive(event);
  expect(result).toHaveBeenCalledExactlyOnceWith(event.data);
  dispose();
  expect(target.removeEventListener).toHaveBeenCalledWith('message', receive);
});

it('standalone clients send nothing and unsupported native target origins fail visibly', () => {
  const standalone = { postMessage: vi.fn() };
  standalone.parent = standalone;
  expect(requestDesktopFolder('a', standalone)).toBe(false);
  expect(standalone.postMessage).not.toHaveBeenCalled();
  expect(requestDesktopFolder('a', { parent: { postMessage() { throw new Error('unsupported'); } } })).toBe(false);
});
