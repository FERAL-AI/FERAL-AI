/**
 * VoiceFullscreen mute: the UI half of the end-to-end control.
 *
 * Pre-fix, `isMuted` was a local boolean with no relationship to
 * anything. It did not stop the microphone (BrowserNode was never
 * told), it did not reflect brain state (nothing read `voice_status`),
 * and the status line kept saying "Listening…" while the button said
 * muted. It was reset to false on every reopen, so a reconnect into a
 * still-muted session showed a live mic.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, fireEvent, act } from '@testing-library/react';
import { VoiceFullscreen } from '../../../pages/phone/VoiceFullscreen';
import { __resetAudioContextForTests } from '../../../lib/audioContext';

let frameListeners = [];

function makeNode() {
  return {
    _micMuted: false,
    setMicMuted: vi.fn(function (m) { this._micMuted = m; }),
    isMicMuted: vi.fn(function () { return this._micMuted; }),
  };
}

function makeShell(overrides = {}) {
  return {
    sendFrame: vi.fn(),
    subscribeFrame: vi.fn((cb) => {
      frameListeners.push(cb);
      return () => {
        frameListeners = frameListeners.filter((l) => l !== cb);
      };
    }),
    voice_config: { mode: 'openai_realtime' },
    node: null,
    ...overrides,
  };
}

function pushFrame(type, payload = {}) {
  act(() => {
    frameListeners.forEach((cb) => cb({ type, payload }));
  });
}

beforeEach(() => {
  frameListeners = [];
  __resetAudioContextForTests();
  vi.stubGlobal('requestAnimationFrame', vi.fn((cb) => setTimeout(cb, 0)));
  vi.stubGlobal('cancelAnimationFrame', vi.fn((id) => clearTimeout(id)));
  vi.stubGlobal('navigator', { ...navigator, vibrate: vi.fn(() => true) });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('VoiceFullscreen mute', () => {
  it('stops microphone capture at the node, not just in the UI', () => {
    const node = makeNode();
    const shell = makeShell({ node });
    const { getByTestId } = render(
      <VoiceFullscreen open={true} onClose={vi.fn()} shell={shell} />,
    );
    fireEvent.click(getByTestId('mute-button'));
    expect(node.setMicMuted).toHaveBeenCalledWith(true);
    fireEvent.click(getByTestId('mute-button'));
    expect(node.setMicMuted).toHaveBeenLastCalledWith(false);
  });

  it('never shows "Listening" while muted', () => {
    const shell = makeShell();
    const { getByTestId } = render(
      <VoiceFullscreen
        open={true}
        onClose={vi.fn()}
        shell={shell}
        variant="docked"
      />,
    );
    pushFrame('voice_vad', { speaking: true });
    fireEvent.click(getByTestId('mute-button'));
    expect(getByTestId('voice-fullscreen').textContent).not.toMatch(/Listening/i);
    expect(getByTestId('voice-fullscreen').textContent).toMatch(/Muted/i);
  });

  it('reconciles the button from the brain voice_status frame', () => {
    // The brain is the authority: a mute applied from another surface,
    // or one that survived a reconnect, has to show up here.
    const shell = makeShell();
    const { getByTestId } = render(
      <VoiceFullscreen open={true} onClose={vi.fn()} shell={shell} />,
    );
    expect(getByTestId('mute-button').getAttribute('aria-pressed')).toBe('false');
    pushFrame('voice_status', { state: 'available', muted: true });
    expect(getByTestId('mute-button').getAttribute('aria-pressed')).toBe('true');
  });

  it('a degraded voice_status that reports muted keeps the mic muted', () => {
    const shell = makeShell();
    const { getByTestId } = render(
      <VoiceFullscreen open={true} onClose={vi.fn()} shell={shell} />,
    );
    fireEvent.click(getByTestId('mute-button'));
    pushFrame('voice_status', {
      state: 'degraded',
      reason: 'openai_realtime_quota',
      muted: true,
    });
    expect(getByTestId('mute-button').getAttribute('aria-pressed')).toBe('true');
  });

  it('a voice_status frame without a muted field does not unmute', () => {
    // Older brains omit the field entirely. Absence is not "unmuted",
    // and treating it as such would drop the mute silently.
    const shell = makeShell();
    const { getByTestId } = render(
      <VoiceFullscreen open={true} onClose={vi.fn()} shell={shell} />,
    );
    fireEvent.click(getByTestId('mute-button'));
    pushFrame('voice_status', { state: 'available' });
    expect(getByTestId('mute-button').getAttribute('aria-pressed')).toBe('true');
  });

  it('adopts the node mute state when reopening a still-muted session', () => {
    const node = makeNode();
    node._micMuted = true;
    const shell = makeShell({ node });
    const { getByTestId } = render(
      <VoiceFullscreen open={true} onClose={vi.fn()} shell={shell} />,
    );
    expect(getByTestId('mute-button').getAttribute('aria-pressed')).toBe('true');
  });

  it('still sends the voice_mute envelope', () => {
    const shell = makeShell();
    const { getByTestId } = render(
      <VoiceFullscreen open={true} onClose={vi.fn()} shell={shell} />,
    );
    fireEvent.click(getByTestId('mute-button'));
    expect(shell.sendFrame).toHaveBeenCalledWith('voice_mute', { muted: true });
  });
});
