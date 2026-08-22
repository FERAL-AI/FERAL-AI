/*
 * Zod mirrors of the HUP v1 wire schemas from ../../HUP_SPEC.md §5.
 * These validate outbound frames before send so daemons are conformant by construction.
 */

import { z } from "zod";

export const HUP_VERSION = "1.3.0";

// Per-frame decoded-size caps from HUP_SPEC.md §5.4.1 / §5.4.2.
export const AUDIO_FRAME_MAX_BYTES = 64 * 1024;
export const VIDEO_FRAME_MAX_BYTES = 512 * 1024;

function decodedBase64Size(b64: string): number {
  // Cheap tight upper bound: bytes = (len * 3) / 4 - padding count.
  const len = b64.length;
  const padding = (b64.endsWith("==") ? 2 : b64.endsWith("=") ? 1 : 0);
  return Math.floor((len * 3) / 4) - padding;
}

export const NodeType = z.enum([
  "desktop",
  "server",
  "rpi",
  "robot",
  "glasses",
  "phone",
  "actuator",
  "sensor",
  "wearable",
  "camera",
  "vehicle",
  "appliance",
]);
export type NodeType = z.infer<typeof NodeType>;

export const NodeRegisterPayload = z.object({
  node_id: z.string().regex(/^[A-Za-z0-9._:-]{1,128}$/),
  node_type: NodeType.default("sensor"),
  name: z.string().default(""),
  manufacturer: z.string().default(""),
  model: z.string().default(""),
  firmware_version: z.string().default(""),
  platform: z.string().default(""),
  os: z.string().default(""),
  capabilities: z.array(z.string()).default([]),
  sensors: z.array(z.string()).default([]),
  actuators: z.array(z.string()).default([]),
  location: z.string().default(""),
  tags: z.array(z.string()).default([]),
});
export type NodeRegisterPayload = z.infer<typeof NodeRegisterPayload>;

export const NodeAckPayload = z.object({
  node_id: z.string(),
  session_token: z.string(),
  primary_session_id: z.string().default(""),
  heartbeat_ms: z.number().int().default(10_000),
  server_time: z.number().default(() => Date.now() / 1000),
  granted_capabilities: z.array(z.string()).default([]),
  denied_capabilities: z.array(z.string()).default([]),
});
export type NodeAckPayload = z.infer<typeof NodeAckPayload>;

export const TextResponsePayload = z.object({
  text: z.string(),
  session_id: z.string().default(""),
  reply_mode: z.string().default(""),
  channel: z.string().default(""),
  source: z.string().default(""),
  trigger_id: z.string().default(""),
  priority: z.string().default(""),
  title: z.string().default(""),
  voice_text: z.string().default(""),
  context: z.record(z.any()).default({}),
});
export type TextResponsePayload = z.infer<typeof TextResponsePayload>;

export const NodeHeartbeatPayload = z.object({
  ts: z.number().default(() => Date.now() / 1000),
  battery_pct: z.number().int().min(0).max(100).nullable().optional(),
  rssi: z.number().int().nullable().optional(),
});
export type NodeHeartbeatPayload = z.infer<typeof NodeHeartbeatPayload>;

export const DeviceEventPayload = z.object({
  node_id: z.string(),
  event_type: z.string(),
  data: z.record(z.any()).default({}),
  ts: z.number().default(() => Date.now() / 1000),
});
export type DeviceEventPayload = z.infer<typeof DeviceEventPayload>;

// HUP v1.1 audio_frame payload — per HUP_SPEC.md §5.4.1.
export const AudioFramePayload = z.object({
  event_type: z.literal("audio_frame").default("audio_frame"),
  codec: z.enum(["opus", "pcm16"]),
  sample_rate: z.number().int().min(8000).max(96000),
  channels: z.number().int().min(1).max(2),
  frame_ms: z.number().int().min(1).max(120).default(20),
  sequence: z.number().int().min(0),
  data_b64: z.string().refine(
    (v) => decodedBase64Size(v) <= AUDIO_FRAME_MAX_BYTES,
    { message: `audio_frame data_b64 exceeds ${AUDIO_FRAME_MAX_BYTES} bytes decoded` },
  ),
});
export type AudioFramePayload = z.infer<typeof AudioFramePayload>;

// HUP v1.1 video_frame payload — per HUP_SPEC.md §5.4.2.
export const VideoFramePayload = z.object({
  event_type: z.literal("video_frame").default("video_frame"),
  codec: z.enum(["jpeg", "h264"]),
  width: z.number().int().min(1).max(8192),
  height: z.number().int().min(1).max(8192),
  sequence: z.number().int().min(0),
  keyframe: z.boolean().default(true),
  data_b64: z.string().refine(
    (v) => decodedBase64Size(v) <= VIDEO_FRAME_MAX_BYTES,
    { message: `video_frame data_b64 exceeds ${VIDEO_FRAME_MAX_BYTES} bytes decoded` },
  ),
});
export type VideoFramePayload = z.infer<typeof VideoFramePayload>;

// HUP v1.3.0 glasses_frame payload — per HUP_SPEC.md §5.4.3.
export const GlassesFramePayload = z.object({
  device_id: z.string().min(1).max(128),
  timestamp: z.number().default(() => Date.now() / 1000),
  encoding: z.enum(["jpeg", "png", "webp"]).default("jpeg"),
  data_b64: z.string().refine(
    (v) => decodedBase64Size(v) <= VIDEO_FRAME_MAX_BYTES,
    { message: `glasses_frame data_b64 exceeds ${VIDEO_FRAME_MAX_BYTES} bytes decoded` },
  ),
  width: z.number().int().min(1).max(8192).nullable().optional(),
  height: z.number().int().min(1).max(8192).nullable().optional(),
  source: z.string().default("glasses"),
  sequence: z.number().int().min(0).nullable().optional(),
});
export type GlassesFramePayload = z.infer<typeof GlassesFramePayload>;

// HUP v1.3.0 device_announce payload — per HUP_SPEC.md §5.4.4.
export const DeviceAnnouncePayload = z.object({
  scanner_node_id: z.string().default(""),
  device_id: z.string().min(1).max(128),
  device_kind: z.enum([
    "bluetooth_le",
    "bluetooth_classic",
    "mdns",
    "usb",
    "airplay",
    "homekit",
    "unknown",
  ]).default("unknown"),
  name: z.string().default(""),
  manufacturer: z.string().default(""),
  rssi_dbm: z.number().int().min(-127).max(20).nullable().optional(),
  advertised_services: z.array(z.string()).default([]),
  first_seen: z.number().nullable().optional(),
  last_seen: z.number().nullable().optional(),
  metadata: z.record(z.any()).default({}),
});
export type DeviceAnnouncePayload = z.infer<typeof DeviceAnnouncePayload>;

export const HUPActionRequestPayload = z.object({
  action_id: z.string().min(1).max(64),
  name: z.string().min(1).max(64),
  params: z.record(z.any()).default({}),
  timeout_ms: z.number().int().min(1).max(120_000).default(5000),
  requires_confirmation: z.boolean().default(false),
});
export type HUPActionRequestPayload = z.infer<typeof HUPActionRequestPayload>;

export const HUPActionResponsePayload = z.object({
  action_id: z.string(),
  success: z.boolean(),
  result: z.record(z.any()).default({}),
  error: z.string().nullable().optional(),
  duration_ms: z.number().int().min(0).default(0),
});
export type HUPActionResponsePayload = z.infer<typeof HUPActionResponsePayload>;

export const NodeByePayload = z.object({
  reason: z.string().default("shutdown"),
  restart_in_s: z.number().int().default(0),
});
export type NodeByePayload = z.infer<typeof NodeByePayload>;

export const ErrorPayload = z.object({
  code: z.number().int(),
  name: z.string(),
  message: z.string(),
  recoverable: z.boolean().default(true),
  ref_action_id: z.string().nullable().optional(),
});
export type ErrorPayload = z.infer<typeof ErrorPayload>;

export const HUPMessageType = z.enum([
  "node_register",
  "node_ack",
  "node_heartbeat",
  "device_event",
  "hup_action_request",
  "hup_action_response",
  "node_bye",
  "text_response",
  "error",
  "glasses_frame",
  "device_announce",
]);
export type HUPMessageType = z.infer<typeof HUPMessageType>;

const SCHEMAS: Record<string, z.ZodTypeAny> = {
  node_register: NodeRegisterPayload,
  node_ack: NodeAckPayload,
  node_heartbeat: NodeHeartbeatPayload,
  device_event: DeviceEventPayload,
  hup_action_request: HUPActionRequestPayload,
  hup_action_response: HUPActionResponsePayload,
  node_bye: NodeByePayload,
  text_response: TextResponsePayload,
  error: ErrorPayload,
  glasses_frame: GlassesFramePayload,
  device_announce: DeviceAnnouncePayload,
};

export interface HUPFrame {
  hup_version: string;
  type: string;
  ts: number;
  payload: Record<string, unknown>;
}

export function buildFrame(
  type: string,
  payload: Record<string, unknown>,
): HUPFrame {
  const schema = SCHEMAS[type];
  if (!schema) {
    throw new Error(`unknown HUP message type: ${type}`);
  }
  const parsed = schema.parse(payload) as Record<string, unknown>;
  return {
    hup_version: HUP_VERSION,
    type,
    ts: Date.now() / 1000,
    payload: parsed,
  };
}
