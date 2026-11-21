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
import { apiJson, apiFetch, ApiError } from '../lib/api';


const STEPS = [
  { id: 'welcome', label: 'Welcome' },
  { id: 'llm', label: 'AI provider' },
  { id: 'audio', label: 'Voice' },
  { id: 'identity', label: 'About you' },
  { id: 'pair', label: 'Connect devices' },
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

// Inventory is passive. Missing legacy flags are unconfirmed, not a claim that
// an adapter is implemented; partial/false/malformed flags cannot authorize setup.
function selectableProvider(p) {
  return p && ((p.runtime_supported === true && p.setup_selectable === true)
    || (!Object.hasOwn(p, 'runtime_supported') && !Object.hasOwn(p, 'setup_selectable')));
}
function providerRows(body) {
  if (!Array.isArray(body?.providers) || body.providers.length > 128) throw new Error('Invalid inventory');
  const ids = new Set();
  for (const p of body.providers) {
    if (!p || typeof p !== 'object' || Array.isArray(p) || typeof p.id !== 'string'
      || !/^[a-zA-Z0-9_-]{1,80}$/.test(p.id) || ids.has(p.id)) throw new Error('Invalid provider identity');
    ids.add(p.id);
  }
  return body.providers;
}
function configStamp(c) {
  if (!c || typeof c !== 'object' || Array.isArray(c)
    || ['provider', 'model', 'base_url'].some(k => c[k] != null && (typeof c[k] !== 'string' || c[k].length > 4096))
    || (c.configured != null && typeof c.configured !== 'boolean')
    || (c.fallback_providers != null && (!Array.isArray(c.fallback_providers)
      || c.fallback_providers.length > 128 || c.fallback_providers.some(p => typeof p !== 'string' || p.length > 80))))
    throw new Error('Invalid configuration');
  return JSON.stringify([c.provider ?? '', c.model ?? '', c.base_url ?? '', c.fallback_providers ?? [], c.configured ?? null]);
}
function descriptorStamp(p) {
  if (!p) return '';
  // These observation fields can change as a consequence of our own probe.
  const { reachable, last_refresh, default_model, error, ...identity } = p;
  return JSON.stringify(Object.keys(identity).sort().map(k => [k, identity[k]]));
}
function probeReceipt(body, pid) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error('Invalid probe receipt');
  const ids = [body.id, body.provider_id].filter(v => v !== undefined);
  if (!ids.length || ids.some(id => id !== pid) || typeof body.reachable !== 'boolean') throw new Error('Invalid probe identity');
  if (body.error != null && (typeof body.error !== 'string' || body.error.length > 4096
    || (body.reachable && body.error.trim()))) throw new Error('Invalid probe result');
  return body.reachable;
}
function providerPresentation(p, history) {
  if (!selectableProvider(p)) return 'Unsupported adapter — read-only';
  if (history?.pid === p.id) return history.reachable ? 'Last explicit probe: reachable' : 'Last explicit probe: unreachable';
  if (p.reachable === true) return 'Runtime reports reachable — inference unverified';
  if (p.reachable === false) return 'Runtime reports unreachable';
  return p.requires_api_key && p.configured === false ? 'Needs API key; reachability unknown' : 'Reachability unknown — not probed';
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
  const [probeHistory, setProbeHistory] = useState(null);
  const llmScope = React.useRef({ mounted: false, revision: 0, operation: null, refresh: 0, config: null, providers: [] });
  const retireDraft = useCallback(() => {
    llmScope.current.revision += 1;
    setProbeHistory(null);
    setLlmError(null);
  }, []);
  const pickProvider = useCallback((pid) => {
    if (!selectableProvider(llmScope.current.providers.find(p => p.id === pid))) return;
    retireDraft(); setPickedProvider(pid); setPickedModel(''); setApiKey('');
  }, [retireDraft]);
  const pickModel = useCallback((model) => { retireDraft(); setPickedModel(model); }, [retireDraft]);
  const editKey = useCallback((key) => { retireDraft(); setApiKey(key); }, [retireDraft]);

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

  // Requests carry a draft generation: editing and returning to the same values
  // still retires any earlier asynchronous result.
  useEffect(() => {
    const scope = llmScope.current;
    scope.mounted = true;
    const revision = scope.revision;
    (async () => {
      const [provs, currentLlm, currentAudio, audioAll] = await Promise.allSettled([
        apiJson('/api/llm/providers', { silent: true }), apiJson('/api/llm/config', { silent: true }),
        apiJson('/api/audio/config'), apiJson('/api/audio/providers'),
      ]);
      if (!scope.mounted) return;
      if (scope.revision === revision) {
        try {
          if (provs.status !== 'fulfilled' || currentLlm.status !== 'fulfilled') throw new Error('Unavailable inventory');
          const rows = providerRows(provs.value);
          scope.config = configStamp(currentLlm.value); scope.providers = rows;
          setProviders(rows);
          const eligible = rows.filter(selectableProvider);
          const preferred = eligible.find(p => p.supports_local && p.reachable === true)
            || eligible.find(p => !p.supports_local && p.configured === true) || eligible[0];
          // Keep an existing unsupported selection visible, without silently replacing it.
          setPickedProvider(currentLlm.value.provider || preferred?.id || '');
          setPickedModel(currentLlm.value.model || '');
        } catch { setLlmError('Provider inventory or saved configuration could not be verified. Refresh to retry.'); }
      }
      if (audioAll.status === 'fulfilled') setAudioProviders(audioAll.value || { stt: [], tts: [] });
      if (currentAudio.status === 'fulfilled' && currentAudio.value) setAudio(prev => ({ ...prev, ...currentAudio.value }));
    })();
    return () => { scope.mounted = false; scope.revision += 1; scope.refresh += 1; };
  }, []);

  useEffect(() => {
    let active = true;
    const revision = llmScope.current.revision;
    setModels([]); setModelSource('');
    if (!pickedProvider || !selectableProvider(llmScope.current.providers.find(p => p.id === pickedProvider))) return;
    (async () => {
      try {
        const r = await apiJson(`/api/llm/providers/${encodeURIComponent(pickedProvider)}/models`
          + '?live=false&recommended=true&model_class=chat', { silent: true });
        if (!active || !llmScope.current.mounted || revision !== llmScope.current.revision) return;
        if (!Array.isArray(r?.models) || r.models.length > 4096
          || r.models.some(m => typeof m !== 'string' || !m || m.length > 256)) throw new Error('Invalid models');
        setModels(r.models); setModelSource(typeof r.source === 'string' ? r.source : 'unknown');
        // Suggestions do not change the draft or imply installation/inference.
      } catch {
        if (active && llmScope.current.mounted && revision === llmScope.current.revision)
          setLlmError('Cached model inventory could not be verified. Enter a model name or refresh.');
      }
    })();
    return () => { active = false; };
  }, [pickedProvider]);

  const readInventory = useCallback(async () => {
    const [catalog, config] = await Promise.all([
      apiJson('/api/llm/providers', { silent: true }), apiJson('/api/llm/config', { silent: true }),
    ]);
    return { rows: providerRows(catalog), config, stamp: configStamp(config) };
  }, []);
  const refreshProviders = useCallback(async () => {
    const scope = llmScope.current;
    if (scope.operation) return;
    const request = ++scope.refresh, revision = scope.revision;
    try {
      const fresh = await readInventory();
      if (!scope.mounted || request !== scope.refresh || revision !== scope.revision || scope.operation) return;
      setProbeHistory(history => history && history.config === fresh.stamp
        && history.descriptor === descriptorStamp(fresh.rows.find(p => p.id === history.pid)) ? history : null);
      scope.config = fresh.stamp; scope.providers = fresh.rows;
      setProviders(fresh.rows); setLlmError(null);
    } catch {
      if (scope.mounted && request === scope.refresh && revision === scope.revision && !scope.operation) {
        setProbeHistory(null); setLlmError('Provider inventory or saved configuration could not be verified.');
      }
    }
  }, [readInventory]);

  const probeProvider = useCallback(async (pid) => {
    const scope = llmScope.current;
    const descriptor = scope.providers.find(p => p.id === pid);
    if (scope.operation || !selectableProvider(descriptor) || scope.config === null) return;
    const operation = {}, revision = scope.revision;
    const config = scope.config, stamp = descriptorStamp(descriptor);
    const current = () => scope.mounted && scope.operation === operation && scope.revision === revision;
    scope.operation = operation; ++scope.refresh;
    setLlmBusy(true); setLlmError(null); setProbeHistory(null);
    try {
      const before = await readInventory();
      if (!current()) return;
      if (before.stamp !== config || descriptorStamp(before.rows.find(p => p.id === pid)) !== stamp)
        throw new Error('Configuration changed');
      const path = `/api/llm/providers/${encodeURIComponent(pid)}/probe`;
      let result;
      try { result = await apiJson(path, { method: 'POST', silent: true }); }
      catch (e) {
        // apiFetch treats a successful negative ProviderStatus.error as ApiError.
        // Only its exact HTTP-success receipt may represent an unreachable probe.
        if (!(e instanceof ApiError) || e.path !== path || e.status < 200 || e.status >= 300
          || probeReceipt(e.raw, pid) !== false) throw e;
        result = e.raw;
      }
      if (!current()) return;
      const reachable = probeReceipt(result, pid);
      const after = await readInventory();
      if (!current()) return;
      if (after.stamp !== config || descriptorStamp(after.rows.find(p => p.id === pid)) !== stamp)
        throw new Error('Configuration changed');
      scope.providers = after.rows; setProviders(after.rows);
      setProbeHistory({ pid, reachable, config, descriptor: stamp });
    } catch {
      if (current()) { setProbeHistory(null); setLlmError('Probe could not be verified for this configuration. Refresh and retry.'); }
    } finally {
      if (scope.operation === operation) { scope.operation = null; if (scope.mounted) setLlmBusy(false); }
    }
  }, [readInventory]);

  const saveLlm = useCallback(async () => {
    const scope = llmScope.current;
    if (scope.operation) return false;
    if (!pickedProvider || !pickedModel || !selectableProvider(scope.providers.find(p => p.id === pickedProvider))) {
      setLlmError('Choose a supported AI provider and model before continuing.'); return false;
    }
    const descriptor = descriptorStamp(scope.providers.find(p => p.id === pickedProvider));
    const operation = {}, revision = scope.revision;
    const current = () => scope.mounted && scope.operation === operation && scope.revision === revision;
    scope.operation = operation; ++scope.refresh;
    setLlmBusy(true); setLlmError(null); setProbeHistory(null);
    try {
      const before = await readInventory();
      if (!current()) return false;
      if (before.stamp !== scope.config || descriptorStamp(before.rows.find(p => p.id === pickedProvider)) !== descriptor
        || !selectableProvider(before.rows.find(p => p.id === pickedProvider)))
        throw new Error('Configuration changed');
      const previousProvider = before.config.provider || '';
      // The server resolves aliases before applying omitted-URL semantics. A
      // noncanonical saved identity is not enough evidence to predict that write.
      if (previousProvider && !before.rows.some(p => p.id === previousProvider))
        throw new Error('Saved provider identity is ambiguous');
      const expectedBaseURL = previousProvider === pickedProvider ? (before.config.base_url || '') : '';
      const expectedFallbacks = (before.config.fallback_providers || []).filter(p => p !== pickedProvider);
      if (previousProvider && previousProvider !== pickedProvider && !expectedFallbacks.includes(previousProvider))
        expectedFallbacks.unshift(previousProvider);
      const body = { provider: pickedProvider, model: pickedModel };
      if (apiKey) body.api_key = apiKey;
      const receipt = await apiJson('/api/llm/config', { method: 'POST', body: JSON.stringify(body), silent: true });
      if (receipt?.success !== true || receipt.provider !== pickedProvider || receipt.model !== pickedModel
        || receipt.persisted?.ok !== true) throw new Error('Invalid save receipt');
      if (!current()) return false;
      const after = await readInventory();
      if (!current()) return false;
      const beforeDescriptor = before.rows.find(p => p.id === pickedProvider);
      const afterDescriptor = after.rows.find(p => p.id === pickedProvider);
      if (after.config.provider !== pickedProvider || after.config.model !== pickedModel
        || (after.config.base_url || '') !== expectedBaseURL
        || JSON.stringify(after.config.fallback_providers || []) !== JSON.stringify(expectedFallbacks)
        || !selectableProvider(afterDescriptor)
        || ['requires_api_key', 'runtime_supported', 'setup_selectable'].some(k => afterDescriptor[k] !== beforeDescriptor[k]))
        throw new Error('Saved configuration did not match');
      scope.config = after.stamp; scope.providers = after.rows; setProviders(after.rows);
      return true;
    } catch {
      if (current()) setLlmError('Provider settings could not be verified. They may have been saved; refresh before retrying.');
      return false;
    } finally {
      if (scope.operation === operation) { scope.operation = null; if (scope.mounted) setLlmBusy(false); }
    }
  }, [pickedProvider, pickedModel, apiKey, readInventory]);

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
    if (llmScope.current.operation) retireDraft();
    if (stepIdx > 0) setStepIdx(stepIdx - 1);
  }, [stepIdx, retireDraft]);

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
              if (idx >= 0) {
                if (id !== step.id && llmScope.current.operation) retireDraft();
                setStepIdx(idx);
              }
            }}
            items={STEPS}
          />
        )}
      >
        <p className="v2-p v2-p--muted">
          Make FERAL your own. Choose how it responds, add a few details about you, and connect your devices.
        </p>
      </Pane>

      {step.id === 'welcome' && <WelcomeStep />}

      {step.id === 'llm' && (
        <LLMStep
          providers={providers}
          pickedProvider={pickedProvider}
          onPickProvider={pickProvider}
          apiKey={apiKey}
          onApiKey={editKey}
          models={models}
          modelSource={modelSource}
          pickedModel={pickedModel}
          onPickModel={pickModel}
          onProbe={probeProvider}
          onRefresh={refreshProviders}
          error={llmError}
          busy={llmBusy}
          descriptor={selectedDescriptor}
          probeHistory={probeHistory}
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
        Let’s get your personal assistant ready:
      </p>
      <ol>
        <li>Choose an AI provider that runs on this device or in the cloud.</li>
        <li>Set up voice if you’d like to speak with FERAL.</li>
        <li>Add a few details about yourself (optional).</li>
        <li>Connect your devices, or keep FERAL on this Mac.</li>
        <li>Start chatting.</li>
      </ol>
      <p className="v2-p v2-p--muted">
        You can change these choices later in Settings.
      </p>
    </Pane>
  );
}


function LLMStep({
  providers, pickedProvider, onPickProvider,
  apiKey, onApiKey, models, modelSource, pickedModel, onPickModel,
  onProbe, onRefresh, error, busy, descriptor, probeHistory,
}) {
  return (
    <>
      <Pane
        title="AI provider"
        actions={(
          <button type="button" className="v2-btn v2-btn--ghost" onClick={onRefresh} disabled={busy} aria-label="Refresh">
            <RefreshCw size={13} />
          </button>
        )}
      >
        <p className="v2-p v2-p--muted">
          Choose how FERAL answers you. Local providers run on this device; cloud providers
          connect to an online service. A cloud provider may need your API key.
          Probe checks the provider catalog connection; it does not verify the active model endpoint, unsaved credentials or model inference.
        </p>
        <div className="v2-skills-grid" data-testid="v2-setup-providers">
          {providers.map((p) => {
            const isPicked = p.id === pickedProvider;
            const label = providerPresentation(p, probeHistory);
            const reachable = probeHistory?.pid === p.id ? probeHistory.reachable : p.reachable;
            return (
              <Glass
                key={p.id}
                level={isPicked ? 2 : 0}
                radius="md"
                padding="md"
                className={isPicked ? 'v2-setup-provider is-picked' : 'v2-setup-provider'}
              >
                <header style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                  <StatusDot
                    tone={statusTone(!selectableProvider(p) ? '' : reachable === true ? 'ready' : reachable === false ? 'unreachable' : '')}
                    label={`${p.display_name}: ${label}`}
                  />
                  <div style={{ fontWeight: 600 }}>{p.display_name}</div>
                </header>
                <div className="v2-p v2-p--muted v2-p--tiny" style={{ marginBottom: 6 }}>
                  {p.supports_local ? 'Local provider — installation unverified' : 'Cloud service'}
                </div>
                <div className="v2-p v2-p--tiny" style={{ marginBottom: 8 }}>
                  {label}
                  {selectableProvider(p) && p.runtime_supported !== true && <span> · Legacy adapter support unconfirmed</span>}
                </div>
                <div style={{ display: 'flex', gap: 4 }}>
                  <button
                    type="button"
                    className={`v2-btn ${isPicked ? 'v2-btn--primary' : ''}`}
                    onClick={() => onPickProvider(p.id)}
                    disabled={busy || !selectableProvider(p)}
                    data-testid={`v2-setup-pick-${p.id}`}
                  >
                    {isPicked ? 'Selected' : 'Select'}
                  </button>
                  {selectableProvider(p) && (
                    <button
                      type="button"
                      className="v2-btn v2-btn--ghost"
                      onClick={() => onProbe(p.id)}
                      title="Explicitly check provider reachability"
                      disabled={busy}
                      data-testid={`v2-setup-probe-${p.id}`}
                    >
                      Probe
                    </button>
                  )}
                </div>
              </Glass>
            );
          })}
        </div>
        {probeHistory && <p className="v2-p v2-p--muted">Explicit probe history checks reachability; model inference, tools and voice remain unverified.</p>}
      </Pane>

      {descriptor && selectableProvider(descriptor) && descriptor.requires_api_key && !descriptor.reachable && (
        <Pane title={`API key for ${descriptor.display_name}`}>
          <p className="v2-p v2-p--muted">
            Enter your provider’s API key to connect FERAL to this service.
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
              ? `Choose one of ${models.length} model suggestions, or enter another model name.`
              : 'No cached model suggestions. Enter a model name from your provider.'}
          </p>
          <p className="v2-p v2-p--muted v2-p--tiny">Inventory: {modelSource || 'unknown'}. Suggestions do not verify installation, inference, tools or voice.</p>
          <input
            type="text"
            disabled={!selectableProvider(descriptor)}
            value={pickedModel}
            onChange={(e) => onPickModel(e.target.value)}
            placeholder="Model name"
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
                  disabled={busy || !selectableProvider(descriptor)}
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
      <Pane title="Voice">
        <p className="v2-p v2-p--muted">
          Choose how FERAL understands your speech and speaks back. Cloud voice needs
          a provider API key. Local voice runs on this device once available.
        </p>
      </Pane>

      <Pane title="Speech recognition">
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
                  <StatusDot
                    tone={statusTone(p.is_local ? (p.available ? 'ready' : 'unavailable') : (p.needs_api_key ? 'needs_api_key' : 'ready'))}
                    label={`Speech to text ${p.display_name}: ${p.is_local ? (p.available ? 'installed' : 'not installed') : (p.needs_api_key ? 'needs API key' : 'ready')}`}
                  />
                  <div style={{ fontWeight: 600 }}>{p.display_name}</div>
                </header>
                <div className="v2-p v2-p--tiny v2-p--muted">
                  {p.is_local ? (p.available ? 'Available on this device' : 'Requires additional setup') : 'Cloud service'}
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
            <label className="v2-p v2-p--muted">Speech recognition model</label>
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

      <Pane title="Spoken responses">
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
                  <StatusDot
                    tone={statusTone(p.is_local ? (p.available ? 'ready' : 'unavailable') : (p.needs_api_key ? 'needs_api_key' : 'ready'))}
                    label={`Text to speech ${p.display_name}: ${p.is_local ? (p.available ? 'installed' : 'not installed') : (p.needs_api_key ? 'needs API key' : 'ready')}`}
                  />
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
            <label className="v2-p v2-p--muted">Speech model</label>
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
        Help FERAL get to know you. Share only what you’re comfortable with; you can edit this anytime in Settings.
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
                  background: 'var(--v2-well)',
                  border: '1px solid var(--v2-hairline)',
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
        <CheckCircle2 size={48} style={{ color: saved ? 'var(--v2-state-live)' : 'var(--v2-text-tertiary)' }} />
        <h2 style={{ marginTop: 12 }}>
          {saved ? 'Setup complete.' : 'You’re ready. Select Finish to start chatting.'}
        </h2>
        <p className="v2-p v2-p--muted">
          Your conversation with FERAL is next.
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
