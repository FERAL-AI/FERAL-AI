/*
 * FeralNode — the public class every TypeScript/Node.js hardware daemon is built on.
 * Mirrors the Python `FeralNode` surface: auto-reconnect with jittered backoff,
 * automatic heartbeat loop, outbound schema validation (HUP v1, see ../../HUP_SPEC.md),
 * optional mDNS discovery and 6-digit pairing helpers.
 */

import WebSocket from "ws";
import { Capability, CapabilityName } from "./capability";
import { discoverBrain as _discoverBrain } from "./discovery";
import { loadKey as _loadKey, pair as _pair, PairOptions } from "./pairing";
import {
  buildFrame,
  DeviceEventPayload,
  HUP_VERSION,
  HUPActionRequestPayload,
  HUPActionResponsePayload,
  NodeAckPayload,
  NodeByePayload,
  NodeHeartbeatPayload,
  NodeRegisterPayload,
  NodeType,
} from "./schemas";

export type ActionHandler = (
  params: Record<string, unknown>,
) => Promise<Record<string, unknown> | void> | Record<string, unknown> | void;

export interface FeralNodeOptions {
  nodeId: string;
  name?: string;
  firmwareVersion?: string;
  brainUrl?: string;
  apiKey?: string;
  capabilities?: Array<CapabilityName | string>;
  nodeType?: NodeType;
  manufacturer?: string;
  model?: string;
  platform?: string;
  sensors?: string[];
  actuators?: string[];
  location?: string;
  tags?: string[];
  heartbeatMs?: number;
}

const PASSIVE_SENSOR_CAPS = new Set<string>([
  Capability.HEART_RATE,
  Capability.SPO2,
  Capability.TEMPERATURE,
  Capability.UV,
  Capability.ACCELEROMETER,
  Capability.GYROSCOPE,
  Capability.AMBIENT_LIGHT,
  Capability.STEPS,
  Capability.BATTERY,
  Capability.GPS,
  Capability.MICROPHONE,
  Capability.CAMERA,
]);

const ACTUATOR_CAPS = new Set<string>([
  Capability.DISPLAY,
  Capability.SPEAKER,
  Capability.HAPTIC,
  Capability.BUZZER,
  Capability.LED,
  Capability.MOTOR,
  Capability.RELAY,
  Capability.VALVE,
]);

export class FeralNode {
  readonly nodeId: string;
  readonly name: string;
  readonly firmwareVersion: string;
  readonly nodeType: NodeType;
  readonly capabilities: string[];
  readonly sensors: string[];
  readonly actuators: string[];

  private brainUrl: string | undefined;
  private apiKeyOverride: string | undefined;
  private manufacturer: string;
  private model: string;
  private platform: string;
  private location: string;
  private tags: string[];
  private heartbeatMs: number;

  private ws: WebSocket | null = null;
  private handlers = new Map<string, ActionHandler>();
  private stopping = false;
  // `null` until the brain's node_ack lands, which is NOT the same as an
  // empty set. An empty set is a brain saying "you may do nothing"; null
  // is "nobody has told me yet". The dispatch gate has to tell those
  // apart, which is exactly what the old
  // `granted_capabilities.length ? granted : capabilities` fallback
  // could not do.
  private granted: Set<string> | null = null;
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private sessionToken: string | null = null;
  private connectedResolver: (() => void) | null = null;
  private connectedPromise: Promise<void>;

  constructor(opts: FeralNodeOptions) {
    this.nodeId = opts.nodeId;
    this.name = opts.name ?? opts.nodeId;
    this.firmwareVersion = opts.firmwareVersion ?? "0.0.0";
    this.brainUrl = opts.brainUrl;
    this.apiKeyOverride = opts.apiKey;
    this.nodeType = (opts.nodeType ?? "sensor") as NodeType;
    this.manufacturer = opts.manufacturer ?? "";
    this.model = opts.model ?? "";
    this.platform = opts.platform ?? "";
    this.location = opts.location ?? "";
    this.tags = opts.tags ?? [];
    this.heartbeatMs = opts.heartbeatMs ?? 10_000;

    this.capabilities = (opts.capabilities ?? []).map(String);
    this.sensors = opts.sensors ?? this.capabilities.filter((c) => PASSIVE_SENSOR_CAPS.has(c));
    this.actuators = opts.actuators ?? this.capabilities.filter((c) => ACTUATOR_CAPS.has(c));

    this.connectedPromise = new Promise<void>((res) => {
      this.connectedResolver = res;
    });
  }

  onAction(name: string, handler: ActionHandler): this {
    this.handlers.set(name, handler);
    return this;
  }

  async emitEvent(
    eventType: string,
    data: Record<string, unknown>,
  ): Promise<void> {
    const payload: DeviceEventPayload = {
      node_id: this.nodeId,
      event_type: eventType,
      data,
      ts: Date.now() / 1000,
    };
    await this.send("device_event", payload);
  }

  static async discoverBrain(timeoutMs = 3000): Promise<string | null> {
    return _discoverBrain(timeoutMs);
  }

  static async pair(opts: PairOptions): Promise<string> {
    return _pair(opts);
  }

  async run(mainLoop?: () => Promise<void>): Promise<void> {
    const supervisor = this.wsSupervisor();
    if (!mainLoop) {
      await supervisor;
      return;
    }
    const main = (async () => {
      await this.connectedPromise;
      await mainLoop();
    })();
    await Promise.race([supervisor, main]);
    this.stopping = true;
  }

  async close(reason = "shutdown"): Promise<void> {
    this.stopping = true;
    try {
      await this.send("node_bye", { reason, restart_in_s: 0 } as NodeByePayload);
    } catch {
      /* ignore */
    }
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
  }

  private async resolveApiKey(): Promise<string | null> {
    if (this.apiKeyOverride) return this.apiKeyOverride;
    return _loadKey(this.nodeId);
  }

  private async wsSupervisor(): Promise<void> {
    let backoff = 100;
    while (!this.stopping) {
      const url = this.brainUrl ?? (await _discoverBrain(3000));
      if (!url) {
        await this.sleep(backoff);
        backoff = Math.min(backoff * 2, 30_000);
        continue;
      }

      const apiKey = await this.resolveApiKey();
      const headers: Record<string, string> = apiKey
        ? { Authorization: `Bearer ${apiKey}` }
        : {};

      try {
        await this.runOnce(url, headers);
        backoff = 100;
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn(`[feral-node-sdk] ws error: ${(err as Error).message}`);
      } finally {
        this.ws = null;
        if (this.heartbeatTimer) {
          clearInterval(this.heartbeatTimer);
          this.heartbeatTimer = null;
        }
      }

      if (this.stopping) return;
      const jitter = backoff * (0.5 + Math.random());
      await this.sleep(Math.min(jitter, 30_000));
      backoff = Math.min(backoff * 2, 30_000);
    }
  }

  private runOnce(
    url: string,
    headers: Record<string, string>,
  ): Promise<void> {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(url, { headers, maxPayload: 2 * 1024 * 1024 });
      this.ws = ws;

      ws.on("open", () => {
        this.handshake().catch(reject);
      });

      ws.on("message", (raw) => {
        this.handleFrame(raw.toString()).catch((err) => {
          // eslint-disable-next-line no-console
          console.warn("[feral-node-sdk] frame handler error:", err);
        });
      });

      ws.on("error", (err) => reject(err));
      ws.on("close", () => resolve());
    });
  }

  private async handshake(): Promise<void> {
    const payload: NodeRegisterPayload = {
      node_id: this.nodeId,
      node_type: this.nodeType,
      name: this.name,
      manufacturer: this.manufacturer,
      model: this.model,
      firmware_version: this.firmwareVersion,
      platform: this.platform,
      os: "",
      capabilities: this.capabilities,
      sensors: this.sensors,
      actuators: this.actuators,
      location: this.location,
      tags: this.tags,
    };
    await this.send("node_register", payload);
  }

  private async handleFrame(raw: string): Promise<void> {
    let frame: { type?: string; payload?: Record<string, unknown> };
    try {
      frame = JSON.parse(raw);
    } catch {
      return;
    }
    const type = frame.type ?? "";
    const payload = (frame.payload ?? {}) as Record<string, unknown>;

    switch (type) {
      case "node_ack": {
        const ack = payload as unknown as NodeAckPayload;
        this.sessionToken = ack.session_token ?? null;
        // Presence of the key, not truthiness of its length. A brain
        // that omits it entirely is pre-HUP-1.4 and has no grant store,
        // and only that case falls back to this node's own declaration.
        this.granted = new Set(
          ack.granted_capabilities !== undefined
            ? ack.granted_capabilities
            : this.capabilities,
        );
        this.heartbeatMs = Math.max(1000, Number(ack.heartbeat_ms) || this.heartbeatMs);
        this.startHeartbeat();
        this.connectedResolver?.();
        return;
      }
      case "hup_action_request": {
        const req = payload as unknown as HUPActionRequestPayload;
        await this.dispatchAction(req);
        return;
      }
      default:
        return;
    }
  }

  private async dispatchAction(req: HUPActionRequestPayload): Promise<void> {
    // HUP_SPEC.md section 6, nodes: "MUST refuse any hup_action_request
    // whose name is not in their registered capabilities, replying with
    // success=false, error='capability_denied'". `this.granted` was
    // assigned from node_ack and then never read, so the grant the brain
    // sent had no effect on this side at all.
    if (this.granted !== null && !this.granted.has(req.name)) {
      await this.sendActionResponse(req.action_id, {
        success: false,
        error: `capability_denied: ${req.name}`,
      });
      return;
    }
    const handler = this.handlers.get(req.name);
    if (!handler) {
      await this.sendActionResponse(req.action_id, {
        success: false,
        error: `capability_denied: ${req.name}`,
      });
      return;
    }
    const started = Date.now();
    try {
      const result = await handler(req.params ?? {});
      await this.sendActionResponse(req.action_id, {
        success: true,
        result: (result && typeof result === "object" ? (result as Record<string, unknown>) : { ok: true }),
        duration_ms: Date.now() - started,
      });
    } catch (err) {
      await this.sendActionResponse(req.action_id, {
        success: false,
        error: (err as Error).message,
        duration_ms: Date.now() - started,
      });
    }
  }

  private async sendActionResponse(
    actionId: string,
    extras: Partial<HUPActionResponsePayload> & { success: boolean },
  ): Promise<void> {
    const payload: HUPActionResponsePayload = {
      action_id: actionId,
      success: extras.success,
      result: extras.result ?? {},
      error: extras.error ?? null,
      duration_ms: extras.duration_ms ?? 0,
    };
    await this.send("hup_action_response", payload);
  }

  private startHeartbeat(): void {
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = setInterval(() => {
      const payload: NodeHeartbeatPayload = { ts: Date.now() / 1000 };
      this.send("node_heartbeat", payload).catch(() => undefined);
    }, this.heartbeatMs);
  }

  private async send(type: string, payload: Record<string, unknown>): Promise<void> {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const frame = buildFrame(type, payload);
    await new Promise<void>((resolve, reject) => {
      this.ws!.send(JSON.stringify(frame), (err) => (err ? reject(err) : resolve()));
    });
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((r) => setTimeout(r, ms));
  }
}

export { HUP_VERSION, Capability };
export type { CapabilityName, PairOptions };
