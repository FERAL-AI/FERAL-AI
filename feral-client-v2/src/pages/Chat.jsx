import React, { useCallback, useEffect, useRef, useState } from 'react';
import { History, Save, GitBranch, Plus, Trash2, X, Mic, MicOff, Paperclip, FileText } from 'lucide-react';
import Pane from '../ui/Pane';
import Glass from '../ui/Glass';
import Orb from '../ui/Orb';
import EmptyState from '../ui/EmptyState';
import SduiRenderer, { applySduiPatches } from '../ui/SduiRenderer';
import { useFeralSocket, sendUiEvent } from '../hooks/useFeralSocket';
import { useConnectionStatus } from '../hooks/useConnectionStatus';
import { apiJson, apiFetch } from '../lib/api';
import { unlockSharedAudioContext } from '../lib/audioContext';
import { friendlyToolLabel } from '../lib/toolDisplay';
import { useChatThread } from '../shell/Shell';
import { useVoice } from '../shell/VoiceContext';
import MarkdownMessage from '../lib/markdown.jsx';
import BudgetExceededBanner from '../components/BudgetExceededBanner';
import { ToolCallList } from '../components/ToolCallCard';
import ReasoningSection from '../components/ReasoningSection';
import TimelineCard from '../components/TimelineCard';

function newId() {
  return `m_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

// Client-side defense-in-depth scrubber for assistant display text.
// Mirrors feral-core/agents/chat_sanitizer.py; if an older brain
// build forgets to strip control-token residue, the UI still
// presents clean prose. Kept narrow: only strips recognized residue,
// never invents content.
const TOOL_TAG = '(?:tool_calls|tool_call|function_call|function_calls|tool_use|tool_result|tools)';
const SENTINEL_RE = /<\|[^|>\s][^|>]*\|>/g;
const TOOL_BLOCK_RE = new RegExp(`<\\s*${TOOL_TAG}\\b[^>]*>[\\s\\S]*?<\\/\\s*${TOOL_TAG}\\s*>`, 'gi');
const ORPHAN_CLOSE_RE = new RegExp(`<\\/\\s*${TOOL_TAG}\\s*>`, 'gi');
const ORPHAN_OPEN_RE = new RegExp(`<\\s*${TOOL_TAG}\\b[^>]*\\/?>`, 'gi');
const INVOKE_RE = /\binvoke\s*\[[^\[\]]*(?:\[[^\[\]]*\][^\[\]]*)*\]/gi;
const TRAILING_MARKER_RE = /(?:^|\s)(?:FUNCTION|FUNCTIONS|TOOL|TOOLS)\s*$/;

// S6 helper — normalize both the session-scoped `budget_exceeded`
// frame (chat path, payload from agents/orchestrator.py) and the
// brain-broadcast `cost_cap_hit` event (background-subsystem path
// wrapped by BrainState.broadcast_event as state_push) into the
// shared shape consumed by <BudgetExceededBanner>. Exported for the
// vitest in __tests__/pages/Chat.cost-cap-hit.test.jsx.
export function budgetBannerFromCapHit(p, fallbackSite = 'unknown') {
  const src = p || {};
  return {
    callSite: src.call_site || fallbackSite,
    capDollars: Number(src.cap_dollars || 0),
    currentDollars: Number(src.current_dollars || 0),
    resetAt: src.reset_at,
    subsystem: src.subsystem || null,
  };
}

export function sanitizeAssistantText(input) {
  if (!input) return input;
  let out = String(input);
  out = out.replace(TOOL_BLOCK_RE, '');
  out = out.replace(INVOKE_RE, '');
  out = out.replace(ORPHAN_CLOSE_RE, '');
  out = out.replace(ORPHAN_OPEN_RE, '');
  out = out.replace(SENTINEL_RE, '');
  out = out.replace(TRAILING_MARKER_RE, '');
  return out;
}

export default function Chat() {
  const socket = useFeralSocket();
  const { state } = useConnectionStatus();
  const thread = useChatThread();
  const [localMessages, setLocalMessages] = useState([
    { id: 'hello', role: 'assistant', text: 'FERAL v2 is listening. What do you need?' },
  ]);
  const messages = thread?.messages || localMessages;
  const setMessages = thread?.setMessages || setLocalMessages;
  const [input, setInput] = useState('');
  const [thinking, setThinking] = useState(false);
  const [streamingText, setStreamingText] = useState('');
  const [streamingReasoning, setStreamingReasoning] = useState('');
  const [toolChip, setToolChip] = useState(null);
  // S6 — yellow inline banner emitted by Lane 08's `budget_exceeded`
  // WS frame. Multiple call-sites can exceed simultaneously (chat +
  // vision), so we key the active banners by call_site.
  const [budgetBanners, setBudgetBanners] = useState({});
  const [paneOpen, setPaneOpen] = useState(null); // 'threads' | 'snapshots' | null
  const [pausedThoughts, setPausedThoughts] = useState([]);
  // PR 9 (gap-fill) — in-composer voice mic state. Sourced from the
  // shared VoiceContext so toggling the menubar mic and the chat mic
  // stay in sync.
  const voice = useVoice();
  // PR 10 (gap-fill) — pending attachments (uploaded but not yet sent).
  // Each item is the AttachmentRef shape from POST /api/uploads.
  const [pendingAttachments, setPendingAttachments] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const [dragOver, setDragOver] = useState(false);
  // RC polish: when ``socket.send`` returns false the composer used to
  // empty itself silently and the user's message vanished. We now
  // restore the text + surface a small inline chip so the user knows
  // to retry. Cleared on the next successful send.
  const [sendError, setSendError] = useState('');
  const fileInputRef = useRef(null);

  const bottomRef = useRef(null);
  const streamBufferRef = useRef('');
  const streamReasoningRef = useRef('');
  const pendingTraceRef = useRef([]);
  const greetingSeenRef = useRef(false);
  const chatReady = thread?.ready ?? true;

  // On mount, pull paused thoughts from the consciousness store so the
  // user can re-thread any half-formed sentence the agent was in the
  // middle of before the last restart. These are real paused
  // ConsciousnessEntity rows — not a local state guess. Resume
  // routes through the brain which registers the thought with the
  // orchestrator so the LLM sees [RESUMED THOUGHT] X before the next
  // user message.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiJson('/api/consciousness/state?kind=thought');
        if (cancelled) return;
        const paused = (data?.entities || []).filter(
          (e) => e.status === 'paused' || e.status === 'waiting_user',
        );
        setPausedThoughts(paused);
      } catch {
        /* consciousness endpoint not available -> skip silently */
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const resumeThought = async (thoughtId) => {
    try {
      await apiFetch('/api/consciousness/resume', {
        method: 'POST',
        body: JSON.stringify({ id: thoughtId }),
      });
      setPausedThoughts((prev) => prev.filter((t) => t.id !== thoughtId));
      // Surface the resumed text as an assistant row so the user sees
      // what's about to be re-threaded into the next LLM call.
      const t = pausedThoughts.find((p) => p.id === thoughtId);
      const text = t?.context_json?.text || t?.summary || '';
      if (text) {
        setMessages((prev) => [
          ...prev,
          { id: newId(), role: 'assistant', text: `[continuing from earlier] ${text}` },
        ]);
      }
    } catch { /* keep the thought in the list so user can retry */ }
  };

  const abandonThought = async (thoughtId) => {
    try {
      await apiFetch('/api/consciousness/abandon', {
        method: 'POST',
        body: JSON.stringify({ id: thoughtId }),
      });
    } catch { /* fall through */ }
    setPausedThoughts((prev) => prev.filter((t) => t.id !== thoughtId));
  };

  useEffect(() => {
    const traceKey = (payload) => payload?.call_id || payload?.tool || payload?.name || `tool-${Date.now()}`;

    const flushTrace = () => {
      const trace = pendingTraceRef.current;
      pendingTraceRef.current = [];
      return trace.length > 0 ? trace : undefined;
    };

    const commit = (text, extras = {}) => {
      const clean = text.trim();
      const reasoning = (streamReasoningRef.current || '').trim();
      const tools = flushTrace();
      const timeline = extras.timeline || null;
      // If there's literally nothing to render (no text, no reasoning,
      // no tool trace, no timeline), drop the row — otherwise an empty
      // assistant bubble appears after every cancelled stream.
      if (!clean && !reasoning && (!tools || tools.length === 0) && !timeline) return;
      const id = newId();
      streamReasoningRef.current = '';
      setStreamingReasoning('');
      setMessages((prev) => [...prev, {
        id,
        role: 'assistant',
        text: clean,
        reasoning,
        tools,
        timeline,
      }]);
    };

    const unsub = socket.subscribe((msg) => {
      const type = msg?.type;
      if (type === 'stream_delta') {
        const p = msg.payload || {};
        if (p.is_final) {
          const final = streamBufferRef.current;
          streamBufferRef.current = '';
          setStreamingText('');
          setThinking(false);
          setToolChip(null);
          commit(final);
          return;
        }
        // Reasoning deltas (extended thinking, R1-style models) come
        // through the same frame with `kind: "reasoning"`. Keep them
        // out of the visible buffer; surface them via the collapsed
        // ReasoningSection on the committed assistant row.
        if (p.kind === 'reasoning') {
          const r = String(p.delta || '');
          if (!r) return;
          streamReasoningRef.current += r;
          setStreamingReasoning(streamReasoningRef.current);
          setThinking(false);
          return;
        }
        const delta = sanitizeAssistantText(p.delta || '');
        if (!delta) return;
        streamBufferRef.current += delta;
        setStreamingText(streamBufferRef.current);
        setThinking(false);
      } else if (type === 'text_response' || type === 'chat_response') {
        const p = msg.payload || {};
        const text = sanitizeAssistantText(p.text || p.message || '');
        // Pick up reasoning attached to the final payload if the brain
        // didn't stream it as deltas.
        if (typeof p.reasoning === 'string' && p.reasoning.trim() && !streamReasoningRef.current) {
          streamReasoningRef.current = p.reasoning;
        }
        if (text === 'FERAL Brain connected. How can I help?') {
          if (greetingSeenRef.current) return;
          greetingSeenRef.current = true;
        }
        setThinking(false);
        setToolChip(null);
        const streamed = streamBufferRef.current;
        const finalText = streamed && streamed.length > (text?.length || 0) ? streamed : text;
        streamBufferRef.current = '';
        setStreamingText('');
        commit(finalText || '', { timeline: p.timeline || null });
      } else if (type === 'tool_start' || type === 'tool_call' || type === 'skill_start') {
        const p = msg.payload || {};
        const key = traceKey(p);
        const label = friendlyToolLabel(p);
        // Capture the args preview from whichever field the brain
        // happened to use this turn — different skills emit different
        // shapes (args_preview, args, arguments, params).
        const argsPreview = p.args_preview
          || (p.args != null ? p.args : null)
          || (p.arguments != null ? p.arguments : null)
          || (p.params != null ? p.params : null)
          || '';
        pendingTraceRef.current = [
          ...pendingTraceRef.current.filter((t) => t.key !== key),
          {
            key,
            label,
            args_preview: argsPreview,
            success: null,
            error: '',
            latency_ms: 0,
          },
        ];
        setToolChip(label);
      } else if (type === 'tool_result' || type === 'skill_result') {
        const p = msg.payload || {};
        const key = traceKey(p);
        const label = friendlyToolLabel(p);
        const idx = pendingTraceRef.current.findIndex((t) => t.key === key);
        const next = [...pendingTraceRef.current];
        const result = {
          key,
          label,
          args_preview: '',
          result_preview: p.result_preview
            || (p.result != null ? p.result : null)
            || (p.output != null ? p.output : null)
            || '',
          success: p.success !== false,
          error: p.error || '',
          latency_ms: Number(p.latency_ms || 0),
        };
        if (idx >= 0) {
          next[idx] = {
            ...next[idx],
            success: result.success,
            error: result.error,
            latency_ms: result.latency_ms,
            result_preview: result.result_preview,
          };
        } else {
          next.push(result);
        }
        pendingTraceRef.current = next;
        setToolChip(null);
      } else if (type === 'budget_exceeded') {
        // S6 closer — yellow inline banner. Keyed by call_site so
        // multiple budgets can be exceeded at once. Same banner now
        // also fires for ScreenLoop / proactive / cron / email / mqtt
        // background subsystems via the state_push branch below.
        const p = msg.payload || msg || {};
        const site = p.call_site || 'chat';
        setBudgetBanners((prev) => ({
          ...prev,
          [site]: budgetBannerFromCapHit(p, site),
        }));
      } else if (type === 'state_push' && msg.event === 'cost_cap_hit') {
        // S6 closer (ScreenLoop / background subsystem path) — the
        // brain's BudgetLoopGuard emits cost_cap_hit which BrainState
        // wraps as {type: state_push, event, data}. Normalize and
        // render the same yellow banner.
        const p = msg.data || {};
        const site = p.call_site || 'unknown';
        setBudgetBanners((prev) => ({
          ...prev,
          [site]: budgetBannerFromCapHit(p, site),
        }));
      } else if (type === 'budget_reset') {
        const site = msg?.payload?.call_site || msg?.call_site;
        if (!site) return;
        setBudgetBanners((prev) => {
          if (!prev[site]) return prev;
          const next = { ...prev };
          delete next[site];
          return next;
        });
      } else if (type === 'timeline') {
        // S1 closer (cut-list item #8): the brain emits a dedicated
        // `timeline` frame in parallel with the streaming chat
        // response so the TimelineCard can render before the LLM
        // finishes narrating. Insert it as its own assistant bubble,
        // keyed by session_id + query so a repeat of the same
        // question replaces the prior card instead of stacking.
        const p = msg.payload || {};
        const sessKey = String(p.session_id || msg.session_id || '');
        const queryKey = String(p.query || '');
        const key = `tl_${sessKey}_${queryKey}`;
        const card = {
          id: key,
          role: 'assistant',
          text: '',
          type: 'timeline',
          timeline: {
            entries: Array.isArray(p.entries) ? p.entries : [],
            window: p.window || {},
            summary: p.summary || '',
            degraded_sources: Array.isArray(p.degraded_sources) ? p.degraded_sources : [],
            sources_queried: Array.isArray(p.sources_queried) ? p.sources_queried : [],
            query: queryKey,
          },
        };
        setMessages((prev) => {
          const existing = prev.findIndex((m) => m.id === key && m.type === 'timeline');
          if (existing >= 0) {
            const next = prev.slice();
            next[existing] = card;
            return next;
          }
          return [...prev, card];
        });
      } else if (type === 'transcript') {
        const p = msg.payload || {};
        if (p.is_partial) return;
        const role = p.role || (p.text?.startsWith('[user] ') ? 'user' : 'assistant');
        const text = role === 'user' && p.text?.startsWith('[user] ')
          ? p.text.slice(7) : (p.text || '');
        if (!text) return;
        setMessages((prev) => [...prev, { id: newId(), role, text, source: 'voice' }]);
      } else if (type === 'sdui') {
        // Brain-emitted SDUI payload. Append as its own message so the
        // recursive renderer can mount the tree inline in the chat log.
        const p = msg.payload || {};
        const root = p.root || p;
        if (!root || typeof root !== 'object') return;
        setMessages((prev) => [
          ...prev,
          {
            id: newId(),
            role: 'assistant',
            type: 'sdui',
            sdui: root,
            screen_id: p.screen_id || null,
          },
        ]);
      } else if (type === 'sdui_patch') {
        // In-place mutation of an already-mounted SDUI message. Match
        // on the trailing screen_id so multiple surfaces don't clobber
        // each other.
        const p = msg.payload || {};
        const targetId = p.screen_id;
        if (!targetId) return;
        setMessages((prev) => prev.map((m) => (
          m.type === 'sdui' && m.screen_id === targetId
            ? { ...m, sdui: applySduiPatches(m.sdui, p.patches || []) }
            : m
        )));
      } else if (type === 'permission_request') {
        // Brain refused a computer_use file/shell call because the path
        // is outside the sandbox. Render an inline approval card so the
        // operator can grant the folder without leaving the chat.
        const p = msg.payload || {};
        if (!p.request_id) return;
        setMessages((prev) => {
          // Replace any existing card for this request_id (re-emits are
          // possible if the brain retries after a transient failure).
          const filtered = prev.filter(
            (m) => !(m.type === 'permission_request' && m.requestId === p.request_id),
          );
          return [
            ...filtered,
            {
              id: newId(),
              role: 'assistant',
              type: 'permission_request',
              requestId: p.request_id,
              path: p.path || '',
              operation: p.operation || 'access',
              reason: p.reason || '',
            },
          ];
        });
        setThinking(false);
      }
    });
    return unsub;
  }, [socket]);

  useEffect(() => {
    const el = bottomRef.current;
    if (el && typeof el.scrollIntoView === 'function') el.scrollIntoView({ behavior: 'smooth' });
  }, [messages, thinking, streamingText]);

  const submit = async (e) => {
    e.preventDefault();
    const text = input.trim();
    if (!text || state !== 'open' || !chatReady) return;
    if (thread?.ensureConversation) {
      try {
        await thread.ensureConversation();
      } catch {
        // best effort; keep chatting even if thread ensure call fails
      }
    }
    const attachmentsToSend = pendingAttachments;
    // RC polish: snapshot the composer state BEFORE clearing so we can
    // restore it verbatim if the WS write fails. Pre-fix the user's
    // text was wiped the moment they hit Enter, then ``socket.send``
    // could return ``false`` silently and the message would be lost.
    const previousInput = input;
    const previousAttachments = pendingAttachments;
    setMessages((prev) => [
      ...prev,
      { id: newId(), role: 'user', text, attachments: attachmentsToSend },
    ]);
    setInput('');
    setPendingAttachments([]);
    setThinking(true);
    setSendError('');
    streamBufferRef.current = '';
    pendingTraceRef.current = [];
    setStreamingText('');
    // PR 10: ship the AttachmentRef list verbatim. The brain
    // (api/server.py text_command handler) forwards `payload.attachments`
    // into the orchestrator context so the model can ground on them.
    const envelope = {
      hop: 'client',
      type: 'text_command',
      payload: {
        text,
        context: {},
        ...(attachmentsToSend.length > 0 ? { attachments: attachmentsToSend } : {}),
      },
    };
    // Prefer the tagged ``sendOrFail`` result when the socket exposes
    // it; fall back to the boolean ``send`` for older socket stubs and
    // any test mock that only provides ``send``. Either way ``ok`` is
    // ``false`` only when the write definitively failed — undefined /
    // truthy values are treated as success (back-compat).
    let ok = true;
    let reason = 'ws_not_open';
    if (typeof socket.sendOrFail === 'function') {
      const result = socket.sendOrFail(envelope);
      ok = result?.ok !== false;
      if (!ok) reason = result?.reason || reason;
    } else {
      const sent = socket.send(envelope);
      if (sent === false) ok = false;
    }
    if (!ok) {
      // Restore composer text + attachments and pull the optimistic
      // user-row back out so the user isn't lied to about delivery.
      setInput(previousInput);
      setPendingAttachments(previousAttachments);
      setMessages((prev) => prev.slice(0, -1));
      setThinking(false);
      setSendError(
        reason === 'serialize_failed'
          ? "couldn't send — message too large, try again"
          : "couldn't send — connection issue, try again",
      );
    }
  };

  // ── PR 10: upload helpers ─────────────────────────────────────
  const uploadFiles = useCallback(async (fileList) => {
    if (!fileList || fileList.length === 0) return;
    setUploading(true);
    setUploadError('');
    const accepted = [];
    for (const file of fileList) {
      try {
        const fd = new FormData();
        fd.append('file', file, file.name);
        const resp = await apiFetch('/api/uploads', { method: 'POST', body: fd });
        if (!resp.ok) {
          const errBody = await resp.json().catch(() => ({}));
          throw new Error(errBody.detail || `upload failed (${resp.status})`);
        }
        const rec = await resp.json();
        accepted.push({
          upload_id: rec.upload_id,
          filename: rec.filename,
          content_type: rec.content_type,
          size_bytes: rec.size_bytes,
          sha256: rec.sha256,
        });
      } catch (err) {
        setUploadError(String(err.message || err));
      }
    }
    if (accepted.length > 0) {
      setPendingAttachments((prev) => [...prev, ...accepted]);
    }
    setUploading(false);
  }, []);

  const onFilePick = useCallback((e) => {
    const fl = e?.target?.files;
    if (fl && fl.length > 0) uploadFiles(Array.from(fl));
    if (e?.target) e.target.value = '';
  }, [uploadFiles]);

  const onPaste = useCallback((e) => {
    const items = e?.clipboardData?.items || [];
    const files = [];
    for (const it of items) {
      if (it.kind === 'file') {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length > 0) {
      e.preventDefault();
      uploadFiles(files);
    }
  }, [uploadFiles]);

  const onDrop = useCallback((e) => {
    e.preventDefault();
    setDragOver(false);
    const files = Array.from(e.dataTransfer?.files || []);
    if (files.length > 0) uploadFiles(files);
  }, [uploadFiles]);

  const removeAttachment = useCallback((uploadId) => {
    setPendingAttachments((prev) => prev.filter((a) => a.upload_id !== uploadId));
  }, []);

  // ── PR 9: voice toggle ────────────────────────────────────────
  const onMicClick = useCallback(() => {
    // v2026.5.29 — unlock the shared AudioContext inside the click
    // gesture so VoiceOverlay / VoiceContext's assistant audio
    // actually plays. Chrome's autoplay policy keeps new contexts
    // suspended until a user gesture resumes them; an async resume
    // later silently no-ops.
    void unlockSharedAudioContext();
    if (!voice || !voice.toggle) return;
    voice.toggle();
  }, [voice]);

  // RC polish: thread switches used to keep any in-flight ``thinking``
  // / ``streamingText`` / buffered delta from the previous thread alive
  // — the mid-stream indicator stuck on the new thread until a new
  // ``stream_delta`` final arrived (which it never would, because the
  // brain finalized on the prior conversation_id). Reset every
  // streaming surface explicitly before loading the new conversation
  // so the UI never mixes two threads' state.
  const resetStreamingState = useCallback(() => {
    setThinking(false);
    setStreamingText('');
    setStreamingReasoning('');
    streamBufferRef.current = '';
    streamReasoningRef.current = '';
    pendingTraceRef.current = [];
    setToolChip(null);
  }, []);

  const respondToPermission = useCallback((requestId, granted) => {
    if (!requestId) return;
    sendUiEvent(socket, {
      screen_id: 'chat',
      action_id: `${granted ? 'perm_grant_' : 'perm_deny_'}${requestId}`,
      event: 'tap',
    });
    // Replace the live card with a settled receipt so the user sees
    // the decision was registered. The brain emits its own follow-up
    // text, but the receipt is shown immediately so the UI never
    // looks unresponsive between click and reply.
    setMessages((prev) => prev.map((m) => (
      m.type === 'permission_request' && m.requestId === requestId
        ? { ...m, type: 'permission_request_settled', granted }
        : m
    )));
  }, [socket, setMessages]);

  return (
    <div className="v2-chat v2-chat--paned" data-testid="v2-marker">
      <Pane
        title="Conversation"
        actions={(
          <>
            <button type="button" className={`v2-btn v2-btn--ghost${paneOpen === 'threads' ? ' is-active' : ''}`} onClick={() => setPaneOpen((p) => p === 'threads' ? null : 'threads')} title="Threads">
              <History size={13} />
            </button>
            <button type="button" className={`v2-btn v2-btn--ghost${paneOpen === 'snapshots' ? ' is-active' : ''}`} onClick={() => setPaneOpen((p) => p === 'snapshots' ? null : 'snapshots')} title="Snapshots">
              <Save size={13} />
            </button>
          </>
        )}
      >
        {pausedThoughts.length > 0 && (
          <div className="v2-chat-rehydrate" role="status" aria-live="polite">
            {pausedThoughts.map((t) => {
              const text = t.context_json?.text || t.summary || '';
              return (
                <Glass key={t.id} level={0} radius="md" padding="sm" className="v2-chat-rehydrate-row">
                  <div className="v2-chat-rehydrate-body">
                    <strong>Continuing from earlier:</strong>
                    <div className="v2-p v2-p--muted" style={{ marginTop: 4 }}>
                      {text.slice(0, 200)}{text.length > 200 ? '…' : ''}
                    </div>
                  </div>
                  <div className="v2-chat-rehydrate-actions">
                    <button type="button" className="v2-btn v2-btn--primary" onClick={() => resumeThought(t.id)}>
                      Resume
                    </button>
                    <button type="button" className="v2-btn" onClick={() => abandonThought(t.id)}>
                      Abandon
                    </button>
                  </div>
                </Glass>
              );
            })}
          </div>
        )}
        <div className="v2-chat-log">
          {messages.map((m) => (
            <div key={m.id} className={`v2-chat-row v2-chat-row--${m.role}`}>
              <div className="v2-chat-role" aria-hidden="true">
                <Orb size={22} mode={m.role === 'user' ? 'observing' : 'idle'} />
              </div>
              <div className="v2-chat-body">
                {m.type === 'sdui' ? (
                  <SduiRenderer
                    tree={m.sdui}
                    onAction={(action_id, value) => sendUiEvent(socket, {
                      screen_id: m.screen_id || m.id,
                      action_id,
                      value,
                    })}
                  />
                ) : m.type === 'permission_request' ? (
                  <PermissionCard
                    path={m.path}
                    operation={m.operation}
                    reason={m.reason}
                    onAllow={() => respondToPermission(m.requestId, true)}
                    onDeny={() => respondToPermission(m.requestId, false)}
                  />
                ) : m.type === 'permission_request_settled' ? (
                  <div className="v2-chat-perm v2-chat-perm--settled">
                    {m.granted ? `Granted access to ${m.path || 'requested folder'}.`
                      : `Denied access to ${m.path || 'requested folder'}.`}
                  </div>
                ) : m.type === 'timeline' ? (
                  <TimelineCard timeline={m.timeline} />
                ) : (
                  <>
                    {m.role === 'assistant' ? (
                      <MarkdownMessage text={m.text} />
                    ) : (
                      m.text
                    )}
                    {m.reasoning && (
                      <ReasoningSection text={m.reasoning} defaultOpen={false} />
                    )}
                    {m.timeline && (
                      <TimelineCard timeline={m.timeline} />
                    )}
                    {m.tools?.length > 0 && (
                      <ToolCallList traces={m.tools} />
                    )}
                  </>
                )}
              </div>
            </div>
          ))}
          {Object.values(budgetBanners).map((b) => (
            <BudgetExceededBanner
              key={b.callSite}
              callSite={b.callSite}
              capDollars={b.capDollars}
              currentDollars={b.currentDollars}
              resetAt={b.resetAt}
              subsystem={b.subsystem}
              onDismiss={() => setBudgetBanners((prev) => {
                const next = { ...prev };
                delete next[b.callSite];
                return next;
              })}
            />
          ))}
          {(streamingText || streamingReasoning) && (
            <div className="v2-chat-row v2-chat-row--assistant">
              <div className="v2-chat-role" aria-hidden="true"><Orb size={22} mode="speaking" /></div>
              <div className="v2-chat-body">
                {streamingText && <MarkdownMessage text={streamingText} />}
                {streamingReasoning && !streamingText && (
                  <ReasoningSection text={streamingReasoning} defaultOpen />
                )}
                <span className="v2-chat-cursor" aria-hidden="true" />
              </div>
            </div>
          )}
          {thinking && !streamingText && (
            <div className="v2-chat-row v2-chat-row--assistant">
              <div className="v2-chat-role" aria-hidden="true"><Orb size={22} mode="thinking" /></div>
              <div className="v2-chat-body v2-chat-body--thinking">
                {toolChip ? `using ${toolChip}…` : 'thinking…'}
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </Pane>

      {pendingAttachments.length > 0 && (
        <div className="v2-chat-attachment-chips" role="list" aria-label="Pending attachments">
          {pendingAttachments.map((att) => (
            <span key={att.upload_id} className="v2-chat-attachment-chip" role="listitem">
              <FileText size={14} aria-hidden="true" />
              <span className="v2-chat-attachment-chip__name" title={att.filename}>
                {att.filename}
              </span>
              <button
                type="button"
                className="v2-chat-attachment-chip__remove"
                onClick={() => removeAttachment(att.upload_id)}
                aria-label={`Remove ${att.filename}`}
              >
                <X size={12} />
              </button>
            </span>
          ))}
          {uploading && <span className="v2-chat-attachment-chip v2-chat-attachment-chip--loading">uploading…</span>}
        </div>
      )}
      {uploadError && (
        <div className="v2-chat-upload-error" role="alert">{uploadError}</div>
      )}
      {sendError && (
        <div
          className="v2-chat-send-error"
          role="alert"
          data-testid="chat-send-error"
        >
          {sendError}
        </div>
      )}

      <Glass
        as="form"
        level={2}
        radius="pill"
        padding="sm"
        className={`v2-chat-composer${dragOver ? ' v2-chat-composer--dragover' : ''}`}
        onSubmit={submit}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
      >
        <button
          type="button"
          className="v2-chat-attach"
          onClick={() => fileInputRef.current && fileInputRef.current.click()}
          aria-label="Attach file"
          disabled={state !== 'open' || !chatReady || uploading}
        >
          <Paperclip size={18} aria-hidden="true" />
        </button>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          onChange={onFilePick}
          style={{ display: 'none' }}
          aria-hidden="true"
        />
        <input
          className="v2-chat-input"
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onPaste={onPaste}
          placeholder={!chatReady ? 'Loading conversation…' : state === 'open' ? 'Ask FERAL…' : 'Reconnecting…'}
          disabled={state !== 'open' || !chatReady}
        />
        <button
          type="button"
          className={`v2-chat-mic${voice?.active ? ' v2-chat-mic--active' : ''}`}
          onClick={onMicClick}
          aria-label={voice?.active ? 'Stop voice mode' : 'Start voice mode'}
          aria-pressed={!!voice?.active}
          disabled={state !== 'open' || !chatReady}
          title={voice?.active ? `Voice active (${voice.provider || 'realtime'})` : 'Hold a conversation by voice'}
        >
          {voice?.active ? <MicOff size={18} aria-hidden="true" /> : <Mic size={18} aria-hidden="true" />}
        </button>
        <button type="submit" className="v2-chat-send" disabled={!input.trim() || state !== 'open' || !chatReady} aria-label="Send">Send</button>
      </Glass>

      {paneOpen === 'threads' && (
        <ThreadsPane
          onClose={() => setPaneOpen(null)}
          onOpenConversation={async (conversationId) => {
            // Drop any in-flight streaming state from the previous
            // thread before swapping the transcript — otherwise the
            // "thinking…" indicator or a half-streamed assistant
            // bubble can carry over and look like the new thread is
            // mid-reply when it isn't.
            resetStreamingState();
            if (thread?.loadConversation) {
              const ok = await thread.loadConversation(conversationId);
              if (ok) setPaneOpen(null);
              return;
            }
            try {
              const d = await apiJson(`/api/conversations/${encodeURIComponent(conversationId)}`);
              const msgs = (d.messages || []).map((m) => ({ id: m.id || newId(), role: m.role, text: m.content || m.text || '' }));
              setMessages(msgs);
              setPaneOpen(null);
            } catch {
              /* silent */
            }
          }}
          onStartNewConversation={async () => {
            resetStreamingState();
            if (thread?.startNewConversation) {
              await thread.startNewConversation();
              setPaneOpen(null);
              return;
            }
            try {
              const r = await apiFetch('/api/conversations/new', { method: 'POST' });
              if (r.ok) {
                setMessages([{ id: 'hello', role: 'assistant', text: 'New thread started. What do you need?' }]);
                setPaneOpen(null);
              }
            } catch {
              /* silent */
            }
          }}
        />
      )}
      {paneOpen === 'snapshots' && (
        <SnapshotsPane
          onClose={() => setPaneOpen(null)}
          onRestore={(msgs) => { resetStreamingState(); setMessages(msgs); setPaneOpen(null); }}
        />
      )}
    </div>
  );
}

function PermissionCard({ path, operation, reason, onAllow, onDeny }) {
  const verb = operation === 'write' ? 'write to' : operation === 'read' ? 'read from' : 'access';
  return (
    <Glass level={1} radius="md" padding="sm" className="v2-chat-perm">
      <div className="v2-chat-perm-head">
        <strong>FERAL needs permission to {verb}:</strong>
        <code className="v2-chat-perm-path">{path || '(unknown path)'}</code>
      </div>
      {reason && <div className="v2-chat-perm-reason">{reason}</div>}
      <div className="v2-chat-perm-actions">
        <button type="button" className="v2-btn v2-btn--primary" onClick={onAllow}>
          Allow
        </button>
        <button type="button" className="v2-btn" onClick={onDeny}>
          Deny
        </button>
      </div>
      <div className="v2-chat-perm-hint v2-p v2-p--muted">
        Allowing grants persistent {operation === 'write' ? 'read+write' : 'read'} access
        until you revoke it (Settings → Workspace grants, or
        <code style={{ marginLeft: 4 }}>feral grant revoke {path || '<path>'}</code>).
      </div>
    </Glass>
  );
}

function ThreadsPane({ onClose, onOpenConversation, onStartNewConversation }) {
  const [threads, setThreads] = useState([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const d = await apiJson('/api/conversations');
      setThreads(d.conversations || d.items || []);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  const open = async (id) => {
    try {
      if (onOpenConversation) await onOpenConversation(id);
    } finally {
      refresh();
    }
  };

  const startNew = async () => {
    try {
      if (onStartNewConversation) await onStartNewConversation();
    } finally {
      refresh();
    }
  };

  const del = async (id) => {
    await apiFetch(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
    refresh();
  };

  return (
    <div className="v2-chat-pane">
      <header className="v2-chat-pane-head">
        <h3>Threads</h3>
        <button type="button" className="v2-btn v2-btn--ghost" onClick={onClose} aria-label="Close"><X size={13} /></button>
      </header>
      <div className="v2-forge-actions">
        <button type="button" className="v2-btn v2-btn--primary" onClick={startNew}><Plus size={12} /> New thread</button>
      </div>
      {loading && <EmptyState title="Loading…" />}
      {!loading && threads.length === 0 && <EmptyState title="No threads yet" />}
      <ul className="v2-mem-list">
        {threads.map((t) => (
          <li key={t.id}>
            <Glass level={0} radius="sm" padding="sm">
              <div className="v2-flow-card-head">
                <button type="button" className="v2-flow-card-title" onClick={() => open(t.id)}>
                  {t.title || t.id.slice(0, 16)}
                </button>
                <button type="button" className="v2-btn v2-btn--ghost" onClick={() => del(t.id)} aria-label="Delete"><Trash2 size={12} /></button>
              </div>
              {t.updated_at && <div className="v2-mem-meta">{new Date(t.updated_at * 1000).toLocaleString()}</div>}
            </Glass>
          </li>
        ))}
      </ul>
    </div>
  );
}

function SnapshotsPane({ onClose, onRestore }) {
  const [snapshots, setSnapshots] = useState([]);
  const [loading, setLoading] = useState(true);
  const [sessionId, setSessionId] = useState('');
  const [busy, setBusy] = useState('');
  const [err, setErr] = useState('');

  // Resolve the primary session_id once on mount. The snapshot
  // endpoints all require it — the UI used to omit it which is why
  // save returned `{error: "session_id is required"}` silently.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const r = await apiJson('/api/sessions/primary', { silent: true });
        if (!cancelled) setSessionId(r?.session_id || '');
      } catch {
        // leave blank — UI surfaces error on save/restore
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const params = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
      const d = await apiJson(`/api/session/snapshots${params}`);
      setSnapshots(d.snapshots || d.items || []);
    } catch (e) {
      setErr(e?.message || 'failed to list snapshots');
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => { refresh(); }, [refresh]);

  const save = async () => {
    if (!sessionId) { setErr('No active session yet'); return; }
    setBusy('save');
    setErr('');
    try {
      await apiJson('/api/session/snapshot', {
        method: 'POST',
        body: JSON.stringify({ session_id: sessionId }),
      });
      await refresh();
    } catch (e) {
      setErr(e?.message || 'snapshot failed');
    } finally {
      setBusy('');
    }
  };

  // Restore parses the snapshot's own message history via the
  // /api/session/snapshots/{id} GET (since /session/restore returns
  // metadata, not the messages themselves).
  const restore = async (id) => {
    setBusy(`restore:${id}`);
    setErr('');
    try {
      await apiJson('/api/session/restore', {
        method: 'POST',
        body: JSON.stringify({ snapshot_id: id, session_id: sessionId }),
      });
      const snap = await apiJson(`/api/session/snapshots/${encodeURIComponent(id)}`);
      const history = Array.isArray(snap?.history) ? snap.history : [];
      const msgs = history
        .filter((m) => m && (m.role === 'user' || m.role === 'assistant'))
        .map((m) => ({
          id: newId(),
          role: m.role,
          text: typeof m.content === 'string' ? m.content : (m.text || ''),
        }));
      onRestore(msgs);
    } catch (e) {
      setErr(e?.message || 'restore failed');
    } finally {
      setBusy('');
    }
  };

  const branch = async (id) => {
    setBusy(`branch:${id}`);
    setErr('');
    try {
      await apiJson('/api/session/branch', {
        method: 'POST',
        body: JSON.stringify({ snapshot_id: id, session_id: sessionId }),
      });
      await refresh();
    } catch (e) {
      setErr(e?.message || 'branch failed');
    } finally {
      setBusy('');
    }
  };

  return (
    <div className="v2-chat-pane">
      <header className="v2-chat-pane-head">
        <h3>Snapshots</h3>
        <button type="button" className="v2-btn v2-btn--ghost" onClick={onClose} aria-label="Close"><X size={13} /></button>
      </header>
      <div className="v2-forge-actions">
        <button type="button" className="v2-btn v2-btn--primary" onClick={save} disabled={busy === 'save' || !sessionId}>
          <Save size={12} /> {busy === 'save' ? 'Saving…' : 'Snapshot now'}
        </button>
      </div>
      {err && <div className="v2-chip v2-chip--error" role="alert">{err}</div>}
      {loading && <EmptyState title="Loading…" />}
      {!loading && snapshots.length === 0 && <EmptyState title="No snapshots yet" />}
      <ul className="v2-mem-list">
        {snapshots.map((s) => {
          // Backend stores snapshots with `snapshot_id` (preferred) but
          // some older rows surface as `id`. Normalise so the UI never
          // dispatches an empty id (the bug that made Restore a no-op).
          const sid = s.snapshot_id || s.id;
          return (
            <li key={sid}>
              <Glass level={0} radius="sm" padding="sm">
                <div className="v2-flow-card-head">
                  <span className="v2-flow-card-title">{s.label || s.title || sid.slice(0, 16)}</span>
                </div>
                {s.created_at && <div className="v2-mem-meta">{new Date(s.created_at * 1000).toLocaleString()}</div>}
                <div className="v2-forge-actions">
                  <button type="button" className="v2-btn" onClick={() => restore(sid)} disabled={busy === `restore:${sid}`}>
                    {busy === `restore:${sid}` ? 'Restoring…' : 'Restore'}
                  </button>
                  <button type="button" className="v2-btn" onClick={() => branch(sid)} disabled={busy === `branch:${sid}`}>
                    <GitBranch size={12} /> {busy === `branch:${sid}` ? 'Branching…' : 'Branch'}
                  </button>
                </div>
              </Glass>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
