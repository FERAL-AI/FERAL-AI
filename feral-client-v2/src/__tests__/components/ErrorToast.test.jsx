import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import ErrorToast from '../../components/ErrorToast';
import { pushGlobalError, _resetGlobalErrorsForTesting } from '../../hooks/useGlobalErrors';
import { ApiError } from '../../lib/api';

describe('ErrorToast', () => {
  beforeEach(() => {
    _resetGlobalErrorsForTesting();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders nothing when store is empty', () => {
    const { container } = render(<ErrorToast />);
    expect(container.firstChild).toBeNull();
  });

  it('renders alert with message and dismisses on click', () => {
    pushGlobalError(new ApiError({ detail: "Theme '' not found", path: '/api/genui/themes/activate' }));
    render(<ErrorToast />);
    const stack = screen.getByTestId('error-toast-stack');
    expect(stack).toHaveAttribute('role', 'alert');
    // Was 'polite', which contradicted role="alert" (implicitly
    // assertive). See a11y/roles.test.jsx for the reasoning.
    expect(stack).toHaveAttribute('aria-live', 'assertive');
    expect(screen.getByText("Theme '' not found")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Dismiss error'));
    expect(screen.queryByTestId('error-toast-stack')).not.toBeInTheDocument();
  });

  it('auto-dismisses after 6 seconds', () => {
    pushGlobalError(new ApiError({ detail: 'temporary', path: '/x' }));
    render(<ErrorToast />);
    expect(screen.getByText('temporary')).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(6000); });
    expect(screen.queryByText('temporary')).not.toBeInTheDocument();
  });
});
