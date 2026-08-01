/**
 * Settings -> Integrations + Voice: connecting has to actually connect.
 *
 * Pins four things that were broken:
 *
 *  (a) The Home Assistant card had a token field and nothing else, so a
 *      Home Assistant anywhere other than homeassistant.local:8123 could
 *      not be connected from this page at all. It now takes a URL and
 *      sends it.
 *  (b) The page told the user status "comes from a real backend probe"
 *      while nothing in the brain ran the probes. There is now a Refresh
 *      status action, and a credential nobody has verified renders as
 *      "stored, unverified" rather than as a green connected badge.
 *  (c) There was no surface for skill API keys in this UI.
 *  (d) The chained STT and TTS pickers passed the SAME settings key, so
 *      each pick wiped the other, and the key they wrote had no reader.
 *      They now write voice.chained.stt_provider / tts_provider, which
 *      VoiceRouter._resolve_chained_config resolves.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { cleanup, fireEvent, waitFor } from '@testing-library/react';
import { renderV2 } from '../_helpers/renderV2';
import Settings from '../../pages/Settings';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const VOICE_STATUS = {
  realtime_available: true,
  audio_available: true,
  active_realtime_sessions: 0,
};

function makeFetcher({
  calls = [],
  providers = [],
  haUrl = '',
  skillKeys = { ok: true, needs_key: [], configured: [] },
  tokenResponse = { ok: true, provider: 'home_assistant', base_url: 'http://10.0.0.4:8123', connected: true },
  chained = {},
  voiceProviders = [],
} = {}) {
  return (url, init) => {
    calls.push({ url, method: init?.method || 'GET', body: init?.body });

    if (url.includes('/api/integrations/refresh')) {
      return { ok: true, results: { spotify: false }, statuses: {} };
    }
    if (url.includes('/api/integrations/token')) return tokenResponse;
    if (url.includes('/api/integrations/disconnect')) return { ok: true };
    if (url.includes('/api/integrations')) {
      return { providers, home_assistant_url: haUrl, probe_statuses: {} };
    }
    if (url.includes('/api/skills/keys')) return skillKeys;
    if (url.match(/\/api\/skills\/[^/]+\/key/)) return { ok: true, persisted: true, has_key: true };
    if (url.includes('/api/voice/providers/probe')) return { ok: true, reason: 'ok' };
    if (url.includes('/api/voice/providers')) return { providers: voiceProviders };
    if (url.includes('/api/voice/status')) return VOICE_STATUS;
    if (url.includes('/api/ambient/wake_word/status')) return { enabled: false, supported: true };
    if (url.includes('/api/config/update')) return { ok: true };
    if (url.includes('/api/config')) {
      return {
        audio: { realtime_providers: ['openai'], realtime_model: '' },
        voice: { chained },
      };
    }
    return {};
  };
}

const SPOTIFY_VERIFIED = {
  id: 'spotify', name: 'Spotify', auth_type: 'oauth2',
  connected: true, has_client_id: true, probe_verified: true, probe_reason: 'ok',
};
const SPOTIFY_UNVERIFIED = {
  id: 'spotify', name: 'Spotify', auth_type: 'oauth2',
  connected: true, has_client_id: true, probe_verified: false, probe_reason: '',
};

// ── (a) Home Assistant URL ───────────────────────────────────────

describe('Settings -> Integrations -> Home Assistant', () => {
  it('sends the pasted URL alongside the token', async () => {
    const calls = [];
    const { getByText, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({ calls }),
    });
    fireEvent.click(getByText(/^Integrations$/));

    const urlField = await findByTestId('ha-url');
    fireEvent.change(urlField, { target: { value: '10.0.0.4:8123' } });
    fireEvent.change(await findByTestId('ha-token'), { target: { value: 'llat' } });
    fireEvent.click(await findByTestId('ha-save'));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes('/api/integrations/token') && c.method === 'POST');
      expect(post).toBeTruthy();
      const body = JSON.parse(post.body);
      expect(body.provider_id).toBe('home_assistant');
      expect(body.url).toBe('10.0.0.4:8123');
      expect(body.token).toBe('llat');
    });
  });

  it('prefills the URL the brain is currently using', async () => {
    const { getByText, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({ haUrl: 'http://192.168.1.9:8123' }),
    });
    fireEvent.click(getByText(/^Integrations$/));
    const urlField = await findByTestId('ha-url');
    await waitFor(() => expect(urlField.value).toBe('http://192.168.1.9:8123'));
  });

  it('reports a failed probe instead of a hopeful "saved"', async () => {
    const { getByText, findByTestId, findByRole } = renderV2(<Settings />, {
      fetch: makeFetcher({
        tokenResponse: {
          ok: true, provider: 'home_assistant', base_url: 'http://10.0.0.4:8123',
          connected: false, probe: { reason: 'network_error' },
        },
      }),
    });
    fireEvent.click(getByText(/^Integrations$/));
    fireEvent.change(await findByTestId('ha-token'), { target: { value: 'llat' } });
    fireEvent.click(await findByTestId('ha-save'));

    const alert = await findByRole('alert');
    expect(alert.textContent).toMatch(/did not answer/i);
    expect(alert.textContent).toMatch(/network_error/);
  });

  it('refuses to post an empty form', async () => {
    const calls = [];
    const { getByText, findByTestId, findByRole } = renderV2(<Settings />, {
      fetch: makeFetcher({ calls }),
    });
    fireEvent.click(getByText(/^Integrations$/));
    fireEvent.click(await findByTestId('ha-save'));
    expect((await findByRole('alert')).textContent).toMatch(/token, a URL, or both/i);
    expect(calls.some((c) => c.url.includes('/api/integrations/token'))).toBe(false);
  });
});

// ── (b) The badge tells the truth ────────────────────────────────

describe('Settings -> Integrations -> probe status', () => {
  it('renders an unverified credential as unverified, not connected', async () => {
    const { getByText, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({ providers: [SPOTIFY_UNVERIFIED] }),
    });
    fireEvent.click(getByText(/^Integrations$/));
    const card = await findByTestId('oauth-card-spotify');
    expect(card.textContent).toMatch(/stored, unverified/i);
  });

  it('renders a probe-backed connection as connected', async () => {
    const { getByText, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({ providers: [SPOTIFY_VERIFIED] }),
    });
    fireEvent.click(getByText(/^Integrations$/));
    const card = await findByTestId('oauth-card-spotify');
    expect(card.textContent).toMatch(/connected/);
    expect(card.textContent).not.toMatch(/unverified/i);
  });

  it('Refresh status forces a re-probe and re-reads the list', async () => {
    const calls = [];
    const { getByText, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({ calls, providers: [SPOTIFY_UNVERIFIED] }),
    });
    fireEvent.click(getByText(/^Integrations$/));
    fireEvent.click(await findByTestId('integrations-refresh'));

    await waitFor(() => {
      expect(calls.some((c) => c.url.includes('/api/integrations/refresh') && c.method === 'POST')).toBe(true);
    });
    await waitFor(() => {
      const listings = calls.filter((c) => c.url.endsWith('/api/integrations') && c.method === 'GET');
      expect(listings.length).toBeGreaterThan(1);
    });
  });
});

// ── (c) Skill API keys ───────────────────────────────────────────

describe('Settings -> Integrations -> skill API keys', () => {
  const NEEDS_KEY = {
    ok: true,
    needs_key: [
      { skill_id: 'weather', name: 'Weather', auth_type: 'api_key', has_key: false },
    ],
    configured: [],
  };

  it('posts a skill key to the route the executor reads', async () => {
    const calls = [];
    const { getByText, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({ calls, skillKeys: NEEDS_KEY }),
    });
    fireEvent.click(getByText(/^Integrations$/));

    fireEvent.change(await findByTestId('skill-key-input-weather'), { target: { value: 'w-secret' } });
    fireEvent.click(await findByTestId('skill-key-save-weather'));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes('/api/skills/weather/key') && c.method === 'POST');
      expect(post).toBeTruthy();
      expect(JSON.parse(post.body).key).toBe('w-secret');
    });
  });

  it('says so when the key is not durable', async () => {
    const { getByText, findByTestId } = renderV2(<Settings />, {
      fetch: (url, init) => {
        if (url.match(/\/api\/skills\/[^/]+\/key/) && init?.method === 'POST') {
          return { ok: true, persisted: false, has_key: true };
        }
        return makeFetcher({ skillKeys: NEEDS_KEY })(url, init);
      },
    });
    fireEvent.click(getByText(/^Integrations$/));
    fireEvent.change(await findByTestId('skill-key-input-weather'), { target: { value: 'w-secret' } });
    fireEvent.click(await findByTestId('skill-key-save-weather'));

    const row = await findByTestId('skill-key-weather');
    await waitFor(() => expect(row.textContent).toMatch(/this session only/i));
  });
});

// ── (d) Chained pickers no longer clobber each other ─────────────

describe('Settings -> Voice -> chained pickers', () => {
  const VOICE_CATALOGUE = [
    { id: 'deepgram', kind: 'stt', name: 'Deepgram', configured: true, probe_status: 'ok' },
    { id: 'groq_whisper', kind: 'stt', name: 'Groq Whisper', configured: true, probe_status: 'ok' },
    { id: 'elevenlabs', kind: 'tts', name: 'ElevenLabs', configured: true, probe_status: 'ok' },
    { id: 'openai_tts', kind: 'tts', name: 'OpenAI TTS', configured: true, probe_status: 'ok' },
  ];

  it('STT writes voice.chained.stt_provider and keeps the TTS pick', async () => {
    const calls = [];
    const { getByText, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({
        calls,
        voiceProviders: VOICE_CATALOGUE,
        chained: { stt_provider: 'deepgram', tts_provider: 'elevenlabs' },
      }),
    });
    fireEvent.click(getByText(/^Voice$/));
    fireEvent.click(await findByTestId('voice-use-groq_whisper'));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes('/api/config/update') && c.method === 'POST');
      expect(post).toBeTruthy();
      const body = JSON.parse(post.body);
      expect(body.section).toBe('voice');
      expect(body.key).toBe('chained');
      expect(body.value.stt_provider).toBe('groq_whisper');
      // The bug: both groups shared one key, so choosing an STT engine
      // erased the TTS choice.
      expect(body.value.tts_provider).toBe('elevenlabs');
    });
  });

  it('TTS writes the registry id, not the catalogue id', async () => {
    const calls = [];
    const { getByText, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({
        calls,
        voiceProviders: VOICE_CATALOGUE,
        chained: { stt_provider: 'deepgram', tts_provider: 'elevenlabs' },
      }),
    });
    fireEvent.click(getByText(/^Voice$/));
    fireEvent.click(await findByTestId('voice-use-openai_tts'));

    await waitFor(() => {
      const post = calls.find((c) => c.url.includes('/api/config/update') && c.method === 'POST');
      const body = JSON.parse(post.body);
      // `openai_tts` is the catalogue id; `voice/tts_providers` registers
      // it as `openai`, and the pipeline resolves the registry name.
      expect(body.value.tts_provider).toBe('openai');
      expect(body.value.stt_provider).toBe('deepgram');
    });
  });

  it('marks the stored chained picks as in use', async () => {
    const { getByText, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({
        voiceProviders: VOICE_CATALOGUE,
        chained: { stt_provider: 'deepgram', tts_provider: 'openai' },
      }),
    });
    fireEvent.click(getByText(/^Voice$/));
    expect((await findByTestId('voice-use-deepgram')).textContent).toMatch(/in use/i);
    expect((await findByTestId('voice-use-openai_tts')).textContent).toMatch(/in use/i);
    expect((await findByTestId('voice-use-groq_whisper')).textContent).toMatch(/use/i);
  });

  it('never writes the dead audio.chained_providers key', async () => {
    const calls = [];
    const { getByText, findByTestId } = renderV2(<Settings />, {
      fetch: makeFetcher({ calls, voiceProviders: VOICE_CATALOGUE, chained: {} }),
    });
    fireEvent.click(getByText(/^Voice$/));
    fireEvent.click(await findByTestId('voice-use-deepgram'));

    await waitFor(() => {
      expect(calls.some((c) => c.url.includes('/api/config/update'))).toBe(true);
    });
    const wrote = calls
      .filter((c) => c.url.includes('/api/config/update') && c.body)
      .map((c) => JSON.parse(c.body));
    expect(wrote.some((b) => b.key === 'chained_providers')).toBe(false);
  });
});
