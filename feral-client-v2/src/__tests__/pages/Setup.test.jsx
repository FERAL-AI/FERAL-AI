import { describe, it, expect } from 'vitest';
import { waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Setup from '../../pages/Setup';


describe('Setup (new v2 wizard)', () => {
  it('renders the step tabs on mount', async () => {
    const { getAllByRole, container } = renderV2(<Setup />, {
      fetch: (url) => {
        if (url.includes('/api/llm/providers')) return { providers: [], count: 0 };
        if (url.includes('/api/llm/config')) return { provider: '', model: '' };
        if (url.includes('/api/audio/providers')) return { stt: [], tts: [] };
        if (url.includes('/api/audio/config')) {
          return { stt_provider: 'openai', stt_model: 'whisper-1', tts_provider: 'openai', tts_model: 'tts-1', tts_voice: 'nova' };
        }
        return {};
      },
    });
    // Tabs render as role="tab"
    await waitFor(() => {
      const tabs = getAllByRole('tab');
      expect(tabs.length).toBeGreaterThanOrEqual(5);
    });
    expect(container.querySelector('[data-testid="v2-marker"]')).toBeTruthy();
  });

  it('renders providers once the API responds', async () => {
    const { findByTestId } = renderV2(<Setup />, {
      fetch: (url) => {
        if (url.includes('/api/llm/providers') && !url.includes('/models')) {
          return {
            providers: [
              {
                id: 'openai', display_name: 'OpenAI', supports_local: false,
                requires_api_key: true, configured: false, reachable: false,
                default_base_url: 'https://api.openai.com/v1', default_model: 'gpt-4o-mini',
                credential_env_var: 'OPENAI_API_KEY', aliases: ['open ai'], notes: '',
                last_refresh: 0, error: '',
              },
              {
                id: 'ollama', display_name: 'Ollama (local)', supports_local: true,
                requires_api_key: false, configured: true, reachable: true,
                default_base_url: 'http://localhost:11434', default_model: 'llama3.3',
                credential_env_var: '', aliases: [], notes: '',
                last_refresh: 0, error: '',
              },
            ],
            count: 2,
          };
        }
        if (url.includes('/models')) return { models: [], source: 'fallback' };
        return {};
      },
    });
    // Navigate to the LLM step
    const tabButtons = document.querySelectorAll('[role="tab"]');
    // Second tab is "AI provider"
    tabButtons[1]?.click();
    expect(await findByTestId('v2-setup-pick-openai')).toBeInTheDocument();
    expect(await findByTestId('v2-setup-pick-ollama')).toBeInTheDocument();
  });

  it('has a Continue button', async () => {
    const { findByTestId } = renderV2(<Setup />, {
      fetch: () => ({}),
    });
    expect(await findByTestId('v2-setup-next')).toBeInTheDocument();
  });

  it('attempts remote-up when Anywhere mode is selected', async () => {
    const calls = [];
    const { getByRole, findByTestId } = renderV2(<Setup />, {
      fetch: (url, init) => {
        calls.push({ url, method: init?.method || 'GET' });
        if (url.includes('/api/llm/providers') && !url.includes('/models')) return { providers: [], count: 0 };
        if (url.includes('/api/llm/config')) return { provider: '', model: '' };
        if (url.includes('/api/audio/providers')) return { stt: [], tts: [] };
        if (url.includes('/api/audio/config')) return {
          stt_provider: 'openai', stt_model: 'whisper-1', tts_provider: 'openai', tts_model: 'tts-1', tts_voice: 'nova',
        };
        if (url.includes('/api/config/update')) return { ok: true };
        if (url.includes('/api/access/remote-up')) return { ok: true, pairing_mode: 'remote', remote_url: 'https://demo.ts.net' };
        if (url.includes('/api/devices/pair/url')) {
          return {
            mode: 'remote',
            url: 'https://demo.ts.net/pair?t=abc',
            expires: 0,
            diagnostic: { advertised_lan_ip: 'demo.ts.net', honest_caveats: [] },
          };
        }
        return {};
      },
    });

    getByRole('tab', { name: /connect devices/i }).click();
    const remoteBtn = await findByTestId('v2-setup-pair-remote');
    remoteBtn.click();

    await waitFor(() => {
      expect(calls.some((c) => c.url.includes('/api/access/remote-up'))).toBe(true);
      expect(calls.some((c) => c.url.includes('/api/devices/pair/url'))).toBe(true);
    });
  });
});
