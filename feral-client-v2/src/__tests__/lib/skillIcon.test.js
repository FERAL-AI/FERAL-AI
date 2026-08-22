/**
 * The per-skill icon has to be real, and it has to be stable.
 *
 * "Real" is not decoration here: lucide-react 1.x dropped its brand set,
 * and a named import of an icon that no longer exists resolves to
 * `undefined` under vitest while failing the production rollup build.
 * That is exactly the shape that ships a page which renders in the test
 * suite and cannot be built, so every entry in every map is checked for
 * being a component.
 *
 * "Stable" means the same skill gets the same icon on every call. The
 * derivation reads the manifest (`categories`, then the skill id), never
 * a hash, a random pick or a position in a list.
 */
import { describe, it, expect } from 'vitest';
import { Wrench } from 'lucide-react';

import { skillIcon, oneLineSummary, CATEGORY_ICONS, SKILL_ID_ICONS } from '../../lib/skillIcon';

/** Every real category string in feral-core/skills/manifests/*.json. */
const SHIPPED_FIRST_CATEGORIES = [
  'computer_use', 'browser', 'productivity', 'development', 'system',
  'hardware', 'desktop', 'identity', 'communication', 'coding',
  'developer', 'people', 'files', 'health', 'creative', 'messaging',
  'documents', 'vision', 'smart_home', 'music', 'orchestration',
  'search', 'weather',
];

function isComponent(value) {
  return typeof value === 'function' || (typeof value === 'object' && value !== null);
}

describe('skillIcon', () => {
  it('maps every icon it declares to something React can render', () => {
    for (const [key, Icon] of Object.entries(CATEGORY_ICONS)) {
      expect(isComponent(Icon), `CATEGORY_ICONS.${key} is not a component`).toBe(true);
    }
    for (const [key, Icon] of Object.entries(SKILL_ID_ICONS)) {
      expect(isComponent(Icon), `SKILL_ID_ICONS.${key} is not a component`).toBe(true);
    }
  });

  it('has an icon for every category a shipped manifest leads with', () => {
    for (const category of SHIPPED_FIRST_CATEGORIES) {
      expect(CATEGORY_ICONS[category], `no icon for category "${category}"`).toBeTruthy();
    }
  });

  it('prefers the skill-id override over the category', () => {
    const spotify = skillIcon({ skill_id: 'spotify_music', categories: ['music'] });
    expect(spotify).toBe(SKILL_ID_ICONS.spotify_music);
  });

  it('falls back to the first category it knows, not the first category', () => {
    // 'autonomy:user_confirm' is a real leading-ish category on
    // external_agent and is not an icon subject. It must be skipped
    // rather than turned into the generic fallback.
    const icon = skillIcon({ skill_id: 'external_agent', categories: ['autonomy:user_confirm', 'coding'] });
    expect(icon).toBe(CATEGORY_ICONS.coding);
  });

  it('reads the skill id when no category is recognised', () => {
    const icon = skillIcon({ skill_id: 'some_weather_thing', categories: ['made_up'] });
    expect(icon).toBe(CATEGORY_ICONS.weather);
  });

  it('never returns nothing', () => {
    expect(skillIcon({ skill_id: 'zzz', categories: [] })).toBe(Wrench);
    expect(skillIcon({})).toBe(Wrench);
    expect(skillIcon(null)).toBe(Wrench);
    // A brain older than this change sends no `categories` key at all.
    expect(skillIcon({ skill_id: 'zzz' })).toBe(Wrench);
  });

  it('is stable across calls', () => {
    const skill = { skill_id: 'macos_ax', categories: ['system', 'desktop', 'accessibility'] };
    const first = skillIcon(skill);
    for (let i = 0; i < 20; i += 1) expect(skillIcon(skill)).toBe(first);
  });
});

describe('oneLineSummary', () => {
  it('takes the first sentence of a long description', () => {
    const text = 'Control Spotify playback (play/pause, skip). Backend: SpotifyIntegration. '
      + 'Every playback endpoint acts on whatever device Spotify calls ACTIVE.';
    expect(oneLineSummary(text, 200)).toBe('Control Spotify playback (play/pause, skip).');
  });

  it('caps a first sentence that is itself enormous', () => {
    const text = `${'a'.repeat(400)}. And then more.`;
    const out = oneLineSummary(text);
    expect(out.length).toBeLessThanOrEqual(110);
    expect(out.endsWith('…')).toBe(true);
  });

  it('keeps a short description whole', () => {
    expect(oneLineSummary('Calendar access')).toBe('Calendar access');
  });

  it('collapses the newlines a manifest description can carry', () => {
    expect(oneLineSummary('Line one\n\n  line two')).toBe('Line one line two');
  });

  it('handles nothing at all', () => {
    expect(oneLineSummary(undefined)).toBe('');
    expect(oneLineSummary('')).toBe('');
    expect(oneLineSummary(null)).toBe('');
  });
});
