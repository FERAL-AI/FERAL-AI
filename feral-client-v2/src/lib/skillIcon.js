/**
 * A stable icon for a skill.
 *
 * WHAT THIS IS DERIVED FROM, stated plainly because it matters: no skill
 * manifest carries an icon. All 41 shipped manifests under
 * `feral-core/skills/manifests/` were read; the only image-ish field is
 * `brand.logo_url`, which is empty in 39 of them and in the remaining two
 * (`cutebot.json`, `github.json`) points at a third-party CDN. There is
 * no emoji field, no `icon` field and no glyph field anywhere in the
 * schema (`models/skill_manifest.py`), so nothing here reads one.
 *
 * What every manifest DOES declare is `categories`, a non-empty list, and
 * `GET /skills` now sends it. So the icon is derived, in order:
 *
 *   1. an explicit per-skill-id override, for the handful whose brand is
 *      more recognisable than their category (GitHub, Spotify, Notion),
 *   2. the first entry of `categories` that this module has an icon for,
 *   3. a single-word match against the skill id, which is what covers a
 *      skill installed from the marketplace whose categories are ones we
 *      have never seen,
 *   4. `Wrench`, the same icon the nav uses for the Skills page itself.
 *
 * The result is stable: the same skill gets the same icon on every render
 * and every reload, because the input is the manifest, not a hash of
 * anything or a position in a list.
 */
import {
  Activity, AppWindow, Bell, BookOpen, Bot, Brain, Braces, Calendar,
  Camera, Cloud, Code2, Cpu, Eye, FileText, FolderOpen, GitBranch,
  HeartPulse, Home, Image, ListChecks, Mail, MessageSquare, Monitor,
  MousePointerClick, Music, Network, Search, Settings, ShoppingCart,
  Sparkles, StickyNote, Terminal, UserCircle2, Users, Wrench,
} from 'lucide-react';

/** category -> icon. Keys are the exact strings the manifests declare. */
export const CATEGORY_ICONS = {
  accessibility: Eye,
  agent: Bot,
  agents: Bot,
  analysis: Activity,
  assistive: Eye,
  automation: ListChecks,
  biometric: HeartPulse,
  browser: AppWindow,
  calendar: Calendar,
  camera: Camera,
  channels: MessageSquare,
  code: Code2,
  coding: Code2,
  commerce: ShoppingCart,
  communication: Mail,
  computer_use: MousePointerClick,
  creative: Sparkles,
  desktop: Monitor,
  developer: Code2,
  developer_tools: Code2,
  development: Code2,
  documents: FileText,
  entertainment: Music,
  escape_hatch: Terminal,
  files: FolderOpen,
  hardware: Cpu,
  health: HeartPulse,
  identity: UserCircle2,
  iot: Cpu,
  knowledge: BookOpen,
  media: Image,
  memory: Brain,
  messaging: MessageSquare,
  meta: Braces,
  music: Music,
  notes: StickyNote,
  orchestration: Network,
  people: Users,
  planning: ListChecks,
  productivity: ListChecks,
  reasoning: Brain,
  reminders: Bell,
  robotics: Bot,
  scheduling: Calendar,
  search: Search,
  self: Braces,
  settings: Settings,
  smart_home: Home,
  system: Settings,
  utility: Wrench,
  vision: Eye,
  weather: Cloud,
  web: AppWindow,
  wellness: HeartPulse,
  workflows: ListChecks,
};

/**
 * Skill ids whose brand beats their category. Kept deliberately short:
 * every entry here is a claim that a human recognises the product faster
 * than the category, and that is only true for a few.
 */
export const SKILL_ID_ICONS = {
  // lucide-react 1.x dropped its brand set, so there is no GitHub mark
  // to use. GitBranch is the nearest thing the library still ships and
  // is at least the right subject; it is not the GitHub logo and is not
  // pretending to be.
  github_api: GitBranch,
  spotify_music: Music,
  notion: StickyNote,
  email: Mail,
  google_drive: FolderOpen,
  image_gen: Image,
  code_interpreter: Terminal,
  web_search: Search,
};

/** Single words in a skill id that name an icon. Step 3 of the ladder. */
const ID_WORD_ICONS = {
  browser: AppWindow,
  calendar: Calendar,
  code: Code2,
  coding: Code2,
  desktop: Monitor,
  drive: FolderOpen,
  email: Mail,
  health: HeartPulse,
  home: Home,
  image: Image,
  mail: Mail,
  memory: Brain,
  messaging: MessageSquare,
  music: Music,
  notes: StickyNote,
  pdf: FileText,
  robot: Bot,
  screen: Camera,
  search: Search,
  settings: Settings,
  weather: Cloud,
  web: AppWindow,
};

/**
 * The icon component for one skill row from `GET /skills`.
 *
 * Never returns null: a caller that renders `<Icon />` unconditionally is
 * the point, so an unknown skill still gets a glyph rather than a hole in
 * the grid.
 */
export function skillIcon(skill) {
  if (!skill) return Wrench;
  const id = String(skill.skill_id || skill.id || '');
  if (SKILL_ID_ICONS[id]) return SKILL_ID_ICONS[id];

  const categories = Array.isArray(skill.categories) ? skill.categories : [];
  for (const raw of categories) {
    const key = String(raw || '').toLowerCase();
    if (CATEGORY_ICONS[key]) return CATEGORY_ICONS[key];
  }

  for (const word of id.toLowerCase().split(/[^a-z0-9]+/)) {
    if (ID_WORD_ICONS[word]) return ID_WORD_ICONS[word];
  }

  return Wrench;
}

/**
 * The first sentence of a description, for the one-line card summary.
 *
 * The Skills page used to print the whole `description` inline. These are
 * written for the LLM, not for a card: the longest shipped one
 * (`macos_ax`) is over 2,000 characters of preconditions, and it made its
 * card twice the height of its neighbours. Measured on the live brain
 * before this change, card heights in one grid ran 527px to 1055px.
 *
 * The full text is not thrown away, it moves to the detail sheet.
 */
export function oneLineSummary(description, max = 110) {
  const text = String(description || '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  // Sentence end = . ! or ? followed by a space, ignoring "e.g." style
  // abbreviations by requiring at least a few characters before it.
  const match = text.match(/^.{12,}?[.!?](?=\s)/);
  let first = match ? match[0] : text;
  if (first.length > max) {
    first = `${first.slice(0, max - 1).replace(/[\s,;:.]+$/, '')}…`;
  }
  return first;
}
