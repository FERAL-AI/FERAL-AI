import { describe, expect, it } from 'vitest';
import { resolveBrainEndpoints } from '../../lib/config';

describe('resolveBrainEndpoints', () => {
  it('uses the browser origin when served through default HTTPS', () => {
    const endpoints = resolveBrainEndpoints({
      location: {
        origin: 'https://feral-admin.example',
        hostname: 'feral-admin.example',
        port: '',
        protocol: 'https:',
      },
    });

    expect(endpoints).toEqual({
      API_BASE: 'https://feral-admin.example',
      WS_BASE: 'wss://feral-admin.example',
      WS_URL: 'wss://feral-admin.example/v1/session',
    });
  });

  it('preserves an explicit browser port', () => {
    const endpoints = resolveBrainEndpoints({
      location: {
        origin: 'http://localhost:9090',
        hostname: 'localhost',
        port: '9090',
        protocol: 'http:',
      },
    });

    expect(endpoints.API_BASE).toBe('http://localhost:9090');
    expect(endpoints.WS_URL).toBe('ws://localhost:9090/v1/session');
  });

  it('preserves an explicit base URL', () => {
    const endpoints = resolveBrainEndpoints({
      baseUrl: 'https://brain.example:9443/',
      location: {
        origin: 'https://feral-admin.example',
        hostname: 'feral-admin.example',
        port: '',
        protocol: 'https:',
      },
    });

    expect(endpoints.API_BASE).toBe('https://brain.example:9443');
    expect(endpoints.WS_URL).toBe('wss://brain.example:9443/v1/session');
  });

  it('preserves explicit host and port overrides', () => {
    const endpoints = resolveBrainEndpoints({
      host: 'brain.internal',
      port: '9443',
      location: {
        origin: 'https://feral-admin.example',
        hostname: 'feral-admin.example',
        port: '',
        protocol: 'https:',
      },
    });

    expect(endpoints.API_BASE).toBe('https://brain.internal:9443');
    expect(endpoints.WS_URL).toBe('wss://brain.internal:9443/v1/session');
  });

  it('keeps the legacy 9090 default when only the host is overridden', () => {
    const endpoints = resolveBrainEndpoints({
      host: 'brain.internal',
      location: {
        origin: 'https://feral-admin.example',
        hostname: 'feral-admin.example',
        port: '',
        protocol: 'https:',
      },
    });

    expect(endpoints.API_BASE).toBe('https://brain.internal:9090');
    expect(endpoints.WS_URL).toBe('wss://brain.internal:9090/v1/session');
  });

  it('keeps the development fallback for a non-HTTP browser origin', () => {
    const endpoints = resolveBrainEndpoints({
      location: {
        origin: 'null',
        hostname: '',
        port: '',
        protocol: 'file:',
      },
    });

    expect(endpoints.API_BASE).toBe('http://localhost:9090');
    expect(endpoints.WS_URL).toBe('ws://localhost:9090/v1/session');
  });

  it('keeps the non-browser development fallback', () => {
    const endpoints = resolveBrainEndpoints();

    expect(endpoints.API_BASE).toBe('http://localhost:9090');
    expect(endpoints.WS_URL).toBe('ws://localhost:9090/v1/session');
  });
});
