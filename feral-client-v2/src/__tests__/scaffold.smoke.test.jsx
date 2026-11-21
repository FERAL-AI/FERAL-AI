import { describe, it, expect } from 'vitest';
import { renderV2 } from './_helpers/renderV2';
import App from '../App';

describe('v2 scaffold', () => {
  it('renders the Chat marker on /', () => {
    const { getByTestId } = renderV2(<App />, { route: '/' });
    expect(getByTestId('v2-marker')).toBeInTheDocument();
  });

  it('renders the Chat page under /chat', () => {
    const { getByTestId } = renderV2(<App />, { route: '/chat' });
    expect(getByTestId('v2-marker')).toBeInTheDocument();
  });

  it('renders Forge, Devices, Canvas, Glass Brain, Timeline, Flows, Intents, Marketplace, Settings', () => {
    for (const route of ['/forge', '/devices', '/canvas', '/glass-brain', '/timeline', '/flows', '/intents', '/marketplace', '/settings']) {
      const { getAllByTestId, unmount } = renderV2(<App />, { route });
      expect(getAllByTestId('v2-marker').length).toBeGreaterThan(0);
      unmount();
    }
  });
});
