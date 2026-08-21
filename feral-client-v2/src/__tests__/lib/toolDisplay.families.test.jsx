/**
 * Guard: every skill the brain ships has a glyph.
 *
 * The card used to draw five icons (spinner / tick / cross / shield /
 * chevron), all five of them encoding the OUTCOME, across the forty-one
 * skill manifests in feral-core/skills/manifests. Every tool call
 * therefore looked the same.
 *
 * This test reads those manifests rather than a fixture list. A fixture
 * would go stale the first time a skill is added to the brain and this
 * map is not updated, which is the exact failure it exists to catch.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, it, expect } from 'vitest';
import {
  DEFAULT_TOOL_FAMILY,
  SKILL_FAMILY,
  TOOL_FAMILIES,
  toolFamily,
  toolIds,
  friendlyToolLabel,
} from '../../lib/toolDisplay';
import { FAMILY_ICONS } from '../../components/ToolCallCard';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MANIFEST_DIR = path.resolve(HERE, '../../../../feral-core/skills/manifests');

function shippedSkillIds() {
  return fs.readdirSync(MANIFEST_DIR)
    .filter((f) => f.endsWith('.json'))
    .map((f) => JSON.parse(fs.readFileSync(path.join(MANIFEST_DIR, f), 'utf8')))
    .map((m) => m.skill_id)
    .filter(Boolean);
}

describe('skill family coverage', () => {
  it('finds the shipped manifests (guard is only real if it reads them)', () => {
    expect(fs.existsSync(MANIFEST_DIR)).toBe(true);
    expect(shippedSkillIds().length).toBeGreaterThanOrEqual(40);
  });

  it('gives every shipped skill_id a family that is not the fallback', () => {
    const unmapped = shippedSkillIds().filter(
      (id) => (SKILL_FAMILY[id] || DEFAULT_TOOL_FAMILY) === DEFAULT_TOOL_FAMILY,
    );
    expect(unmapped).toEqual([]);
  });

  it('uses more than a handful of families across those skills', () => {
    const families = new Set(shippedSkillIds().map((id) => SKILL_FAMILY[id]));
    // Five icons for forty skills was the defect. Anything that
    // collapses back toward that is a regression.
    expect(families.size).toBeGreaterThanOrEqual(10);
  });

  it('maps every family to a declared name and a real glyph', () => {
    for (const family of Object.values(SKILL_FAMILY)) {
      expect(TOOL_FAMILIES[family], `no name for family ${family}`).toBeTruthy();
      expect(typeof FAMILY_ICONS[family], `no glyph for family ${family}`).toBe('object');
    }
    for (const family of Object.keys(TOOL_FAMILIES)) {
      expect(FAMILY_ICONS[family], `no glyph for family ${family}`).toBeTruthy();
    }
  });
});

describe('toolFamily resolution', () => {
  it('reads skill_id off a tool_start payload', () => {
    expect(toolFamily({ tool: 'web_search__search', skill_id: 'web_search' })).toBe('search');
  });

  it('splits `tool` on __ for a tool_result payload, which carries no skill_id', () => {
    // models/protocol.py ToolResultPayload has tool/call_id/success/...
    // and no skill_id or endpoint_id at all.
    expect(toolFamily({ tool: 'coding_tools__bash' })).toBe('code');
    expect(toolIds({ tool: 'coding_tools__bash' })).toEqual({ skill: 'coding_tools', endpoint: 'bash' });
  });

  it('falls back to the generic family rather than guessing', () => {
    expect(toolFamily({ tool: 'some_third_party__thing' })).toBe(DEFAULT_TOOL_FAMILY);
    expect(toolFamily({})).toBe(DEFAULT_TOOL_FAMILY);
  });

  it('leaves the existing labels intact', () => {
    expect(friendlyToolLabel({ tool: 'coding_tools__bash' })).toBe('Run local command');
    expect(friendlyToolLabel({ tool: 'web_search__search' })).toBe('Search web');
    expect(friendlyToolLabel({ display_name: 'Explicit' })).toBe('Explicit');
    expect(friendlyToolLabel({ tool: 'browser__click' })).toBe('Browser: Click');
  });
});
