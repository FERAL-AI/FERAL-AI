/**
 * AppSurface — sandbox + CSP regression tests.
 *
 * Roadmap §3.3 #2 demands the surface render inside an iframe with:
 * - sandbox="allow-scripts" and explicitly NOT "allow-same-origin"
 * - referrerpolicy="no-referrer"
 * - a <meta http-equiv="Content-Security-Policy"> in the srcdoc
 * whose connect-src/img-src/script-src directives are derived from
 * manifest.permissions.network.
 *
 * The spec text mentioned `feral-core/tests/test_genui_csp.py` but
 * AppSurface itself is a React component, so this lives as a vitest in
 * feral-client-v2/.
 */

import { describe, it, expect } from 'vitest';
import { Routes, Route } from 'react-router-dom';
import { renderV2 } from '../_helpers/renderV2';
import AppSurface from '../../pages/AppSurface';

function TestRoutes() {
  return (
    <Routes>
      <Route path="/apps/:app_id" element={<AppSurface />} />
    </Routes>
  );
}

function makeFixture({ network }) {
  const manifest = {
    app_id: 'demo-app-csp',
    version: '1.0.0',
    brand: { name: 'Demo CSP' },
    entry_surface_id: 'home',
    permissions: { network },
    surfaces: [{ surface_id: 'home', title: 'Home' }],
  };
  const surfaceOpen = {
    success: true,
    app_id: 'demo-app-csp',
    surface_id: 'home',
    screen_id: 'demo-app-csp:home:v2-user',
    root: { type: 'VStack', children: [{ type: 'Text', value: 'csp-fixture' }] },
  };
  return { manifest, surfaceOpen };
}

function renderWithFixture({ network }) {
  const { manifest, surfaceOpen } = makeFixture({ network });
  return renderV2(<TestRoutes />, {
    route: '/apps/demo-app-csp',
    fetch: (url) => {
      if (url.includes('/api/apps/demo-app-csp/manifest')) {
        return { app_id: 'demo-app-csp', manifest };
      }
      if (url.includes('/api/apps/demo-app-csp/open')) {
        return surfaceOpen;
      }
      return {};
    },
  });
}

function decodeHtmlEntities(s) {
  return s
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>');
}

function extractCspContent(srcDoc) {
  const match = srcDoc.match(
    /<meta[^>]*http-equiv=["']Content-Security-Policy["'][^>]*content=["']([^"']+)["'][^>]*>/i,
  );
  return match ? decodeHtmlEntities(match[1]) : null;
}

describe('AppSurface sandbox + CSP', () => {
  it('renders inside an iframe with allow-scripts but NOT allow-same-origin', async () => {
    const { findByTestId } = renderWithFixture({ network: [] });
    const iframe = await findByTestId('v2-appsurface-iframe');
    const sandbox = iframe.getAttribute('sandbox') || '';
    expect(sandbox).toContain('allow-scripts');
    expect(sandbox).not.toContain('allow-same-origin');
    expect(iframe.getAttribute('referrerpolicy')).toBe('no-referrer');
  });

  it('emits a CSP meta tag with default-deny + connect-src none when no network is granted', async () => {
    const { findByTestId } = renderWithFixture({ network: [] });
    const iframe = await findByTestId('v2-appsurface-iframe');
    const csp = extractCspContent(iframe.getAttribute('srcdoc') || '');
    expect(csp).not.toBeNull();
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("connect-src 'none'");
    expect(csp).toContain("script-src 'unsafe-inline'");
    expect(csp).toContain("frame-ancestors 'self'");
    expect(csp).toContain("base-uri 'none'");
    expect(csp).toContain("form-action 'none'");
 // No wildcard outbound network ever.
    expect(csp).not.toMatch(/connect-src[^;]*\*/);
  });

  it('derives connect-src directives from manifest.permissions.network', async () => {
    const { findByTestId } = renderWithFixture({
 network: ['https://api.example.com', 'https://cdn.example.com'],
    });
    const iframe = await findByTestId('v2-appsurface-iframe');
    const csp = extractCspContent(iframe.getAttribute('srcdoc') || '');
    expect(csp).not.toBeNull();
 expect(csp).toContain('connect-src https://api.example.com https://cdn.example.com');
    expect(csp).not.toMatch(/connect-src[^;]*\*/);
  });
});
