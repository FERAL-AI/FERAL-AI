/** HTTP and tracked-turn client. Processing completion is not effect verification. */
import type { DashboardData, SystemInfo, ChatTurnReceipt, TurnProcessingOutcome } from './types';

export interface FeralClientOptions {
  bearerToken?: string;
  timeoutMs?: number;
  chatTimeoutMs?: number;
  sessionId?: string;
}
export interface ChatOptions {
  sessionId?: string;
  timeoutMs?: number;
  signal?: AbortSignal;
}
export interface SkillInvocationOptions { sessionId?: string; confirm?: boolean; }

export class FeralHTTPError extends Error {
  constructor(readonly status: number) {
    super(`FERAL HTTP request failed with status ${status}`);
    this.name = 'FeralHTTPError';
  }
}
export class ChatTurnError extends Error {
  constructor(readonly code: string, readonly receipt?: ChatTurnReceipt) {
    super(`FERAL chat ${code}; reconcile external action outcomes before retrying`);
    this.name = 'ChatTurnError';
  }
}
export class ChatTurnTimeout extends ChatTurnError {
  constructor() { super('timeout'); this.name = 'ChatTurnTimeout'; }
}
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function positiveTimeout(value: number): number {
  if (!Number.isFinite(value) || value <= 0) throw new Error('Timeout must be finite and positive');
  return value;
}
function sessionIdentity(value: string): string {
  if (typeof value !== 'string' || !value.trim() || value !== value.trim()
      || value.length > 1024 || /[\u0000-\u001f\u007f]/.test(value)) {
    throw new Error('sessionId must be nonempty trimmed text without control characters, at most 1024 characters');
  }
  return value;
}
function canonicalUUID(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
}
function processingOutcome(value: unknown): value is TurnProcessingOutcome {
  return typeof value === 'string' && ['completed', 'awaiting_approval', 'failed', 'cancelled',
    'outcome_unknown', 'unavailable', 'refused', 'budget_exceeded'].includes(value);
}

export class FeralClient {
  private readonly baseUrl: string;
  private readonly wsUrl: string;
  private readonly bearerToken?: string;
  private readonly timeoutMs: number;
  private readonly chatTimeoutMs: number;
  private readonly sessionId: string;
  private readonly sockets = new Set<WebSocket>();
  private readonly sessions = new Set<string>();

  constructor(baseUrl = 'http://localhost:9090', options: FeralClientOptions = {}) {
    let url: URL;
    try { url = new URL(baseUrl); } catch { throw new Error('baseUrl must be a valid HTTP(S) URL'); }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
      throw new Error('baseUrl must be HTTP(S) without URL credentials, query or fragment');
    }
    if (options.bearerToken !== undefined && (typeof options.bearerToken !== 'string'
        || !options.bearerToken.trim() || /[\r\n]/.test(options.bearerToken))) {
      throw new Error('bearerToken must be a nonempty single-line credential');
    }
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.wsUrl = this.baseUrl.replace(/^http:/, 'ws:').replace(/^https:/, 'wss:') + '/v1/session';
    this.bearerToken = options.bearerToken;
    this.timeoutMs = positiveTimeout(options.timeoutMs ?? 30000);
    this.chatTimeoutMs = positiveTimeout(options.chatTimeoutMs ?? 60000);
    this.sessionId = options.sessionId === undefined ? crypto.randomUUID() : sessionIdentity(options.sessionId);
  }

  private async request(method: string, path: string, body?: Record<string, unknown>): Promise<unknown> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const headers: Record<string, string> = { Accept: 'application/json' };
      if (this.bearerToken !== undefined) headers.Authorization = `Bearer ${this.bearerToken}`;
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      const response = await fetch(this.baseUrl + path, {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal, redirect: 'manual',
      });
      if (!response.ok) throw new FeralHTTPError(response.status);
      try { return await response.json(); }
      catch { throw new Error('FERAL returned invalid JSON'); }
    } catch (error) {
      if (error instanceof FeralHTTPError) throw error;
      if (controller.signal.aborted) throw new Error('FERAL HTTP deadline expired; outcome may be unknown');
      if (error instanceof Error && error.message === 'FERAL returned invalid JSON') throw error;
      throw new Error('FERAL HTTP transport failed; outcome may be unknown');
    } finally { clearTimeout(timeout); }
  }
  private async object(method: string, path: string, body?: Record<string, unknown>): Promise<Record<string, unknown>> {
    const data = await this.request(method, path, body);
    if (!record(data)) throw new Error('FERAL returned a JSON value where an object was required');
    return data;
  }
  private rows(value: unknown): Array<Record<string, unknown>> {
    if (!Array.isArray(value) || !value.every(record)) throw new Error('FERAL returned an invalid record list');
    return value;
  }
  async health(): Promise<Record<string, unknown>> { return this.object('GET', '/health'); }
  async getDashboard(): Promise<DashboardData> {
    return await this.object('GET', '/api/dashboard') as unknown as DashboardData;
  }
  async getSystemInfo(): Promise<SystemInfo> {
    return await this.object('GET', '/api/system/info') as unknown as SystemInfo;
  }
  async listSkills(): Promise<Array<Record<string, unknown>>> {
    const data = await this.request('GET', '/skills');
    return this.rows(record(data) ? data.skills : data);
  }
  async searchMemory(query: string, limit = 10): Promise<Array<Record<string, unknown>>> {
    const data = await this.object('GET', `/api/memory/search?${new URLSearchParams({ q: query, limit: String(limit) })}`);
    return this.rows(data.results);
  }
  async listConversations(limit = 20): Promise<Array<Record<string, unknown>>> {
    const data = await this.object('GET', `/api/conversations?${new URLSearchParams({ limit: String(limit) })}`);
    return this.rows(data.conversations);
  }
  async invokeSkill(skillId: string, endpoint: string, args: Record<string, unknown> = {},
    options: SkillInvocationOptions = {}): Promise<Record<string, unknown>> {
    const confirm = options.confirm ?? false;
    if (typeof confirm !== 'boolean' || !record(args)) throw new Error('confirm must be Boolean and args an object');
    const body: Record<string, unknown> = { skill_id: skillId, endpoint, args, confirm };
    if (options.sessionId !== undefined) body.session_id = sessionIdentity(options.sessionId);
    return this.object('POST', '/api/tools/execute', body);
  }
  async createNote(content: string, tags: string[] = [], options: SkillInvocationOptions = {}): Promise<Record<string, unknown>> {
    return this.invokeSkill('notes_memory', 'save_note', { content, tags }, options);
  }

  async chat(message: string, options: ChatOptions = {}): Promise<string> {
    const receipt = await this.chatTurn(message, options);
    if (receipt.processing_outcome !== 'completed') throw new ChatTurnError(receipt.processing_outcome, receipt);
    if (!receipt.final_text) throw new ChatTurnError('missing_response', receipt);
    return receipt.final_text;
  }
  async chatTurn(message: string, options: ChatOptions = {}): Promise<ChatTurnReceipt> {
    if (typeof message !== 'string' || !message.trim()) throw new Error('message must be nonempty text');
    const sid = options.sessionId === undefined ? this.sessionId : sessionIdentity(options.sessionId);
    const duration = positiveTimeout(options.timeoutMs ?? this.chatTimeoutMs);
    if (this.sessions.has(sid)) throw new ChatTurnError('session_busy');
    if (options.signal?.aborted) throw new ChatTurnError('transport_cancelled');
    this.sessions.add(sid);
    const requestId = crypto.randomUUID();
    const capabilityId = crypto.randomUUID();
    const url = new URL(this.wsUrl);
    url.searchParams.set('session_id', sid);
    return new Promise((resolve, reject) => {
      let ws: WebSocket | undefined;
      let turnId: string | undefined;
      let negotiated = false;
      let settled = false;
      const finish = (error?: Error, receipt?: ChatTurnReceipt) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        options.signal?.removeEventListener('abort', abort);
        this.sessions.delete(sid);
        if (ws) { this.sockets.delete(ws); try { ws.close(); } catch { /* Already closed. */ } }
        if (error) reject(error);
        else if (receipt) resolve(receipt);
      };
      const abort = () => finish(new ChatTurnError('transport_cancelled'));
      const timer = setTimeout(() => finish(new ChatTurnTimeout()), duration);
      options.signal?.addEventListener('abort', abort, { once: true });
      try { ws = new WebSocket(url.toString()); }
      catch { finish(new ChatTurnError('connection_failed')); return; }
      this.sockets.add(ws);
      ws.onopen = () => {
        try {
          if (this.bearerToken !== undefined) ws!.send(JSON.stringify({ type: 'auth', token: this.bearerToken }));
          ws!.send(JSON.stringify({ type: 'req', id: capabilityId, method: 'chat.capabilities', params: {} }));
        } catch { finish(new ChatTurnError('connection_failed')); }
      };
      ws.onmessage = (event) => {
        if (settled) return;
        let frame: unknown;
        try { frame = JSON.parse(event.data as string); }
        catch { finish(new ChatTurnError('invalid_json')); return; }
        if (!record(frame) || typeof frame.type !== 'string') { finish(new ChatTurnError('invalid_frame')); return; }
        if (frame.type === 'res' && frame.id === capabilityId && !negotiated) {
          const capability = frame.payload;
          if (frame.ok !== true || !record(capability) || !Array.isArray(capability.turn_contract_versions)
              || !capability.turn_contract_versions.includes(1) || capability.durable_receipts !== true
              || capability.whole_turn_terminal !== true || capability.session_id !== sid) {
            finish(new ChatTurnError('unsupported_turn_contract')); return;
          }
          negotiated = true;
          try {
            ws!.send(JSON.stringify({ msg_id: requestId, type: 'text_command', session_id: sid,
              payload: { text: message, turn_contract_version: 1 } }));
          } catch { finish(new ChatTurnError('connection_failed')); }
          return;
        }
        const payload = frame.payload ?? {};
        if (!record(payload)) { finish(new ChatTurnError('invalid_payload')); return; }
        if (frame.type === 'error') {
          if (payload.request_id === requestId) {
            const known = ['chat_turn_request_conflict', 'chat_turn_invalid_request', 'chat_turn_quota', 'chat_turn_receipt_unavailable'];
            const code = typeof payload.code === 'string' && known.includes(payload.code) ? payload.code : 'request_rejected';
            finish(new ChatTurnError(code));
          }
          return;
        }
        if (!['chat_turn_accepted', 'chat_turn_terminal'].includes(frame.type) || payload.request_id !== requestId) return;
        if (!negotiated) { finish(new ChatTurnError('unnegotiated_turn_receipt')); return; }
        if (frame.session_id !== sid || payload.session_id !== sid || payload.contract_version !== 1
            || payload.durable !== true || !canonicalUUID(payload.turn_id) || typeof payload.replayed !== 'boolean') {
          finish(new ChatTurnError('invalid_turn_receipt')); return;
        }
        if (frame.type === 'chat_turn_accepted') {
          if (payload.status !== 'accepted' || (turnId !== undefined && turnId !== payload.turn_id)) {
            finish(new ChatTurnError('invalid_turn_acceptance')); return;
          }
          turnId = payload.turn_id;
          return;
        }
        if (turnId === undefined || payload.turn_id !== turnId) { finish(new ChatTurnError('uncorrelated_terminal')); return; }
        if (!processingOutcome(payload.processing_outcome)) { finish(new ChatTurnError('invalid_turn_outcome')); return; }
        if (typeof payload.final_text !== 'string' || (payload.action_outcome !== 'not_asserted' && payload.action_outcome !== 'unknown')
            || !Array.isArray(payload.approval_request_ids)
            || !payload.approval_request_ids.every((id): id is string => typeof id === 'string' && !!id)) {
          finish(new ChatTurnError('invalid_turn_receipt')); return;
        }
        finish(undefined, { request_id: requestId, turn_id: turnId, session_id: sid,
          processing_outcome: payload.processing_outcome, final_text: payload.final_text,
          action_outcome: payload.action_outcome, approval_request_ids: payload.approval_request_ids,
          replayed: payload.replayed, durable: true, contract_version: 1 });
      };
      ws.onerror = () => finish(new ChatTurnError('connection_failed'));
      ws.onclose = (event) => finish(new ChatTurnError(event.code === 4001 ? 'unauthorized' : 'connection_closed'));
    });
  }
  /** Close owned transports. External effect cancellation is never asserted. */
  close(): void { for (const ws of Array.from(this.sockets)) ws.close(); }
}
