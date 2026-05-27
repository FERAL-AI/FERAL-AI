import React, { useCallback, useEffect, useRef, useState } from 'react';
import { apiJson, apiFetch } from '../../lib/api';

const VOICE_MODES = [
  { value: 'openai_realtime', label: 'OpenAI Realtime' },
  { value: 'gemini_live', label: 'Gemini Live' },
  { value: 'chained', label: 'Chained' },
];

const STT_PROVIDERS = [
  { value: 'deepgram', label: 'Deepgram' },
  { value: 'openai_whisper', label: 'OpenAI Whisper' },
  { value: 'groq_whisper', label: 'Groq Whisper' },
];

const STT_MODELS = {
  deepgram: [{ value: 'nova-3', label: 'Nova 3' }, { value: 'nova-2', label: 'Nova 2' }],
  openai_whisper: [{ value: 'whisper-1', label: 'Whisper 1' }],
  groq_whisper: [{ value: 'whisper-large-v3', label: 'Whisper Large v3' }],
};

const TTS_PROVIDERS = [
  { value: 'openai', label: 'OpenAI' },
  { value: 'elevenlabs', label: 'ElevenLabs' },
];

const TTS_VOICES = {
  openai: [
    { value: 'alloy', label: 'Alloy' },
    { value: 'echo', label: 'Echo' },
    { value: 'fable', label: 'Fable' },
    { value: 'onyx', label: 'Onyx' },
    { value: 'nova', label: 'Nova' },
    { value: 'shimmer', label: 'Shimmer' },
  ],
  elevenlabs: [
    { value: 'rachel', label: 'Rachel' },
    { value: 'adam', label: 'Adam' },
  ],
};

const OPENAI_VOICES = [
  { value: 'marin', label: 'Marin' },
  { value: 'alloy', label: 'Alloy' },
  { value: 'echo', label: 'Echo' },
  { value: 'ash', label: 'Ash' },
  { value: 'ballad', label: 'Ballad' },
  { value: 'coral', label: 'Coral' },
  { value: 'sage', label: 'Sage' },
  { value: 'verse', label: 'Verse' },
];

const GEMINI_MODELS = [
  { value: 'gemini-2.0-flash-exp', label: 'Gemini 2.0 Flash (exp)' },
  { value: 'gemini-2.0-flash', label: 'Gemini 2.0 Flash' },
];

// Lane U2 — OpenAI Realtime model fallback list. Mirrors the GA +
// preview ids surfaced by /api/voice/providers; kept inline so the
// phone Settings card can render even before the catalogue fetch
// completes. The Settings card upgrades to the live list when
// /api/voice/providers returns one.
const OPENAI_REALTIME_MODELS = [
  { value: 'gpt-realtime', label: 'GPT Realtime (GA)' },
  { value: 'gpt-realtime-mini', label: 'GPT Realtime Mini' },
  { value: 'gpt-4o-realtime-preview', label: 'GPT-4o Realtime (preview)' },
  { value: 'gpt-4o-mini-realtime-preview', label: 'GPT-4o Mini Realtime (preview)' },
];

const DEFAULT_VOICE_CONFIG = {
  mode: 'openai_realtime',
  realtime: { openai_voice: 'marin', openai_model: 'gpt-realtime', gemini_model: 'gemini-2.0-flash-exp' },
  chained: { stt_provider: 'deepgram', stt_model: 'nova-3', tts_provider: 'openai', tts_voice: 'alloy' },
};

export default function SettingsPanel({ initialConfig }) {
  const [voiceConfig, setVoiceConfig] = useState(null);
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(true);
  const debounceRef = useRef(null);
  const savedTimerRef = useRef(null);

  const loadConfig = useCallback(async () => {
    try {
      const data = initialConfig || await apiJson('/api/config/settings').catch(() => apiJson('/api/config'));
      const voice = data?.voice || data?.settings?.voice || DEFAULT_VOICE_CONFIG;
      setVoiceConfig({
        mode: voice.mode || DEFAULT_VOICE_CONFIG.mode,
        realtime: { ...DEFAULT_VOICE_CONFIG.realtime, ...voice.realtime },
        chained: { ...DEFAULT_VOICE_CONFIG.chained, ...voice.chained },
      });
    } catch {
      setVoiceConfig({ ...DEFAULT_VOICE_CONFIG });
    } finally {
      setLoading(false);
    }
  }, [initialConfig]);

  useEffect(() => { loadConfig(); }, [loadConfig]);

  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
    };
  }, []);

  const persistVoice = useCallback((nextVoice) => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      try {
        const res = await apiFetch('/api/config/settings', {
          method: 'PATCH',
          body: JSON.stringify({ voice: nextVoice }),
        }).catch(() =>
          apiFetch('/api/config/update', {
            method: 'POST',
            body: JSON.stringify({ section: 'voice', key: 'voice', value: nextVoice }),
          })
        );
        if (res.ok) {
          setSaved(true);
          if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
          savedTimerRef.current = setTimeout(() => setSaved(false), 2000);
        }
      } catch { /* best effort */ }
    }, 300);
  }, []);

  const updateVoice = useCallback((updater) => {
    setVoiceConfig((prev) => {
      const next = typeof updater === 'function' ? updater(prev) : { ...prev, ...updater };
      persistVoice(next);
      return next;
    });
  }, [persistVoice]);

  if (loading || !voiceConfig) {
    return <div className="phone-settings" data-testid="phone-settings"><p>Loading settings...</p></div>;
  }

  return (
    <div className="phone-settings" data-testid="phone-settings">
      <h2>Settings</h2>
      <section className="phone-settings-section" data-testid="voice-section">
        <h3>Voice</h3>
        {saved && <div className="phone-settings-saved" data-testid="saved-indicator" role="status">Saved</div>}
        <div className="phone-settings-row">
          <label>Mode</label>
          <div className="phone-settings-segmented" data-testid="mode-picker" role="radiogroup" aria-label="Voice mode">
            {VOICE_MODES.map((m) => (
              <button key={m.value} type="button" role="radio" aria-checked={voiceConfig.mode === m.value}
                className={`phone-settings-seg-btn${voiceConfig.mode === m.value ? ' is-active' : ''}`}
                onClick={() => updateVoice({ mode: m.value })} data-testid={`mode-${m.value}`}>{m.label}</button>
            ))}
          </div>
        </div>

        {voiceConfig.mode === 'openai_realtime' && (
          <div className="phone-settings-sub" data-testid="openai-sub">
            <div className="phone-settings-row">
              <label htmlFor="openai-model-select">Model</label>
              <select id="openai-model-select" value={voiceConfig.realtime?.openai_model || 'gpt-realtime'}
                onChange={(e) => {
                  const next = e.target.value;
                  updateVoice((prev) => ({ ...prev, realtime: { ...prev.realtime, openai_model: next } }));
                  // Lane U2 — keep audio.realtime_model in lockstep
                  // with the phone picker so the Brain's RealtimeProxy
                  // picks up the operator's choice on the next session
                  // open regardless of which surface set it.
                  apiFetch('/api/config/update', {
                    method: 'POST',
                    body: JSON.stringify({ section: 'audio', key: 'realtime_model', value: next }),
                  }).catch(() => {});
                }}
                data-testid="openai-realtime-model-picker">
                {OPENAI_REALTIME_MODELS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
            </div>
            <div className="phone-settings-row">
              <label htmlFor="openai-voice-select">Voice</label>
              <select id="openai-voice-select" value={voiceConfig.realtime?.openai_voice || 'marin'}
                onChange={(e) => updateVoice((prev) => ({ ...prev, realtime: { ...prev.realtime, openai_voice: e.target.value } }))}
                data-testid="openai-voice-picker">
                {OPENAI_VOICES.map((v) => <option key={v.value} value={v.value}>{v.label}</option>)}
              </select>
            </div>
          </div>
        )}

        {voiceConfig.mode === 'gemini_live' && (
          <div className="phone-settings-sub" data-testid="gemini-sub">
            <div className="phone-settings-row">
              <label htmlFor="gemini-model-select">Model</label>
              <select id="gemini-model-select" value={voiceConfig.realtime?.gemini_model || 'gemini-2.0-flash-exp'}
                onChange={(e) => updateVoice((prev) => ({ ...prev, realtime: { ...prev.realtime, gemini_model: e.target.value } }))}
                data-testid="gemini-model-picker">
                {GEMINI_MODELS.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
            </div>
          </div>
        )}

        {voiceConfig.mode === 'chained' && (
          <div className="phone-settings-sub" data-testid="chained-sub">
            <div className="phone-settings-row">
              <label htmlFor="stt-provider-select">STT Provider</label>
              <select id="stt-provider-select" value={voiceConfig.chained?.stt_provider || 'deepgram'}
                onChange={(e) => {
                  const provider = e.target.value;
                  const defaultModel = (STT_MODELS[provider] || [])[0]?.value || '';
                  updateVoice((prev) => ({ ...prev, chained: { ...prev.chained, stt_provider: provider, stt_model: defaultModel } }));
                }} data-testid="stt-provider-picker">
                {STT_PROVIDERS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
            </div>
            <div className="phone-settings-row">
              <label htmlFor="stt-model-select">STT Model</label>
              <select id="stt-model-select" value={voiceConfig.chained?.stt_model || ''}
                onChange={(e) => updateVoice((prev) => ({ ...prev, chained: { ...prev.chained, stt_model: e.target.value } }))}
                data-testid="stt-model-picker">
                {(STT_MODELS[voiceConfig.chained?.stt_provider] || []).map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
              </select>
            </div>
            <div className="phone-settings-row">
              <label htmlFor="tts-provider-select">TTS Provider</label>
              <select id="tts-provider-select" value={voiceConfig.chained?.tts_provider || 'openai'}
                onChange={(e) => {
                  const provider = e.target.value;
                  const defaultVoice = (TTS_VOICES[provider] || [])[0]?.value || '';
                  updateVoice((prev) => ({ ...prev, chained: { ...prev.chained, tts_provider: provider, tts_voice: defaultVoice } }));
                }} data-testid="tts-provider-picker">
                {TTS_PROVIDERS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
            </div>
            <div className="phone-settings-row">
              <label htmlFor="tts-voice-select">TTS Voice</label>
              <select id="tts-voice-select" value={voiceConfig.chained?.tts_voice || ''}
                onChange={(e) => updateVoice((prev) => ({ ...prev, chained: { ...prev.chained, tts_voice: e.target.value } }))}
                data-testid="tts-voice-picker">
                {(TTS_VOICES[voiceConfig.chained?.tts_provider] || []).map((v) => <option key={v.value} value={v.value}>{v.label}</option>)}
              </select>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
