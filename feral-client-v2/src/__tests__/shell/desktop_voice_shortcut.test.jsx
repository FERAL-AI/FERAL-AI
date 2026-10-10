import React from 'react';
import { act, cleanup, render } from '@testing-library/react';
import { forwardVoiceActivation, brainFrameOrigin, registerVoiceActivation } from '../../../../desktop/src/voiceShortcut';
import { installDesktopVoiceShortcut } from '../../lib/desktopVoiceShortcut';

const mocks = vi.hoisted(() => ({ starts: vi.fn(), stops: vi.fn(), socket: {
  ws: { readyState: 1 }, subscribe: () => () => {},
} }));
vi.mock('../../lib/voiceRealtime', () => ({
  RealtimeVoiceEngine: class {
    start = mocks.starts;
    stop = mocks.stops;
  },
}));
vi.mock('../../hooks/useFeralSocket', () => ({ useFeralSocket: () => mocks.socket }));
vi.mock('../../lib/api', () => ({ apiJson: vi.fn().mockResolvedValue({ features: { voice_provider: 'openai' } }) }));
import { VoiceProvider, useVoice } from '../../shell/VoiceContext';

const originalParent = window.parent;
let voice;
function Probe() { voice = useVoice(); return null; }
afterEach(() => {
  cleanup();
  Object.defineProperty(window, 'parent', { configurable: true, value: originalParent });
  mocks.starts.mockReset();
  mocks.stops.mockReset();
  mocks.socket.ws = { readyState: 1 };
});

function framedWindow(origin = 'tauri://localhost') {
  const parent = {};
  Object.defineProperty(window, 'parent', { configurable: true, value: parent });
  return {
    parent,
    deliver(data, source = parent, senderOrigin = origin) {
      window.dispatchEvent(new MessageEvent('message', { data, source, origin: senderOrigin }));
    },
  };
}

describe('desktop voice shortcut crosses the frame boundary', () => {
  it('forwards the actual shell command to the exact brain origin and starts the real voice hook', async () => {
    const shell = framedWindow();
    render(<VoiceProvider><Probe /></VoiceProvider>);
    const postMessage = vi.fn((data) => shell.deliver(data));
    const frame = { src: 'http://127.0.0.1:8000/chat', isConnected: true, contentWindow: { postMessage } };
    let activate;
    const unlisten = vi.fn();
    const listen = vi.fn((eventName, handler) => { activate = handler; return Promise.resolve(unlisten); });
    let currentFrame = null;
    expect(await registerVoiceActivation(listen, () => currentFrame)).toBe(unlisten);
    expect(listen).toHaveBeenCalledWith('voice-activation', expect.any(Function));
    expect(activate()).toBe(false);
    currentFrame = frame;
    await act(async () => { expect(activate()).toBe(true); });
    expect(postMessage).toHaveBeenCalledWith({ type: 'feral-desktop-voice-activation', v: 1 }, 'http://127.0.0.1:8000');
    expect(mocks.starts).toHaveBeenCalledTimes(1);
    expect(voice.active).toBe(true);
    await act(async () => { activate(); });
    expect(mocks.starts).toHaveBeenCalledTimes(1);
  });

  it('uses the normal unavailable state without attempting capture when disconnected', async () => {
    const shell = framedWindow();
    mocks.socket.ws = null;
    render(<VoiceProvider><Probe /></VoiceProvider>);
    await act(async () => shell.deliver({ type: 'feral-desktop-voice-activation', v: 1 }));
    expect(mocks.starts).not.toHaveBeenCalled();
    expect(voice.state).toBe('degraded');
  });

  it('rejects forged origins, sibling sources and unrecognized commands', () => {
    const shell = framedWindow();
    const activate = vi.fn();
    const dispose = installDesktopVoiceShortcut(activate);
    const command = { type: 'feral-desktop-voice-activation', v: 1 };
    shell.deliver(command, shell.parent, 'https://attacker.example');
    shell.deliver(command, {}, 'tauri://localhost');
    shell.deliver({ ...command, v: 2 });
    shell.deliver({ ...command, execute: 'anything' });
    shell.deliver({ type: 'other', v: 1 });
    shell.deliver(null);
    expect(activate).not.toHaveBeenCalled();
    shell.deliver(command);
    expect(activate).toHaveBeenCalledTimes(1);
    dispose();
    shell.deliver(command);
    expect(activate).toHaveBeenCalledTimes(1);
  });

  it.each(['tauri://localhost', 'http://tauri.localhost', 'https://tauri.localhost'])('accepts native shell origin %s', (origin) => {
    const shell = framedWindow(origin);
    const activate = vi.fn();
    const dispose = installDesktopVoiceShortcut(activate);
    shell.deliver({ type: 'feral-desktop-voice-activation', v: 1 });
    expect(activate).toHaveBeenCalledTimes(1);
    dispose();
  });

  it('does not install a desktop command surface in a top-level browser', () => {
    Object.defineProperty(window, 'parent', { configurable: true, value: window });
    const activate = vi.fn();
    const dispose = installDesktopVoiceShortcut(activate);
    window.dispatchEvent(new MessageEvent('message', {
      source: window, origin: 'tauri://localhost', data: { type: 'feral-desktop-voice-activation', v: 1 },
    }));
    expect(activate).not.toHaveBeenCalled();
    dispose();
  });

  it('does not post to a removed frame, missing frame or arbitrary external URL', () => {
    const postMessage = vi.fn();
    expect(forwardVoiceActivation(null)).toBe(false);
    expect(forwardVoiceActivation({ isConnected: false, src: 'http://localhost:8000', contentWindow: { postMessage } })).toBe(false);
    expect(forwardVoiceActivation({ isConnected: true, src: 'https://attacker.example', contentWindow: { postMessage } })).toBe(false);
    expect(brainFrameOrigin({ src: 'javascript:alert(1)' })).toBeNull();
    expect(postMessage).not.toHaveBeenCalled();
  });
});
