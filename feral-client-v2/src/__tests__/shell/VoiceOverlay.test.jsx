/**
 * v2026.5.30 — desktop voice overlay docking contract.
 *
 * Pre-fix the overlay was `position:fixed; inset:0` and the shell
 * dimmed everything behind it, so starting voice from the menubar
 * locked the entire WebUI. This suite locks in:
 *   - Default variant when voice opens is `docked` (no inset:0, no
 *     aria-modal, end of session reaches the chat composer).
 *   - Expand flips variant to fullscreen, Minimize flips it back.
 *   - End voice resets to docked for the next session.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/react';
import VoiceOverlay from '../../shell/VoiceOverlay';

// Mock the `useVoice` hook so each test can swap in its own snapshot
// without rebuilding a full VoiceProvider context tree.
vi.mock('../../shell/VoiceContext', async (importOriginal) => {
  const mod = await importOriginal();
  return {
    ...mod,
    useVoice: () => globalThis.__mockVoice,
  };
});

function setVoice(overrides = {}) {
  globalThis.__mockVoice = {
    active: true,
    state: 'active',
    provider: 'openai',
    transcript: '',
    setProvider: () => {},
    start: vi.fn(),
    stop: vi.fn(),
    toggle: vi.fn(),
    ...overrides,
  };
  return globalThis.__mockVoice;
}

beforeEach(() => {
  cleanup();
  setVoice();
});

describe('VoiceOverlay', () => {
  it('opens in docked variant by default when voice goes active', () => {
    setVoice({ active: true });
    const { container } = render(<VoiceOverlay />);
    const overlay = container.querySelector('.v2-voice-overlay');
    expect(overlay).toBeTruthy();
    expect(overlay.getAttribute('data-variant')).toBe('docked');
    // Docked must NOT be aria-modal: screen readers should treat the
    // rest of the page as normal content.
    expect(overlay.getAttribute('aria-modal')).toBeNull();
    expect(overlay.getAttribute('role')).toBe('region');
  });

  it('Expand button flips docked → fullscreen', () => {
    setVoice({ active: true });
    const { container, getByLabelText } = render(<VoiceOverlay />);
    fireEvent.click(getByLabelText('Expand voice'));
    const overlay = container.querySelector('.v2-voice-overlay');
    expect(overlay.getAttribute('data-variant')).toBe('fullscreen');
    expect(overlay.getAttribute('aria-modal')).toBe('true');
    expect(overlay.getAttribute('role')).toBe('dialog');
  });

  it('Minimize button flips fullscreen → docked', () => {
    setVoice({ active: true });
    const { container, getByLabelText } = render(<VoiceOverlay />);
    fireEvent.click(getByLabelText('Expand voice'));
    fireEvent.click(getByLabelText('Minimize voice'));
    expect(container.querySelector('.v2-voice-overlay').getAttribute('data-variant'))
      .toBe('docked');
  });

  it('End voice button calls voice.stop()', () => {
    const voice = setVoice({ active: true });
    const { getByText } = render(<VoiceOverlay />);
    fireEvent.click(getByText('End voice'));
    expect(voice.stop).toHaveBeenCalled();
  });

  it('renders provider label from voice.provider', () => {
    setVoice({ active: true, provider: 'gemini' });
    const { getByText } = render(<VoiceOverlay />);
    expect(getByText('Gemini Live')).toBeInTheDocument();
  });

  it('hides itself when voice is inactive', () => {
    setVoice({ active: false });
    const { container } = render(<VoiceOverlay />);
    const overlay = container.querySelector('.v2-voice-overlay');
    expect(overlay.classList.contains('is-visible')).toBe(false);
    expect(overlay.getAttribute('aria-hidden')).toBe('true');
  });

  // Audit-r11 — Bug 3 (silent voice). When the brain emits
  // `voice_status state=degraded reason=openai_realtime_quota` the
  // overlay must render a banner explaining the failure so users
  // don't blame the app for going silent.
  it('renders voice_status banner when brain reports degraded state', () => {
    setVoice({
      active: true,
      voiceStatus: {
        state: 'degraded',
        reason: 'openai_realtime_quota',
        provider: 'openai',
        fallbackProvider: 'whisper',
        detail: '',
      },
    });
    const { getByText } = render(<VoiceOverlay />);
    expect(getByText(/Voice degraded, using fallback TTS/)).toBeInTheDocument();
    expect(getByText(/out of credit/i)).toBeInTheDocument();
  });

  it('renders Voice unavailable banner on state=unavailable', () => {
    setVoice({
      active: true,
      voiceStatus: {
        state: 'unavailable',
        reason: 'fallback_tts_failed',
        provider: 'openai',
        fallbackProvider: '',
        detail: '',
      },
    });
    const { getByText } = render(<VoiceOverlay />);
    expect(getByText(/Voice unavailable/i)).toBeInTheDocument();
  });

  it('does not render banner when voiceStatus is null', () => {
    setVoice({ active: true, voiceStatus: null });
    const { container } = render(<VoiceOverlay />);
    expect(container.querySelector('.v2-voice-status-banner')).toBeNull();
  });
});
