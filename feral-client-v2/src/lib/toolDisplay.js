const HUMAN_LABELS = {
  web_search: 'Search web',
  weather_current: 'Check weather',
};

function humanize(value) {
  const text = String(value || '').replaceAll('_', ' ').trim();
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : 'Use tool';
}

export function friendlyToolLabel(payload = {}) {
  if (payload.display_name) return String(payload.display_name);
  const raw = String(payload.tool || payload.name || '');
  const explicitSkill = payload.skill_id ? String(payload.skill_id) : '';
  const explicitEndpoint = payload.endpoint_id ? String(payload.endpoint_id) : '';
  const [rawSkill, rawEndpoint = ''] = raw.includes('__') ? raw.split('__', 2) : [raw, ''];
  const skill = explicitSkill || rawSkill;
  const endpoint = explicitEndpoint || rawEndpoint;

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
