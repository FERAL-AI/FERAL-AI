/**
 * ChatPanel — phone-side chat tab.
 *
 * Combines PR #58's working shell.sendFrame / subscribeFrame contract
 * with Subagent D's new iOS Web Speech dictation (short tap → native
 * STT into input; long press → open VoiceFullscreen).
 *
 * Post-PR #61 live test fix: Subagent D's initial version used
 * `shell.send({type, payload})` which does not exist on the
 * PairShell context (the real API is `shell.sendFrame(type, payload)`
 * + `shell.subscribeFrame(cb)`). That's why chat messages appeared
 * to send but never hit the brain, and assistant replies never
 * rendered. This rewrite uses the correct shell API end-to-end.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useOutletContext } from 'react-router-dom';
import { Mic, Send } from 'lucide-react';
import { useWebSpeech } from '../../hooks/useWebSpeech';
import { unlockSharedAudioContext } from '../../lib/audioContext';
import { VoiceFullscreen } from './VoiceFullscreen';
import MarkdownMessage from '../../lib/markdown.jsx';
import BudgetExceededBanner from '../../components/BudgetExceededBanner';

const LONG_PRESS_MS = 400;

// Module-level cache of chat messages keyed by deviceId. React Router
// unmounts ChatPanel when the user switches tabs — without this, the
// conversation vanishes every time they tap Voice/Vision/Settings and
// come back. The cache survives unmount since it's in module scope,
// so on remount we rehydrate and keep appending.
const _chatHistory = new Map(); // deviceId → messages[]

function newId() {
  return `m_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 7)}`;
}

function extractChatText(payload) {
  if (!payload) return '';
  if (typeof payload.text === 'string' && payload.text.trim()) return payload.text.trim();
  if (typeof payload.message === 'string' && payload.message.trim()) return payload.message.trim();
  if (typeof payload.content === 'string' && payload.content.trim()) return payload.content.trim();
  return '';
}

export default function ChatPanel({ shell: shellProp }) {
  const outletShell = useOutletContext();
  const shell = shellProp || outletShell || {};

  const cacheKey = shell?.deviceId || 'anonymous';
  const [messages, setMessagesRaw] = useState(() => _chatHistory.get(cacheKey) || []);
  const setMessages = useCallback((updater) => {
    setMessagesRaw((prev) => {
      const next = typeof updater === 'function' ? updater(prev) : updater;
      _chatHistory.set(cacheKey, next);
      return next;
    });
  }, [cacheKey]);
  const [input, setInput] = useState('');
  const [voiceFullscreenOpen, setVoiceFullscreenOpen] = useState(false);
  const [dictationError, setDictationError] = useState(null);
  const [streamingText, setStreamingText] = useState('');
  const [budgetBanners, setBudgetBanners] = useState({});

  const longPressTimerRef = useRef(null);
  const longPressTriggeredRef = useRef(false);
  const bottomRef = useRef(null);
  const historyRef = useRef(null);
  const streamBufferRef = useRef('');

  const speech = useWebSpeech({ continuous: false, interimResults: true });

  const startVoiceSession = useCallback(async () => {
    if (typeof shell?.sendFrame === 'function') {
      const mode = shell?.voice_config?.mode || 'openai_realtime';
      const streamId = `voice-${shell?.deviceId || 'session'}`;
      shell.sendFrame('voice_session_start', {
        session_id: streamId,
        stream_id: streamId,
        voice_mode: mode,
        sample_rate: 24000,
        channels: 1,
        language_hint: 'en-US',
        interrupt_policy: 'barge_in',
      });
    }
    if (shell?.node?.startMic) {
      try {
        await shell.node.startMic();
      } catch {
        // Keep fullscreen available even when mic startup fails.
      }
    }
  }, [shell]);

  // ─ Subscribe to brain frames: chat_response + stream_delta + budget ─
  // Finding 02: phone ChatPanel previously only listened for
  // `chat_response` and so the streaming reply appeared as a lump-sum
  // at the end rather than chunk-by-chunk. We now mirror Chat.jsx and
  // VoiceFullscreen by subscribing to `stream_delta` too, and pre-
  // empt the final `chat_response` if streaming already delivered
  // longer text (avoids the double-bubble).
  useEffect(() => {
    if (typeof shell?.subscribeFrame !== 'function') return undefined;
    return shell.subscribeFrame((frame) => {
      const type = frame?.type;
      if (type === 'stream_delta') {
        const p = frame.payload || {};
        if (p.is_final) {
          const finalText = streamBufferRef.current;
          streamBufferRef.current = '';
          setStreamingText('');
          if (finalText.trim()) {
            setMessages((prev) => [...prev, { id: newId(), role: 'assistant', text: finalText }]);
          }
          return;
        }
        // Skip reasoning deltas on the mobile compact surface — we
        // don't have screen real estate for a collapsed disclosure.
        if (p.kind === 'reasoning') return;
        const delta = String(p.delta || '');
        if (!delta) return;
        streamBufferRef.current += delta;
        setStreamingText(streamBufferRef.current);
        return;
      }
      if (type === 'chat_response' || type === 'text_response') {
        const text = extractChatText(frame.payload);
        if (!text) return;
        // If we already streamed more than the final payload, keep
        // the streamed copy (some providers don't include the full
        // text in the close frame).
        const streamed = streamBufferRef.current;
        streamBufferRef.current = '';
        setStreamingText('');
        const finalText = streamed && streamed.length > text.length ? streamed : text;
        setMessages((prev) => [...prev, { id: newId(), role: 'assistant', text: finalText }]);
        return;
      }
      if (type === 'budget_exceeded') {
        const p = frame.payload || frame || {};
        const site = p.call_site || 'chat';
        setBudgetBanners((prev) => ({
          ...prev,
          [site]: {
            callSite: site,
            capDollars: Number(p.cap_dollars || 0),
            currentDollars: Number(p.current_dollars || 0),
            resetAt: p.reset_at,
            subsystem: p.subsystem || null,
          },
        }));
      } else if (type === 'state_push' && frame.event === 'cost_cap_hit') {
        // S6 — ScreenLoop / background subsystem cap path. Same yellow
        // banner, payload is wrapped in frame.data by BrainState.
        const p = frame.data || {};
        const site = p.call_site || 'unknown';
        setBudgetBanners((prev) => ({
          ...prev,
          [site]: {
            callSite: site,
            capDollars: Number(p.cap_dollars || 0),
            currentDollars: Number(p.current_dollars || 0),
            resetAt: p.reset_at,
            subsystem: p.subsystem || null,
          },
        }));
      }
    });
  }, [shell, setMessages]);

  // ─ Dictation → input field ───────────────────────────────────
  useEffect(() => {
    if (speech.transcript) setInput(speech.transcript);
  }, [speech.transcript]);

  useEffect(() => {
    if (speech.interimTranscript && !speech.transcript) {
      setInput(speech.interimTranscript);
    }
  }, [speech.interimTranscript, speech.transcript]);

  useEffect(() => {
    if (speech.error) setDictationError(speech.error);
  }, [speech.error]);

  // ─ Scroll to latest ──────────────────────────────────────────
  useEffect(() => {
    if (historyRef.current) {
      historyRef.current.scrollTop = historyRef.current.scrollHeight;
    }
    if (bottomRef.current?.scrollIntoView) {
      bottomRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages]);

  // ─ Send ──────────────────────────────────────────────────────
  const canSend = useMemo(
    () => !!shell?.sendFrame && input.trim().length > 0,
    [input, shell],
  );

  const handleSend = useCallback((e) => {
    e?.preventDefault?.();
    const text = input.trim();
    if (!text || typeof shell?.sendFrame !== 'function') return;

    shell.sendFrame('chat_request', {
      session_id: `phone-${shell?.deviceId || 'session'}`,
      text,
      reply_mode: 'stream',
      channel: 'chat',
      reply_to: null,
    });
    setMessages((prev) => [...prev, { id: newId(), role: 'user', text }]);
    setInput('');
    if (speech.listening) speech.stop();
  }, [input, shell, speech]);

  // ─ Mic button: short tap → dictation; long press → fullscreen ─
  const toggleDictation = useCallback(() => {
    if (!speech.supported) return;
    if (speech.listening) {
      speech.stop();
    } else {
      setDictationError(null);
      speech.reset();
      speech.start();
    }
  }, [speech]);

  const handleMicPointerDown = useCallback(() => {
    // v2026.5.29 — kick the shared AudioContext to `running` from
    // inside the synchronous pointer-down gesture. By the time the
    // long-press timer fires `startVoiceSession()` (and the brain
    // streams `audio_response` PCM back), the gesture stack is gone
    // and `AudioContext.resume()` would silently no-op on Chrome,
    // making every later `BufferSource.start()` play silence. Fire-
    // and-forget — the unlock is best-effort and we don't gate the
    // long-press behaviour on it.
    void unlockSharedAudioContext();
    if (!speech.supported) return;
    longPressTriggeredRef.current = false;
    longPressTimerRef.current = setTimeout(() => {
      longPressTriggeredRef.current = true;
      if (speech.listening) speech.stop();
      void startVoiceSession().finally(() => setVoiceFullscreenOpen(true));
    }, LONG_PRESS_MS);
  }, [speech, startVoiceSession]);

  const handleMicPointerUp = useCallback(() => {
    if (longPressTimerRef.current) {
      clearTimeout(longPressTimerRef.current);
      longPressTimerRef.current = null;
    }
    if (!longPressTriggeredRef.current) {
      toggleDictation();
    }
  }, [toggleDictation]);

  const handleMicPointerLeave = useCallback(() => {
    if (longPressTimerRef.current) {
      clearTimeout(longPressTimerRef.current);
      longPressTimerRef.current = null;
    }
  }, []);

  const retryDictation = useCallback(() => {
    setDictationError(null);
    speech.reset();
    speech.start();
  }, [speech]);

  const handleVoiceFullscreenClose = useCallback(async () => {
    try {
      if (shell?.node?.stopMic) await shell.node.stopMic();
    } catch {
      // Ignore stop errors on close.
    }
    setVoiceFullscreenOpen(false);
  }, [shell]);

  return (
    <div className="phone-chat-panel" data-testid="phone-chat-panel">
      <div ref={historyRef} className="phone-chat-log" data-testid="chat-log" aria-live="polite">
        {messages.length === 0 ? (
          <p style={{ opacity: 0.5, fontSize: 14, textAlign: 'center', padding: 24 }}>
            Ask anything to the paired brain.
          </p>
        ) : (
          messages.map((m) => (
            <div key={m.id} className={`phone-chat-row phone-chat-row--${m.role}`}>
              <div className="phone-chat-bubble">
                {m.role === 'assistant' ? <MarkdownMessage text={m.text} className="v2-md--inline" /> : m.text}
              </div>
            </div>
          ))
        )}
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
        {streamingText && (
          <div className="phone-chat-row phone-chat-row--assistant">
            <div className="phone-chat-bubble">
              <MarkdownMessage text={streamingText} className="v2-md--inline" />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      <form className="phone-chat-composer" onSubmit={handleSend} data-testid="chat-composer">
        <div className="phone-chat-input-wrap">
          {speech.listening && (
            <span
              className="phone-chat-recording-dot"
              aria-hidden="true"
              data-testid="recording-dot"
            />
          )}
          <input
            className="phone-chat-input"
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Type a message..."
            aria-label="Chat input"
            data-testid="chat-input"
          />
        </div>

        <button
          type="button"
          className={`phone-chat-mic${speech.listening ? ' is-listening' : ''}${!speech.supported ? ' is-disabled' : ''}`}
          disabled={!speech.supported}
          onPointerDown={speech.supported ? handleMicPointerDown : undefined}
          onPointerUp={speech.supported ? handleMicPointerUp : undefined}
          onPointerLeave={speech.supported ? handleMicPointerLeave : undefined}
          aria-label="Dictate message"
          aria-pressed={speech.listening}
          title={!speech.supported ? 'Dictation not supported on this browser; long-press for voice mode' : undefined}
          data-testid="mic-button"
        >
          <Mic size={18} />
        </button>

        <button
          type="submit"
          className="phone-chat-send"
          disabled={!canSend}
          aria-label="Send"
          data-testid="send-button"
        >
          <Send size={18} />
        </button>
      </form>

      {dictationError && (
        <div className="phone-chat-error" role="alert" data-testid="dictation-error">
          <span>{dictationError.message || 'Dictation error'}</span>
          <button type="button" onClick={retryDictation} data-testid="retry-link">
            Retry
          </button>
        </div>
      )}

      {voiceFullscreenOpen && (
        <VoiceFullscreen
          open={voiceFullscreenOpen}
          onClose={handleVoiceFullscreenClose}
          initialMode="listening"
          shell={shell}
          // v2026.5.29 — long-press in the chat composer opens voice
          // in a docked strip pinned to the viewport bottom; the chat
          // composer and the rest of the dashboard stay interactive
          // (the previous full-viewport modal blocked everything).
          variant="docked"
        />
      )}
    </div>
  );
}
