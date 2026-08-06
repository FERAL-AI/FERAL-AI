/**
 * Setup — browser equivalent of the terminal `feral setup` wizard.
 *
 * Reads + writes the same settings.json + credentials.json the CLI
 * touches, via the REST endpoints added in
 * feral-core/api/routes/llm.py and feral-core/api/routes/audio.py.
 * That contract is the single source of truth so a user can start in
 * the terminal and finish here, or vice versa.
 *
 * Steps:
 *   1. Welcome
 *   2. LLM provider (side-by-side table, fuzzy match, free-text model)
 *   3. Audio (STT + TTS, local vs cloud)
 *   4. Identity (name / occupation / location)
 *   5. Pair your phone (Mode A LAN / Mode B localhost / Mode C remote)
 *   6. Done — finish posts /api/setup/complete
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ChevronRight, ChevronLeft, RefreshCw, CheckCircle2, Smartphone, Wifi, Globe, Laptop2 } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Tabs from '../ui/Tabs';
import StatusDot from '../ui/StatusDot';
import { apiJson, apiFetch } from '../lib/api';


const STEPS = [
  { id: 'welcome', label: 'Welcome' },
  { id: 'llm', label: 'LLM provider' },
  { id: 'audio', label: 'Voice (STT + TTS)' },
  { id: 'identity', label: 'About you' },
  { id: 'pair', label: 'Pair your phone' },
  { id: 'done', label: 'Ready' },
];


function statusTone(status) {
  if (status === 'ready') return 'live';
  if (status === 'needs_api_key') return 'warn';
  if (status === 'unreachable') return 'error';
  return 'neutral';
}


function statusLabel(s) {
  if (s === 'ready') return 'ready';
  if (s === 'needs_api_key') return 'needs API key';
  if (s === 'unreachable') return 'unreachable';
  if (s === 'unavailable') return 'not installed';
  return s || '';
}

function formatApiDetail(body, fallback = 'request failed') {
  if (!body || typeof body !== 'object') return fallback;
  const detail = body.detail;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  if (detail && typeof detail === 'object') {
    const message = typeof detail.message === 'string' ? detail.message.trim() : '';
    const remediation = typeof detail.remediation === 'string' ? detail.remediation.trim() : '';
    const code = typeof detail.code === 'string' ? detail.code.trim() : '';
    if (message && remediation) return `${message} ${remediation}`;
    if (message) return message;
    if (remediation) return remediation;
    if (code) return code;
  }
  if (typeof body.error === 'string' && body.error.trim()) return body.error.trim();
  return fallback;
}

const ACCESS_MODE_LABELS = {
  local: 'Same WiFi',
  remote: 'Anywhere',
  localhost: 'This Mac only',
};


export default function Setup() {
  const navigate = useNavigate();
  const [stepIdx, setStepIdx] = useState(0);
  const step = STEPS[stepIdx];

  const [providers, setProviders] = useState([]);
  const [pickedProvider, setPickedProvider] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [models, setModels] = useState([]);
  const [modelSource, setModelSource] = useState('');
  const [pickedModel, setPickedModel] = useState('');
  const [llmError, setLlmError] = useState(null);
  const [llmBusy, setLlmBusy] = useState(false);

  const [audioProviders, setAudioProviders] = useState({ stt: [], tts: [] });
  const [audio, setAudio] = useState({
    stt_provider: 'openai', stt_model: 'whisper-1',
    tts_provider: 'openai', tts_model: 'tts-1', tts_voice: 'nova',
  });
  const [audioError, setAudioError] = useState(null);

  const [identity, setIdentity] = useState({ name: '', occupation: '', location: '' });

  // Phase 4 — pairing step. ``pairChoice`` is one of:
  //   ``""``       — user has not yet picked
  //   ``"local"``  — Mode A LAN
  //   ``"localhost"`` — Mode B (skip pairing)
  //   ``"remote"`` — Mode C (Tailscale)
  // ``pairPayload`` is the response from /api/devices/pair/url after
  // mode is selected; null if not yet fetched or if the mode is
  // localhost (no payload). ``pairError`` surfaces any 409 from the
  // brain — most likely "LAN IP not detected" or "Mode C not configured".
  const [pairChoice, setPairChoice] = useState('');
  const [pairPayload, setPairPayload] = useState(null);
  const [pairError, setPairError] = useState(null);
  // The single action that would make pairing work, when the brain
  // says one exists. Private is the default access mode, so the
  // common refusal is "not exposed yet" and it deserves a button
  // rather than a paragraph telling the user to go find Settings.
  const [pairFix, setPairFix] = useState(null);
  const pickAccessModeRef = React.useRef(null);
  const [pairBusy, setPairBusy] = useState(false);

  const [saved, setSaved] = useState(false);
  const [finishError, setFinishError] = useState(null);

  // Initial fetch — providers, current config
  useEffect(() => {
    (async () => {
      try {
        const [provs, currentLlm, currentAudio, audioAll] = await Promise.allSettled([
          apiJson('/api/llm/providers'),
          apiJson('/api/llm/config'),
          apiJson('/api/audio/config'),
          apiJson('/api/audio/providers'),
        ]);
        if (provs.status === 'fulfilled') {
          setProviders(provs.value?.providers || []);
          const readyLocal = (provs.value?.providers || []).find(p => p.supports_local && p.reachable);
          const readyCloud = (provs.value?.providers || []).find(p => !p.supports_local && p.configured);
          const fallback = (provs.value?.providers || [])[0]?.id || 'openai';
          if (currentLlm.status === 'fulfilled' && currentLlm.value?.provider) {
            setPickedProvider(currentLlm.value.provider);
            setPickedModel(currentLlm.value.model || '');
          } else {
            setPickedProvider((readyLocal || readyCloud || { id: fallback }).id);
          }
        }
        if (audioAll.status === 'fulfilled') {
          setAudioProviders(audioAll.value || { stt: [], tts: [] });
        }
        if (currentAudio.status === 'fulfilled' && currentAudio.value) {
          setAudio((prev) => ({ ...prev, ...currentAudio.value }));
        }
      } catch (e) {
        // Non-fatal; user can still advance.
      }
    })();
  }, []);

  // Whenever provider changes, refresh its model list
  useEffect(() => {
    if (!pickedProvider) return;
    (async () => {
      setLlmError(null);
      try {
        // Default to the conductor-curated chat-ready shortlist so the
        // wizard surfaces "the 6-10 models that actually earn their $$"
        // instead of the raw /v1/models dump (which includes embeddings,
        // whisper-*, tts-*, image models, etc). Backend filter is
        // projection-only — the catalog's raw list is untouched.
        const r = await apiJson(
          `/api/llm/providers/${encodeURIComponent(pickedProvider)}/models`
          + `?live=true&recommended=true&model_class=chat`,
        );
        setModels(r?.models || []);
        setModelSource(r?.source || '');
        if (!pickedModel && r?.models?.length) {
          // Default to descriptor default if present; otherwise first model.
          const desc = providers.find(p => p.id === pickedProvider);
          const defaultModel = desc?.default_model && r.models.includes(desc.default_model)
            ? desc.default_model
            : r.models[0];
          setPickedModel(defaultModel);
        }
      } catch (e) {
        setLlmError(e?.message || 'failed to fetch models');
      }
    })();
  }, [pickedProvider]);  // eslint-disable-line react-hooks/exhaustive-deps

  const refreshProviders = useCallback(async () => {
    try {
      const r = await apiJson('/api/llm/providers');
      setProviders(r?.providers || []);
    } catch (e) {
      setLlmError(e?.message || 'refresh failed');
    }
  }, []);

  const probeProvider = useCallback(async (pid) => {
    try {
      const r = await apiFetch(`/api/llm/providers/${encodeURIComponent(pid)}/probe`, { method: 'POST' });
      if (r.ok) {
        await refreshProviders();
      }
    } catch { /* ignore */ }
  }, [refreshProviders]);

  const saveLlm = useCallback(async () => {
    if (!pickedProvider || !pickedModel) {
      setLlmError('Pick a provider + model before continuing.');
      return false;
    }
    setLlmBusy(true);
    setLlmError(null);
    try {
      const body = { provider: pickedProvider, model: pickedModel };
      if (apiKey) body.api_key = apiKey;
      const r = await apiFetch('/api/llm/config', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setLlmError(err?.detail || `${r.status}`);
        return false;
      }
      // Refresh status so the "ready" badge updates
      await refreshProviders();
      return true;
    } finally {
      setLlmBusy(false);
    }
  }, [pickedProvider, pickedModel, apiKey, refreshProviders]);

  const saveAudio = useCallback(async () => {
    setAudioError(null);
    try {
      const r = await apiFetch('/api/audio/config', {
        method: 'POST',
        body: JSON.stringify(audio),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setAudioError(err?.detail || `${r.status}`);
        return false;
      }
      return true;
    } catch (e) {
      setAudioError(e?.message || 'save failed');
      return false;
    }
  }, [audio]);

  const applyPairFix = useCallback(async (fix) => {
    setPairBusy(true);
    setPairError(null);
    try {
      const r = await apiFetch('/api/access/mode', {
        method: 'POST',
        body: JSON.stringify({ mode: fix.mode }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setPairError(formatApiDetail(err, `could not enable pairing (${r.status})`));
        return;
      }
      const applied = await r.json().catch(() => ({}));
      setPairFix(null);
      if (applied?.restart?.required) {
        setPairError(
          `${applied.restart.reason || 'This change needs a restart to take effect'}`
          + (applied.restart.command ? ` — run \`${applied.restart.command}\`, then try again.` : '.')
        );
        return;
      }
      await pickAccessModeRef.current?.(fix.mode);
    } catch (e) {
      setPairError(e?.message || 'could not enable pairing');
    } finally {
      setPairBusy(false);
    }
  }, []);

  const pickAccessMode = useCallback(async (mode) => {
    setPairChoice(mode);
    setPairError(null);
    setPairFix(null);
    setPairPayload(null);
    setPairBusy(true);
    try {
      // Persist the access mode FIRST so the next /api/devices/pair/url
      // call resolves through the right resolver branch.
      //
      // Via /api/access/mode, not the generic /api/config/update setter:
      // the latter writes access.pairing_mode alone, leaving bind_host
      // wherever it was, which is how "Same WiFi" used to produce a QR
      // pointing at an address the brain was not listening on.
      const r = await apiFetch('/api/access/mode', {
        method: 'POST',
        body: JSON.stringify({ mode }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setPairError(formatApiDetail(err, `failed to persist mode (${r.status})`));
        return;
      }
      const applied = await r.json().catch(() => ({}));
      if (applied?.restart?.required) {
        // Pairing now would mint a token against a listener that has not
        // moved yet. Say so instead of handing over a QR that cannot work.
        setPairError(
          `${applied.restart.reason || 'This change needs a restart to take effect'}`
          + (applied.restart.command ? ` — run \`${applied.restart.command}\`, then try again.` : '.')
        );
        return;
      }
      if (mode === 'localhost') {
        // Mode B does not emit a pair URL — the user is opting out
        // of phone pairing for now.
        return;
      }
      if (mode === 'remote') {
        // Attempt full remote setup from UI so users don't have to drop to
        // terminal by default. If this fails, we surface the server-provided
        // remediation (install/login/funnel URL).
        const remoteResp = await apiFetch('/api/access/remote-up', {
          method: 'POST',
        });
        if (!remoteResp.ok) {
          const err = await remoteResp.json().catch(() => ({}));
          setPairError(formatApiDetail(err, `failed to enable Anywhere mode (${remoteResp.status})`));
          return;
        }
      }
      const urlResp = await apiFetch('/api/devices/pair/url?name=phone-from-setup', {
        method: 'GET',
      });
      if (!urlResp.ok) {
        const err = await urlResp.json().catch(() => ({}));
        // A refusal that carries a `fix` is a one-tap consent, not a
        // dead end. Private is the default, so "phones cannot reach
        // this brain yet" is the expected fresh-install state and the
        // user should get a button, not an instruction to go find
        // Settings and learn what an access mode is.
        const fix = err?.detail?.fix;
        if (fix?.action === 'set_access_mode') setPairFix(fix);
        setPairError(formatApiDetail(err, `pair URL unavailable (${urlResp.status})`));
        return;
      }
      const body = await urlResp.json();
      setPairPayload(body);
    } catch (e) {
      setPairError(e?.message || 'failed to set access mode');
    } finally {
      setPairBusy(false);
    }
  }, []);

  pickAccessModeRef.current = pickAccessMode;

  const finishSetup = useCallback(async () => {
    setFinishError(null);
    try {
      const r = await apiFetch('/api/setup/complete', {
        method: 'POST',
        body: JSON.stringify({ identity }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        setFinishError(err?.detail || `setup-complete failed (${r.status})`);
        return false;
      }
      setSaved(true);
      return true;
    } catch (e) {
      setFinishError(e?.message || 'setup-complete failed');
      return false;
    }
  }, [identity]);

  const next = useCallback(async () => {
    if (step.id === 'llm') {
      const ok = await saveLlm();
      if (!ok) return;
    }
    if (step.id === 'audio') {
      const ok = await saveAudio();
      if (!ok) return;
    }
    if (step.id === 'identity') {
      // Identity is optional — no validation.
    }
    if (step.id === 'pair') {
      // Block until the user has picked a mode (or explicitly skipped
      // via the ``Skip for now`` button which sets pairChoice="localhost").
      if (!pairChoice) {
        setPairError('Pick an option above (or "Skip for now") before continuing.');
        return;
      }
    }
    if (step.id === 'done') {
      const ok = await finishSetup();
      if (!ok) return;
      setTimeout(() => navigate('/'), 800);
      return;
    }
    if (stepIdx < STEPS.length - 1) {
      setStepIdx(stepIdx + 1);
    }
  }, [step.id, stepIdx, saveLlm, saveAudio, pairChoice, finishSetup, navigate]);

  const back = useCallback(() => {
    if (stepIdx > 0) setStepIdx(stepIdx - 1);
  }, [stepIdx]);

  const selectedDescriptor = useMemo(
    () => providers.find((p) => p.id === pickedProvider),
    [providers, pickedProvider],
  );

  return (
    <div className="v2-page v2-page--stack" data-testid="v2-marker">
      <Pane
        title="FERAL setup"
        actions={(
          <Tabs
            value={step.id}
            onChange={(id) => {
              const idx = STEPS.findIndex((s) => s.id === id);
              if (idx >= 0) setStepIdx(idx);
            }}
            items={STEPS}
          />
        )}
      >
        <p className="v2-p v2-p--muted">
          Same steps as <code>feral setup</code> in your terminal. Everything you enter here writes to the same
          <code> ~/.feral/settings.json</code> so the two wizards are interchangeable.
        </p>
      </Pane>

      {step.id === 'welcome' && <WelcomeStep />}

      {step.id === 'llm' && (
        <LLMStep
          providers={providers}
          pickedProvider={pickedProvider}
          onPickProvider={(pid) => { setPickedProvider(pid); setPickedModel(''); setApiKey(''); }}
          apiKey={apiKey}
          onApiKey={setApiKey}
          models={models}
          modelSource={modelSource}
          pickedModel={pickedModel}
          onPickModel={setPickedModel}
          onProbe={probeProvider}
          onRefresh={refreshProviders}
          error={llmError}
          busy={llmBusy}
          descriptor={selectedDescriptor}
        />
      )}

      {step.id === 'audio' && (
        <AudioStep
          providers={audioProviders}
          value={audio}
          onChange={setAudio}
          error={audioError}
        />
      )}

      {step.id === 'identity' && (
        <IdentityStep value={identity} onChange={setIdentity} />
      )}

      {step.id === 'pair' && (
        <PairStep
          choice={pairChoice}
          onPick={pickAccessMode}
          payload={pairPayload}
          error={pairError}
          fix={pairFix}
          onApplyFix={applyPairFix}
          busy={pairBusy}
        />
      )}

      {step.id === 'done' && <DoneStep saved={saved} error={finishError} pairChoice={pairChoice} />}

      <Pane>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between' }}>
          <button
            type="button"
            className="v2-btn v2-btn--ghost"
            onClick={back}
            disabled={stepIdx === 0}
          >
            <ChevronLeft size={13} /> Back
          </button>
          <button
            type="button"
            className="v2-btn v2-btn--primary"
            onClick={next}
            disabled={llmBusy}
            data-testid="v2-setup-next"
          >
            {stepIdx === STEPS.length - 1 ? 'Finish' : 'Continue'} <ChevronRight size={13} />
          </button>
        </div>
      </Pane>
    </div>
  );
}


function WelcomeStep() {
  return (
    <Pane title="Welcome">
      <p className="v2-p">
        This wizard sets up your local FERAL brain in four steps:
      </p>
      <ol>
        <li>Choose an LLM provider (cloud or local).</li>
        <li>Pick STT + TTS providers for voice.</li>
        <li>Tell the agent who you are.</li>
        <li>Start chatting.</li>
      </ol>
      <p className="v2-p v2-p--muted">
        Prefer terminal? Run <code>feral setup</code> on your shell — same endpoints, same config.
      </p>
    </Pane>
  );
}


function LLMStep({
  providers, pickedProvider, onPickProvider,
  apiKey, onApiKey, models, modelSource, pickedModel, onPickModel,
  onProbe, onRefresh, error, busy, descriptor,
}) {
  return (
    <>
      <Pane
        title="Providers"
        actions={(
          <button type="button" className="v2-btn v2-btn--ghost" onClick={onRefresh} aria-label="Refresh">
            <RefreshCw size={13} />
          </button>
        )}
      >
        <p className="v2-p v2-p--muted">
          Click any provider to select it. Local providers show <strong>ready</strong> when detected.
          Cloud providers show <strong>needs API key</strong> until you enter one — you can still select
          them and add the key on the right.
        </p>
        <div className="v2-skills-grid" data-testid="v2-setup-providers">
          {providers.map((p) => {
            const isPicked = p.id === pickedProvider;
            return (
              <Glass
                key={p.id}
                level={isPicked ? 2 : 0}
                radius="md"
                padding="md"
                className={isPicked ? 'v2-setup-provider is-picked' : 'v2-setup-provider'}
              >
                <header style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                  <StatusDot tone={statusTone(p.reachable ? 'ready' : (p.configured ? 'unreachable' : 'needs_api_key'))} />
                  <div style={{ fontWeight: 600 }}>{p.display_name}</div>
                </header>
                <div className="v2-p v2-p--muted v2-p--tiny" style={{ marginBottom: 6 }}>
                  {p.supports_local ? `local · ${p.default_base_url}` : `env: ${p.credential_env_var || '—'}`}
                </div>
                <div className="v2-p v2-p--tiny" style={{ marginBottom: 8 }}>
                  {p.reachable ? 'ready' : p.configured ? 'unreachable' : 'needs API key'}
                </div>
                <div style={{ display: 'flex', gap: 4 }}>
                  <button
                    type="button"
                    className={`v2-btn ${isPicked ? 'v2-btn--primary' : ''}`}
                    onClick={() => onPickProvider(p.id)}
                    data-testid={`v2-setup-pick-${p.id}`}
                  >
                    {isPicked ? 'Selected' : 'Select'}
                  </button>
                  {!p.reachable && (
                    <button
                      type="button"
                      className="v2-btn v2-btn--ghost"
                      onClick={() => onProbe(p.id)}
                      title="Re-probe"
                    >
                      Probe
                    </button>
                  )}
                </div>
              </Glass>
            );
          })}
        </div>
      </Pane>

      {descriptor && descriptor.requires_api_key && !descriptor.reachable && (
        <Pane title={`API key for ${descriptor.display_name}`}>
          <p className="v2-p v2-p--muted">
            Routed into the BlindVault under <code>{descriptor.credential_env_var}</code> — never written to settings.json in plaintext.
          </p>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => onApiKey(e.target.value)}
            placeholder={`Your ${descriptor.display_name} API key`}
            className="v2-input"
            data-testid="v2-setup-apikey"
            style={{ width: '100%', padding: 8 }}
          />
        </Pane>
      )}

      {pickedProvider && (
        <Pane title="Model">
          <p className="v2-p v2-p--muted">
            {models.length > 0
              ? `Found ${models.length} models (source: ${modelSource}). Pick one or type a newer name.`
              : 'No models discovered yet — type the exact model id.'}
          </p>
          <input
            type="text"
            value={pickedModel}
            onChange={(e) => onPickModel(e.target.value)}
            placeholder="Model id"
            className="v2-input"
            data-testid="v2-setup-model"
            style={{ width: '100%', padding: 8, marginBottom: 8 }}
          />
          {models.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
              {models.slice(0, 16).map((m) => (
                <button
                  key={m}
                  type="button"
                  className={`v2-chip${m === pickedModel ? ' v2-chip--live' : ''}`}
                  onClick={() => onPickModel(m)}
                >
                  {m}
                </button>
              ))}
            </div>
          )}
        </Pane>
      )}

      {error && <div className="v2-chip v2-chip--error">{error}</div>}
      {busy && <div className="v2-chip v2-chip--warn">Saving…</div>}
    </>
  );
}


function AudioStep({ providers, value, onChange, error }) {
  return (
    <>
      <Pane title="Speech in / out">
        <p className="v2-p v2-p--muted">
          Cloud (OpenAI) needs the key you already entered. Local
          (faster-whisper + piper) runs entirely offline once installed.
        </p>
      </Pane>

      <Pane title="Speech-to-text">
        <div className="v2-skills-grid">
          {(providers.stt || []).map((p) => {
            const picked = value.stt_provider === p.id;
            return (
              <Glass
                key={p.id}
                level={picked ? 2 : 0}
                radius="md"
                padding="md"
                className={picked ? 'v2-setup-audio is-picked' : 'v2-setup-audio'}
              >
                <header style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                  <StatusDot tone={statusTone(p.is_local ? (p.available ? 'ready' : 'unavailable') : (p.needs_api_key ? 'needs_api_key' : 'ready'))} />
                  <div style={{ fontWeight: 600 }}>{p.display_name}</div>
                </header>
                <div className="v2-p v2-p--tiny v2-p--muted">
                  {p.is_local ? (p.available ? 'installed' : 'install via pip install feral-ai[stt]') : `env: ${p.credential_env_var}`}
                </div>
                <button
                  type="button"
                  className={`v2-btn ${picked ? 'v2-btn--primary' : ''}`}
                  onClick={() => onChange({
                    ...value,
                    stt_provider: p.id,
                    stt_model: p.default_model || (p.available_models || [])[0] || value.stt_model,
                  })}
                  style={{ marginTop: 6 }}
                  data-testid={`v2-setup-stt-${p.id}`}
                >
                  {picked ? 'Selected' : 'Select'}
                </button>
              </Glass>
            );
          })}
        </div>
        {value.stt_provider && (
          <div style={{ marginTop: 10 }}>
            <label className="v2-p v2-p--muted">STT model</label>
            <input
              type="text"
              value={value.stt_model || ''}
              onChange={(e) => onChange({ ...value, stt_model: e.target.value })}
              className="v2-input"
              style={{ width: '100%', padding: 8 }}
            />
          </div>
        )}
      </Pane>

      <Pane title="Text-to-speech">
        <div className="v2-skills-grid">
          {(providers.tts || []).map((p) => {
            const picked = value.tts_provider === p.id;
            return (
              <Glass
                key={p.id}
                level={picked ? 2 : 0}
                radius="md"
                padding="md"
                className={picked ? 'v2-setup-audio is-picked' : 'v2-setup-audio'}
              >
                <header style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                  <StatusDot tone={statusTone(p.is_local ? (p.available ? 'ready' : 'unavailable') : (p.needs_api_key ? 'needs_api_key' : 'ready'))} />
                  <div style={{ fontWeight: 600 }}>{p.display_name}</div>
                </header>
                <button
                  type="button"
                  className={`v2-btn ${picked ? 'v2-btn--primary' : ''}`}
                  onClick={() => onChange({
                    ...value,
                    tts_provider: p.id,
                    tts_model: p.default_model || value.tts_model,
                    tts_voice: p.default_voice || value.tts_voice,
                  })}
                  style={{ marginTop: 6 }}
                  data-testid={`v2-setup-tts-${p.id}`}
                >
                  {picked ? 'Selected' : 'Select'}
                </button>
              </Glass>
            );
          })}
        </div>
        {value.tts_provider && (
          <div style={{ marginTop: 10, display: 'grid', gap: 6, gridTemplateColumns: '1fr 1fr' }}>
            <label className="v2-p v2-p--muted">TTS model</label>
            <label className="v2-p v2-p--muted">Voice</label>
            <input
              type="text"
              value={value.tts_model || ''}
              onChange={(e) => onChange({ ...value, tts_model: e.target.value })}
              className="v2-input"
              style={{ padding: 8 }}
            />
            <input
              type="text"
              value={value.tts_voice || ''}
              onChange={(e) => onChange({ ...value, tts_voice: e.target.value })}
              className="v2-input"
              style={{ padding: 8 }}
            />
          </div>
        )}
      </Pane>

      {error && <div className="v2-chip v2-chip--error">{error}</div>}
    </>
  );
}


function IdentityStep({ value, onChange }) {
  return (
    <Pane title="About you (optional)">
      <p className="v2-p v2-p--muted">
        Short identity block the agent can reference. You can edit it anytime in Settings → Self.
      </p>
      <div style={{ display: 'grid', gap: 6, marginTop: 10 }}>
        <label className="v2-p v2-p--muted">Name</label>
        <input
          type="text"
          value={value.name || ''}
          onChange={(e) => onChange({ ...value, name: e.target.value })}
          className="v2-input"
          style={{ padding: 8 }}
          data-testid="v2-setup-identity-name"
        />
        <label className="v2-p v2-p--muted">Occupation</label>
        <input
          type="text"
          value={value.occupation || ''}
          onChange={(e) => onChange({ ...value, occupation: e.target.value })}
          className="v2-input"
          style={{ padding: 8 }}
        />
        <label className="v2-p v2-p--muted">Location</label>
        <input
          type="text"
          value={value.location || ''}
          onChange={(e) => onChange({ ...value, location: e.target.value })}
          className="v2-input"
          style={{ padding: 8 }}
        />
      </div>
    </Pane>
  );
}


function PairStep({ choice, onPick, payload, error, fix, onApplyFix, busy }) {
  const cards = [
    {
      id: 'local',
      icon: <Wifi size={20} />,
      title: 'Same WiFi',
      blurb: 'Phone is on the same network as this Mac. Free, instant, only works on this WiFi.',
    },
    {
      id: 'localhost',
      icon: <Laptop2 size={20} />,
      title: 'This Mac only',
      blurb: 'Skip phone pairing for now. Re-run setup later when you want to pair.',
    },
    {
      id: 'remote',
      icon: <Globe size={20} />,
      title: 'Anywhere',
      blurb: 'Tailscale-encrypted private network. Setup will try to configure this automatically.',
    },
  ];

  return (
    <>
      <Pane title="Pair your phone" actions={<Smartphone size={16} />}>
        <p className="v2-p v2-p--muted">
          Where do you want to pair your phone from?
        </p>
        <div className="v2-skills-grid" data-testid="v2-setup-pair-modes" style={{ marginTop: 12 }}>
          {cards.map((c) => {
            const isPicked = choice === c.id;
            return (
              <Glass
                key={c.id}
                level={isPicked ? 2 : 0}
                radius="md"
                padding="md"
                className={isPicked ? 'v2-setup-pair is-picked' : 'v2-setup-pair'}
              >
                <header style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                  {c.icon}
                  <div style={{ fontWeight: 600 }}>{c.title}</div>
                </header>
                <div className="v2-p v2-p--tiny v2-p--muted" style={{ marginBottom: 10 }}>
                  {c.blurb}
                </div>
                <button
                  type="button"
                  className={`v2-btn ${isPicked ? 'v2-btn--primary' : ''}`}
                  onClick={() => onPick(c.id)}
                  data-testid={`v2-setup-pair-${c.id}`}
                  disabled={busy}
                >
                  {isPicked ? 'Selected' : c.id === 'localhost' ? 'Skip for now' : 'Use this'}
                </button>
              </Glass>
            );
          })}
        </div>
      </Pane>

      {choice && choice !== 'localhost' && payload && (
        <Pane title="Share this with your phone" data-testid="v2-setup-pair-payload">
          <div style={{ display: 'grid', gap: 12 }}>
            <div>
              <label className="v2-p v2-p--muted">Pair URL</label>
              <code
                style={{
                  display: 'block',
                  padding: '10px 12px',
                  background: '#0a0a0a',
                  border: '1px solid #1a1a1a',
                  borderRadius: 8,
                  wordBreak: 'break-all',
                  fontSize: 13,
                }}
                data-testid="v2-setup-pair-url"
              >
                {payload.url}
              </code>
              <button
                type="button"
                className="v2-btn v2-btn--ghost"
                onClick={() => navigator.clipboard?.writeText(payload.url)}
                style={{ marginTop: 6 }}
              >
                Copy URL
              </button>
            </div>

            {payload.diagnostic && (
              <div className="v2-p v2-p--tiny v2-p--muted">
                <strong>Reachability:</strong> mode = <code>{ACCESS_MODE_LABELS[payload.mode] || payload.mode}</code>; brain advertised{' '}
                <code>{payload.diagnostic.advertised_lan_ip || '—'}</code>.
                <ul style={{ marginTop: 4, paddingLeft: 18 }}>
                  {(payload.diagnostic.honest_caveats || []).map((c, i) => (
                    <li key={i}>{c}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className="v2-p v2-p--tiny v2-p--muted">
              Token expires {payload.expires ? new Date(payload.expires * 1000).toLocaleString() : '—'}.
            </div>
          </div>
        </Pane>
      )}

      {choice === 'localhost' && (
        <Pane>
          <p className="v2-p v2-p--muted">
            Pairing skipped. You can pair later by re-running setup and choosing
            <strong> Same WiFi</strong> or <strong>Anywhere</strong>.
          </p>
        </Pane>
      )}

      {error && (
        <Pane>
          <div className="v2-chip v2-chip--error" data-testid="v2-setup-pair-error">
            {error}
          </div>
          {fix?.action === 'set_access_mode' && (
            <div style={{ display: 'grid', gap: 8, marginTop: 8 }}>
              {fix.consequence && (
                <div className="v2-chip v2-chip--warn">{fix.consequence}</div>
              )}
              <button
                type="button"
                className="v2-btn v2-btn--primary"
                disabled={busy}
                onClick={() => onApplyFix?.(fix)}
                data-testid="v2-setup-pair-enable-lan"
              >
                {fix.label || 'Enable same-WiFi pairing'}
              </button>
            </div>
          )}
        </Pane>
      )}
    </>
  );
}


function DoneStep({ saved, error, pairChoice }) {
  return (
    <Pane title="Ready">
      <div style={{ textAlign: 'center', padding: 20 }}>
        <CheckCircle2 size={48} style={{ color: saved ? '#22c55e' : '#64748b' }} />
        <h2 style={{ marginTop: 12 }}>
          {saved ? 'Setup complete.' : 'Click Finish to write settings to disk.'}
        </h2>
        <p className="v2-p v2-p--muted">
          Start a chat at <code>/chat</code> or open the dashboard at <code>/</code>.
        </p>
        {pairChoice === 'localhost' && (
          <p className="v2-p v2-p--muted" style={{ marginTop: 10 }}>
            You skipped phone pairing. Re-run setup any time to switch to Same WiFi or Anywhere.
          </p>
        )}
        {error && (
          <div className="v2-chip v2-chip--error" style={{ marginTop: 10 }} data-testid="v2-setup-finish-error">
            {error}
          </div>
        )}
      </div>
    </Pane>
  );
}
