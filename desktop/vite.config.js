import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

const __dirname = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
  },
  envPrefix: ['VITE_', 'TAURI_'],
  build: {
    rollupOptions: {
      // Two entries, not one. tauri.conf.json declares a second window
      // (label "floating", url "floating-window.html") that a global
      // shortcut and the tray menu both toggle, but Vite's default single
      // index.html entry never emitted it, so dist/ held only index.html
      // and the shortcut opened a 404. The window was shipped, reachable
      // by two paths, and had never once loaded.
      input: {
        main: resolve(__dirname, 'index.html'),
        floating: resolve(__dirname, 'floating-window.html'),
      },
    },
  },
});
