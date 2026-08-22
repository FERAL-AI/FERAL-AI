import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// feral-client-v2 is served at / by default (v2 is the only UI), with the
// /v2/ alias retained for back-compat.
//
// The base must be absolute. It was './' so that relative "./assets/..."
// refs would work at both mount points, and they do, but a relative ref
// resolves against the *current URL's directory*, not the mount point, so
// any route one level deep asked for its assets from the wrong place. The
// SPA fallback then answered with index.html at status 200 and the browser
// executed HTML as JavaScript. Measured against a running brain: a hard
// load of /memory/context requested /memory/assets/index-<hash>.js and
// rendered a completely blank page, 0 body characters, 0 #root children.
// /apps/publish, /pair/:id and /setup/legacy did the same. In-app
// navigation was fine, so only a refresh or a bookmark hit it.
//
// '/' resolves identically from every route AND from every mount point,
// so it fixes the deep routes without costing the /v2/ alias anything.
// The Tauri desktop app is unaffected: it ships its own build from
// desktop/dist and only syncs tokens.css from here.
export default defineConfig({
  base: '/',
  plugins: [react(), tailwindcss()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
