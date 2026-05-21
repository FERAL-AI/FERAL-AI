import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import {
  useGlobalErrors,
  pushGlobalError,
  clearGlobalError,
  clearAllGlobalErrors,
  _resetGlobalErrorsForTesting,
} from '../../hooks/useGlobalErrors';
import { ApiError } from '../../lib/api';
import { FeralSocket, wireSocketGlobalErrors } from '../../lib/feralSocket';

describe('useGlobalErrors', () => {
  beforeEach(() => {
    _resetGlobalErrorsForTesting();
  });

  it('push, clear, and clearAll', () => {
    const { result } = renderHook(() => useGlobalErrors());

    act(() => {
      result.current.push(new ApiError({ detail: 'one', path: '/a' }));
      result.current.push(new ApiError({ detail: 'two', path: '/b' }));
    });
    expect(result.current.errors).toHaveLength(2);

    const firstId = result.current.errors[0].id;
    act(() => result.current.clear(firstId));
    expect(result.current.errors).toHaveLength(1);

    act(() => result.current.clearAll());
    expect(result.current.errors).toHaveLength(0);
  });

  it('wireSocketGlobalErrors pushes type:error frames', () => {
    const socket = new FeralSocket('ws://invalid.test');
    wireSocketGlobalErrors(socket);
    const { result } = renderHook(() => useGlobalErrors());
    act(() => {
      socket.listeners.forEach((fn) => fn({ type: 'error', data: { error: 'brain rejected' } }));
    });
    expect(result.current.errors[0].message).toContain('brain rejected');
  });

  it('module pushGlobalError is reflected in hook', () => {
    const { result } = renderHook(() => useGlobalErrors());
    act(() => pushGlobalError(new ApiError({ detail: 'ws', path: 'websocket' })));
    expect(result.current.errors[0].message).toBe('ws');
    act(() => clearAllGlobalErrors());
    expect(result.current.errors).toHaveLength(0);
  });
});
