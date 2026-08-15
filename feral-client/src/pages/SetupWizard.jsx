import React, { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Brain, Key, Puzzle, Cpu, ChevronRight, ChevronLeft,
  Check, AlertCircle, Loader2, Eye, EyeOff, Sparkles,
  Wifi, WifiOff, Shield, Zap, Link2, Music, Home, FileText,
  ExternalLink, Server, User, Heart, Smartphone, Globe, Search,
} from 'lucide-react';

import { API_BASE as API } from '../config';

const STEPS = [
  { id: 'welcome', label: 'Welcome', icon: Brain },
  { id: 'identity', label: 'You & Your Agent', icon: User },
  { id: 'llm', label: 'LLM Provider', icon: Sparkles },
  { id: 'keys', label: 'API Keys', icon: Key },
  { id: 'devices', label: 'Devices', icon: Smartphone },
  { id: 'skills', label: 'Skills', icon: Puzzle },
  { id: 'apps', label: 'Connect Apps', icon: Link2 },
  { id: 'features', label: 'Features', icon: Zap },
  { id: 'finish', label: 'Launch', icon: Check },
];

const PERSONALITY_PRESETS = [
  { id: 'assistant', name: 'Personal Assistant', desc: 'Warm, direct, learns your preferences over time', icon: Heart },
  { id: 'engineer', name: 'Technical Partner', desc: 'Precise, code-oriented, prefers concrete answers', icon: Cpu },
  { id: 'coach', name: 'Wellness Coach', desc: 'Encouraging, health-focused, proactive about wellbeing', icon: Shield },
  { id: 'minimal', name: 'Minimal', desc: 'Brief responses, no small talk, just the facts', icon: Zap },
];

const LLM_PROVIDERS = [
  { id: 'openai', name: 'OpenAI', desc: 'GPT-4.1, GPT-4o, o3-mini, realtime voice, DALL-E', badge: 'Recommended', envKey: 'OPENAI_API_KEY',
    models: ['gpt-4.1', 'gpt-4.1-mini', 'gpt-4.1-nano', 'gpt-4o', 'gpt-4o-mini', 'o3-mini'], defaultModel: 'gpt-4.1', baseUrl: '' },
  { id: 'anthropic', name: 'Anthropic', desc: 'Claude Sonnet 4, Claude Opus — strong reasoning', envKey: 'ANTHROPIC_API_KEY',
    models: ['claude-sonnet-4-20250514', 'claude-3.5-sonnet-20241022', 'claude-3-opus-20240229'], defaultModel: 'claude-sonnet-4-20250514', baseUrl: '' },
  { id: 'gemini', name: 'Google Gemini', desc: 'Gemini 2.5 Flash/Pro, realtime voice, multimodal', envKey: 'GOOGLE_API_KEY',
    models: ['gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-2.0-flash'], defaultModel: 'gemini-2.5-flash', baseUrl: '' },
  { id: 'openrouter', name: 'OpenRouter', desc: 'Gateway to 300+ models (OpenAI, Claude, Gemini, DeepSeek)', envKey: 'OPENROUTER_API_KEY',
    models: ['openai/gpt-4.1', 'anthropic/claude-sonnet-4', 'google/gemini-2.5-flash', 'deepseek/deepseek-chat', 'meta-llama/llama-3.3-70b-instruct'],
    defaultModel: 'openai/gpt-4.1', baseUrl: 'https://openrouter.ai/api/v1' },
  { id: 'deepseek', name: 'DeepSeek', desc: 'DeepSeek V3 / R1 — strong reasoning, low cost', envKey: 'DEEPSEEK_API_KEY',
    models: ['deepseek-chat', 'deepseek-reasoner'], defaultModel: 'deepseek-chat', baseUrl: 'https://api.deepseek.com' },
  { id: 'kimi', name: 'Kimi (Moonshot)', desc: 'Moonshot v1, 128K context, Chinese + English', envKey: 'MOONSHOT_API_KEY',
    models: ['moonshot-v1-128k', 'moonshot-v1-32k', 'moonshot-v1-8k'], defaultModel: 'moonshot-v1-128k', baseUrl: 'https://api.moonshot.cn/v1' },
  { id: 'qwen', name: 'Qwen (Alibaba)', desc: 'Qwen Max/Plus/Turbo — strong multilingual', envKey: 'DASHSCOPE_API_KEY',
    models: ['qwen-max', 'qwen-plus', 'qwen-turbo'], defaultModel: 'qwen-max', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { id: 'groq', name: 'Groq', desc: 'Ultra-fast inference — Llama 3.3, DeepSeek, Mixtral', envKey: 'GROQ_API_KEY',
    models: ['llama-3.3-70b-versatile', 'deepseek-r1-distill-llama-70b', 'mixtral-8x7b-32768'], defaultModel: 'llama-3.3-70b-versatile',
    baseUrl: 'https://api.groq.com/openai/v1' },
  { id: 'ollama', name: 'Ollama (Local)', desc: 'Free, private, runs on your machine — no API key', badge: 'Private', envKey: '',
    models: [], defaultModel: '', baseUrl: '' },
];

const TOOL_KEYS_CONFIG = [
  { env: 'EXA_API_KEY', name: 'EXA Search', desc: 'Neural search (best quality)', hint: 'exa.ai/dashboard' },
  { env: 'TAVILY_API_KEY', name: 'Tavily', desc: 'Web search (fast, structured)', hint: 'tavily.com' },
  { env: 'SERPER_API_KEY', name: 'Serper', desc: 'Google search API', hint: 'serper.dev' },
  { env: 'BRAVE_API_KEY', name: 'Brave Search', desc: 'Privacy-focused search', hint: 'brave.com/search/api' },
  { env: 'OPENWEATHER_API_KEY', name: 'OpenWeatherMap', desc: 'Weather data', hint: 'openweathermap.org' },
  { env: 'GITHUB_TOKEN', name: 'GitHub', desc: 'Repo operations, PRs', hint: 'github.com/settings/tokens' },
  { env: 'SPOTIFY_CLIENT_ID', name: 'Spotify', desc: 'Music control', hint: 'developer.spotify.com', extraKeys: ['SPOTIFY_CLIENT_SECRET'] },
  { env: 'GOOGLE_CALENDAR_CREDENTIALS', name: 'Google Calendar', desc: 'Scheduling', hint: 'console.cloud.google.com' },
];

const TECH_LEVELS = ['beginner', 'intermediate', 'advanced', 'developer'];
const USE_CASES = ['personal-assistant', 'developer-tool', 'health-monitoring', 'home-automation', 'research', 'other'];
const COMM_STYLES = ['detailed', 'concise', 'casual', 'formal'];

export default function SetupWizard({ onComplete }) {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [config, setConfig] = useState(null);
  const [credentials, setCredentials] = useState({});
  const [settings, setSettings] = useState({});
  const [keyValid, setKeyValid] = useState(null);
  const [validating, setValidating] = useState(false);
  const [showKey, setShowKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [identity, setIdentity] = useState({
    userName: '', location: '', language: 'English', occupation: '', interests: '',
    techLevel: 'intermediate', useCase: 'personal-assistant', commStyle: 'concise',
    isFeralMember: false, healthGoals: '', glassesModel: '', wristband: false,
    agentName: 'FERAL', personality: 'assistant',
  });
  const [deviceConfig, setDeviceConfig] = useState({
    phoneBridgeUrl: '', glassesModel: '', pairPhone: false, registerGlasses: false,
  });

  useEffect(() => {
    fetch(`${API}/api/config`).then(r => r.json()).then(setConfig).catch(() => {});
  }, []);

  const selectedProvider = LLM_PROVIDERS.find(p => p.id === (settings.llm?.provider || 'openai')) || LLM_PROVIDERS[0];

  const validateKey = async () => {
    const provider = settings.llm?.provider || 'openai';
    const providerInfo = LLM_PROVIDERS.find(p => p.id === provider);
    const key = credentials[providerInfo?.envKey] || '';
    if (!key && provider !== 'ollama') return;

    setValidating(true);
    try {
      const resp = await fetch(`${API}/api/config/validate-key`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider,
          api_key: key,
          base_url: settings.llm?.base_url || providerInfo?.baseUrl || '',
        }),
      });
      const data = await resp.json();
      setKeyValid(data.valid);
    } catch {
      setKeyValid(false);
    }
    setValidating(false);
  };

  const handleFinish = async () => {
    setSaving(true);
    try {
      const finalSettings = {
        ...settings,
        devices: {
          phone_bridge_url: deviceConfig.pairPhone ? (deviceConfig.phoneBridgeUrl || 'auto') : '',
          glasses_model: deviceConfig.registerGlasses ? deviceConfig.glassesModel : '',
        },
      };
      const finalCredentials = { ...credentials };
      if (finalCredentials.GOOGLE_API_KEY && !finalCredentials.GEMINI_API_KEY) {
        finalCredentials.GEMINI_API_KEY = finalCredentials.GOOGLE_API_KEY;
      }
      if (finalCredentials.GEMINI_API_KEY && !finalCredentials.GOOGLE_API_KEY) {
        finalCredentials.GOOGLE_API_KEY = finalCredentials.GEMINI_API_KEY;
      }
      await fetch(`${API}/api/setup/complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings: finalSettings, credentials: finalCredentials, identity }),
      });
      onComplete?.();
      navigate('/');
    } catch (e) {
      console.error('Setup failed:', e);
      setError(e.message || 'Setup failed. Please try again.');
    }
    setSaving(false);
  };

  const updateSetting = (section, key, value) => {
    setSettings(prev => ({
      ...prev,
      [section]: { ...(prev[section] || {}), [key]: value },
    }));
  };

  const currentStep = STEPS[step];

  return (
    <div className="min-h-screen bg-feral-bg flex">
      {/* Left Panel */}
      <div className="hidden lg:flex w-72 bg-feral-card border-r border-feral-border flex-col py-8 px-6">
        <div className="flex items-center gap-3 mb-12">
          <Brain size={28} className="text-feral-accent" />
          <span className="text-lg font-bold tracking-wider">FERAL</span>
        </div>
        <div className="space-y-2">
          {STEPS.map((s, i) => {
            const Icon = s.icon;
            const isActive = i === step;
            const isDone = i < step;
            return (
              <div
                key={s.id}
                className={`flex items-center gap-3 px-4 py-3 rounded-xl transition-all ${
                  isActive ? 'bg-feral-accent/15 text-feral-accent' :
                  isDone ? 'text-green-400 opacity-70' : 'text-feral-text-muted'
                }`}
              >
                <div className={`w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold ${
                  isActive ? 'bg-feral-accent text-white' :
                  isDone ? 'bg-green-500/20 text-green-400' :
                  'bg-white/5'
                }`}>
                  {isDone ? <Check size={14} /> : i + 1}
                </div>
                <span className="text-sm font-medium">{s.label}</span>
              </div>
            );
          })}
        </div>
        <div className="mt-auto pt-8 text-xs opacity-30">FERAL v2026.8.10</div>
      </div>

      {/* Right Panel */}
      <div className="flex-1 flex flex-col">
        <div className="lg:hidden flex items-center gap-2 p-4 border-b border-feral-border">
          {STEPS.map((_, i) => (
            <div key={i} className={`h-1.5 flex-1 rounded-full ${i <= step ? 'bg-feral-accent' : 'bg-feral-border'}`} />
          ))}
        </div>

        <div className="flex-1 flex items-center justify-center p-8">
          <div className="w-full max-w-lg">

            {/* ── Welcome ── */}
            {currentStep.id === 'welcome' && (
              <div className="text-center space-y-6">
                <img src="/feral-banner.png" alt="FERAL" style={{ maxWidth: 400, width: '100%', margin: '0 auto 24px', display: 'block', borderRadius: 12 }} />
                <h1 className="text-3xl font-bold">Welcome to FERAL</h1>
                <p className="text-lg font-medium text-feral-accent">The Open AI Operating System</p>
                <p className="text-feral-text-secondary text-sm leading-relaxed max-w-md mx-auto">
                  FERAL is not just another computer-use agent. Unlike tools that only control your screen,
                  FERAL is a full platform that learns, controls hardware, generates dynamic UI, and keeps your data private.
                </p>
                <div className="grid grid-cols-2 gap-3 pt-2 text-sm">
                  {[
                    { icon: Sparkles, text: 'Self-Learning Skills' },
                    { icon: Cpu, text: 'Hardware Control' },
                    { icon: Shield, text: 'Privacy-First Memory' },
                    { icon: Smartphone, text: 'Multi-Device Bridge' },
                    { icon: Globe, text: 'Dynamic GenUI' },
                    { icon: Zap, text: 'NixOS Vision' },
                  ].map(({ icon: I, text }) => (
                    <div key={text} className="flex items-center gap-2 bg-feral-card rounded-lg p-3 border border-feral-border">
                      <I size={16} className="text-feral-accent" />
                      <span className="opacity-80">{text}</span>
                    </div>
                  ))}
                </div>
                <p className="text-xs text-feral-text-muted pt-2">
                  Built for AI developers to extend. Add skills, hardware daemons, GenUI providers, and more.
                </p>
              </div>
            )}

            {/* ── Identity ── */}
            {currentStep.id === 'identity' && (
              <div className="space-y-5">
                <h2 className="text-2xl font-bold">You & Your Agent</h2>
                <p className="text-feral-text-secondary text-sm">Tell FERAL who you are. Saved locally in ~/.feral/USER.md.</p>

                <div className="grid grid-cols-2 gap-3">
                  <InputField label="Your Name" placeholder="Mahmoud" value={identity.userName}
                    onChange={v => setIdentity(p => ({ ...p, userName: v }))} />
                  <InputField label="Location" placeholder="San Francisco, US" value={identity.location}
                    onChange={v => setIdentity(p => ({ ...p, location: v }))} />
                  <InputField label="Preferred Language" placeholder="English" value={identity.language}
                    onChange={v => setIdentity(p => ({ ...p, language: v }))} />
                  <InputField label="Occupation" placeholder="Engineer, Student, ..." value={identity.occupation}
                    onChange={v => setIdentity(p => ({ ...p, occupation: v }))} />
                </div>

                <InputField label="Interests / Hobbies" placeholder="AI, health tech, music" value={identity.interests}
                  onChange={v => setIdentity(p => ({ ...p, interests: v }))} />

                <div className="grid grid-cols-3 gap-3">
                  <SelectField label="Tech Level" value={identity.techLevel} options={TECH_LEVELS}
                    onChange={v => setIdentity(p => ({ ...p, techLevel: v }))} />
                  <SelectField label="Use Case" value={identity.useCase} options={USE_CASES}
                    onChange={v => setIdentity(p => ({ ...p, useCase: v }))} />
                  <SelectField label="Comm. Style" value={identity.commStyle} options={COMM_STYLES}
                    onChange={v => setIdentity(p => ({ ...p, commStyle: v }))} />
                </div>

                <div className="border-t border-feral-border pt-4 space-y-3">
                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={identity.isFeralMember}
                      onChange={e => setIdentity(p => ({ ...p, isFeralMember: e.target.checked }))}
                      className="accent-feral-accent" />
                    I'm part of the FERAL ecosystem (glasses / wristband)
                  </label>
                  {identity.isFeralMember && (
                    <div className="grid grid-cols-2 gap-3 pl-6">
                      <InputField label="Health Goals" placeholder="Better sleep, cardio..." value={identity.healthGoals}
                        onChange={v => setIdentity(p => ({ ...p, healthGoals: v }))} />
                      <SelectField label="Glasses Model" value={identity.glassesModel}
                        options={['none', 'W300', 'W610', 'other']}
                        onChange={v => setIdentity(p => ({ ...p, glassesModel: v }))} />
                    </div>
                  )}
                </div>

                <div className="border-t border-feral-border pt-4 space-y-3">
                  <InputField label="Agent Name" placeholder="FERAL" value={identity.agentName}
                    onChange={v => setIdentity(p => ({ ...p, agentName: v }))} />
                  <label className="text-sm font-medium text-feral-text block">Agent Personality</label>
                  <div className="grid grid-cols-2 gap-3">
                    {PERSONALITY_PRESETS.map(p => {
                      const Icon = p.icon;
                      return (
                        <button key={p.id}
                          onClick={() => setIdentity(prev => ({ ...prev, personality: p.id }))}
                          className={`text-left px-4 py-3.5 rounded-xl border transition-all ${
                            identity.personality === p.id
                              ? 'border-feral-accent bg-feral-accent/10' : 'border-feral-border bg-feral-card hover:border-feral-border-bright'
                          }`}>
                          <div className="flex items-center gap-2 mb-1">
                            <Icon size={14} className="text-feral-accent" />
                            <span className="font-semibold text-sm">{p.name}</span>
                          </div>
                          <div className="text-xs text-feral-text-secondary">{p.desc}</div>
                        </button>
                      );
                    })}
                  </div>
                </div>
              </div>
            )}

            {/* ── LLM Provider ── */}
            {currentStep.id === 'llm' && (
              <div className="space-y-5">
                <h2 className="text-2xl font-bold">Choose Your LLM</h2>
                <p className="text-feral-text-secondary text-sm">FERAL needs a language model. Pick your provider.</p>
                <div className="space-y-2 max-h-[400px] overflow-y-auto pr-1">
                  {LLM_PROVIDERS.map(p => (
                    <button key={p.id}
                      onClick={() => {
                        updateSetting('llm', 'provider', p.id);
                        if (p.baseUrl) updateSetting('llm', 'base_url', p.baseUrl);
                        if (p.defaultModel) updateSetting('llm', 'model', p.defaultModel);
                        setKeyValid(null);
                      }}
                      className={`w-full text-left px-4 py-3 rounded-xl border transition-all ${
                        (settings.llm?.provider || 'openai') === p.id
                          ? 'border-feral-accent bg-feral-accent/10' : 'border-feral-border bg-feral-card hover:border-feral-border-bright'
                      }`}>
                      <div className="flex items-center justify-between">
                        <div>
                          <div className="font-semibold text-sm">{p.name}</div>
                          <div className="text-xs text-feral-text-secondary mt-0.5">{p.desc}</div>
                        </div>
                        {p.badge && (
                          <span className="text-xs px-2 py-1 rounded-full bg-feral-accent/20 text-feral-accent shrink-0">{p.badge}</span>
                        )}
                      </div>
                    </button>
                  ))}
                </div>

                {selectedProvider.models.length > 0 && (
                  <div className="space-y-2">
                    <label className="text-sm font-medium">Model</label>
                    <select
                      className="w-full bg-feral-card border border-feral-border rounded-lg px-4 py-3 focus:outline-none focus:ring-1 focus:ring-feral-accent text-sm"
                      value={settings.llm?.model || selectedProvider.defaultModel}
                      onChange={e => updateSetting('llm', 'model', e.target.value)}
                    >
                      {selectedProvider.models.map(m => <option key={m} value={m}>{m}</option>)}
                    </select>
                  </div>
                )}
              </div>
            )}

            {/* ── API Keys ── */}
            {currentStep.id === 'keys' && (
              <div className="space-y-5">
                <h2 className="text-2xl font-bold">API Keys</h2>
                <p className="text-feral-text-secondary text-sm">
                  {selectedProvider.id === 'ollama'
                    ? 'Ollama runs locally — no API key needed. Add tool keys below.'
                    : 'Enter your API key. Stored locally, never leaves your machine.'}
                </p>

                {selectedProvider.envKey && (
                  <div className="space-y-3">
                    <label className="text-sm font-medium flex items-center gap-2">
                      <Key size={14} className="text-feral-accent" />
                      {selectedProvider.name} API Key
                    </label>
                    <div className="relative">
                      <input
                        type={showKey ? 'text' : 'password'}
                        placeholder="sk-..."
                        className="w-full bg-feral-card border border-feral-border rounded-lg px-4 py-3 pr-20 focus:outline-none focus:ring-1 focus:ring-feral-accent font-mono text-sm"
                        value={credentials[selectedProvider.envKey] || ''}
                        onChange={e => {
                          setCredentials(prev => ({ ...prev, [selectedProvider.envKey]: e.target.value }));
                          setKeyValid(null);
                        }}
                      />
                      <button onClick={() => setShowKey(!showKey)}
                        className="absolute right-12 top-1/2 -translate-y-1/2 p-1 opacity-40 hover:opacity-80">
                        {showKey ? <EyeOff size={16} /> : <Eye size={16} />}
                      </button>
                      <button onClick={validateKey} disabled={validating}
                        className="absolute right-2 top-1/2 -translate-y-1/2 px-2 py-1 bg-feral-accent rounded text-xs font-medium hover:bg-feral-accent/80">
                        {validating ? <Loader2 size={14} className="animate-spin" /> : 'Test'}
                      </button>
                    </div>
                    {keyValid === true && <div className="flex items-center gap-2 text-green-400 text-sm"><Check size={14} /> Key is valid</div>}
                    {keyValid === false && <div className="flex items-center gap-2 text-red-400 text-sm"><AlertCircle size={14} /> Key is invalid or expired</div>}
                  </div>
                )}

                <div className="border-t border-feral-border pt-4 space-y-3">
                  <label className="text-sm font-medium opacity-60 flex items-center gap-2">
                    <Search size={14} /> Tool API Keys (optional)
                  </label>
                  <p className="text-xs text-feral-text-muted">Unlock search, weather, GitHub, music, and more.</p>
                  <div className="space-y-2 max-h-[280px] overflow-y-auto pr-1">
                    {TOOL_KEYS_CONFIG.map(tk => (
                      <div key={tk.env} className="space-y-1">
                        <div className="flex items-center gap-2">
                          <span className="text-xs w-36 text-feral-text-secondary truncate" title={tk.desc}>
                            {tk.name} <span className="opacity-50">({tk.hint})</span>
                          </span>
                          <input type="password" placeholder="API key..."
                            className="flex-1 bg-feral-card border border-feral-border rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-feral-accent font-mono"
                            value={credentials[tk.env] || ''}
                            onChange={e => setCredentials(prev => ({ ...prev, [tk.env]: e.target.value }))} />
                        </div>
                        {tk.extraKeys?.map(ek => (
                          <div key={ek} className="flex items-center gap-2 pl-4">
                            <span className="text-xs w-32 text-feral-text-muted truncate">{ek}</span>
                            <input type="password" placeholder={`${ek}...`}
                              className="flex-1 bg-feral-card border border-feral-border rounded px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-feral-accent font-mono"
                              value={credentials[ek] || ''}
                              onChange={e => setCredentials(prev => ({ ...prev, [ek]: e.target.value }))} />
                          </div>
                        ))}
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* ── Devices ── */}
            {currentStep.id === 'devices' && (
              <div className="space-y-5">
                <h2 className="text-2xl font-bold">Connect Your Devices</h2>
                <p className="text-feral-text-secondary text-sm">
                  FERAL can connect to your phone, glasses, wristband, and more. Your phone acts as a bridge.
                </p>
                <div className="bg-feral-card border border-feral-border rounded-xl p-4 font-mono text-xs text-center space-y-1 text-feral-text-secondary">
                  <div className="flex items-center justify-center gap-2 flex-wrap">
                    <span className="px-2 py-1 rounded bg-feral-accent/10 text-feral-accent">Glasses / Sensors</span>
                    <ChevronRight size={14} className="opacity-40" />
                    <span className="px-2 py-1 rounded bg-feral-accent/10 text-feral-accent">Phone (Bridge)</span>
                    <ChevronRight size={14} className="opacity-40" />
                    <span className="px-2 py-1 rounded bg-feral-accent/10 text-feral-accent">Brain (This PC)</span>
                    <ChevronRight size={14} className="opacity-40" />
                    <span className="px-2 py-1 rounded bg-feral-accent/10 text-feral-accent">Actions</span>
                  </div>
                  <div className="text-[10px] opacity-50 pt-1">Robot, Apps, Home devices, Browser...</div>
                </div>

                <div className="space-y-4">
                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={deviceConfig.pairPhone}
                      onChange={e => setDeviceConfig(p => ({ ...p, pairPhone: e.target.checked }))}
                      className="accent-feral-accent" />
                    Pair a phone as a bridge
                  </label>
                  {deviceConfig.pairPhone && (
                    <InputField label="Phone Bridge URL (leave blank for auto-discovery)"
                      placeholder="ws://192.168.1.100:9090/v1/daemon"
                      value={deviceConfig.phoneBridgeUrl}
                      onChange={v => setDeviceConfig(p => ({ ...p, phoneBridgeUrl: v }))} />
                  )}

                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={deviceConfig.registerGlasses}
                      onChange={e => setDeviceConfig(p => ({ ...p, registerGlasses: e.target.checked }))}
                      className="accent-feral-accent" />
                    Register FERAL glasses
                  </label>
                  {deviceConfig.registerGlasses && (
                    <SelectField label="Glasses Model" value={deviceConfig.glassesModel}
                      options={['W300', 'W610', 'other']}
                      onChange={v => setDeviceConfig(p => ({ ...p, glassesModel: v }))} />
                  )}
                </div>

                {!deviceConfig.pairPhone && !deviceConfig.registerGlasses && (
                  <p className="text-xs text-feral-text-muted">
                    Skip this step if you don't have devices yet. You can pair later from Settings &gt; Devices.
                  </p>
                )}
              </div>
            )}

            {/* ── Skills ── */}
            {currentStep.id === 'skills' && (
              <div className="space-y-6">
                <h2 className="text-2xl font-bold">Skills</h2>
                <p className="text-feral-text-secondary">
                  Skills are API-backed capabilities FERAL can use. Toggle which ones to enable.
                </p>
                <SkillsList />
              </div>
            )}

            {/* ── Connect Apps ── */}
            {currentStep.id === 'apps' && (
              <div className="space-y-6">
                <h2 className="text-2xl font-bold">Connect Apps</h2>
                <p className="text-feral-text-secondary">
                  Link your favorite services. OAuth for Spotify & Notion, API token for Home Assistant.
                </p>
                <div className="space-y-3">
                  <AppConnectCard icon={Music} name="Spotify" desc="Control music playback, search, queue tracks"
                    providerId="spotify" authType="oauth" />
                  <AppConnectCard icon={Home} name="Home Assistant" desc="Control lights, sensors, automations"
                    providerId="home_assistant" authType="token"
                    onTokenSave={(token) => {
                      fetch(`${API}/api/integrations/token`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ provider_id: 'home_assistant', token }),
                      });
                    }} />
                  <AppConnectCard icon={FileText} name="Notion" desc="Read and write pages, query databases"
                    providerId="notion" authType="oauth" />
                </div>
                <div className="border-t border-feral-border pt-4">
                  <h3 className="text-sm font-medium opacity-60 mb-3 flex items-center gap-2">
                    <Server size={14} /> MCP Servers (Advanced)
                  </h3>
                  <p className="text-xs text-feral-text-muted mb-3">
                    Connect MCP-compatible tools like GitHub, Slack, databases. Configure in Settings after setup.
                  </p>
                  <MCPServerList />
                </div>
              </div>
            )}

            {/* ── Features ── */}
            {currentStep.id === 'features' && (
              <div className="space-y-6">
                <h2 className="text-2xl font-bold">Features</h2>
                <p className="text-feral-text-secondary">Enable or disable advanced capabilities.</p>
                <div className="space-y-4">
                  {[
                    { key: 'streaming', label: 'Streaming Responses', desc: 'See LLM output token-by-token in real-time', section: 'features' },
                    { key: 'proactive', label: 'Proactive Agent', desc: 'Autonomous alerts on health anomalies and device status', section: 'features' },
                    { key: 'self_learning', label: 'Self-Learning', desc: 'Extract knowledge from conversations and learn preferences', section: 'features' },
                    { key: 'enabled', label: 'Vision Pipeline', desc: 'Process camera frames from connected glasses', section: 'vision' },
                  ].map(f => (
                    <div key={f.key} className="flex items-center justify-between bg-feral-card border border-feral-border rounded-xl px-5 py-4">
                      <div>
                        <div className="font-medium text-sm">{f.label}</div>
                        <div className="text-xs text-feral-text-secondary mt-0.5">{f.desc}</div>
                      </div>
                      <button
                        onClick={() => {
                          const current = settings[f.section]?.[f.key] ?? (f.key === 'self_learning');
                          updateSetting(f.section, f.key, !current);
                        }}
                        className={`w-12 h-7 rounded-full transition-all flex items-center px-1 ${
                          (settings[f.section]?.[f.key] ?? (f.key === 'self_learning'))
                            ? 'bg-feral-accent justify-end' : 'bg-zinc-700 justify-start'
                        }`}>
                        <div className="w-5 h-5 bg-white rounded-full shadow transition-all" />
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* ── Finish ── */}
            {currentStep.id === 'finish' && (
              <div className="text-center space-y-6">
                <div className="w-20 h-20 rounded-full bg-green-500/15 flex items-center justify-center mx-auto">
                  <Check size={40} className="text-green-400" />
                </div>
                <h2 className="text-3xl font-bold">Ready to Launch</h2>
                <p className="text-feral-text-secondary text-lg max-w-md mx-auto">
                  FERAL is configured and ready. You can always change settings later.
                </p>
                <div className="bg-feral-card border border-feral-border rounded-xl p-5 text-left space-y-2 text-sm">
                  {identity.userName && <SummaryRow label="User" value={identity.userName} />}
                  <SummaryRow label="Agent" value={identity.agentName || 'FERAL'} />
                  <SummaryRow label="Personality" value={PERSONALITY_PRESETS.find(p => p.id === identity.personality)?.name || 'Assistant'} />
                  <SummaryRow label="Provider" value={selectedProvider.name} mono />
                  <SummaryRow label="Model" value={settings.llm?.model || selectedProvider.defaultModel} mono />
                  {deviceConfig.pairPhone && <SummaryRow label="Phone Bridge" value={deviceConfig.phoneBridgeUrl || 'auto-discover'} />}
                  {deviceConfig.registerGlasses && <SummaryRow label="Glasses" value={deviceConfig.glassesModel} />}
                  <SummaryRow label="Streaming" value={(settings.features?.streaming) ? 'On' : 'Off'} />
                  <SummaryRow label="Self-Learning" value={(settings.features?.self_learning ?? true) ? 'On' : 'Off'} />
                </div>
                {error && (
                  <div className="flex items-center gap-2 bg-red-500/10 border border-red-500/30 text-red-400 text-sm px-4 py-3 rounded-xl">
                    <AlertCircle size={16} /> {error}
                  </div>
                )}
                <button onClick={() => { setError(null); handleFinish(); }} disabled={saving}
                  className="px-8 py-4 bg-feral-accent text-white rounded-xl font-bold text-lg hover:bg-feral-accent/90 transition-all active:scale-95 disabled:opacity-50 flex items-center gap-3 mx-auto">
                  {saving ? <Loader2 size={20} className="animate-spin" /> : <Sparkles size={20} />}
                  Launch FERAL
                </button>
              </div>
            )}
          </div>
        </div>

        {/* Navigation */}
        <div className="flex items-center justify-between p-6 border-t border-feral-border">
          <button onClick={() => setStep(s => Math.max(0, s - 1))} disabled={step === 0}
            className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm text-feral-text-secondary hover:text-white disabled:opacity-20 transition">
            <ChevronLeft size={16} /> Back
          </button>
          <div className="text-xs opacity-30">{step + 1} / {STEPS.length}</div>
          {step < STEPS.length - 1 && (
            <button onClick={() => setStep(s => Math.min(STEPS.length - 1, s + 1))}
              className="flex items-center gap-2 px-5 py-2.5 bg-feral-accent text-white rounded-lg text-sm font-medium hover:bg-feral-accent/90 transition active:scale-95">
              Next <ChevronRight size={16} />
            </button>
          )}
          {step === STEPS.length - 1 && <div />}
        </div>
      </div>
    </div>
  );
}


function InputField({ label, placeholder, value, onChange, type = 'text' }) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm font-medium text-feral-text">{label}</label>
      <input type={type}
        className="w-full bg-feral-card border border-feral-border rounded-lg px-4 py-3 text-sm focus:outline-none focus:ring-1 focus:ring-feral-accent"
        placeholder={placeholder} value={value}
        onChange={e => onChange(e.target.value)} />
    </div>
  );
}

function SelectField({ label, value, options, onChange }) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm font-medium text-feral-text">{label}</label>
      <select
        className="w-full bg-feral-card border border-feral-border rounded-lg px-4 py-3 text-sm focus:outline-none focus:ring-1 focus:ring-feral-accent"
        value={value} onChange={e => onChange(e.target.value)}>
        {options.map(o => <option key={o} value={o}>{o}</option>)}
      </select>
    </div>
  );
}

function SummaryRow({ label, value, mono }) {
  return (
    <div className="flex justify-between">
      <span className="text-feral-text-secondary">{label}</span>
      <span className={mono ? 'font-mono' : ''}>{value}</span>
    </div>
  );
}

function AppConnectCard({ icon: Icon, name, desc, providerId, authType, onTokenSave }) {
  const [connected, setConnected] = useState(false);
  const [tokenInput, setTokenInput] = useState('');
  const [showToken, setShowToken] = useState(false);

  useEffect(() => {
    fetch(`${API}/api/integrations`).then(r => r.json()).then(data => {
      setConnected(data[`${providerId}_connected`] || false);
    }).catch(() => {});
  }, [providerId]);

  const handleOAuth = async () => {
    const resp = await fetch(`${API}/api/oauth/authorize/${providerId}`);
    const data = await resp.json();
    if (data.url) window.open(data.url, '_blank');
  };

  const handleTokenSave = () => {
    if (tokenInput && onTokenSave) {
      onTokenSave(tokenInput);
      setConnected(true);
      setTokenInput('');
    }
  };

  return (
    <div className={`bg-feral-card border rounded-xl px-5 py-4 transition-all ${
      connected ? 'border-green-500/40' : 'border-feral-border'
    }`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${
            connected ? 'bg-green-500/15' : 'bg-white/5'
          }`}>
            <Icon size={20} className={connected ? 'text-green-400' : 'text-feral-text-secondary'} />
          </div>
          <div>
            <div className="font-medium text-sm">{name}</div>
            <div className="text-xs text-feral-text-secondary">{desc}</div>
          </div>
        </div>
        {connected ? (
          <span className="text-xs px-2 py-1 rounded-full bg-green-500/20 text-green-400">Connected</span>
        ) : authType === 'oauth' ? (
          <button onClick={handleOAuth}
            className="flex items-center gap-1 px-3 py-1.5 bg-feral-accent/15 text-feral-accent text-xs rounded-lg hover:bg-feral-accent/25 transition">
            <ExternalLink size={12} /> Connect
          </button>
        ) : null}
      </div>
      {authType === 'token' && !connected && (
        <div className="mt-3 flex gap-2">
          <input type={showToken ? 'text' : 'password'} placeholder="Long-lived access token..."
            className="flex-1 bg-feral-bg border border-feral-border rounded px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-feral-accent"
            value={tokenInput} onChange={e => setTokenInput(e.target.value)} />
          <button onClick={handleTokenSave} disabled={!tokenInput}
            className="px-3 py-2 bg-feral-accent text-white text-xs rounded hover:bg-feral-accent/90 disabled:opacity-30">Save</button>
        </div>
      )}
    </div>
  );
}

function MCPServerList() {
  const [servers, setServers] = useState([]);
  useEffect(() => {
    fetch(`${API}/api/mcp/registry`).then(r => r.json()).then(data => {
      setServers(data.servers || []);
    }).catch(() => {});
  }, []);

  if (!servers.length) {
    return (
      <div className="text-xs text-feral-text-muted bg-feral-card border border-feral-border rounded-lg p-4">
        MCP servers will be available after setup. Common servers: GitHub, Slack, PostgreSQL, Browser.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 gap-2">
      {servers.slice(0, 6).map(s => (
        <div key={s.id} className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-xs ${
          s.installed ? 'border-feral-border' : 'border-feral-border opacity-50'
        }`}>
          <Server size={12} className={s.installed ? 'text-feral-accent' : 'text-feral-text-muted'} />
          <span>{s.name}</span>
          {s.installed && <Check size={10} className="text-green-400 ml-auto" />}
        </div>
      ))}
    </div>
  );
}

function SkillsList() {
  const [skills, setSkills] = useState([]);
  useEffect(() => {
    fetch(`${API}/api/skills`).then(r => r.json()).then(setSkills).catch(() => {});
  }, []);

  if (!skills.length) {
    return (
      <div className="bg-feral-card border border-feral-border rounded-xl p-6 text-center">
        <Puzzle size={32} className="mx-auto opacity-30 mb-3" />
        <p className="text-sm opacity-50">No skills loaded yet. Skills will appear when the brain starts.</p>
        <p className="text-xs opacity-30 mt-1">Add JSON manifests to ~/.feral/skills/ or feral-core/skills/manifests/</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {skills.map(s => (
        <div key={s.skill_id} className="flex items-center justify-between bg-feral-card border border-feral-border rounded-xl px-5 py-4">
          <div>
            <div className="font-medium text-sm">{s.name}</div>
            <div className="text-xs text-feral-text-secondary">{s.description}</div>
            <div className="text-xs text-feral-text-muted mt-1">{s.endpoints} endpoint{s.endpoints !== 1 ? 's' : ''}</div>
          </div>
          <div className="w-12 h-7 rounded-full bg-feral-accent flex items-center justify-end px-1">
            <div className="w-5 h-5 bg-white rounded-full shadow" />
          </div>
        </div>
      ))}
    </div>
  );
}
