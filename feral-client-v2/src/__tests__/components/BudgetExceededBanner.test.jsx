// Lane 12 Wave 3 — S6 budget banner rendering contract.
//
// Backs the chat surface's `budget_exceeded` WS frame handler with a
// pinned render test so a future refactor can't accidentally drop the
// banner's reset-time, cap dollars, or deeplink to Settings.

import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi } from 'vitest';
import BudgetExceededBanner from '../../components/BudgetExceededBanner';

function renderBanner(props) {
  return render(
    <MemoryRouter>
      <BudgetExceededBanner {...props} />
    </MemoryRouter>,
  );
}

describe('BudgetExceededBanner (S6)', () => {
  it('renders cap, current spend, reset time + Settings deeplink', () => {
    renderBanner({
      callSite: 'chat',
      capDollars: 0.1,
      currentDollars: 0.12,
      resetAt: Math.floor(Date.now() / 1000) + 600,
    });
    expect(screen.getByTestId('budget-exceeded-banner')).toBeInTheDocument();
    expect(screen.getByText(/Chat budget reached/i)).toBeInTheDocument();
    expect(screen.getByText(/\$0\.12 \/ \$0\.10/)).toBeInTheDocument();
    expect(screen.getByText(/Resets at/i)).toBeInTheDocument();
    const link = screen.getByRole('link', { name: /Adjust caps in Settings/i });
    expect(link).toHaveAttribute('href', expect.stringContaining('/settings?section=Cost'));
    expect(link).toHaveAttribute('href', expect.stringContaining('call_site=chat'));
  });

  it('renders without current spend (cap-only mode)', () => {
    renderBanner({ callSite: 'vision', capDollars: 0.05, resetAt: '14:30' });
    expect(screen.getByText(/Vision budget reached/i)).toBeInTheDocument();
    expect(screen.getByText(/\$0\.05 cap/)).toBeInTheDocument();
    expect(screen.getByText('14:30')).toBeInTheDocument();
  });

  it('dismiss button invokes onDismiss', async () => {
    const onDismiss = vi.fn();
    renderBanner({ callSite: 'chat', capDollars: 0.1, currentDollars: 0.1, resetAt: 0, onDismiss });
    const btn = screen.getByRole('button', { name: /Dismiss budget banner/i });
    btn.click();
    expect(onDismiss).toHaveBeenCalledOnce();
  });
});
