// Only the brain frame receives native voice activation. DOM events on the
// shell window do not cross the iframe boundary.
export function brainFrameOrigin(frame) {
  try {
    const url = new URL(frame.src);
    if (!['http:', 'https:'].includes(url.protocol)) return null;
    if (!['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) return null;
    return url.origin;
  } catch {
    return null;
  }
}

export function forwardVoiceActivation(frame) {
  if (!frame?.isConnected || !frame.contentWindow) return false;
  const origin = brainFrameOrigin(frame);
  if (!origin) return false;
  frame.contentWindow.postMessage({ type: 'feral-desktop-voice-activation', v: 1 }, origin);
  return true;
}

export function registerVoiceActivation(listen, getFrame) {
  return listen('voice-activation', () => forwardVoiceActivation(getFrame()));
}
