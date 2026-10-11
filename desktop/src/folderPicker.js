import { brainFrameOrigin } from './voiceShortcut.js';

// Only the local brain frame may open the native folder chooser. Choosing a
// path is separate from granting the coding agent access to it.
export function registerFolderPicker(invoke, getFrame, target = window) {
  const handler = async (event) => {
    const frame = getFrame();
    const origin = frame && brainFrameOrigin(frame);
    const data = event.data;
    if (!origin || event.origin !== origin || event.source !== frame.contentWindow) return;
    if (data?.type !== 'feral:pick-working-directory' || typeof data.requestId !== 'string' || data.requestId.length > 128) return;
    let path = null;
    let error = null;
    try { path = await invoke('pick_working_directory'); }
    catch { error = 'The folder chooser could not open. You can enter a folder path instead.'; }
    if (getFrame() === frame && frame.isConnected) {
      frame.contentWindow.postMessage({ type: 'feral:working-directory-picked', requestId: data.requestId, path, error }, origin);
    }
  };
  target.addEventListener('message', handler);
  return () => target.removeEventListener('message', handler);
}
