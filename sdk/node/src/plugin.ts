/** Declare tools independently of transport; bind a manifest to an explicit host. */
export interface ToolParameter {
  type: 'string' | 'integer' | 'number' | 'boolean' | 'array' | 'object';
  description: string;
  required?: boolean;
  default?: unknown;
}
export interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, ToolParameter>;
  handler: (args: Record<string, unknown>) => Promise<unknown>;
}
export interface PluginDefinition {
  name: string;
  version?: string;
  description?: string;
  tools: ToolDefinition[];
}
export interface DefinedPlugin extends PluginDefinition {
  toManifest: (transport: {baseUrl: string}) => Record<string, unknown>;
  execute: (endpoint: string, args: Record<string, unknown>) => Promise<unknown>;
}
const identifier = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
function validateIdentifier(value: string): void {
  if (typeof value !== 'string' || !identifier.test(value) || value.includes('__')) {
    throw new Error('Plugin/tool identifiers must be bounded and cannot contain __');
  }
}
function jsonClone<T>(value: T): T {
  const encoded = JSON.stringify(value, (_key, item) => {
    if (typeof item === 'number' && !Number.isFinite(item)) throw new Error('Defaults must be finite JSON');
    if (['undefined', 'function', 'symbol', 'bigint'].includes(typeof item)) throw new Error('Defaults must be JSON');
    return item;
  });
  return JSON.parse(encoded);
}
export function definePlugin(def: PluginDefinition): DefinedPlugin {
  validateIdentifier(def.name);
  if ((def.version !== undefined && typeof def.version !== 'string') ||
      (def.description !== undefined && typeof def.description !== 'string')) throw new Error('Invalid plugin metadata');
  if (!Array.isArray(def.tools) || def.tools.length === 0 || def.tools.length > 64) {
    throw new Error('A plugin requires 1 through 64 tools');
  }
  const seen = new Set<string>();
  const tools = def.tools.map(t => {
    validateIdentifier(t.name);
    if (seen.has(t.name)) throw new Error('Duplicate tool name');
    seen.add(t.name);
    if (typeof t.description !== 'string' || typeof t.handler !== 'function' || !t.parameters || typeof t.parameters !== 'object' || Array.isArray(t.parameters)) {
      throw new Error('Invalid tool definition');
    }
    if (Object.keys(t.parameters).length > 64) throw new Error('Too many tool parameters');
    const parameters: Record<string, ToolParameter> = Object.create(null);
    for (const [name, p] of Object.entries(t.parameters)) {
      validateIdentifier(name);
      if (!p || !['string', 'integer', 'number', 'boolean', 'array', 'object'].includes(p.type) ||
          (p.required !== undefined && typeof p.required !== 'boolean') || typeof p.description !== 'string') {
        throw new Error('Invalid tool parameter');
      }
      parameters[name] = {type: p.type, description: p.description, required: p.required ?? true};
      if (Object.hasOwn(p, 'default')) parameters[name].default = jsonClone(p.default);
    }
    return {name: t.name, description: t.description, parameters, handler: t.handler};
  });
  const name = def.name;
  const version = def.version || '1.0.0';
  const description = def.description || '';
  return {
    name, version, description,
    tools: tools.map(t => ({...t, parameters: structuredClone(t.parameters)})),
    toManifest(transport) {
      if (!transport || typeof transport.baseUrl !== 'string') throw new Error('An explicit running loopback HTTP plugin host is required');
      const base = new URL(transport.baseUrl);
      if (base.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(base.hostname) ||
          base.username || base.password || base.search || base.hash || base.pathname !== '/') {
        throw new Error('Plugin base URL must be a bare loopback HTTP origin');
      }
      return {
        skill_id: name, version, description,
        brand: {name: name.replace(/_/g, ' '), icon: 'puzzle'}, auth: {type: 'bearer'},
        endpoints: tools.map(t => ({
          id: t.name, method: 'POST', url: `${base.origin}/feral/${name}/${t.name}`,
          description: t.description, safety_tier: 'confirm', requires_user_approval: true,
          params: Object.entries(t.parameters).map(([paramName, p]) => ({name: paramName, ...structuredClone(p)})),
        })),
        trigger_phrases: [], categories: ['plugin'],
      };
    },
    async execute(endpoint, args) {
      const tool = tools.find(t => t.name === endpoint);
      if (!tool) throw new Error('Unknown plugin endpoint');
      if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('Arguments must be an object');
      return tool.handler(args);
    },
  };
}
