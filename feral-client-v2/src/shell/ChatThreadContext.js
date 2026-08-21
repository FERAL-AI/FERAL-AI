import { createContext, useContext } from 'react';

/**
 * The chat thread context, extracted out of Shell.jsx.
 *
 * It lives in its own module for one reason: CommandPalette needs to
 * read the thread (to start a new conversation, and to hand an Ask
 * query to the composer) and Shell renders CommandPalette. Importing
 * `useChatThread` from Shell.jsx would make that an import cycle.
 * Shell.jsx re-exports `useChatThread` so the ~dozen pages that import
 * it from there keep working unchanged.
 */
export const ChatThreadContext = createContext(null);

export function useChatThread() {
  return useContext(ChatThreadContext);
}
