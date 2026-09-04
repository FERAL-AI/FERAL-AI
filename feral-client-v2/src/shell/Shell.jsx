import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Outlet } from 'react-router-dom';
import Ambient from './Ambient';
import Dock from './Dock';
import SystemBar from './SystemBar';
import RuntimeNotice from './RuntimeNotice';
import WorkRail from './WorkRail';
import CommandPalette from './CommandPalette';
import { PaletteProvider } from './PaletteContext';
import { ChatThreadContext, useChatThread } from './ChatThreadContext';
import { VoiceProvider, useVoice } from './VoiceContext';
import VoiceOverlay from './VoiceOverlay';
import PerceptionShare from '../components/PerceptionShare';
import ProactiveToast from '../components/ProactiveToast';
import ErrorToast from '../components/ErrorToast';
import { apiFetch, apiJson } from '../lib/api';

const ACTIVE_CONVERSATION_KEY = 'feral_v2_active_conversation';
const DEFAULT_GREETING = {
  id: 'hello',
  role: 'assistant',
  text: 'FERAL v2 is listening. What do you need?',
};

function newMessageId() {
  return `m_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

function cloneGreeting() {
  return [{ ...DEFAULT_GREETING }];
}

function readActiveConversationId() {
  try {
    if (typeof localStorage === 'undefined') return '';
    return localStorage.getItem(ACTIVE_CONVERSATION_KEY) || '';
  } catch {
    return '';
  }
}

function writeActiveConversationId(conversationId) {
  try {
    if (typeof localStorage === 'undefined') return;
    if (conversationId) localStorage.setItem(ACTIVE_CONVERSATION_KEY, conversationId);
    else localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
  } catch {
    // best effort only
  }
}

function textFromContent(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map((entry) => {
      if (typeof entry === 'string') return entry;
      if (!entry || typeof entry !== 'object') return '';
      if (typeof entry.text === 'string') return entry.text;
      if (typeof entry.value === 'string') return entry.value;
      return '';
    }).filter(Boolean).join('\n');
  }
  if (content && typeof content === 'object') {
    if (typeof content.text === 'string') return content.text;
    if (typeof content.value === 'string') return content.value;
  }
  return '';
}

/**
 * Does this row carry something the chat log renders BESIDES prose?
 *
 * A tool trace, a reasoning block, a timeline, a notice, an SDUI tree or
 * an attachment list is content in its own right. A turn that ran four
 * tools and answered with an empty string is a real turn, and dropping
 * it for having no text is how a tool-only turn used to vanish.
 */
function isRichMessage(message) {
  if (!message || typeof message !== 'object') return false;
  return !!(
    message.type
    || (Array.isArray(message.tools) && message.tools.length > 0)
    || message.timeline
    || message.reasoning
    || message.notice
    || message.sdui
    || (Array.isArray(message.attachments) && message.attachments.length > 0)
  );
}

/**
 * Coerce a chat row into the shape the log renders: a real id, a role,
 * and `text` resolved from either `text` or the store's `content`.
 *
 * It PRESERVES every other field. It used to project each row down to
 * `{id, role, text}` (with one hand-written exception for `sdui`), and
 * because `Chat.setMessages` is this function, every committed assistant
 * turn lost `tools`, `reasoning`, `timeline`, `model`, `usage`,
 * `attachments` and `type` the instant it was written. Measured against
 * the live brain: four `tool_start` / `tool_result` pairs arrived at the
 * page, rendered as live cards during the turn, and left zero tool cards
 * in the transcript once the turn committed. The page's own unit tests
 * could not see it because they render <Chat /> outside this provider,
 * where the fallback `useState` setter does no normalising at all.
 *
 * Projecting DOWN is the bug, so the fix is to stop projecting: spread
 * the row and override only the three fields this function owns. A new
 * per-turn field cannot silently go missing again.
 */
function normaliseUiMessages(rawMessages) {
  const list = Array.isArray(rawMessages) ? rawMessages : [];
  const mapped = list.map((message) => {
    if (!message || typeof message !== 'object') return null;
    const role = message.role || 'assistant';
    const text = textFromContent(message.text ?? message.content);
    if (!text && !isRichMessage(message)) return null;
    // `content` is the STORE's field name; the log reads `text`. Drop it
    // so one row never carries two spellings of the same prose.
    const { content, ...rest } = message;
    return { ...rest, id: message.id || newMessageId(), role, text };
  }).filter(Boolean);

  if (mapped.length === 0) return cloneGreeting();
  return mapped;
}

/**
 * Project a chat row into the shape the conversation store keeps: the
 * same row, with the log's `text` spelled as the store's `content`.
 *
 * It PRESERVES every other field. It used to project each row down to
 * `{id, role, content}` (with one hand-written exception for `sdui`),
 * which is the write-side twin of the bug normaliseUiMessages above was
 * fixed for. That fix stopped the setter erasing a turn's tool trace at
 * commit time, and this function then erased it again 450 ms later when
 * the autosave POSTed the transcript to /api/conversations/save. So
 * `tools`, `reasoning`, `timeline`, `notice`, `attachments`, `model`,
 * `usage` and `type` all survived on screen and none of them survived a
 * reload. Measured against the live brain: GET
 * /api/conversations/thread-mtn5j55b returned messages whose only keys
 * were ['content', 'id', 'role'] for a turn that had run seven tools.
 * Because the loss happens at write time, a reload (or a brain restart)
 * rendered every past assistant turn as bare prose, and nothing could
 * recover the cards after the fact.
 *
 * The rule is the one normaliseUiMessages follows: spread the row and
 * override only the fields this function owns (`id`, `role`, `content`).
 * Nothing is excluded on purpose. No message row carries a field that
 * Chat.jsx marks as transient-only (`pendingAttachments` is composer
 * state, not a message field, and the `attachments` a user row carries
 * are AttachmentRef handles from POST /api/uploads, not file bodies).
 * The server is a plain `json.dumps` of the list (memory/store.py
 * conversation_save) and `json.loads` on the way back, so nothing is
 * stripped there either. A new per-turn field cannot silently go
 * missing from the store again.
 */
function serialiseConversationMessages(messages) {
  const list = Array.isArray(messages) ? messages : [];
  return list.map((message) => {
    if (!message || typeof message !== 'object') return null;
    // `text` is the LOG's field name; the store reads `content`. Drop it
    // so one row never carries two spellings of the same prose.
    const { text, ...rest } = message;
    return {
      ...rest,
      id: message.id || newMessageId(),
      role: message.role || 'assistant',
      content: typeof text === 'string' ? text : textFromContent(message.content),
    };
  }).filter(Boolean);
}

function deriveConversationTitle(messages) {
  const firstUser = (messages || []).find((m) => m?.role === 'user' && typeof m?.text === 'string' && m.text.trim());
  if (!firstUser) return 'New conversation';
  return firstUser.text.trim().slice(0, 80);
}

// Re-exported from its own module (see ChatThreadContext.js) so
// CommandPalette can read the thread without an import cycle back
// through Shell. Every existing `import { useChatThread } from
// '../shell/Shell'` keeps resolving.
export { useChatThread };

// Named so the round trip between the log's row shape and the store's
// row shape can be unit-tested directly (see
// __tests__/shell/Shell.message-roundtrip.test.jsx). Everything else
// reaches these through <Shell />.
export { normaliseUiMessages, serialiseConversationMessages };

/**
 * Shell is the v2 chrome: ambient background + minimal top menubar + bottom
 * dock. Pages render in the Outlet between them. The VoiceProvider lifts
 * voice state so Menubar + VoiceOverlay agree on one mode.
 *
 * Navigation is exactly two mechanisms. The Dock pins eight destinations;
 * the CommandPalette indexes every destination, every Settings section,
 * and the shell's verbs. Both read `shell/navigation.js`, and the palette
 * is opened from three places (the Dock button, the Menubar search field,
 * Cmd-K) that all drive one piece of state living here. It used to live
 * inside Dock.jsx, which meant the Menubar could not open it and any
 * second trigger would have had its own copy.
 *
 * PerceptionShare.FloatingChip is mounted at the Shell level so the
 * "Sharing camera" indicator is visible no matter which route the user
 * navigates to after they grant permission.
 */
function ShellFrame() {
  const voice = useVoice();
  const [messages, setMessagesState] = useState(() => cloneGreeting());
  const [conversationId, setConversationIdState] = useState('');
  // Initialise ready=true so the chat composer is never wedged on the
  // initial hydration round-trip. Hydration enriches state but doesn't
  // gate the UI. We previously left ready=false until the conversation
  // fetch chain finished, which meant any silent failure (or a slow
  // remount race after navigating away/back) left the chat composer
  // stuck on "Loading conversation…" with no recovery path short of
  // closing the tab. Submission still calls thread.ensureConversation()
  // before sending, so a not-yet-created conversation is materialised
  // on the first user message.
  const [ready, setReady] = useState(true);
  const hydratedRef = useRef(false);
  // The brain's per-install primary orchestrator session id, and which
  // UI conversation is bound to it. Every OTHER conversation is bound to
  // its own orchestrator session (== its conversationId) so threads keep
  // separate histories instead of all funnelling into primary.
  const [primarySessionId, setPrimarySessionId] = useState('');
  const [primaryConversationId, setPrimaryConversationId] = useState('');
  // The palette's Ask row parks the typed query here and navigates to
  // /chat; the composer picks it up and clears it. Shell state rather
  // than a query param because the text is a user's prose and does not
  // belong in a URL, and rather than a DOM event because the composer
  // may not be mounted yet at the moment the palette fires.
  const [askDraft, setAskDraft] = useState('');
  const clearAskDraft = useCallback(() => setAskDraft(''), []);

  // Palette open/close, owned here so the Dock button, the Menubar
  // search field and Cmd-K all drive the same dialog.
  const [paletteOpen, setPaletteOpen] = useState(false);
  const openPalette = useCallback(() => setPaletteOpen(true), []);
  const closePalette = useCallback(() => setPaletteOpen(false), []);
  const togglePalette = useCallback(() => setPaletteOpen((prev) => !prev), []);

  useEffect(() => {
    const onKey = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        togglePalette();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [togglePalette]);

  const palette = useMemo(
    () => ({ open: paletteOpen, openPalette, closePalette, togglePalette }),
    [paletteOpen, openPalette, closePalette, togglePalette],
  );

  const setMessages = useCallback((next) => {
    setMessagesState((prev) => {
      const resolved = typeof next === 'function' ? next(prev) : next;
      return normaliseUiMessages(resolved);
    });
  }, []);

  const setConversation = useCallback((nextConversationId, nextMessages) => {
    const cid = nextConversationId || '';
    setConversationIdState(cid);
    writeActiveConversationId(cid);
    if (nextMessages !== undefined) {
      setMessagesState(normaliseUiMessages(nextMessages));
    }
  }, []);

  const fetchConversation = useCallback(async (targetConversationId) => {
    if (!targetConversationId) return null;
    try {
      const data = await apiJson(`/api/conversations/${encodeURIComponent(targetConversationId)}`);
      if (data?.error) return null;
      return {
        id: data.id || targetConversationId,
        messages: normaliseUiMessages(data.messages || []),
      };
    } catch {
      return null;
    }
  }, []);

  const loadConversation = useCallback(async (targetConversationId) => {
    const loaded = await fetchConversation(targetConversationId);
    if (!loaded) return false;
    setConversation(loaded.id, loaded.messages);
    return true;
  }, [fetchConversation, setConversation]);

  const startNewConversation = useCallback(async () => {
    const fallbackId = `thread-${Date.now().toString(36)}`;
    let nextId = fallbackId;
    try {
      const response = await apiFetch('/api/conversations/new', {
        method: 'POST',
        body: JSON.stringify({ id: fallbackId, title: 'New conversation' }),
      });
      const body = await response.json().catch(() => ({}));
      if (response.ok && body && !body.error) {
        nextId = body.id || fallbackId;
      }
    } catch {
      // keep local fallback id
    }
    const initial = cloneGreeting();
    setConversation(nextId, initial);
    return { id: nextId, messages: initial };
  }, [setConversation]);

  const ensureConversation = useCallback(async () => {
    if (conversationId) return conversationId;
    const created = await startNewConversation();
    return created.id;
  }, [conversationId, startNewConversation]);

  useEffect(() => {
    if (hydratedRef.current) return;
    hydratedRef.current = true;
    let cancelled = false;

    (async () => {
      // Resolve the brain's primary session id up front so we can tell
      // which thread is the cross-surface "primary" one.
      try {
        const ps = await apiJson('/api/sessions/primary', { silent: true });
        if (!cancelled && ps?.session_id) setPrimarySessionId(ps.session_id);
      } catch {
        /* primary id optional — primary thread still works via default ws */
      }

      const stored = readActiveConversationId();
      let hydratedFromConversations = false;
      try {
        const query = stored ? `?conversation_id=${encodeURIComponent(stored)}` : '';
        const active = await apiJson(`/api/conversations/active/thread${query}`);
        if (!active?.error && active?.id) {
          setConversation(active.id, active.messages || []);
          // The conversation resolved by the boot hydration is the
          // default/primary thread; bind it to the primary session.
          if (!cancelled) setPrimaryConversationId(active.id);
          hydratedFromConversations = true;
        }
      } catch {
        // fall through to explicit create
      }

      // v2026.5.29 — also fetch the canonical primary-thread transcript
      // (Phase 9) from the orchestrator and merge any turns the
      // conversations store doesn't yet carry. This makes WebSocket-
      // only chat turns survive a hard refresh: previously the brain
      // appended them to the orchestrator's in-RAM history but the
      // WebUI only rehydrated through /api/conversations/* which is a
      // separate store. If anything goes wrong we just keep the
      // conversation-store thread we already loaded.
      try {
        const transcript = await apiJson('/api/sessions/primary/transcript');
        const wsMessages = Array.isArray(transcript?.messages) ? transcript.messages : [];
        if (wsMessages.length) {
          // Use the functional updater so we see the current messages
          // (whether they came from the conversations store above or
          // are empty) and dedupe by role+text signature.
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
        }
      } catch {
        // Phase 9 endpoint optional — never block hydration on it.
      }

      if (!hydratedFromConversations) {
        const created = await startNewConversation();
        // First-boot default thread is the primary thread.
        if (!cancelled && created?.id) setPrimaryConversationId(created.id);
      }
      // ready is always true (see useState init); no-op here so the
      // composer is interactive even when one of the hydration calls
      // silently hangs or returns an error envelope.
    })();

    return () => { cancelled = true; };
  }, [setConversation, setMessages, startNewConversation]);

  useEffect(() => {
    if (!ready || !conversationId) return;
    const timer = setTimeout(() => {
      const payload = {
        id: conversationId,
        messages: serialiseConversationMessages(messages),
        title: deriveConversationTitle(messages),
      };
      apiFetch('/api/conversations/save', {
        method: 'POST',
        body: JSON.stringify(payload),
      }).catch(() => {
        // best-effort autosave
      });
    }, 450);
    return () => clearTimeout(timer);
  }, [conversationId, messages, ready]);

  // Is the active conversation the primary (cross-surface) thread? Until
  // the primary thread id is known we optimistically treat the active
  // thread as primary so the default chat keeps its existing behaviour.
  const isPrimaryThread = !primaryConversationId || conversationId === primaryConversationId;
  // Token passed to the WebSocket (?session_id=). '' = default/primary
  // connection; a real id binds the socket to that thread's session.
  const activeSessionToken = isPrimaryThread ? '' : conversationId;
  // The REAL orchestrator session id this thread maps to — used for
  // transcript rehydration and for filtering inbound WS frames.
  const activeSessionId = isPrimaryThread ? (primarySessionId || conversationId) : conversationId;

  const chatThread = useMemo(() => ({
    ready,
    conversationId,
    messages,
    setMessages,
    setConversation,
    loadConversation,
    startNewConversation,
    ensureConversation,
    primarySessionId,
    isPrimaryThread,
    activeSessionToken,
    activeSessionId,
    askDraft,
    setAskDraft,
    clearAskDraft,
  }), [
    conversationId,
    ensureConversation,
    loadConversation,
    messages,
    ready,
    setConversation,
    setMessages,
    startNewConversation,
    primarySessionId,
    isPrimaryThread,
    activeSessionToken,
    activeSessionId,
    askDraft,
    clearAskDraft,
  ]);

  // The design's rail collapses, and says so in its own instructions:
  // "Press B to collapse the rail." It is the only way to give the page
  // the full width on a laptop, and there was no control for it at all.
  const [railOpen, setRailOpen] = useState(true);
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'b' && e.key !== 'B') return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      // Never while the operator is typing: b is a letter first.
      const el = document.activeElement;
      const tag = (el?.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || el?.isContentEditable) return;
      e.preventDefault();
      setRailOpen((r) => !r);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    <ChatThreadContext.Provider value={chatThread}>
      <PaletteProvider value={palette}>
        <div className={`v2-shell${voice.active ? ' is-voice-mode' : ''}`}>
          <Ambient />
          {/* The approved design leads with the machine, not a page:
              vitals across the top, the work rail down the left, the
              page in the middle. Menubar stays for the theme and voice
              controls it owns; the search field it used to carry has
              moved to the palette, which is where search belongs. */}
          <SystemBar
            onOpenPalette={openPalette}
            railOpen={railOpen}
            onToggleRail={() => setRailOpen((r) => !r)}
          />
          {/* Sits between the bar and the page, and renders nothing at
              all unless the brain reports something the operator has to
              act on (today: this process is not the build that is
              installed). It reserves its own height on .v2-shell rather
              than floating, so it never lands on a control the way the
              error toast did. */}
          <RuntimeNotice />
          <div className={`v2-shell-body${railOpen ? '' : ' is-rail-collapsed'}`}>
            {railOpen && <WorkRail />}
            <main className="v2-shell-main">
              <Outlet />
            </main>
          </div>
          <Dock />
          <CommandPalette open={paletteOpen} onClose={closePalette} />
          <VoiceOverlay />
          <ProactiveToast />
          <ErrorToast />
          <PerceptionShare.FloatingChip />
        </div>
      </PaletteProvider>
    </ChatThreadContext.Provider>
  );
}

export default function Shell() {
  return (
    <VoiceProvider>
      <ShellFrame />
    </VoiceProvider>
  );
}
