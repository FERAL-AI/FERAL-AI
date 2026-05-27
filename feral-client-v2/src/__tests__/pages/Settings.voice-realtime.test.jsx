/**
 * Lane U2 — Settings → Voice realtime model picker.
 *
 * Pins the contract:
 *   (a) When /api/voice/providers attaches `models[]` + `default_model`
 *       to the openai_realtime entry AND the user has activated the
 *       provider, the Voice card renders a <select> with
 *       data-testid="openai-realtime-model-picker" populated from the
 *       catalogue list — NOT the LLM-style free-text fallback.
 *   (b) When `models[]` is missing or empty the dropdown is hidden
 *       and the existing Use/Test buttons render unchanged.
 *   (c) Changing the dropdown persists via /api/config/update under
 *       audio.realtime_model.
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

const WAKE_WORD = { enabled: false, supported: true };

function makeVoiceFetcher({
  models = ['gpt-realtime', 'gpt-realtime-mini'],
  defaultModel = 'gpt-realtime',
  activeRealtime = ['openai_realtime'],
  realtimeModel = '',
  capture,
}) {
  return (url, init) => {
    if (capture) capture.push({ url, method: init?.method || 'GET', body: init?.body });

    if (url.includes('/api/voice/providers/probe')) {
      return { ok: true, reason: 'ok', latency_ms: 12 };
    }
    if (url.includes('/api/voice/providers')) {
      const providers = [
        {
          id: 'openai_realtime',
          kind: 'realtime',
          name: 'OpenAI Realtime',
          configured: true,
          probe_status: 'ok',
          probe_detail: 'OK',
          latency_ms: 12.4,
        },
        {
          id: 'gemini_live',
          kind: 'realtime',
          name: 'Gemini Live',
          configured: false,
          probe_status: 'no_key',
          probe_detail: '',
          latency_ms: 0,
        },
      ];
      if (models && models.length > 0) {
        providers[0].models = models;
        providers[0].default_model = defaultModel;
      }
      return { providers };
    }
    if (url.includes('/api/voice/status')) return VOICE_STATUS;
    if (url.includes('/api/ambient/wake_word/status')) return WAKE_WORD;
    if (url.includes('/api/config/update')) return { ok: true };
    if (url.includes('/api/config')) {
      return {
        audio: {
          realtime_providers: activeRealtime,
          chained_providers: [],
          realtime_model: realtimeModel,
        },
      };
    }
    return {};
  };
}

describe('Settings → Voice realtime model picker (Lane U2)', () => {
  it('openai_realtime active card renders model dropdown when models provided', async () => {
    const { getByText, findByTestId, queryByPlaceholderText } = renderV2(
      <Settings />,
      { fetch: makeVoiceFetcher({}) },
    );
    fireEvent.click(getByText(/^Voice$/));
    const picker = await findByTestId('openai-realtime-model-picker');
    expect(picker).toBeInTheDocument();
    expect(picker.tagName).toBe('SELECT');
    const options = Array.from(picker.querySelectorAll('option')).map((o) => o.value);
    expect(options).toContain('gpt-realtime');
    expect(options).toContain('gpt-realtime-mini');
    // The LLM picker's free-text fallback MUST NOT appear here.
    expect(queryByPlaceholderText(/type any model/i)).toBeNull();
  });

  it('hides dropdown when models[] is missing', async () => {
    const { getByText, findByTestId, queryByTestId } = renderV2(
      <Settings />,
      { fetch: makeVoiceFetcher({ models: null, defaultModel: null }) },
    );
    fireEvent.click(getByText(/^Voice$/));
    await findByTestId('voice-card-openai_realtime');
    expect(queryByTestId('openai-realtime-model-picker')).toBeNull();
  });

  it('hides dropdown when models[] is empty', async () => {
    const { getByText, findByTestId, queryByTestId } = renderV2(
      <Settings />,
      { fetch: makeVoiceFetcher({ models: [], defaultModel: '' }) },
    );
    fireEvent.click(getByText(/^Voice$/));
    await findByTestId('voice-card-openai_realtime');
    expect(queryByTestId('openai-realtime-model-picker')).toBeNull();
  });

  it('hides dropdown when openai_realtime is not in the active list', async () => {
    const { getByText, findByTestId, queryByTestId } = renderV2(
      <Settings />,
      { fetch: makeVoiceFetcher({ activeRealtime: [] }) },
    );
    fireEvent.click(getByText(/^Voice$/));
    await findByTestId('voice-card-openai_realtime');
    expect(queryByTestId('openai-realtime-model-picker')).toBeNull();
  });

  it('change persists to audio.realtime_model via /api/config/update', async () => {
    const calls = [];
    const { getByText, findByTestId } = renderV2(
      <Settings />,
      { fetch: makeVoiceFetcher({ capture: calls }) },
    );
    fireEvent.click(getByText(/^Voice$/));
    const picker = await findByTestId('openai-realtime-model-picker');
    fireEvent.change(picker, { target: { value: 'gpt-realtime-mini' } });
    await waitFor(() => {
      const hit = calls.find((c) => (
        c.url.includes('/api/config/update')
        && c.method === 'POST'
        && typeof c.body === 'string'
        && c.body.includes('realtime_model')
        && c.body.includes('gpt-realtime-mini')
      ));
      expect(hit).toBeTruthy();
    });
  });
});
