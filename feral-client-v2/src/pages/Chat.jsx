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
import { insertTranscriptMessage, transcriptRowFromPayload } from '../lib/transcriptOrder';
import { useChatThread } from '../shell/Shell';
import { useVoice } from '../shell/VoiceContext';
import MarkdownMessage from '../lib/markdown.jsx';
import BudgetExceededBanner from '../components/BudgetExceededBanner';
import { ToolCallList } from '../components/ToolCallCard';
import ReasoningSection from '../components/ReasoningSection';
import TimelineCard from '../components/TimelineCard';
import TodoPanel from '../components/TodoPanel';
import PlanModeBanner from '../components/PlanModeBanner';
import ChatNotice from '../components/ChatNotice';
import CopyButton from '../ui/CopyButton';

function newId() {
  return `m_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

// Tools whose calls are NOT rendered as a ToolCallCard.
//
// `feral_workflows__todo_write` is a full-list-replacement endpoint that
// the model rewrites on nearly every step, so one card per write buries
// the conversation under a stack of near-identical entries. Its state is
// pinned in <TodoPanel> instead, fed by the `todo_update` frame the
// brain emits alongside the tool result. Exported for the vitest.
export const SUPPRESSED_TOOL_CARDS = new Set(['feral_workflows__todo_write']);

export function isSuppressedToolCard(payload) {
  const name = payload?.tool || payload?.name || '';
  if (SUPPRESSED_TOOL_CARDS.has(name)) return true;
  const skill = payload?.skill_id || '';
  const endpoint = payload?.endpoint_id || '';
  return !!skill && !!endpoint && SUPPRESSED_TOOL_CARDS.has(`${skill}__${endpoint}`);
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
  // Tool calls belonging to the turn currently in flight. Mirrors
  // `pendingTraceRef` into React state so an in-progress call renders
  // live (spinner + ticking elapsed) instead of appearing only after
  // the turn commits. A hung tool used to look like a dead UI.
  const [liveTools, setLiveTools] = useState([]);
  // The agent's own task list for this thread. Replaced wholesale on
  // every `todo_update` frame, mirroring the brain's full-list-
  // replacement contract, so the panel and the store cannot drift.
  const [todos, setTodos] = useState([]);
  // Per-session plan-mode posture. Same contract as `todos`: the brain's
  // `plan_mode` frame is a full state snapshot, so each one replaces
  // this wholesale. Unlike `todo_update` the frame is emitted on
  // TRANSITIONS ONLY, so the effect below also hydrates it over REST
  // when the active session changes; a reload inside plan mode would
  // otherwise drop the banner while the mode was still on.
  const [planMode, setPlanMode] = useState(null);
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
  const logRef = useRef(null);
  const stickToBottomRef = useRef(true);
  const streamBufferRef = useRef('');
  const streamReasoningRef = useRef('');
  const pendingTraceRef = useRef([]);
  const greetingSeenRef = useRef(false);
  // True between "user hit send" and "turn produced something". Used
  // to decide whether an empty finalization is a benign no-op (idle
  // socket chatter) or a turn that silently died and must surface.
  const turnActiveRef = useRef(false);
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

  // Nav-away recovery: the WebSocket is app-level (lives in the Shell)
  // and stays open while the user navigates, so the brain keeps
  // generating and records the completed turn server-side. But the
  // stream handler + commit live in THIS page, which unmounts on
  // navigation — so an answer that finishes while the user is on
  // Settings/another tab is never written into the thread, and the
  // Shell only hydrates the transcript once at boot. On every Chat
  // mount we re-pull the canonical primary transcript and merge any
  // turns the thread is missing (deduped by role+text), so returning
  // to /chat shows the answer that completed while we were away
  // instead of a silently dropped reply. Mirrors the Shell boot merge.
  // Bind the app-level WebSocket to the active thread's orchestrator
  // session. For the primary thread the token is '' (default
  // connection); other threads bind to their own session so chat turns
  // route to — and stream back from — the right conversation. The
  // socket persists across navigation, so a turn started here keeps
  // running server-side even if the user leaves the page.
  const activeSessionToken = thread?.activeSessionToken ?? '';
  const activeSessionId = thread?.activeSessionId || '';
  useEffect(() => {
    if (socket && typeof socket.setSession === 'function') {
      socket.setSession(activeSessionToken);
    }
  }, [socket, activeSessionToken]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      // Rehydrate THIS thread's own orchestrator transcript (never the
      // primary's unless this IS the primary thread) so returning to a
      // thread recovers any turn that completed while we were away —
      // without bleeding another thread's messages in.
      if (!activeSessionId) return;
      try {
        const transcript = await apiJson(`/api/sessions/${encodeURIComponent(activeSessionId)}/transcript`);
        if (cancelled) return;
        const wsMessages = Array.isArray(transcript?.messages) ? transcript.messages : [];
        if (!wsMessages.length) return;
        setMessages((prev) => {
          const seen = new Set(prev.map((m) => `${m.role}|${(m.text || '').trim()}`));
          const additions = [];
          for (const m of wsMessages) {
            const role = m?.role;
            const text = (m?.text || '').trim();
            if (!role || !text) continue;
            const sig = `${role}|${text}`;
            if (seen.has(sig)) continue;
            seen.add(sig);
            additions.push({
              id: `pt_${m.ts_ms || Math.random().toString(36).slice(2, 8)}`,
              role,
              text,
            });
          }
          return additions.length ? [...prev, ...additions] : prev;
        });
      } catch {
        /* transcript optional — never block the page on it */
      }
    })();
    return () => { cancelled = true; };
  }, [setMessages, activeSessionId]);

  // Plan-mode hydration. The `plan_mode` WS frame fires on transitions
  // only, so a tab that opens (or reloads, or switches threads) while a
  // session is already in plan mode never receives one and would show
  // no banner while every mutating call is still being refused. This
  // asks the brain directly. Cheap, and the frame keeps it live after.
  useEffect(() => {
    let cancelled = false;
    setPlanMode(null);
    if (!activeSessionId) return undefined;
    (async () => {
      try {
        const state = await apiJson(
          `/api/sessions/${encodeURIComponent(activeSessionId)}/plan_mode`,
        );
        if (!cancelled) setPlanMode(state || null);
      } catch {
        /* posture is advisory chrome, never block the page on it */
      }
    })();
    return () => { cancelled = true; };
  }, [activeSessionId]);

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

    const syncTrace = (next) => {
      pendingTraceRef.current = next;
      setLiveTools(next);
    };

    const flushTrace = () => {
      const trace = pendingTraceRef.current;
      syncTrace([]);
      return trace.length > 0 ? trace : undefined;
    };

    const pushNotice = (notice) => {
      setThinking(false);
      setToolChip(null);
      streamBufferRef.current = '';
      setStreamingText('');
      turnActiveRef.current = false;
      // Keep any tool trace collected so far attached to the notice:
      // "which call was it on when it blew up" is the first question.
      const tools = flushTrace();
      setMessages((prev) => [...prev, {
        id: newId(),
        role: 'assistant',
        type: 'notice',
        notice,
        tools,
      }]);
    };

    const commit = (text, extras = {}) => {
      const clean = text.trim();
      const reasoning = (streamReasoningRef.current || '').trim();
      const tools = flushTrace();
      const timeline = extras.timeline || null;
      // If there's literally nothing to render (no text, no reasoning,
      // no tool trace, no timeline), don't emit an empty bubble. But if
      // the user was waiting on a live turn, say so instead of leaving
      // the log unchanged. Silent death was the #1 complaint.
      if (!clean && !reasoning && (!tools || tools.length === 0) && !timeline) {
        if (turnActiveRef.current) {
          turnActiveRef.current = false;
          setMessages((prev) => [...prev, {
            id: newId(),
            role: 'assistant',
            type: 'notice',
            notice: {
              kind: 'stalled',
              message: 'The brain closed the stream without sending any content.',
              hint: 'Send the message again, or check the brain logs if this repeats.',
            },
          }]);
        }
        return;
      }
      turnActiveRef.current = false;
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
        // Only present when the provider actually reported them. Never
        // synthesize a zero here: "0 tokens" reads as a real measurement
        // and would be a lie on providers that report no usage.
        model: extras.model || '',
        usage: extras.usage || null,
      }]);
    };

    const unsub = socket.subscribe((msg) => {
      const type = msg?.type;
      // Drop chat frames addressed to a DIFFERENT thread's session. A
      // turn that was started on thread A can finalize after the user
      // switched to thread B; without this guard its delta/response
      // would render in the wrong thread. Frames without a session_id
      // (legacy / broadcast) and frames matching the active session
      // pass through.
      const frameSession = msg?.session_id || '';
      const CHAT_FRAME_TYPES = new Set([
        'stream_delta', 'text_response', 'chat_response',
        'tool_start', 'tool_call', 'skill_start', 'tool_end',
        'tool_result', 'reasoning', 'budget_exceeded', 'skill_proposal',
        'refusal', 'error',
        // The todo panel is per-thread state, so a write on thread A
        // must not repaint thread B's panel.
        'todo_update',
        // Plan mode is per-session for the same reason: entering it on
        // thread A must not tell thread B it cannot act.
        'plan_mode',
        // `transcript` belongs here too: voice frames are session-scoped
        // like every other chat frame, and without it a transcript from
        // a voice session started on thread A rendered into whichever
        // thread happened to be open.
        'transcript',
      ]);
      if (
        frameSession
        && activeSessionId
        && frameSession !== activeSessionId
        && CHAT_FRAME_TYPES.has(type)
      ) {
        return;
      }
      if (type === 'stream_delta') {
        const p = msg.payload || {};
        if (p.is_final) {
          const final = streamBufferRef.current;
          streamBufferRef.current = '';
          setStreamingText('');
          setThinking(false);
          setToolChip(null);
          commit(final, {
            model: p.model || '',
            usage: p.usage && Object.keys(p.usage).length ? p.usage : null,
          });
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
        // The brain re-sends its greeting on every connect that omits
        // ?session_id=, which is every reconnect on the primary thread —
        // and ws.js reconnects ~2s after any drop. This guard is what stops
        // the log filling with greetings after a network blip or a brain
        // restart.
        //
        // It used to compare against the literal 'FERAL Brain connected.
        // How can I help?'. The brain actually sends
        // `${agent_name} connected. How can I help?` (api/routes/config.py
        // _build_greeting), i.e. "FERAL connected." with no "Brain" — and
        // agent_name is operator-configurable, so no literal can be right.
        // The guard therefore never fired. Match the shape instead: the
        // greeting is the only assistant line of the form
        // "<agent> connected. …how can I help?", in either the bare or the
        // "Hey <name>," variant.
        if (/^.+ connected\.\s+(how can i help\?|hey .+, how can i help\?)$/i.test(text.trim())) {
          if (greetingSeenRef.current) return;
          greetingSeenRef.current = true;
        }
        setThinking(false);
        setToolChip(null);
        const streamed = streamBufferRef.current;
        const finalText = streamed && streamed.length > (text?.length || 0) ? streamed : text;
        streamBufferRef.current = '';
        setStreamingText('');
        commit(finalText || '', {
          timeline: p.timeline || null,
          // Same attribution contract as the terminal stream frame. This
          // is the path a default install uses, since `features.streaming`
          // is off unless the operator turns it on.
          model: p.model || '',
          usage: p.usage && Object.keys(p.usage).length ? p.usage : null,
        });
      } else if (type === 'todo_update') {
        // Pinned panel, not a card. See SUPPRESSED_TOOL_CARDS.
        setTodos(Array.isArray(msg.payload?.todos) ? msg.payload.todos : []);
      } else if (type === 'plan_mode') {
        // Full snapshot per frame, like todo_update. <PlanModeBanner>
        // renders nothing when the payload says the mode is off, so the
        // exit frame clears the banner without a second branch here.
        setPlanMode(msg.payload || null);
      } else if (type === 'tool_start' || type === 'tool_call' || type === 'skill_start') {
        const p = msg.payload || {};
        if (isSuppressedToolCard(p)) return;
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
        syncTrace([
          ...pendingTraceRef.current.filter((t) => t.key !== key),
          {
            key,
            label,
            args_preview: argsPreview,
            success: null,
            error: '',
            latency_ms: 0,
            // Wall-clock start so the card can tick a live elapsed
            // counter; the brain only sends latency on completion.
            started_at: Date.now(),
          },
        ]);
        setToolChip(label);
      } else if (type === 'tool_result' || type === 'skill_result') {
        const p = msg.payload || {};
        if (isSuppressedToolCard(p)) return;
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
          // Distinguishes "FERAL declined this" from "this crashed".
          // ToolCallCard keys its refused status off this, never off the
          // error prose. Absent on older brains, which fall back to the
          // previous failed rendering.
          error_code: p.error_code || '',
          latency_ms: Number(p.latency_ms || 0),
        };
        if (idx >= 0) {
          next[idx] = {
            ...next[idx],
            success: result.success,
            error: result.error,
            error_code: result.error_code,
            // Fall back to measured wall-clock when the brain reports
            // 0ms so a slow call never displays as instant.
            latency_ms: result.latency_ms
              || (next[idx].started_at ? Date.now() - next[idx].started_at : 0),
            result_preview: result.result_preview,
          };
        } else {
          next.push(result);
        }
        syncTrace(next);
        // Only clear the "using X…" chip when nothing else is still in
        // flight. Parallel calls used to blank the indicator as soon
        // as the first one returned.
        setToolChip((prev) => {
          const stillRunning = next.find((t) => t.success == null);
          if (stillRunning) return stillRunning.label;
          return prev && next.some((t) => t.label === prev && t.success == null) ? prev : null;
        });
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
      } else if (type === 'refusal') {
        // Structured refusal (supervisor paused / policy gate / autonomy
        // mode). Always rendered inline: a declined turn that shows
        // nothing is indistinguishable from a hung one.
        const p = msg.payload || msg || {};
        pushNotice({
          kind: 'refusal',
          message: p.reason || 'The request was declined.',
          hint: p.retry_hint || '',
          code: p.source ? String(p.source) : '',
        });
      } else if (type === 'error') {
        // Transport-level error frames also raise a global toast via
        // wireSocketGlobalErrors; the inline row is the durable record
        // that *this turn* failed. Skip idle-socket noise: only render
        // when a turn is actually in flight or the frame is addressed
        // to this session.
        const p = msg.payload || msg || {};
        if (!turnActiveRef.current && !frameSession) return;
        pushNotice({
          kind: 'error',
          message: p.message || p.detail || p.reason || 'The brain reported an error.',
          code: p.code ? String(p.code) : '',
          hint: p.recoverable === false
            ? 'This one is not retryable; check the brain logs.'
            : 'You can send the message again.',
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
        // Voice transcripts arrive out of conversation order — the
        // user's own transcription can land after the assistant reply
        // that answered it. Insert by the brain's ordering metadata
        // instead of appending by arrival. See lib/transcriptOrder.js.
        const p = msg.payload || {};
        const row = transcriptRowFromPayload(p, newId());
        if (!row) return;
        // Partials still insert, keyed by item_id, so the user's bubble
        // can appear while they are still speaking; the final replaces
        // it in place rather than stacking a second bubble.
        if (p.is_partial && !row.itemId) return;
        setMessages((prev) => insertTranscriptMessage(prev, row));
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
  }, [socket, activeSessionId]);

  // Streaming polish: only follow the tail when the user is already at
  // (or near) the bottom. Pre-fix, scrolling up to re-read an earlier
  // answer while a new one streamed yanked the view back down on every
  // token, which is the single most obvious "cheap chat UI" tell.
  const onLogScroll = useCallback(() => {
    const el = logRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickToBottomRef.current = distance < 80;
  }, []);

  useEffect(() => {
    if (!stickToBottomRef.current) return;
    const el = bottomRef.current;
    if (!el || typeof el.scrollIntoView !== 'function') return;
    // Instant scroll while streaming: a smooth-scroll animation fights
    // the incoming token cadence and looks janky. Smooth only when settled.
    el.scrollIntoView({ behavior: streamingText ? 'auto' : 'smooth' });
  }, [messages, thinking, streamingText, liveTools]);

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
    setLiveTools([]);
    setStreamingText('');
    turnActiveRef.current = true;
    // PR 10: ship the AttachmentRef list verbatim. The brain
    // (api/server.py text_command handler) forwards `payload.attachments`
    // into the orchestrator context so the model can ground on them.
    const envelope = {
      hop: 'client',
      // Tag the turn with the active thread's session. The socket is
      // already bound to this session (?session_id=), so this is mostly
      // belt-and-suspenders, but it keeps the envelope honest for any
      // server path that reads it.
      ...(activeSessionId ? { session_id: activeSessionId } : {}),
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
      turnActiveRef.current = false;
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
    setLiveTools([]);
    setToolChip(null);
    // The todo list is per-thread. Carrying it across a thread switch
    // would show thread A's tasks under thread B's transcript.
    setTodos([]);
    turnActiveRef.current = false;
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
        <div className="v2-chat-log" ref={logRef} onScroll={onLogScroll}>
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
                ) : m.type === 'notice' ? (
                  <>
                    {m.tools?.length > 0 && <ToolCallList traces={m.tools} />}
                    <ChatNotice {...(m.notice || {})} />
                  </>
                ) : (
                  <>
                    {/* Chronological order: the model reasons, then calls
                        tools, then answers. Reasoning and tool cards are
                        visually subordinate so the answer still reads as
                        the primary content. */}
                    {m.reasoning && (
                      <ReasoningSection text={m.reasoning} defaultOpen={false} />
                    )}
                    {m.tools?.length > 0 && (
                      <ToolCallList traces={m.tools} />
                    )}
                    {m.timeline && (
                      <TimelineCard timeline={m.timeline} />
                    )}
                    {m.role === 'assistant' ? (
                      <MarkdownMessage text={m.text} />
                    ) : (
                      m.text && <div className="v2-chat-bubble">{m.text}</div>
                    )}
                    {m.attachments?.length > 0 && (
                      <div className="v2-chat-msg-attachments">
                        {m.attachments.map((att) => (
                          <span key={att.upload_id} className="v2-chat-attachment-chip">
                            <FileText size={12} aria-hidden="true" />
                            <span className="v2-chat-attachment-chip__name">{att.filename}</span>
                          </span>
                        ))}
                      </div>
                    )}
                    {/* Sibling of the actions row, not a child: that row is
                        hover-revealed via `opacity: 0`, and opacity is
                        inherited by children with no way to opt back in.
                        Attribution has to stay readable without hovering. */}
                    {m.role === 'assistant' && (
                      <TurnMeta model={m.model} usage={m.usage} />
                    )}
                    {m.role === 'assistant' && m.text && (
                      <div className="v2-chat-actions">
                        <CopyButton value={m.text} label="Copy message" />
                      </div>
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
          {(streamingText || streamingReasoning || (liveTools.length > 0 && !thinking)) && (
            <div className="v2-chat-row v2-chat-row--assistant">
              <div className="v2-chat-role" aria-hidden="true"><Orb size={22} mode="speaking" /></div>
              <div className="v2-chat-body">
                {streamingReasoning && (
                  <ReasoningSection
                    text={streamingReasoning}
                    defaultOpen={!streamingText}
                    streaming
                  />
                )}
                {liveTools.length > 0 && <ToolCallList traces={liveTools} />}
                {/* While streaming, render lightweight plain text: the full
                    markdown pipeline (GFM + highlight + KaTeX) is too heavy
                    to re-parse on every frame. The committed message row
                    re-renders once as MarkdownMessage on is_final.
                    `min-height` on the wrapper keeps the first token from
                    shifting the whole log by a line. */}
                {streamingText && (
                  <div className="v2-md v2-stream-plain">
                    {streamingText}
                    <span className="v2-chat-cursor" aria-hidden="true" />
                  </div>
                )}
                {!streamingText && <span className="v2-chat-cursor" aria-hidden="true" />}
              </div>
            </div>
          )}
          {thinking && !streamingText && (
            <div className="v2-chat-row v2-chat-row--assistant">
              <div className="v2-chat-role" aria-hidden="true"><Orb size={22} mode="thinking" /></div>
              <div className="v2-chat-body">
                <div
                  className="v2-chat-working"
                  role="status"
                  aria-live="polite"
                  data-testid="chat-working"
                >
                  <span className="v2-chat-working__dots" aria-hidden="true"><i /><i /><i /></span>
                  <span className="v2-chat-working__label">
                    {toolChip ? `using ${toolChip}…` : 'thinking…'}
                  </span>
                </div>
                {liveTools.length > 0 && <ToolCallList traces={liveTools} />}
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </Pane>

      {/* Pinned below the log, above the composer: the posture stays
          visible as the transcript scrolls. Above the todo list because
          "the agent cannot act right now" outranks "here is what it
          plans to do". */}
      <PlanModeBanner state={planMode} />

      {/* Pinned below the log, above the composer: the list stays visible
          as the transcript scrolls, which is the point of tracking it. */}
      <TodoPanel todos={todos} />

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
            setSendError('');
            if (thread?.loadConversation) {
              const ok = await thread.loadConversation(conversationId);
              // Don't fail silently: if the thread can't be loaded the
              // pane used to just stay open with no feedback, which read
              // as "it won't let me open it". Tell the user instead.
              if (ok) setPaneOpen(null);
              else setSendError("couldn't open that thread — it may have been deleted");
              return;
            }
            try {
              const d = await apiJson(`/api/conversations/${encodeURIComponent(conversationId)}`);
              const msgs = (d.messages || []).map((m) => ({ id: m.id || newId(), role: m.role, text: m.content || m.text || '' }));
              setMessages(msgs);
              setPaneOpen(null);
            } catch {
              setSendError("couldn't open that thread — it may have been deleted");
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

/**
 * Per-turn attribution: which model answered, and what it cost in tokens.
 *
 * Both halves are independently optional because providers differ in what
 * they report. Renders nothing at all when neither is known, rather than
 * showing "unknown" or a zero count. A fabricated number here is worse
 * than an absent one, since the whole point is trusting the meter.
 */
export function TurnMeta({ model, usage }) {
  const hasUsage = usage && (usage.input_tokens || usage.output_tokens || usage.total_tokens);
  if (!model && !hasUsage) return null;
  const inTok = Number(usage?.input_tokens || 0);
  const outTok = Number(usage?.output_tokens || 0);
  const total = Number(usage?.total_tokens || 0) || inTok + outTok;
  const fmt = (n) => n.toLocaleString();
  return (
    <span
      className="v2-chat-turnmeta"
      title={hasUsage ? `${fmt(inTok)} in + ${fmt(outTok)} out = ${fmt(total)} tokens` : undefined}
      data-testid="chat-turn-meta"
    >
      {model && <span className="v2-chat-turnmeta__model">{model}</span>}
      {model && hasUsage && <span className="v2-chat-turnmeta__sep" aria-hidden="true">·</span>}
      {hasUsage && (
        <span className="v2-chat-turnmeta__tokens">
          {fmt(total)}
          {' tokens'}
        </span>
      )}
    </span>
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
