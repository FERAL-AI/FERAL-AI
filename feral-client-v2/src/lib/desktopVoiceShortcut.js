export const DESKTOP_SHELL_ORIGINS = new Set([
  'tauri://localhost',
  'http://tauri.localhost',
  'https://tauri.localhost',
]);
if (import.meta.env.DEV) {
  DESKTOP_SHELL_ORIGINS.add('http://localhost:1420');
  DESKTOP_SHELL_ORIGINS.add('http://127.0.0.1:1420');
}

// This deliberately accepts one command, from the immediate desktop parent.
// Marketplace surfaces, sibling frames and arbitrary websites cannot start
// capture. Media permission and the ordinary voice policy still apply.
export function installDesktopVoiceShortcut(onActivate, targetWindow = window) {
  if (targetWindow.parent === targetWindow) return () => {};
  const handler = (event) => {
    if (event.source !== targetWindow.parent || !DESKTOP_SHELL_ORIGINS.has(event.origin)) return;
    const data = event.data;
    if (!data || typeof data !== 'object' || Array.isArray(data)) return;
    if (Object.keys(data).length !== 2 || data.type !== 'feral-desktop-voice-activation' || data.v !== 1) return;
    onActivate();
  };
  targetWindow.addEventListener('message', handler);
  return () => targetWindow.removeEventListener('message', handler);
}
