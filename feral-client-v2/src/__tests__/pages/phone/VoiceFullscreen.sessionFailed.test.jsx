/**
 * The phone orb left "listening" for a session that never opened.
 *
 * `VoiceFullscreen` sends `voice_session_start` and flips itself to
 * `listening` immediately, on the assumption that silence means the
 * session opened. The brain used to hold up that assumption by
 * accident: when the voice backend refused to open it recorded the
 * start as allowed and sent nothing at all, because the only refusal
 * it noticed was an exception and `VoiceRouter.open_session` reports
 * every other failure by returning None.
 *
 * The brain now answers a refused start with the HUP error frame
 * (code 1099, name `voice_session_failed`). This surface listened for
 * `voice_error`, a frame type that does not exist in HUP, so it would
 * have ignored the real one and kept the orb on "listening" for the
 * life of the connection.
 *
 * Scoped to the name: the brain sends `type: "error"` for schema and
 * capability refusals too, and an oversized frame is not a reason to
 * tear down a live voice session.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, act } from '@testing-library/react';
import { VoiceFullscreen } from '../../../pages/phone/VoiceFullscreen';
import { __resetAudioContextForTests } from '../../../lib/audioContext';

let frameListeners = [];

function makeShell() {
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

describe('VoiceFullscreen and a refused voice_session_start', () => {
  it('leaves the listening state when the brain refuses the session', () => {
    const shell = makeShell();
    const { getByTestId } = render(
      <VoiceFullscreen open={true} onClose={vi.fn()} shell={shell} variant="docked" />,
    );
    pushFrame('voice_vad', { speaking: true });
    expect(getByTestId('voice-fullscreen').textContent).toMatch(/Listening/i);

    pushFrame('error', {
      code: 1099,
      name: 'voice_session_failed',
      message: (
        'voice_session_start failed for stream voice-abc '
        + '(mode=openai_realtime): the openai_realtime backend did not '
        + 'open a session'
      ),
      recoverable: false,
    });

    const text = getByTestId('voice-fullscreen').textContent;
    expect(text).not.toMatch(/Listening/i);
    expect(text).toMatch(/did not\s+open a session|failed/i);
  });

  it('ignores an unrelated error frame', () => {
    const shell = makeShell();
    const { getByTestId } = render(
      <VoiceFullscreen open={true} onClose={vi.fn()} shell={shell} variant="docked" />,
    );
    pushFrame('voice_vad', { speaking: true });
    pushFrame('error', {
      code: 4020,
      name: 'frame_too_large',
      message: 'video_frame.data_b64 over cap',
    });
    expect(getByTestId('voice-fullscreen').textContent).toMatch(/Listening/i);
  });
});
