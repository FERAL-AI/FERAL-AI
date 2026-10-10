import React from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Setup from '../../pages/Setup';

const row = (id = 'openai', more = {}) => ({ id, display_name: id, supports_local: false,
  requires_api_key: true, configured: true, reachable: null, runtime_supported: true,
  setup_selectable: true, default_base_url: 'https://fixture.invalid/v1', ...more });
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const response = (body, status = 200) => new Response(JSON.stringify(body), { status,
  headers: { 'Content-Type': 'application/json' } });
function fixture(options = {}) {
  const state = { rows: options.rows || [row()], config: options.config || {
    provider: 'openai', model: 'fixture-chat', base_url: 'https://fixture.invalid/custom', fallback_providers: [], configured: true,
  }, ...options, calls: [] };
  vi.stubGlobal('fetch', vi.fn(async (url, init = {}) => {
    const path = String(url), method = init.method || 'GET'; state.calls.push({ path, method, body: init.body });
    if (path.includes('/models')) return state.models ? state.models(path) : response({ models: ['fixture-chat'], source: 'cache' });
    if (path.endsWith('/probe')) return state.probe ? state.probe(path) : response({ id: path.split('/').at(-2), reachable: false, error: 'private provider detail' });
    if (path.endsWith('/api/llm/providers')) return state.inventory ? state.inventory() : response({ providers: state.rows });
    if (path.endsWith('/api/llm/config')) {
      if (method === 'POST') {
        const body = JSON.parse(init.body);
        if (state.save) return state.save(body);
        state.config = { ...state.config, provider: body.provider, model: body.model };
        return response({ success: true, provider: body.provider, model: body.model, persisted: { ok: true } });
      }
      return response(state.config);
    }
    if (path.endsWith('/api/audio/providers')) return response({ stt: [], tts: [] });
    return response({});
  }));
  render(<MemoryRouter><Setup /></MemoryRouter>);
  fireEvent.click(screen.getByRole('tab', { name: 'AI provider' })); return state;
}
async function loaded(id = 'openai') {
  await screen.findByTestId(`v2-setup-pick-${id}`);
  await waitFor(() => expect(screen.getByTestId('v2-setup-model')).toHaveValue('fixture-chat'));
}
const probeButton = () => screen.getByTestId('v2-setup-probe-openai');
async function negativeProbe() { fireEvent.click(probeButton()); await screen.findByText('Last explicit probe: unreachable'); }
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Setup passive inventory and provider evidence', () => {
  it('reads cached suggestions without live provider I/O or inference claims', async () => {
    const state = fixture(); await loaded(); await screen.findByText(/Inventory: cache/);
    expect(state.calls.filter(c => c.path.includes('/models'))).toHaveLength(1);
    expect(state.calls.find(c => c.path.includes('/models')).path).toContain('?live=false&recommended=true&model_class=chat');
    expect(state.calls.some(c => c.method === 'POST')).toBe(false);
    expect(screen.getByText('Reachability unknown — not probed')).toBeInTheDocument();
    expect(screen.queryByText('Runtime reports unreachable')).not.toBeInTheDocument();
  });
  it('retains a real HTTP-success negative error receipt through passive refresh and redacts error', async () => {
    const state = fixture(); await loaded(); await negativeProbe();
    expect(screen.queryByText(/private provider detail/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(state.calls.filter(c => c.path.endsWith('/api/llm/providers')).length).toBe(4));
    expect(screen.getByText('Last explicit probe: unreachable')).toBeInTheDocument();
    expect(state.calls.filter(c => c.path.endsWith('/probe'))).toHaveLength(1);
  });
  it('positive receipt proves only reachability', async () => {
    fixture({ probe: () => response({ provider_id: 'openai', reachable: true, error: '' }) }); await loaded();
    fireEvent.click(probeButton()); await screen.findByText('Last explicit probe: reachable');
    expect(screen.getByText(/model inference, tools and voice remain unverified/)).toBeInTheDocument();
  });
  it.each([
    ['wrong provider', { id: 'other', reachable: false, error: 'secret' }, 200],
    ['conflicting identity', { id: 'openai', provider_id: 'other', reachable: false }, 200],
    ['unknown', { id: 'openai', reachable: null }, 200],
    ['string boolean', { id: 'openai', reachable: 'false' }, 200],
    ['contradictory success', { id: 'openai', reachable: true, error: 'secret' }, 200],
    ['HTTP failure', { id: 'openai', reachable: false, error: 'secret' }, 503],
  ])('rejects %s instead of publishing probe evidence', async (_, body, status) => {
    fixture({ probe: () => response(body, status) }); await loaded(); fireEvent.click(probeButton());
    await screen.findByText(/Probe could not be verified/);
    expect(screen.queryByText(/Last explicit probe:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/secret/)).not.toBeInTheDocument();
  });
  it.each(['model', 'key', 'provider'])('retires history on %s draft edit', async kind => {
    fixture({ rows: [row(), row('ollama', { supports_local: true, requires_api_key: false })] }); await loaded(); await negativeProbe();
    if (kind === 'model') fireEvent.change(screen.getByTestId('v2-setup-model'), { target: { value: 'other' } });
    if (kind === 'key') fireEvent.change(screen.getByTestId('v2-setup-apikey'), { target: { value: 'fake-key' } });
    if (kind === 'provider') fireEvent.click(screen.getByTestId('v2-setup-pick-ollama'));
    expect(screen.queryByText(/Last explicit probe:/)).not.toBeInTheDocument();
  });
  it.each(['endpoint', 'model', 'flags'])('invalidates history after passive %s drift', async kind => {
    const state = fixture(); await loaded(); await negativeProbe();
    if (kind === 'endpoint') state.config.base_url = 'https://changed.invalid';
    if (kind === 'model') state.config.model = 'changed';
    if (kind === 'flags') state.rows = [row('openai', { setup_selectable: false })];
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => expect(screen.queryByText(/Last explicit probe:/)).not.toBeInTheDocument());
  });
  it('pending probe cannot publish after edit and restoration of the same draft', async () => {
    const pending = deferred(); const state = fixture({ probe: () => pending.promise }); await loaded();
    fireEvent.click(probeButton()); await waitFor(() => expect(state.calls.some(c => c.path.endsWith('/probe'))).toBe(true));
    fireEvent.change(screen.getByTestId('v2-setup-model'), { target: { value: 'temporary' } });
    fireEvent.change(screen.getByTestId('v2-setup-model'), { target: { value: 'fixture-chat' } });
    await act(async () => pending.resolve(response({ id: 'openai', reachable: false })));
    await waitFor(() => expect(probeButton()).not.toBeDisabled());
    expect(screen.queryByText(/Last explicit probe:|Probe could not be verified/)).not.toBeInTheDocument();
  });
  it('pending readback cannot publish after key draft edit', async () => {
    const pending = deferred(); const state = fixture(); await loaded(); let reads = 0;
    state.inventory = () => ++reads === 2 ? pending.promise : response({ providers: state.rows });
    fireEvent.click(probeButton()); await waitFor(() => expect(reads).toBe(2));
    fireEvent.change(screen.getByTestId('v2-setup-apikey'), { target: { value: 'draft' } });
    await act(async () => pending.resolve(response({ providers: state.rows })));
    await waitFor(() => expect(probeButton()).not.toBeDisabled());
    expect(screen.queryByText(/Last explicit probe:/)).not.toBeInTheDocument();
  });
  it('pre-dispatch configuration drift sends zero POSTs', async () => {
    const state = fixture(); await loaded(); state.config.base_url = 'https://changed.invalid'; fireEvent.click(probeButton());
    await screen.findByText(/Probe could not be verified/); expect(state.calls.filter(c => c.method === 'POST')).toHaveLength(0);
  });
  it('post-probe configuration drift cannot publish historical success', async () => {
    const state = fixture(); await loaded(); state.probe = () => { state.config.model = 'changed'; return response({ id: 'openai', reachable: true }); };
    fireEvent.click(probeButton()); await screen.findByText(/Probe could not be verified/);
    expect(screen.queryByText(/Last explicit probe:/)).not.toBeInTheDocument();
  });
  it('older model inventory errors cannot overwrite new provider results', async () => {
    const pending = deferred(); const state = fixture({ rows: [row(), row('ollama')], models: path => path.includes('/openai/')
      ? pending.promise : response({ models: ['local-fixture'], source: 'cached-local' }) }); await loaded();
    fireEvent.click(screen.getByTestId('v2-setup-pick-ollama')); await screen.findByText('local-fixture');
    await act(async () => pending.resolve(response({ error: 'private old failure' }, 503)));
    expect(screen.getByText('local-fixture')).toBeInTheDocument();
    expect(screen.queryByText(/Cached model inventory could not be verified/)).not.toBeInTheDocument();
    expect(state.calls.filter(c => c.path.includes('/models')).every(c => c.path.includes('live=false'))).toBe(true);
  });
  it('saved unsupported gateway remains selected read-only without automatic rewrite', async () => {
    const state = fixture({ rows: [row('together', { runtime_supported: false, setup_selectable: false }), row()],
      config: { provider: 'together', model: 'fixture-chat', base_url: 'https://saved.invalid/v1' } }); await loaded('together');
    expect(screen.getByTestId('v2-setup-pick-together')).toHaveTextContent('Selected');
    expect(screen.getByTestId('v2-setup-pick-together')).toBeDisabled();
    expect(screen.getByText('Unsupported adapter — read-only')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('v2-setup-next')); await screen.findByText(/Choose a supported AI provider/);
    expect(state.calls.some(c => c.method === 'POST' || c.path.includes('/models'))).toBe(false);
  });
  it('allows supported or legacy-unconfirmed selection, rejects partial or malformed flags', async () => {
    const legacy = row('legacy'); delete legacy.runtime_supported; delete legacy.setup_selectable;
    const partial = row('partial'); delete partial.setup_selectable;
    fixture({ rows: [row(), legacy, partial, row('malformed', { setup_selectable: 'true' }), row('false', { runtime_supported: false })] }); await loaded();
    expect(screen.getByTestId('v2-setup-pick-legacy')).not.toBeDisabled();
    expect(screen.getByText(/Legacy adapter support unconfirmed/)).toBeInTheDocument();
    for (const id of ['partial', 'malformed', 'false']) expect(screen.getByTestId(`v2-setup-pick-${id}`)).toBeDisabled();
  });
  it('malformed numeric catalog identity cannot become a provider selection', async () => {
    const state = fixture({ rows: [row(1)], config: { provider: '', model: '' } });
    await screen.findByText('Provider inventory or saved configuration could not be verified. Refresh to retry.');
    expect(screen.queryByTestId('v2-setup-pick-1')).not.toBeInTheDocument();
    expect(state.calls.some(c => c.method === 'POST' || c.path.includes('/models'))).toBe(false);
  });
  it('does not autochoose explicitly unsupported entries', async () => {
    const state = fixture({ rows: [row('unsupported', { setup_selectable: false })], config: { provider: '', model: '' } });
    await screen.findByTestId('v2-setup-pick-unsupported'); expect(screen.queryByTestId('v2-setup-model')).not.toBeInTheDocument();
    expect(state.calls.some(c => c.path.includes('/models'))).toBe(false);
  });
  it('save advances only after exact receipt and readback without overwriting endpoint', async () => {
    const state = fixture(); await loaded(); fireEvent.change(screen.getByTestId('v2-setup-model'), { target: { value: 'chosen' } });
    fireEvent.click(screen.getByTestId('v2-setup-next')); await screen.findByRole('heading', { name: 'Speech recognition' });
    const write = state.calls.find(c => c.path.endsWith('/api/llm/config') && c.method === 'POST');
    expect(JSON.parse(write.body)).toEqual({ provider: 'openai', model: 'chosen' }); expect(state.config.base_url).toBe('https://fixture.invalid/custom');
  });
  it('mismatched save readback stays on provider step', async () => {
    fixture({ save: body => response({ success: true, ...body, persisted: { ok: true } }) }); await loaded();
    fireEvent.change(screen.getByTestId('v2-setup-model'), { target: { value: 'chosen' } });
    fireEvent.click(screen.getByTestId('v2-setup-next')); await screen.findByText(/Provider settings could not be verified/);
    expect(screen.getByTestId('v2-setup-model')).toBeInTheDocument();
  });
  it('delayed save cannot advance after a draft edit even when its write succeeded', async () => {
    const pending = deferred(); const state = fixture(); await loaded();
    state.save = body => { state.config = { ...state.config, ...body }; return pending.promise; };
    fireEvent.click(screen.getByTestId('v2-setup-next'));
    await waitFor(() => expect(state.calls.some(c => c.method === 'POST')).toBe(true));
    fireEvent.change(screen.getByTestId('v2-setup-model'), { target: { value: 'new-draft' } });
    await act(async () => pending.resolve(response({ success: true, provider: 'openai', model: 'fixture-chat' })));
    await waitFor(() => expect(screen.getByTestId('v2-setup-next')).not.toBeDisabled());
    expect(screen.getByTestId('v2-setup-model')).toHaveValue('new-draft');
    expect(screen.queryByRole('heading', { name: 'Speech recognition' })).not.toBeInTheDocument();
  });
  it('changing setup step retires a pending save instead of redirecting on late success', async () => {
    const pending = deferred(); const state = fixture(); await loaded();
    state.save = body => { state.config = { ...state.config, ...body }; return pending.promise; };
    fireEvent.click(screen.getByTestId('v2-setup-next'));
    await waitFor(() => expect(state.calls.some(c => c.method === 'POST')).toBe(true));
    fireEvent.click(screen.getByRole('tab', { name: 'About you' }));
    await act(async () => pending.resolve(response({ success: true, provider: 'openai', model: 'fixture-chat' })));
    await waitFor(() => expect(screen.getByTestId('v2-setup-next')).not.toBeDisabled());
    expect(screen.getByRole('tab', { name: 'About you' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByRole('heading', { name: 'Speech recognition' })).not.toBeInTheDocument();
  });
  it('simultaneous explicit probe clicks dispatch once', async () => {
    const pending = deferred(); const state = fixture({ probe: () => pending.promise }); await loaded();
    act(() => { probeButton().click(); probeButton().click(); });
    await waitFor(() => expect(state.calls.filter(c => c.path.endsWith('/probe'))).toHaveLength(1));
    await act(async () => pending.resolve(response({ id: 'openai', reachable: true })));
    await screen.findByText('Last explicit probe: reachable');
  });
  it('passive read failure retires prior history without exposing private failure details', async () => {
    const state = fixture(); await loaded(); await negativeProbe();
    state.inventory = () => response({ error: 'private inventory failure' }, 503);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    await screen.findByText('Provider inventory or saved configuration could not be verified.');
    expect(screen.queryByText(/Last explicit probe:|private inventory failure/)).not.toBeInTheDocument();
  });
  it('a delayed successful old model inventory cannot overwrite the new provider', async () => {
    const pending = deferred(); fixture({ rows: [row(), row('ollama')], models: path => path.includes('/openai/')
      ? pending.promise : response({ models: ['current-local'], source: 'cache' }) }); await loaded();
    fireEvent.click(screen.getByTestId('v2-setup-pick-ollama')); await screen.findByText('current-local');
    await act(async () => pending.resolve(response({ models: ['retired-cloud'], source: 'cache' })));
    expect(screen.queryByText('retired-cloud')).not.toBeInTheDocument();
    expect(screen.getByText('current-local')).toBeInTheDocument();
  });
  it.each(['endpoint', 'fallback', 'support flags', 'key requirement'])('save rejects material %s drift despite matching provider/model', async kind => {
    const state = fixture(); await loaded();
    state.save = body => {
      state.config = { ...state.config, ...body };
      if (kind === 'endpoint') state.config.base_url = 'https://concurrent.invalid/v1';
      if (kind === 'fallback') state.config.fallback_providers = ['unreviewed-provider'];
      if (kind === 'support flags') state.rows = [row('openai', { setup_selectable: false })];
      if (kind === 'key requirement') state.rows = [row('openai', { requires_api_key: false })];
      return response({ success: true, ...body, persisted: { ok: true } });
    };
    fireEvent.click(screen.getByTestId('v2-setup-next'));
    await screen.findByText(/Provider settings could not be verified/);
    expect(screen.getByTestId('v2-setup-model')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Speech recognition' })).not.toBeInTheDocument();
  });
  it.each([undefined, false, 'true'])('save rejects malformed persistence receipt %s', async ok => {
    const state = fixture(); await loaded();
    state.save = body => response({ success: true, ...body, persisted: { ok } });
    fireEvent.click(screen.getByTestId('v2-setup-next'));
    await screen.findByText(/Provider settings could not be verified/);
    expect(screen.getByTestId('v2-setup-model')).toBeInTheDocument();
  });
  it('canonical provider change verifies default endpoint and exact backend fallback transformation', async () => {
    const state = fixture({ rows: [row(), row('ollama')], config: { provider: 'openai', model: 'fixture-chat',
      base_url: 'https://old.invalid', fallback_providers: ['ollama', 'anthropic'], configured: true } }); await loaded();
    fireEvent.click(screen.getByTestId('v2-setup-pick-ollama'));
    fireEvent.change(screen.getByTestId('v2-setup-model'), { target: { value: 'local-fixture' } });
    state.save = body => {
      state.config = { provider: body.provider, model: body.model, base_url: '', fallback_providers: ['openai', 'anthropic'], configured: true };
      return response({ success: true, ...body, persisted: { ok: true } });
    };
    fireEvent.click(screen.getByTestId('v2-setup-next')); await screen.findByRole('heading', { name: 'Speech recognition' });
    const write = state.calls.find(c => c.method === 'POST');
    expect(JSON.parse(write.body)).toEqual({ provider: 'ollama', model: 'local-fixture' });
  });
  it('ambiguous saved provider alias sends no write rather than guessing endpoint semantics', async () => {
    const state = fixture({ config: { provider: 'open ai', model: 'fixture-chat', base_url: 'https://saved.invalid' } });
    await screen.findByTestId('v2-setup-pick-openai'); fireEvent.click(screen.getByTestId('v2-setup-pick-openai'));
    fireEvent.change(screen.getByTestId('v2-setup-model'), { target: { value: 'fixture-chat' } });
    fireEvent.click(screen.getByTestId('v2-setup-next')); await screen.findByText(/Provider settings could not be verified/);
    expect(state.calls.some(c => c.method === 'POST')).toBe(false);
  });
  it('late passive refresh cannot replace newer probe history', async () => {
    const pending = deferred(); const state = fixture(); await loaded(); let reads = 0;
    state.inventory = () => ++reads === 1 ? pending.promise : response({ providers: state.rows });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' })); await waitFor(() => expect(reads).toBe(1)); await negativeProbe();
    await act(async () => pending.resolve(response({ providers: [row('old')] })));
    expect(screen.getByText('Last explicit probe: unreachable')).toBeInTheDocument(); expect(screen.queryByTestId('v2-setup-pick-old')).not.toBeInTheDocument();
  });
});
