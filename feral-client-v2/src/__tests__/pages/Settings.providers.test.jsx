/**
 * Settings → Providers regression tests (Roadmap §3.5 P0).
 *
 * Pins three contracts:
 * (a) On initial mount, when the catalog row is older than 24h OR
 * empty, the picker MUST issue a force=true refresh — without
 * this the v2 picker silently served pre-2026 model lists for
 * as long as the brain had been up (Appendix A.1).
 * (b) The Live / Cached / Stale freshness badge renders next to the
 * model dropdown, sourced from CachedModelList.last_refresh.
 * (c) When the backend reports a 401 (warning chip text), the
 * picker renders that warning so the user knows the dropdown
 * is a fallback list, not live data.
 */

import { describe, it, expect, vi } from 'vitest';
import { fireEvent, waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Settings from '../../pages/Settings';

const baseProvider = {
  id: 'openai',
  display_name: 'OpenAI',
  supports_local: false,
  requires_api_key: true,
  configured: true,
  reachable: true,
 default_base_url: 'https://api.openai.com/v1',
  default_model: '',
  credential_env_var: 'OPENAI_API_KEY',
  aliases: ['open ai'],
};

function makeFetcher({ modelsResponse, captureUrls }) {
  return (url) => {
    if (captureUrls) captureUrls.push(url);
    if (url.includes('/api/llm/providers/openai/models')) {
      return modelsResponse(url);
    }
    if (url.includes('/api/llm/providers')) {
      return { providers: [baseProvider], count: 1 };
    }
    if (url.includes('/api/llm/status')) {
      return { available: true, provider: 'openai', model: '' };
    }
    if (url.includes('/api/llm/health')) {
      return { active: { provider: 'openai', model: '' }, candidates: [] };
    }
    if (url.includes('/api/llm/presets')) {
      return { presets: [] };
    }
    return {};
  };
}

async function openProviderForm(getByText, findByText, findByRole) {
  fireEvent.click(getByText(/^Providers$/));
 // Wait for the provider grid to render.
  await findByText(/Current provider/i);
 // Click the "Reconfigure" / "Use this provider" button on the
 // OpenAI card. We mock status so OpenAI is the current provider →
 // the button reads "Reconfigure"; either label opens ProviderForm.
 // Use getByRole to disambiguate the button from the helper text
 // ("Live inference backend — reconfigure via any card below").
  const openButton = await findByRole('button', { name: /^(Reconfigure|Use this provider)$/i });
  fireEvent.click(openButton);
}

describe('Settings → Providers freshness (W1)', () => {
  it('(a) issues a force=true refresh on initial mount when cache is >24h stale', async () => {
    const captured = [];
 // First call returns a 25h-old cache (Unix seconds).
    const STALE_TS = (Date.now() / 1000) - (25 * 3600);
    let callIndex = 0;
    const modelsResponse = () => {
      callIndex += 1;
      if (callIndex === 1) {
        return {
          provider_id: 'openai',
          models: ['gpt-stale'],
          source: 'cache',
          last_refresh: STALE_TS,
          count: 1,
          warning: '',
        };
      }
 // Force refresh returns the fresh list.
      return {
        provider_id: 'openai',
        models: ['gpt-5.5', 'gpt-5.4'],
        source: 'live',
        last_refresh: Date.now() / 1000,
        count: 2,
        warning: '',
      };
    };

    const { getByText, findByText, findByRole, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({ modelsResponse, captureUrls: captured }),
    });
    await openProviderForm(getByText, findByText, findByRole);

 // Wait until BOTH calls have happened — the cached one and the
 // automatic force=true that follows because last_refresh > 24h.
    await waitFor(() => {
      const forceCalls = captured.filter((u) =>
        u.includes('/api/llm/providers/openai/models')
        && u.includes('force=true'));
      expect(forceCalls.length).toBeGreaterThanOrEqual(1);
    });

 // The badge should now read "Live" (or at least a non-stale tone).
    const badge = await findByTestId('model-age-openai');
    expect(badge.getAttribute('data-age-tone')).not.toBe('stale');
  });

  it('(a) issues a force=true refresh when the cached row is empty', async () => {
    const captured = [];
    let callIndex = 0;
    const modelsResponse = () => {
      callIndex += 1;
      if (callIndex === 1) {
        return {
          provider_id: 'openai',
          models: [],
          source: 'fallback',
          last_refresh: 0,
          count: 0,
          warning: '',
        };
      }
      return {
        provider_id: 'openai',
        models: ['gpt-5.5'],
        source: 'live',
        last_refresh: Date.now() / 1000,
        count: 1,
        warning: '',
      };
    };

    const { getByText, findByText, findByRole } = renderV2(<Settings />, {
      fetch: makeFetcher({ modelsResponse, captureUrls: captured }),
    });
    await openProviderForm(getByText, findByText, findByRole);

    await waitFor(() => {
      const forceCalls = captured.filter((u) =>
        u.includes('/api/llm/providers/openai/models')
        && u.includes('force=true'));
      expect(forceCalls.length).toBeGreaterThanOrEqual(1);
    });
  });

  it('(b) renders the freshness badge as Live when last_refresh is recent', async () => {
 const FRESH_TS = (Date.now() / 1000) - 60; // 1 minute ago
    const modelsResponse = () => ({
      provider_id: 'openai',
      models: ['gpt-5.5'],
      source: 'live',
      last_refresh: FRESH_TS,
      count: 1,
      warning: '',
    });

    const { getByText, findByText, findByRole, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({ modelsResponse }),
    });
    await openProviderForm(getByText, findByText, findByRole);

    const badge = await findByTestId('model-age-openai');
    expect(badge.getAttribute('data-age-tone')).toBe('live');
    expect(badge.textContent).toMatch(/Live/);
  });

  it('(b) renders the freshness badge as Cached when last_refresh is between 2h and 24h', async () => {
 const CACHED_TS = (Date.now / 1000) - (5 * 3600); // 5 hours ago
    const modelsResponse = () => ({
      provider_id: 'openai',
      models: ['gpt-5.5'],
      source: 'cache',
      last_refresh: CACHED_TS,
      count: 1,
      warning: '',
    });

    const { getByText, findByText, findByRole, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({ modelsResponse }),
    });
    await openProviderForm(getByText, findByText, findByRole);

    const badge = await findByTestId('model-age-openai');
 // Badge tone is one of: live | cached | stale. 5h-old → cached.
    expect(['cached', 'stale']).toContain(badge.getAttribute('data-age-tone'));
    expect(badge.textContent).toMatch(/Cached|Stale/);
  });

  it('requests the recommended chat-class subset by default', async () => {
 // The v2 picker defaults to the conductor-curated chat shortlist
 // (recommended=true, model_class=chat) so the dropdown never
 // surfaces embeddings / whisper-* / dall-e etc — those are 400s
 // on /chat/completions and were the root cause of the drift bug.
    const captured = [];
    const modelsResponse = () => ({
      provider_id: 'openai',
      models: ['gpt-5.5-pro'],
      source: 'live',
      last_refresh: Date.now() / 1000,
      count: 1,
      warning: '',
    });

    const { getByText, findByText, findByRole } = renderV2(<Settings />, {
      fetch: makeFetcher({ modelsResponse, captureUrls: captured }),
    });
    await openProviderForm(getByText, findByText, findByRole);

    await waitFor(() => {
      const modelCalls = captured.filter((u) =>
        u.includes('/api/llm/providers/openai/models'));
      expect(modelCalls.length).toBeGreaterThanOrEqual(1);
      for (const u of modelCalls) {
        expect(u).toMatch(/recommended=true/);
        expect(u).toMatch(/model_class=chat/);
      }
    });
  });

  it('(c) renders the warning chip when the backend reports a 401', async () => {
    const modelsResponse = () => ({
      provider_id: 'openai',
      models: ['gpt-5.5'],
      source: 'fallback',
      last_refresh: 0,
      count: 1,
      warning: 'provider rejected the API key (HTTP 401)',
    });

    const { getByText, findByText, findByRole, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({ modelsResponse }),
    });
    await openProviderForm(getByText, findByText, findByRole);

    const chip = await findByTestId('model-warning-openai');
    expect(chip.textContent).toMatch(/HTTP 401/);
  });
});

/**
 * Settings → Providers credential-saving tests for A2.
 *
 * A2 decouples persisting provider credentials from switching the
 * active provider. Pasting an Anthropic key while OpenAI is the
 * active provider must NOT automatically flip the live backend to
 * Anthropic — it should just persist the key so failover /
 * "switch later" flows can pick it up. The "Save & switch" button
 * remains the explicit path for users who do want to flip right now.
 */
describe('Settings → Providers credential saving (A2)', () => {
 // anthropic catalog row as a SECOND provider, while status reports
 // openai as the active one. This is exactly the "adding a second
 // key" scenario that the A2 churn bug manifested under.
  const anthropicProvider = {
    id: 'anthropic',
    display_name: 'Anthropic',
    supports_local: false,
    requires_api_key: true,
    configured: false,
    reachable: null,
 default_base_url: 'https://api.anthropic.com',
    default_model: '',
    credential_env_var: 'ANTHROPIC_API_KEY',
    aliases: [],
  };

  function makeMultiProviderFetcher({ captureCalls, modelsResponse }) {
    return (url, init) => {
      if (captureCalls) captureCalls.push({ url, method: init?.method || 'GET', body: init?.body });
      if (url.includes('/api/llm/providers/anthropic/models')
          || url.includes('/api/llm/providers/openai/models')) {
        return modelsResponse
          ? modelsResponse(url)
          : {
              provider_id: url.includes('anthropic') ? 'anthropic' : 'openai',
              models: ['claude-x', 'gpt-x'],
              source: 'live',
              last_refresh: Date.now() / 1000,
              count: 2,
              warning: '',
            };
      }
      if (url.endsWith('/api/llm/providers') || url.includes('/api/llm/providers?')) {
        return {
          providers: [
            { ...baseProvider, configured: true, reachable: true },
            anthropicProvider,
          ],
          count: 2,
        };
      }
      if (url.includes('/api/llm/providers/anthropic/configure')) {
        return {
          success: true,
          status: { ...anthropicProvider, configured: true, provider_id: 'anthropic' },
          persisted: { ok: true, vault: true, warnings: [] },
          active_provider: false,
        };
      }
      if (url.includes('/api/llm/config')) {
        return {
          success: true,
          provider: 'anthropic',
          model: 'claude-x',
          persisted: { ok: true, warnings: [] },
          reconfigured: { ok: true },
        };
      }
      if (url.includes('/api/llm/status')) {
        return { available: true, provider: 'openai', model: 'gpt-x' };
      }
      if (url.includes('/api/llm/health')) {
        return { active: { provider: 'openai', model: 'gpt-x' }, candidates: [] };
      }
      if (url.includes('/api/llm/presets')) {
        return { presets: [] };
      }
      return {};
    };
  }

  async function openAnthropicForm({ getByText, findByText, findAllByRole }) {
    fireEvent.click(getByText(/^Providers$/));
    await findByText(/Current provider/i);
 // Two provider cards render; anthropic is non-current, so its
 // opening button reads "Use this provider". There are two such
 // buttons when neither card is open yet (openai shows
 // "Reconfigure" because it is current), so filter for the
 // non-current one.
    const buttons = await findAllByRole('button', {
      name: /^(Use this provider|Reconfigure)$/i,
    });
    const openNonCurrent = buttons.find((b) => /Use this provider/i.test(b.textContent));
    fireEvent.click(openNonCurrent);
  }

  it('non-current provider: default Save-key button does NOT call /api/llm/config', async () => {
    const calls = [];
    const { getByText, findByText, findAllByRole, findByTestId } = renderV2(<Settings />, {
      fetch: makeMultiProviderFetcher({ captureCalls: calls }),
    });
    await openAnthropicForm({ getByText, findByText, findAllByRole });

 // Find the "Save key" button exposed by the non-current branch.
    const saveKey = await findByTestId('provider-save-key-anthropic');
    expect(saveKey).toBeTruthy();

    fireEvent.click(saveKey);

 // Wait for the configure POST to land.
    await waitFor(() => {
      const postsToConfigure = calls.filter((c) =>
        c.method === 'POST'
        && c.url.includes('/api/llm/providers/anthropic/configure'));
      expect(postsToConfigure.length).toBeGreaterThanOrEqual(1);
    });

 // The active-provider switch endpoint must NOT have been hit —
 // the user only pasted a key, they did not ask to switch.
    const postsToConfig = calls.filter((c) =>
      c.method === 'POST' && c.url.match(/\/api\/llm\/config(\?|$)/));
    expect(postsToConfig.length).toBe(0);
  });

  it('non-current provider: explicit Save & switch still calls /api/llm/config', async () => {
    const calls = [];
    const { getByText, findByText, findAllByRole, findByTestId } = renderV2(<Settings />, {
      fetch: makeMultiProviderFetcher({ captureCalls: calls }),
    });
    await openAnthropicForm({ getByText, findByText, findAllByRole });

 // Wait for models to populate so the switch button isn't disabled.
    await waitFor(() => {
      const modelCalls = calls.filter((c) => c.url.includes('/api/llm/providers/anthropic/models'));
      expect(modelCalls.length).toBeGreaterThanOrEqual(1);
    });

    const saveSwitch = await findByTestId('provider-save-switch-anthropic');
    await waitFor(() => { expect(saveSwitch.disabled).toBe(false); });
    fireEvent.click(saveSwitch);

    await waitFor(() => {
      const postsToConfig = calls.filter((c) =>
        c.method === 'POST' && c.url.match(/\/api\/llm\/config(\?|$)/));
      expect(postsToConfig.length).toBeGreaterThanOrEqual(1);
    });
  });

  it('current provider: primary action calls /api/llm/config (save + apply)', async () => {
 // Use the original single-provider fetcher where openai IS current.
    const calls = [];
    const modelsResponse = () => ({
      provider_id: 'openai',
      models: ['gpt-5.5'],
      source: 'live',
      last_refresh: Date.now() / 1000,
      count: 1,
      warning: '',
    });
    const baseFetcher = makeFetcher({ modelsResponse, captureUrls: [] });
    const fetcher = (url, init) => {
      calls.push({ url, method: init?.method || 'GET' });
      if (url.includes('/api/llm/config') && init?.method === 'POST') {
        return {
          success: true,
          provider: 'openai',
          model: 'gpt-5.5',
          persisted: { ok: true, warnings: [] },
          reconfigured: { ok: true },
        };
      }
      return baseFetcher(url, init);
    };

    const { getByText, findByText, findByRole, findByTestId } = renderV2(<Settings />, {
      fetch: fetcher,
    });
    await openProviderForm(getByText, findByText, findByRole);

 // Current provider only shows the combined "Save & apply" button
 // — the non-current "Save key" / "Save & switch" pair must NOT
 // render here.
    const saveApply = await findByTestId('provider-save-apply-openai');
    await waitFor(() => { expect(saveApply.disabled).toBe(false); });
    fireEvent.click(saveApply);

    await waitFor(() => {
      const postsToConfig = calls.filter((c) =>
        c.method === 'POST' && c.url.match(/\/api\/llm\/config(\?|$)/));
      expect(postsToConfig.length).toBeGreaterThanOrEqual(1);
    });
  });

  it('non-current provider: Save-key triggers a force=true model refresh when a key is pasted', async () => {
 // Contract (5): model list must still refresh after a key save,
 // even on the non-switching path — otherwise the dropdown keeps
 // showing the pre-key list until the user navigates away.
    const calls = [];
    const { getByText, findByText, findAllByRole, findByTestId } = renderV2(<Settings />, {
      fetch: makeMultiProviderFetcher({ captureCalls: calls }),
    });
    await openAnthropicForm({ getByText, findByText, findAllByRole });

 // Type a key so the saveCredentialsOnly path triggers a
 // post-save force refresh.
    const keyInput = await waitFor(() => {
      const inputs = document.querySelectorAll('.v2-provider-form input[type="password"]');
      if (inputs.length === 0) throw new Error('no password input yet');
      return inputs[0];
    });
    fireEvent.change(keyInput, { target: { value: 'sk-ant-test' } });

    const saveKey = await findByTestId('provider-save-key-anthropic');
    fireEvent.click(saveKey);

    await waitFor(() => {
      const forceCalls = calls.filter((c) =>
        c.url.includes('/api/llm/providers/anthropic/models')
        && c.url.includes('force=true'));
      expect(forceCalls.length).toBeGreaterThanOrEqual(1);
    });
  });
});

/**
 * Settings → Providers Reconfigure / model-picker regression tests
 * for Lane 3 U3 (v2026.5.42 wave).
 *
 * Defects covered:
 * (U3-#1) Reconfigure seeded `selectedModel` from
 * `provider.default_model` (== "" for cloud descriptors), so the
 * auto-pick fell back to `list[0]` of the recommended catalog and
 * Save & apply silently swapped the brain model on key rotation.
 * (U3-#2) The current-provider card had no Save-key button; the
 * only action was Save & apply which always POSTed `model:`.
 * (U3-#3) Freshness badge could read "Live · 5m ago" while a yellow
 * warning chip reported HTTP 401 — same row, contradictory signal.
 * (U3-#4) Switching the active labeled key did not refresh an open
 * ProviderForm; the dropdown stayed pinned to the previous key's
 * view of /models.
 * (U3-#5) The recommended chat-class filter hid valid active /
 * custom models from the datalist suggestions.
 * (U3-existing-gap) `reconfigured.ok === false` had no regression
 * test — the false-success bug could silently come back.
 */
describe('Settings → Providers Reconfigure (Lane 3 U3)', () => {
 // OpenAI is current, runtime model is gpt-4-turbo-preview (a chat
 // model that is NOT in the conductor's 2026 recommended shortlist),
 // and the recommended /models response is the new gpt-5.5-pro.
 // Pre-fix, Reconfigure showed gpt-5.5-pro in the input box.
  function makeCurrentProviderFetcher({
    runtimeModel = 'gpt-4-turbo-preview',
    recommendedModels = ['gpt-5.5-pro'],
    configResponse,
    configureResponse,
    captureCalls,
  } = {}) {
    return (url, init) => {
      if (captureCalls) {
        captureCalls.push({ url, method: init?.method || 'GET', body: init?.body });
      }
      if (url.includes('/api/llm/providers/openai/models')) {
        return {
          provider_id: 'openai',
          models: recommendedModels,
          source: 'live',
          last_refresh: Date.now() / 1000,
          count: recommendedModels.length,
          warning: '',
        };
      }
      if (url.includes('/api/llm/providers/openai/configure')) {
        return configureResponse || {
          success: true,
          status: { ...baseProvider, configured: true },
          persisted: { ok: true, warnings: [] },
          active_provider: true,
        };
      }
      if (url.includes('/api/llm/config') && init?.method === 'POST') {
        return configResponse || {
          success: true,
          provider: 'openai',
          model: runtimeModel,
          persisted: { ok: true, warnings: [] },
          reconfigured: { ok: true },
        };
      }
      if (url.endsWith('/api/llm/providers') || url.includes('/api/llm/providers?')) {
        return { providers: [baseProvider], count: 1 };
      }
      if (url.includes('/api/llm/status')) {
        return { available: true, provider: 'openai', model: runtimeModel };
      }
      if (url.includes('/api/llm/health')) {
        return { active: { provider: 'openai', model: runtimeModel }, candidates: [] };
      }
      if (url.includes('/api/llm/presets')) {
        return { presets: [] };
      }
      return {};
    };
  }

  it('(U3-#1) seeds the model input from runtime status.model, not list[0]', async () => {
 // status.model = the actually running model; recommended list
 // omits it. Pre-fix the picker showed gpt-5.5-pro on Reconfigure.
    const { getByText, findByText, findByRole } = renderV2(<Settings />, {
      fetch: makeCurrentProviderFetcher({
        runtimeModel: 'gpt-4-turbo-preview',
        recommendedModels: ['gpt-5.5-pro'],
      }),
    });
    await openProviderForm(getByText, findByText, findByRole);

 // After both the cached + auto-force model fetches settle, the
 // input bound to `selectedModel` must read the runtime model.
    await waitFor(() => {
      const modelInputs = document.querySelectorAll(
        '.v2-provider-model-row input.v2-input',
      );
      expect(modelInputs.length).toBeGreaterThanOrEqual(1);
      expect(modelInputs[0].value).toBe('gpt-4-turbo-preview');
    });
  });

  it('(U3-#1) Save & apply without editing sends the runtime model, not list[0]', async () => {
    const calls = [];
    const { getByText, findByText, findByRole, findByTestId } = renderV2(<Settings />, {
      fetch: makeCurrentProviderFetcher({
        runtimeModel: 'gpt-4-turbo-preview',
        recommendedModels: ['gpt-5.5-pro'],
        captureCalls: calls,
      }),
    });
    await openProviderForm(getByText, findByText, findByRole);

    const saveApply = await findByTestId('provider-save-apply-openai');
    await waitFor(() => { expect(saveApply.disabled).toBe(false); });
    fireEvent.click(saveApply);

    await waitFor(() => {
      const cfgPost = calls.find((c) =>
        c.method === 'POST' && c.url.match(/\/api\/llm\/config(\?|$)/));
      expect(cfgPost).toBeTruthy();
      const body = JSON.parse(cfgPost.body || '{}');
      expect(body.model).toBe('gpt-4-turbo-preview');
    });
  });

  it('(U3-#2) current provider exposes a Save key button that hits /configure (no /api/llm/config)', async () => {
    const calls = [];
    const { getByText, findByText, findByRole, findByTestId } = renderV2(<Settings />, {
      fetch: makeCurrentProviderFetcher({
        runtimeModel: 'gpt-4-turbo-preview',
        recommendedModels: ['gpt-5.5-pro'],
        captureCalls: calls,
      }),
    });
    await openProviderForm(getByText, findByText, findByRole);

 // Wait for the form to settle so the model state stabilises.
    await waitFor(() => {
      const inputs = document.querySelectorAll('.v2-provider-form input[type="password"]');
      expect(inputs.length).toBeGreaterThanOrEqual(1);
    });
    const keyInput = document.querySelectorAll(
      '.v2-provider-form input[type="password"]',
    )[0];
    fireEvent.change(keyInput, { target: { value: 'sk-rotated' } });

 // The new button must be present on the current-provider branch.
    const saveKey = await findByTestId('provider-save-key-openai');
    expect(saveKey).toBeTruthy();
    fireEvent.click(saveKey);

 // After the click settles, /configure must have been POSTed and
 // /api/llm/config must NOT have been called (so the runtime model
 // is not touched by a key-rotation).
    await waitFor(() => {
      const cfgurePosts = calls.filter((c) =>
        c.method === 'POST'
        && c.url.includes('/api/llm/providers/openai/configure'));
      expect(cfgurePosts.length).toBeGreaterThanOrEqual(1);
    });
    const cfgPosts = calls.filter((c) =>
      c.method === 'POST' && c.url.match(/\/api\/llm\/config(\?|$)/));
    expect(cfgPosts.length).toBe(0);
  });

  it('(U3-#3) freshness badge shows error tone when backend reports a warning, regardless of last_refresh', async () => {
 // 1-minute-old cache row with a 401 warning. Pre-fix, badge said
 // "Live · 1m ago" alongside the warning chip — a direct lie.
    const fetcher = (url) => {
      if (url.includes('/api/llm/providers/openai/models')) {
        return {
          provider_id: 'openai',
          models: ['gpt-5.5-pro'],
          source: 'cache',
          last_refresh: (Date.now() / 1000) - 60,
          count: 1,
          warning: 'provider rejected the API key (HTTP 401)',
        };
      }
      if (url.endsWith('/api/llm/providers') || url.includes('/api/llm/providers?')) {
        return { providers: [baseProvider], count: 1 };
      }
      if (url.includes('/api/llm/status')) {
        return { available: true, provider: 'openai', model: 'gpt-4-turbo-preview' };
      }
      if (url.includes('/api/llm/health')) {
        return { active: { provider: 'openai', model: 'gpt-4-turbo-preview' }, candidates: [] };
      }
      if (url.includes('/api/llm/presets')) return { presets: [] };
      return {};
    };
    const { getByText, findByText, findByRole, findByTestId } = renderV2(<Settings />, { fetch: fetcher });
    await openProviderForm(getByText, findByText, findByRole);

    const badge = await findByTestId('model-age-openai');
 // The tone must NOT read "live" — either error (warning present)
 // or stale (non-live source) is acceptable; never the cheery
 // green chip while a HTTP 401 is sitting right next to it.
    await waitFor(() => {
      const tone = badge.getAttribute('data-age-tone');
      expect(['error', 'stale']).toContain(tone);
      expect(tone).not.toBe('live');
    });
  });

  it('(U3-#4) switching the active labeled key triggers a force=true model re-fetch', async () => {
 // Render with one labeled key, then post a Make-active for a
 // second label. Pre-fix, the open ProviderForm kept showing the
 // models the previous key could see.
    const calls = [];
    const keysSnapshots = [
      { keys: [
        { label: 'prod', fingerprint: 'sk-...aaa', probe: { status: 'ok' } },
        { label: 'dev', fingerprint: 'sk-...bbb', probe: { status: 'ok' } },
      ], active_label: 'prod' },
      { keys: [
        { label: 'prod', fingerprint: 'sk-...aaa', probe: { status: 'ok' } },
        { label: 'dev', fingerprint: 'sk-...bbb', probe: { status: 'ok' } },
      ], active_label: 'dev' },
    ];
    let keysCallIdx = 0;
    const fetcher = (url, init) => {
      calls.push({ url, method: init?.method || 'GET' });
      if (url.includes('/api/llm/providers/openai/keys/active')) {
        return { ok: true, active_label: 'dev' };
      }
      if (url.includes('/api/llm/providers/openai/keys')) {
        const snap = keysSnapshots[Math.min(keysCallIdx, keysSnapshots.length - 1)];
        keysCallIdx += 1;
        return snap;
      }
      if (url.includes('/api/llm/providers/openai/models')) {
        return {
          provider_id: 'openai',
          models: ['gpt-5.5-pro'],
          source: 'live',
          last_refresh: Date.now() / 1000,
          count: 1,
          warning: '',
        };
      }
      if (url.endsWith('/api/llm/providers') || url.includes('/api/llm/providers?')) {
        return { providers: [baseProvider], count: 1 };
      }
      if (url.includes('/api/llm/status')) {
        return { available: true, provider: 'openai', model: 'gpt-4-turbo-preview' };
      }
      if (url.includes('/api/llm/health')) {
        return { active: { provider: 'openai', model: 'gpt-4-turbo-preview' }, candidates: [] };
      }
      if (url.includes('/api/llm/presets')) return { presets: [] };
      return {};
    };
    const { getByText, findByText, findByRole, findByTestId } = renderV2(<Settings />, { fetch: fetcher });
    await openProviderForm(getByText, findByText, findByRole);

 // Wait for the keys card to render and the "Make active" button
 // to appear for the `dev` row (prod is the current active one).
    const devRow = await findByTestId('provider-key-row-dev');
    expect(devRow).toBeTruthy();
 // Count force=true model calls before the make-active click; we
 // expect the count to GROW after the click.
    const forceBefore = calls.filter((c) =>
      c.url.includes('/api/llm/providers/openai/models')
      && c.url.includes('force=true')).length;
    const makeActiveBtn = Array.from(devRow.querySelectorAll('button'))
      .find((b) => /Make active/i.test(b.textContent));
    expect(makeActiveBtn).toBeTruthy();
    fireEvent.click(makeActiveBtn);

    await waitFor(() => {
      const forceAfter = calls.filter((c) =>
        c.url.includes('/api/llm/providers/openai/models')
        && c.url.includes('force=true')).length;
      expect(forceAfter).toBeGreaterThan(forceBefore);
    });
  });

  it('(U3-#5) active model is selectable in datalist even when recommended list omits it', async () => {
 // Recommended shortlist contains gpt-5.5-pro only; the runtime
 // model is gpt-4-turbo-preview. The datalist must still include
 // gpt-4-turbo-preview so the user can re-pick it without typing.
    const { getByText, findByText, findByRole, findByTestId } = renderV2(<Settings />, {
      fetch: makeCurrentProviderFetcher({
        runtimeModel: 'gpt-4-turbo-preview',
        recommendedModels: ['gpt-5.5-pro'],
      }),
    });
    await openProviderForm(getByText, findByText, findByRole);

    const dlist = await findByTestId('models-datalist-openai');
    await waitFor(() => {
      const values = Array.from(dlist.querySelectorAll('option')).map((o) => o.getAttribute('value'));
      expect(values).toContain('gpt-4-turbo-preview');
      expect(values).toContain('gpt-5.5-pro');
    });
  });

  it('(U3-existing-gap) reconfigured.ok=false surfaces error chip, never a success chip', async () => {
    const calls = [];
    const { getByText, findByText, findByRole, findByTestId } = renderV2(<Settings />, {
      fetch: makeCurrentProviderFetcher({
        runtimeModel: 'gpt-4-turbo-preview',
        recommendedModels: ['gpt-5.5-pro'],
        configResponse: {
          success: true,
          provider: 'openai',
          model: 'gpt-5.5-pro',
          persisted: { ok: true, warnings: [] },
          reconfigured: { ok: false, reason: 'invalid model' },
        },
        captureCalls: calls,
      }),
    });
    await openProviderForm(getByText, findByText, findByRole);

    const saveApply = await findByTestId('provider-save-apply-openai');
    await waitFor(() => { expect(saveApply.disabled).toBe(false); });
    fireEvent.click(saveApply);

 // The container that holds the form should surface the error
 // chip text "invalid model" and must NOT show the "Saved …"
 // success label. (The freshness badge is also a `.v2-chip--live`
 // span — filter to <div> elements only, since the success/error
 // chips render as <div> while the freshness badge is a <span>.)
    await waitFor(() => {
      const errChip = document.querySelector('.v2-provider-form div.v2-chip--error');
      expect(errChip).toBeTruthy();
      expect(errChip.textContent).toMatch(/invalid model/);
    });
    const successDivs = Array.from(document.querySelectorAll('.v2-provider-form div.v2-chip--live'));
    const sawSuccess = successDivs.some((el) => /Saved/i.test(el.textContent));
    expect(sawSuccess).toBe(false);
  });
});
