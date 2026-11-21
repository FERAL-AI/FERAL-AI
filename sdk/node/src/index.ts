/**
 * @feral/sdk — Build plugins, tools, and device adapters for FERAL.
 *
 * @example
 * ```ts
 * import { FeralClient, definePlugin } from '@feral/sdk';
 *
 * const client = new FeralClient('http://localhost:9090');
 * const response = await client.chat('Hello!');
 * ```
 */

export { FeralClient, ChatTurnError, ChatTurnTimeout, FeralHTTPError,
  type FeralClientOptions, type ChatOptions, type SkillInvocationOptions } from './client';
export { definePlugin, type PluginDefinition, type ToolDefinition, type ToolParameter, type DefinedPlugin } from './plugin';
export { startPluginHost, type PluginHost, type PluginHostOptions } from './plugin_host';
export { FeralNode, type NodeConfig } from './node';
export type {
  FeralMessage,
  TextCommand,
  TextResponse,
  StreamDelta,
  SkillManifest,
  SkillEndpoint,
  HUPAction,
  HUPTelemetry,
  DashboardData,
  SystemInfo,
  ChatTurnReceipt,
  TurnProcessingOutcome,
} from './types';
