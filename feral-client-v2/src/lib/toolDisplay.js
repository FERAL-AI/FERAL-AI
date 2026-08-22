/**
 * toolDisplay: how a tool call is NAMED and ICONED in the transcript.
 *
 * The label half is `friendlyToolLabel`. The icon half is
 * `toolFamily` + `TOOL_FAMILIES`, added because the tool card had
 * exactly five glyphs (spinner, tick, cross, shield, chevron) and all
 * five encoded the call's OUTCOME. Forty-one shipped skills therefore
 * rendered as the same picture, and a wall of tool cards was a wall of
 * identical rows distinguished only by prose. Outcome is now carried
 * by the card's tone, which frees the glyph to say what KIND of thing
 * FERAL did: search the web, drive the browser, touch a file, move a
 * robot.
 *
 * `SKILL_FAMILY` is keyed on `skill_id`, which is what the wire
 * actually carries: `ToolStartPayload.skill_id`, or the left half of
 * `tool` split on `__` (agents/orchestrator.py `_emit_tool_start`).
 * `ToolResultPayload` carries no skill_id at all, only `tool`, so the
 * split is the load-bearing path on result frames.
 *
 * The families are grouped from the `categories` array in each
 * `feral-core/skills/manifests/*.json`.
 * `__tests__/lib/toolDisplay.families.test.js` reads those manifests
 * and fails if a shipped skill has no family, so adding a skill to the
 * brain without a glyph here is a red test rather than a silent
 * fallback to the generic wrench.
 */

const HUMAN_LABELS = {
  web_search: 'Search web',
  weather_current: 'Check weather',
};

/** Family id -> human name. The glyph itself is picked in ToolCallCard. */
export const TOOL_FAMILIES = Object.freeze({
  search: 'Search',
  browser: 'Browser',
  code: 'Code',
  computer: 'Computer',
  vision: 'Vision',
  comms: 'Communication',
  schedule: 'Schedule',
  tasks: 'Tasks',
  notes: 'Documents',
  media: 'Media',
  hardware: 'Hardware',
  health: 'Health',
  system: 'System',
  tool: 'Tool',
});

export const DEFAULT_TOOL_FAMILY = 'tool';

export const SKILL_FAMILY = Object.freeze({
  // categories: search / knowledge
  web_search: 'search',
  // categories: browser / automation / web / commerce
  browser: 'browser',
  browser_memory: 'browser',
  web_actions: 'browser',
  // categories: development / coding / code / analysis / escape_hatch
  coding_tools: 'code',
  code_interpreter: 'code',
  workspace_scripts: 'code',
  github_api: 'code',
  external_agent: 'code',
  // categories: computer_use / desktop / accessibility / automation
  agentic_computer_use: 'computer',
  gui_computer_use: 'computer',
  desktop_automation: 'computer',
  desktop_control: 'computer',
  macos_ax: 'computer',
  // categories: vision / camera / assistive
  perception_query: 'vision',
  screen_capture: 'vision',
  // categories: communication / messaging / people
  email: 'comms',
  messaging_sms: 'comms',
  messaging_channels: 'comms',
  google_contacts: 'comms',
  microsoft365: 'comms',
  // categories: calendar / scheduling / reminders
  calendar_google: 'schedule',
  feral_reminders: 'schedule',
  feral_routines: 'schedule',
  // categories: workflows / planning / orchestration / agent
  feral_workflows: 'tasks',
  plan: 'tasks',
  background_task: 'tasks',
  subagent: 'tasks',
  // categories: notes / documents / files / productivity
  notes_memory: 'notes',
  notion: 'notes',
  pdf_reader: 'notes',
  google_drive: 'notes',
  // categories: creative / media / music / entertainment
  image_gen: 'media',
  spotify_music: 'media',
  // categories: hardware / robotics / iot / smart_home
  cutebot: 'hardware',
  robot_ext: 'hardware',
  smart_home_hue: 'hardware',
  // categories: system / meta / self / identity / settings
  system_settings: 'system',
  self_introspection: 'system',
  digital_twin: 'system',
  // categories: health / biometric / wellness
  health_data: 'health',

  // Aliases for ids that reach the client under a shorter name than the
  // manifest's `skill_id`. `computer_use` was consolidated into
  // `coding_tools` (see friendlyToolLabel below); the rest appear in
  // agents/orchestrator.py `_DEVICE_ACTION_SKILLS` and in tool names
  // the brain builds from the manifest FILE name rather than skill_id.
  computer_use: 'code',
  smart_home: 'hardware',
  robot_arm: 'hardware',
  robot_action: 'hardware',
  calendar: 'schedule',
  messaging: 'comms',
  notes: 'notes',
  github: 'code',
  spotify: 'media',
  task: 'tasks',
});

/** Split a tool payload into its skill / endpoint ids. */
export function toolIds(payload = {}) {
  const raw = String(payload.tool || payload.name || '');
  const [rawSkill, rawEndpoint = ''] = raw.includes('__') ? raw.split('__', 2) : [raw, ''];
  return {
    skill: (payload.skill_id ? String(payload.skill_id) : '') || rawSkill,
    endpoint: (payload.endpoint_id ? String(payload.endpoint_id) : '') || rawEndpoint,
  };
}

/**
 * Family id for a tool payload. Falls back to `tool` (generic glyph)
 * rather than guessing, so an unknown skill reads as "a tool ran" and
 * never as the wrong category.
 */
export function toolFamily(payload = {}) {
  const { skill } = toolIds(payload);
  return SKILL_FAMILY[skill] || DEFAULT_TOOL_FAMILY;
}

function humanize(value) {
  const text = String(value || '').replaceAll('_', ' ').trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : 'Use tool';
}

export function friendlyToolLabel(payload = {}) {
  if (payload.display_name) return String(payload.display_name);
  const { skill, endpoint } = toolIds(payload);

  if (HUMAN_LABELS[skill]) return HUMAN_LABELS[skill];
  if (skill === 'browser') return endpoint ? `Browser: ${humanize(endpoint)}` : 'Use browser';
  // computer_use was consolidated into coding_tools; accept both so the
  // real shipped ids get the intended labels.
  if (skill === 'coding_tools' || skill === 'computer_use') {
    if (endpoint === 'bash') return 'Run local command';
    if (endpoint === 'write_file') return 'Write file';
    if (endpoint === 'read_file') return 'Read file';
    if (endpoint === 'edit_file') return 'Edit file';
    if (endpoint === 'grep_search' || endpoint === 'glob_search') return 'Search files';
  }
  if (skill === 'gui_computer_use' || skill === 'agentic_computer_use' || skill === 'desktop_automation') {
    return 'Use computer';
  }
  return endpoint ? humanize(endpoint) : humanize(skill);
}
