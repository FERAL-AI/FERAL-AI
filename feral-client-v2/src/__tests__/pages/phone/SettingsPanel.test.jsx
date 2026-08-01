import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, waitFor, act } from '@testing-library/react';
import SettingsPanel from '../../../pages/phone/SettingsPanel';

function installFetchMock(responder) {
  const resolveBody = typeof responder === 'function' ? responder : () => ({});
  vi.stubGlobal('fetch', vi.fn((input, init) => {
    const url = typeof input === 'string' ? input : input?.url || '';
    const body = resolveBody(url, init) ?? {};
    return Promise.resolve({
      ok: true, status: 200, statusText: 'OK',
      json: () => Promise.resolve(body),
      text: () => Promise.resolve(JSON.stringify(body)),
      headers: new Map(),
    });
  }));
}

const defaultConfig = {
  voice: {
    mode: 'openai_realtime',
    realtime: { openai_voice: 'marin', gemini_model: 'gemini-2.0-flash-exp' },
    chained: { stt_provider: 'deepgram', stt_model: 'nova-3', tts_provider: 'openai', tts_voice: 'alloy' },
  },
};

async function renderSettings(config = defaultConfig) {
  installFetchMock((url) => {
    if (url.includes('/api/config')) return config;
    return {};
  });
  let result;
  await act(async () => {
    result = render(<SettingsPanel initialConfig={config} />);
  });
  return result;
}

describe('SettingsPanel', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the voice section with mode picker', async () => {
    const { getByTestId } = await renderSettings();
    expect(getByTestId('voice-section')).toBeTruthy();
    expect(getByTestId('mode-picker')).toBeTruthy();
  });

  it('renders current config — openai_realtime active', async () => {
    const { getByTestId } = await renderSettings();
    const btn = getByTestId('mode-openai_realtime');
    expect(btn.getAttribute('aria-checked')).toBe('true');
  });

  it('selecting chained reveals sub-pickers', async () => {
    const { getByTestId, queryByTestId } = await renderSettings();
    expect(queryByTestId('chained-sub')).toBeNull();
    await act(async () => { fireEvent.click(getByTestId('mode-chained')); });
    expect(getByTestId('chained-sub')).toBeTruthy();
    expect(getByTestId('stt-provider-picker')).toBeTruthy();
    expect(getByTestId('tts-provider-picker')).toBeTruthy();
  });

  it('openai_realtime mode shows voice picker', async () => {
    const { getByTestId, queryByTestId } = await renderSettings();
    expect(getByTestId('openai-sub')).toBeTruthy();
    expect(getByTestId('openai-voice-picker')).toBeTruthy();
    expect(queryByTestId('chained-sub')).toBeNull();
  });

  it('gemini_live mode shows model picker', async () => {
    const geminiConfig = { ...defaultConfig, voice: { ...defaultConfig.voice, mode: 'gemini_live' } };
    const { getByTestId } = await renderSettings(geminiConfig);
    expect(getByTestId('gemini-sub')).toBeTruthy();
    expect(getByTestId('gemini-model-picker')).toBeTruthy();
  });

  it('debounced write fires after 300ms on mode change', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { getByTestId } = await renderSettings();
    await act(async () => { fireEvent.click(getByTestId('mode-chained')); });
    await act(async () => { vi.advanceTimersByTime(350); });
    vi.useRealTimers();
    await waitFor(() => {
      const patchCall = fetch.mock.calls.find(
        ([url, init]) => url.includes('/api/config') && (init?.method === 'PATCH' || init?.method === 'POST')
      );
      expect(patchCall).toBeTruthy();
    });
  });

  it('"Saved" indicator shows after successful write', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { getByTestId, queryByTestId } = await renderSettings();
    expect(queryByTestId('saved-indicator')).toBeNull();
    await act(async () => { fireEvent.click(getByTestId('mode-chained')); });
    await act(async () => { vi.advanceTimersByTime(350); });
    vi.useRealTimers();
    await waitFor(() => { expect(queryByTestId('saved-indicator')).toBeTruthy(); });
  });

  it('changing STT provider updates STT model to provider default', async () => {
    const chainedConfig = { ...defaultConfig, voice: { ...defaultConfig.voice, mode: 'chained' } };
    const { getByTestId } = await renderSettings(chainedConfig);
    await act(async () => {
      fireEvent.change(getByTestId('stt-provider-picker'), { target: { value: 'openai_whisper' } });
    });
    expect(getByTestId('stt-model-picker').value).toBe('whisper-1');
  });

  it('changing TTS provider updates TTS voice to provider default', async () => {
    const chainedConfig = { ...defaultConfig, voice: { ...defaultConfig.voice, mode: 'chained' } };
    const { getByTestId } = await renderSettings(chainedConfig);
    await act(async () => {
      fireEvent.change(getByTestId('tts-provider-picker'), { target: { value: 'openai' } });
    });
    expect(getByTestId('tts-voice-picker').value).toBe('alloy');
  });

  it('offers exactly the TTS providers the brain registers', async () => {
    // feral-core/voice/tts_providers/__init__.py registers openai,
    // elevenlabs and cartesia. Anything else makes get_tts_provider
    // raise and the chained session never opens.
    const chainedConfig = { ...defaultConfig, voice: { ...defaultConfig.voice, mode: 'chained' } };
    const { getByTestId } = await renderSettings(chainedConfig);
    const values = [...getByTestId('tts-provider-picker').options].map((o) => o.value);
    expect(values).toEqual(['openai', 'elevenlabs', 'cartesia']);
  });

  it('offers exactly the STT providers the brain registers', async () => {
    const chainedConfig = { ...defaultConfig, voice: { ...defaultConfig.voice, mode: 'chained' } };
    const { getByTestId } = await renderSettings(chainedConfig);
    const values = [...getByTestId('stt-provider-picker').options].map((o) => o.value);
    expect(values).toEqual(['deepgram', 'openai_whisper', 'groq_whisper']);
  });

  it('elevenlabs and cartesia expose a voice ID field, not a name dropdown', async () => {
    // Those two providers address voices by opaque id. The old
    // friendly-name dropdown wrote `chained.tts_voice`, which the brain
    // ignores for both (voice/router.py reads `tts_voice_id`), so every
    // pick silently did nothing.
    const chainedConfig = { ...defaultConfig, voice: { ...defaultConfig.voice, mode: 'chained' } };
    const { getByTestId, queryByTestId } = await renderSettings(chainedConfig);
    await act(async () => {
      fireEvent.change(getByTestId('tts-provider-picker'), { target: { value: 'elevenlabs' } });
    });
    expect(queryByTestId('tts-voice-picker')).toBeNull();
    expect(getByTestId('tts-voice-id-input')).toBeTruthy();

    await act(async () => {
      fireEvent.change(getByTestId('tts-provider-picker'), { target: { value: 'cartesia' } });
    });
    expect(getByTestId('tts-voice-id-input')).toBeTruthy();
  });

  it('persists the chained pick at voice.chained, not voice.voice.chained', async () => {
    // Regression: the /api/config/update fallback used to send
    // {section:'voice', key:'voice'}, which lands at
    // settings.voice.voice.chained, one level below every reader
    // (api/server.py reads voice.mode, voice/router.py reads
    // voice.chained.*). PATCH /api/config/settings does not exist on
    // the brain, so this fallback is the path every save takes.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const chainedConfig = { ...defaultConfig, voice: { ...defaultConfig.voice, mode: 'chained' } };
    installFetchMock((url, init) => {
      if (init?.method === 'PATCH') throw new Error('404');
      if (url.includes('/api/config')) return chainedConfig;
      return {};
    });
    let utils;
    await act(async () => { utils = render(<SettingsPanel initialConfig={chainedConfig} />); });
    await act(async () => {
      fireEvent.change(utils.getByTestId('stt-provider-picker'), { target: { value: 'groq_whisper' } });
    });
    await act(async () => { vi.advanceTimersByTime(350); });
    vi.useRealTimers();

    await waitFor(() => {
      const updateCalls = fetch.mock.calls
        .filter(([url, init]) => url.includes('/api/config/update') && init?.method === 'POST')
        .map(([, init]) => JSON.parse(init.body));
      const chainedWrite = updateCalls.find((b) => b.section === 'voice' && b.key === 'chained');
      expect(chainedWrite).toBeTruthy();
      expect(chainedWrite.value.stt_provider).toBe('groq_whisper');
      expect(chainedWrite.value.stt_model).toBe('whisper-large-v3');
      // The mode must land at voice.mode for api/server.py to see it.
      expect(updateCalls.find((b) => b.section === 'voice' && b.key === 'mode')).toBeTruthy();
      // Nothing may be written under the doubled `voice.voice` key.
      expect(updateCalls.find((b) => b.section === 'voice' && b.key === 'voice')).toBeFalsy();
    });
  });
});
