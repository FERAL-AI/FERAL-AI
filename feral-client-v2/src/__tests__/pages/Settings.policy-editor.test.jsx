/**
 * Settings -> Security -> Policy editor must not swallow failures.
 *
 * The pre-fix save was:
 *
 *     try {
 *       const parsed = JSON.parse(policy);
 *       await apiFetch('/api/policy/update', {...});
 *       setSaved(true); setDirty(false);
 *     } catch { }
 *
 * so a syntax error and a rejected request landed in the same empty catch.
 * The "unsaved" chip stayed, the button stayed enabled, and nothing said
 * why. This is the editor for network allowlists, auto-approve categories
 * and tier gates.
 *
 * What is pinned here is the DISTINCTION, not just the presence of an
 * error: the two failures have different remedies (fix your typing vs the
 * brain refused this document), so one message for both would still leave
 * the user guessing.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, waitFor, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Settings, { locateJsonError } from '../../pages/Settings';

const POLICY_DOC = {
  version: '1.0',
  name: 'default',
  network: { mode: 'allowlist', allowed_domains: ['api.openai.com'] },
  execution: { allow_shell_commands: true },
};

function makeResponse(status, body) {
  const text = JSON.stringify(body);
  const res = {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: new Map(),
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(text),
  };
  res.clone = () => makeResponse(status, body);
  return res;
}

/** Records every POST to /api/policy/update and answers with `postResult`. */
function installFetch(postResult) {
  const posts = [];
  vi.stubGlobal('fetch', vi.fn((input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const method = String(init?.method || 'GET').toUpperCase();
    if (url.includes('/api/policy/update') && method === 'POST') {
      posts.push(init);
      return Promise.resolve(postResult || makeResponse(200, { ok: true }));
    }
    if (url.includes('/api/policy')) {
      return Promise.resolve(makeResponse(200, POLICY_DOC));
    }
    return Promise.resolve(makeResponse(200, { ok: true, keys: {}, entries: [] }));
  }));
  return posts;
}

async function openPolicyEditor() {
  render(
    <MemoryRouter initialEntries={['/settings']}>
      <Settings />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByTestId('settings-tab-security'));
  const editor = await screen.findByLabelText('Editor (json)');
  // Wait for the GET to land so we are editing the real document.
  await waitFor(() => expect(editor.value).toContain('allowlist'));
  return editor;
}

function type(editor, value) {
  fireEvent.change(editor, { target: { value } });
}

function saveButton() {
  return screen.getByRole('button', { name: /save policy/i });
}

beforeEach(() => {
  class StubWebSocket {
    constructor() {
      this.readyState = 1;
      this.send = vi.fn();
      this.close = vi.fn();
      this.addEventListener = vi.fn();
      this.removeEventListener = vi.fn();
    }
  }
  vi.stubGlobal('WebSocket', StubWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Policy editor: invalid JSON', () => {
  it('names the line and column and never sends the request', async () => {
    const posts = installFetch();
    const editor = await openPolicyEditor();

    // Missing value after "mode": V8 reports an offset for this shape.
    type(editor, '{\n  "name": "default",\n  "network": {\n    "mode"\n  }\n}');
    fireEvent.click(saveButton());

    const box = await screen.findByTestId('policy-json-error');
    expect(box.textContent).toMatch(/invalid JSON at line 5, column 3/i);
    // Nothing left the browser, and the editor says so.
    expect(posts).toHaveLength(0);
    expect(box.textContent).toMatch(/the running policy is unchanged/i);
  });

  it('locates the fault when the engine reports no offset', async () => {
    const posts = installFetch();
    const editor = await openPolicyEditor();

    // A stray character. V8 answers "Unexpected token ... is not valid
    // JSON" with no position, which the pre-fix regex-only approach could
    // not have located at all.
    type(editor, '{\n  "name": "default",\n  "network": ,\n  "x": 1\n}');
    fireEvent.click(saveButton());

    const box = await screen.findByTestId('policy-json-error');
    expect(box.textContent).toMatch(/invalid JSON (at|near) line 3/i);
    expect(posts).toHaveLength(0);
  });

  it('shows no "saved" chip and no request error', async () => {
    installFetch();
    const editor = await openPolicyEditor();

    type(editor, '{oops');
    fireEvent.click(saveButton());

    await screen.findByTestId('policy-json-error');
    expect(screen.queryByTestId('policy-save-error')).toBeNull();
    expect(screen.queryByText('saved')).toBeNull();
    // The chip that told the pre-fix user nothing is still honest.
    expect(screen.getByText('unsaved')).toBeTruthy();
  });

  it('clears once the user edits again', async () => {
    installFetch();
    const editor = await openPolicyEditor();

    type(editor, '{oops');
    fireEvent.click(saveButton());
    await screen.findByTestId('policy-json-error');

    type(editor, '{"name": "default"}');
    await waitFor(() => expect(screen.queryByTestId('policy-json-error')).toBeNull());
  });
});

describe('Policy editor: request rejected by the brain', () => {
  it('shows the field the brain named, not a syntax error', async () => {
    const posts = installFetch(makeResponse(400, {
      detail: {
        code: 'invalid_policy',
        field: 'execution.allow_shell_commands',
        message: 'execution.allow_shell_commands must be the JSON boolean true or false, got string.',
      },
    }));
    const editor = await openPolicyEditor();

    type(editor, JSON.stringify({ execution: { allow_shell_commands: 'false' } }, null, 2));
    fireEvent.click(saveButton());

    const box = await screen.findByTestId('policy-save-error');
    expect(box.textContent).toMatch(/execution\.allow_shell_commands/);
    expect(box.textContent).toMatch(/HTTP 400/);
    // The document WAS valid JSON, so the syntax box must stay away.
    expect(screen.queryByTestId('policy-json-error')).toBeNull();
    expect(posts).toHaveLength(1);
  });

  it('distinguishes an unreachable brain from a rejected document', async () => {
    installFetch();
    const editor = await openPolicyEditor();

    // A network-layer failure: apiFetch turns this into status 0.
    global.fetch.mockImplementationOnce(() => Promise.reject(new Error('boom')));
    type(editor, '{"name": "default"}');
    fireEvent.click(saveButton());

    const box = await screen.findByTestId('policy-save-error');
    expect(box.textContent).toMatch(/could not reach the brain|never got a response/i);
    expect(screen.queryByTestId('policy-json-error')).toBeNull();
  });

  it('says the running policy is unchanged and offers a retry', async () => {
    installFetch(makeResponse(500, { detail: 'disk on fire' }));
    const editor = await openPolicyEditor();

    type(editor, '{"name": "default"}');
    fireEvent.click(saveButton());

    const box = await screen.findByTestId('policy-save-error');
    expect(box.textContent).toMatch(/running policy is unchanged/i);
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy();
    expect(screen.queryByText('saved')).toBeNull();
  });

  it('the two failures do not render the same message', async () => {
    // Same component, same click, two different causes.
    const rejected = makeResponse(400, {
      detail: { code: 'invalid_policy', field: 'network.mode', message: 'network.mode must be one of allowlist, denylist.' },
    });
    installFetch(rejected);
    const editor = await openPolicyEditor();

    type(editor, '{"network": {"mode": }}');
    fireEvent.click(saveButton());
    const syntaxText = (await screen.findByTestId('policy-json-error')).textContent;

    type(editor, '{"network": {"mode": "nope"}}');
    fireEvent.click(saveButton());
    const rejectedText = (await screen.findByTestId('policy-save-error')).textContent;

    expect(syntaxText).not.toEqual(rejectedText);
    expect(syntaxText).toMatch(/not valid JSON|invalid JSON/i);
    expect(syntaxText).not.toMatch(/HTTP \d/);
    expect(rejectedText).toMatch(/network\.mode/);
  });
});

describe('Policy editor: success', () => {
  it('only shows the saved chip when the brain accepted it', async () => {
    const posts = installFetch(makeResponse(200, { ok: true }));
    const editor = await openPolicyEditor();

    // Must differ from the loaded document, or `change` is a no-op and the
    // button stays disabled.
    const edited = { ...POLICY_DOC, name: 'tightened' };
    type(editor, JSON.stringify(edited, null, 2));
    fireEvent.click(saveButton());

    await screen.findByText('saved');
    expect(screen.queryByText('unsaved')).toBeNull();
    expect(screen.queryByTestId('policy-json-error')).toBeNull();
    expect(screen.queryByTestId('policy-save-error')).toBeNull();
    expect(JSON.parse(posts[0].body).name).toBe('tightened');
  });
});

describe('locateJsonError', () => {
  const locate = (text) => {
    try {
      JSON.parse(text);
    } catch (err) {
      return locateJsonError(text, err);
    }
    throw new Error('expected a parse failure');
  };

  it('is exact when the engine reports an offset', () => {
    const r = locate('{\n  "a": 1\n  "b": 2\n}');
    expect(r.precise).toBe(true);
    expect(r.line).toBe(3);
    expect(r.excerpt).toContain('"b"');
  });

  it('falls back to the quoted source when there is no offset', () => {
    const r = locate('{\n  "a": 1,\n  "b": ,\n  "c": 3\n}');
    expect(r.line).toBeGreaterThan(0);
    expect(r.precise).toBe(false);
  });

  it('points at the end of the document when input is truncated', () => {
    const text = '{\n  "a": 1,\n';
    const r = locate(text);
    expect(r.line).toBe(text.split('\n').length);
  });

  it('still locates a short garbage document via the quoted source', () => {
    const r = locate('nope');
    expect(r.line).toBe(1);
    expect(r.precise).toBe(false);
  });

  it('reports no location rather than guessing at one', () => {
    // A message from an engine we do not recognise: no offset, no quote.
    const r = locateJsonError('{"a": 1}', { message: 'JSON Parse error: unrecognised' });
    expect(r.line).toBe(0);
    expect(r.raw).toBe('JSON Parse error: unrecognised');
  });

  it('will not point at an ambiguous quote', () => {
    // Two identical candidate sites: pointing at either would be a coin
    // flip presented as a fact.
    const text = 'xy\nxy';
    const r = locateJsonError(text, { message: '..."xy"... is not valid JSON' });
    expect(r.line).toBe(0);
  });

  it('never throws on a missing source or error', () => {
    expect(locateJsonError('', null).line).toBe(0);
    expect(locateJsonError(null, new Error('x')).line).toBe(0);
  });
});
