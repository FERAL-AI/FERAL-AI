import { createContext, useContext } from 'react';

/**
 * One piece of palette state, shared by every control that opens it.
 *
 * The Hub popup's open/closed flag used to live inside Dock.jsx, which
 * is why the Menubar carried a comment promising a command palette
 * "later" and shipped nothing: there was no way to reach the state from
 * outside the Dock. Anything that wanted a second trigger would have had
 * to keep its own boolean, and two booleans for one dialog is a dialog
 * that gets stuck open.
 *
 * `useCommandPalette()` is safe outside the provider: it returns an
 * inert snapshot so a component rendered standalone in a test does not
 * throw.
 */
const PaletteContext = createContext(null);

const INERT = {
  open: false,
  openPalette: () => {},
  closePalette: () => {},
  togglePalette: () => {},
};

export const PaletteProvider = PaletteContext.Provider;

export function useCommandPalette() {
  return useContext(PaletteContext) || INERT;
}
