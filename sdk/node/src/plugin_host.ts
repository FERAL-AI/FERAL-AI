/** Explicit local HTTP adapter for the existing generic SkillExecutor transport. */
import {createServer, type IncomingMessage, type ServerResponse} from 'node:http';
import {randomBytes, timingSafeEqual} from 'node:crypto';
import type {DefinedPlugin} from './plugin';
export interface PluginHostOptions {
  host?: '127.0.0.1' | '::1';
  port?: number;
  bearerToken?: string;
  maxBodyBytes?: number;
  maxResponseBytes?: number;
  maxConcurrent?: number;
  handlerTimeoutMs?: number;
}
export interface PluginHost {
  readonly baseUrl: string;
  /** Service credential, not a FERAL action approval. Never put it in a manifest/log. */
  readonly bearerToken: string;
  readonly manifest: Record<string, unknown>;
  close(): Promise<void>;
}
function bound(value: number | undefined, fallback: number, min: number, max: number): number {
  const n = value ?? fallback;
  if (!Number.isSafeInteger(n) || n < min || n > max) throw new Error('Invalid plugin host bound');
  return n;
}
function send(res: ServerResponse, status: number, body: string): void {
  if (res.destroyed || res.writableEnded) return;
  res.writeHead(status, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
  res.end(body);
}
export async function startPluginHost(plugin: DefinedPlugin, options: PluginHostOptions = {}): Promise<PluginHost> {
  const host = options.host ?? '127.0.0.1';
  if (host !== '127.0.0.1' && host !== '::1') throw new Error('Plugin hosts bind literal loopback only');
  const port = bound(options.port, 0, 0, 65535);
  const bodyLimit = bound(options.maxBodyBytes, 65536, 1, 1048576);
  const responseLimit = bound(options.maxResponseBytes, 65536, 1, 1048576);
  const concurrency = bound(options.maxConcurrent, 4, 1, 64);
  const timeout = bound(options.handlerTimeoutMs, 30000, 10, 120000);
  const token = options.bearerToken ?? randomBytes(32).toString('base64url');
  if (typeof token !== 'string' || !/^[\x21-\x7e]{32,512}$/.test(token)) throw new Error('Invalid plugin host credential');
  const expected = Buffer.from(`Bearer ${token}`);
  const routes = new Set(plugin.tools.map(t => `/feral/${plugin.name}/${t.name}`));
  let active = 0;
  let closing = false;
  const handle = async (req: IncomingMessage, res: ServerResponse) => {
    const authorization = Buffer.from(req.headers.authorization || '');
    if (authorization.length !== expected.length || !timingSafeEqual(authorization, expected)) {
      send(res, 401, '{"error_code":"unauthorized"}'); req.resume(); return;
    }
    if (closing) { send(res, 503, '{"error_code":"closing"}'); req.resume(); return; }
    if (req.method !== 'POST') { send(res, 405, '{"error_code":"method_not_allowed"}'); req.resume(); return; }
    if (!routes.has(req.url || '')) { send(res, 404, '{"error_code":"unknown_endpoint"}'); req.resume(); return; }
    if (req.headers['content-type']?.split(';')[0].trim().toLowerCase() !== 'application/json') {
      send(res, 415, '{"error_code":"json_required"}'); req.resume(); return;
    }
    if (active >= concurrency) { send(res, 503, '{"error_code":"busy"}'); req.resume(); return; }
    active++;
    let handlerStarted = false;
    try {
      const chunks: Buffer[] = [];
      let size = 0;
      for await (const chunk of req) {
        const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        size += bytes.length;
        if (size > bodyLimit) { send(res, 413, '{"error_code":"body_too_large"}'); req.resume(); return; }
        chunks.push(bytes);
      }
      let args: unknown;
      try { args = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(Buffer.concat(chunks))); }
      catch { send(res, 400, '{"error_code":"invalid_json"}'); return; }
      if (!args || typeof args !== 'object' || Array.isArray(args)) {
        send(res, 400, '{"error_code":"object_required"}'); return;
      }
      handlerStarted = true;
      // Capacity stays occupied until the actual handler settles, including after timeout.
      // Closing a socket is not cancellation or rollback of handler effects.
      const work = Promise.resolve().then(() => plugin.execute(req.url!.split('/').at(-1)!, args as Record<string, unknown>));
      void work.then(() => { active--; }, () => { active--; });
      let timer: ReturnType<typeof setTimeout> | undefined;
      try {
        const result = await Promise.race([work, new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new Error('timeout')), timeout);
        })]);
        const encoded = JSON.stringify(result, (_key, value) => {
          if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('Nonfinite result');
          if (['function', 'symbol', 'bigint'].includes(typeof value)) throw new Error('Invalid JSON result');
          return value;
        });
        if (encoded === undefined || Buffer.byteLength(encoded) > responseLimit) {
          send(res, 502, '{"error_code":"invalid_or_oversized_result","outcome":"unknown"}'); return;
        }
        send(res, 200, encoded);
      } catch {
        send(res, 500, '{"error_code":"handler_failed_or_timed_out","outcome":"unknown"}');
      } finally { if (timer) clearTimeout(timer); }
    } catch {
      send(res, 400, '{"error_code":"request_incomplete"}');
    } finally { if (!handlerStarted) active--; }
  };
  const server = createServer((req, res) => { void handle(req, res); });
  server.requestTimeout = 10000;
  server.headersTimeout = 10000;
  server.timeout = 10000;
  try {
    await new Promise<void>((resolve, reject) => {
      server.once('error', reject);
      server.listen(port, host, () => { server.removeListener('error', reject); resolve(); });
    });
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('Plugin host address unavailable');
    const baseUrl = `http://${host === '::1' ? '[::1]' : host}:${address.port}`;
    const manifest = plugin.toManifest({baseUrl});
    let closePromise: Promise<void> | undefined;
    return {
      baseUrl, bearerToken: token, manifest,
      close() {
        if (!closePromise) {
          closing = true;
          closePromise = new Promise<void>((resolve, reject) => {
            const timer = setTimeout(() => server.closeAllConnections(), 2000);
            server.close(err => { clearTimeout(timer); if (err) reject(err); else resolve(); });
            server.closeIdleConnections();
          });
        }
        return closePromise;
      },
    };
  } catch (error) {
    server.closeAllConnections(); server.close(); throw error;
  }
}
