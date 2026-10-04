import { DESKTOP_SHELL_ORIGINS } from './desktopVoiceShortcut';

// A referrer describes navigation, not the current native parent's origin.
// Address only known desktop shells: mismatching target origins are dropped by
// the browser. No arbitrary embedding website receives this request.
export function requestDesktopFolder(requestId, targetWindow = window) {
  if (targetWindow.parent === targetWindow) return false;
  let sent = false;
  for (const origin of DESKTOP_SHELL_ORIGINS) {
    try {
      targetWindow.parent.postMessage({ type: 'feral:pick-working-directory', requestId }, origin);
      sent = true;
    } catch { /* A platform may not support every native origin spelling. */ }
  }
  return sent;
}

export function installDesktopFolderReceiver(getRequestId, onResult, targetWindow = window) {
  if (targetWindow.parent === targetWindow) return () => {};
  const receive = event => {
    if (event.source !== targetWindow.parent || !DESKTOP_SHELL_ORIGINS.has(event.origin)) return;
    const data = event.data;
    if (!data || typeof data !== 'object' || Array.isArray(data)
        || Object.keys(data).length !== 4 || data.type !== 'feral:working-directory-picked'
        || !getRequestId() || data.requestId !== getRequestId()
        || (data.path !== null && typeof data.path !== 'string')
        || (data.error !== null && typeof data.error !== 'string')) return;
    onResult(data);
  };
  targetWindow.addEventListener('message', receive);
  return () => targetWindow.removeEventListener('message', receive);
}
