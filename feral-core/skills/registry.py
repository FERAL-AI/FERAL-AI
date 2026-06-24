"""
FERAL Skill Registry — Loading and Managing Skills
=====================================================
Loads skill manifests, provides embedding-based search,
and converts skills to LLM tool definitions.
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional

from config.loader import feral_home
from models.skill_manifest import SkillManifest, WEATHER_SKILL

logger = logging.getLogger("feral.skills")


class SkillRegistry:
    """Manages all registered skills and provides fast lookup."""

    def __init__(self):
        self.skills: dict[str, SkillManifest] = {}
        self._tool_cache: dict[str, list[dict]] = {}  # skill_id → LLM tool defs
        self._cron_service = None

    def set_cron_service(self, cron_service):
        """Wire the CronService so _auto_create_routines can register jobs.

        Boot order is: ``load_builtin_skills()`` (api/state.py) runs BEFORE the
        CronService exists, so every manifest cron was silently dropped at
        registration time (``_cron_service`` was None). We now re-scan all
        already-registered manifests as soon as the service is wired, with
        dedupe so the persisted SQLite jobs aren't duplicated on every boot.
        """
        self._cron_service = cron_service
        if cron_service is not None:
            self._rescan_auto_routines()

    def _rescan_auto_routines(self):
        """Re-run _auto_create_routines for every registered manifest now that
        the CronService is available. Idempotent via dedupe."""
        for manifest in list(self.skills.values()):
            try:
                self._auto_create_routines(manifest)
            except Exception as exc:
                logger.debug("rescan auto-routine failed for %s: %s", getattr(manifest, "skill_id", "?"), exc)

    def load_builtin_skills(self):
        """Load the default skills that ship with FERAL."""
        # Load hardcoded weather skill
        self.register(WEATHER_SKILL)

        # Load all JSON manifests from the manifests directory
        manifests_dir = Path(__file__).parent / "manifests"
        if manifests_dir.exists():
            self.load_from_directory(manifests_dir)

        # Load marketplace-installed skills from ~/.feral/skills/
        self._load_marketplace_skills()

        logger.info(f"Loaded {len(self.skills)} skills total")

    def _load_marketplace_skills(self):
        """Scan ~/.feral/skills/ for marketplace-installed skill packages."""
        skills_dir = feral_home() / "skills"
        if not skills_dir.exists():
            return

        from skills.package import SkillPackage
        count = 0
        for d in sorted(skills_dir.iterdir()):
            if d.is_dir() and (d / "manifest.json").exists():
                try:
                    pkg = SkillPackage(d)
                    if pkg.load() and pkg.manifest:
                        self.register(pkg.manifest)
                        self._try_load_dynamic_impl(d, pkg.manifest.skill_id)
                        count += 1
                except Exception as e:
                    logger.warning(f"Failed to load marketplace skill from {d}: {e}")

        if count:
            logger.info(f"Loaded {count} marketplace skills from {skills_dir}")

    @staticmethod
    def _try_load_dynamic_impl(skill_dir: Path, skill_id: str):
        """Dynamically load impl.py from a skill directory and register the implementation."""
        impl_path = skill_dir / "impl.py"
        if not impl_path.exists():
            return
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(f"feral_skill_{skill_id}", str(impl_path))
            if not spec or not spec.loader:
                return
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            from skills.base import BaseSkill
            from skills.impl import register_instance
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if isinstance(obj, type) and issubclass(obj, BaseSkill) and obj is not BaseSkill:
                    instance = obj()
                    register_instance(skill_id, instance)
                    logger.info(f"Loaded dynamic implementation for {skill_id} from {impl_path}")
                    return
        except Exception as e:
            logger.warning(f"Failed to load impl.py for {skill_id}: {e}")

    def register(self, manifest: SkillManifest):
        """Register a skill manifest."""
        self.skills[manifest.skill_id] = manifest
        self._tool_cache[manifest.skill_id] = self._manifest_to_tools(manifest)
        logger.info(f"Registered skill: {manifest.brand.name} ({manifest.skill_id})")
        self._auto_create_routines(manifest)

    register_skill = register  # Alias for the skill generator

    def _auto_routine_exists(self, svc, description: str) -> bool:
        """Dedupe key: the deterministic ``[auto] ...`` description. Jobs
        persist in SQLite, so without this every boot would re-create them."""
        try:
            for job in svc.list_jobs():
                if (job.description or "") == description:
                    return True
        except Exception:
            pass
        return False

    def _flow_to_taskflow_steps(self, manifest: SkillManifest, flow_id: str) -> list[dict]:
        """Translate a manifest SkillFlow (endpoint sequence) into TaskFlow
        skill.invoke steps so a cron ``flow_id`` can run via the L2 flow
        branch. Minimal: maps each step's endpoint_id; condition branching in
        SkillFlow is not auto-translated (kept additive / non-sprawling)."""
        for flow in (manifest.flows or []):
            if getattr(flow, "id", None) != flow_id:
                continue
            steps: list[dict] = []
            for fstep in (getattr(flow, "steps", []) or []):
                ep = getattr(fstep, "endpoint_id", None)
                if ep:
                    steps.append({"type": "skill.invoke", "skill_id": manifest.skill_id, "endpoint": ep})
            return steps
        return []

    def _auto_create_routines(self, manifest: SkillManifest):
        try:
            from agents.scheduler import JobType
            svc = getattr(self, '_cron_service', None)
            if svc is None:
                return
            default_ep = manifest.endpoints[0].id if manifest.endpoints else ''
            for cdef in (manifest.crons or []):
                # CronDefinition.schedule is the canonical field; keep legacy
                # attribute fallbacks for non-standard / older manifests.
                expr = (
                    getattr(cdef, 'schedule', '')
                    or getattr(cdef, 'expression', '')
                    or getattr(cdef, 'cron_expr', '')
                )
                if not expr:
                    continue
                cron_id = getattr(cdef, 'id', '') or expr
                desc = f"[auto] {manifest.skill_id}:{cron_id}"
                if self._auto_routine_exists(svc, desc):
                    continue

                flow_id = getattr(cdef, 'flow_id', None)
                endpoint = getattr(cdef, 'endpoint_id', None) or getattr(cdef, 'endpoint', None)

                if flow_id and not endpoint:
                    steps = self._flow_to_taskflow_steps(manifest, flow_id)
                    if not steps:
                        logger.debug("cron flow_id %s has no resolvable steps in %s", flow_id, manifest.skill_id)
                        continue
                    payload = {"flow_id": flow_id, "steps": steps}
                else:
                    payload = {
                        "skill": manifest.skill_id,
                        "endpoint": endpoint or default_ep,
                        "args": getattr(cdef, 'args', {}) or {},
                    }

                svc.create_job(JobType.SCHEDULED, expr, desc, payload, "")
                logger.info("Auto-created routine for cron: %s in skill %s", expr, manifest.skill_id)
            for tdef in (manifest.triggers or []):
                event = getattr(tdef, 'event', '') or getattr(tdef, 'trigger', '') or getattr(tdef, 'id', '')
                if not event:
                    continue
                desc = f"[auto] {manifest.skill_id}: trigger on {event}"
                if self._auto_routine_exists(svc, desc):
                    continue
                payload = {
                    "skill": manifest.skill_id,
                    "endpoint": getattr(tdef, 'action_endpoint_id', None) or getattr(tdef, 'endpoint', None) or default_ep,
                    "trigger_event": event,
                    "condition": getattr(tdef, 'condition', None),
                }
                svc.create_job(JobType.TRIGGERED, "every 1m", desc, payload, "")
                logger.info("Auto-created routine for trigger: %s in skill %s", event, manifest.skill_id)
        except Exception as e:
            logger.debug("Skip auto-routine creation for %s: %s", manifest.skill_id, e)

    def load_from_file(self, path: str | Path):
        """Load a skill manifest from a JSON file."""
        with open(path) as f:
            data = json.load(f)
        manifest = SkillManifest(**data)
        self.register(manifest)

    def load_from_directory(self, directory: str | Path):
        """Load all skill manifests from a directory."""
        p = Path(directory)
        for skill_file in p.glob("*.json"):
            try:
                self.load_from_file(skill_file)
            except Exception as e:
                logger.error(f"Failed to load skill {skill_file}: {e}")

    def reload_skill(self, skill_id: str) -> bool:
        """Re-load a skill from disk and hot-swap its implementation.

        Looks for the manifest + optional impl.py under:
          1. ``~/.feral/skills/<skill_id>/``  (marketplace-installed / promoted)
          2. ``~/.feral/skills/generated/<skill_id>/`` (Tool Genesis output)
          3. ``skills/manifests/<skill_id>.json`` (first-party)

        Registers the manifest, invalidates the tool cache for this skill id,
        and if an ``impl.py`` is present alongside the manifest, re-imports it
        via ``importlib.util`` so the in-process LLM tool list picks up the
        new definition without a restart.

        Returns ``True`` on success.
        """
        from skills.package import SkillPackage

        candidates: list[Path] = []
        home = feral_home()
        candidates.append(home / "skills" / skill_id)
        candidates.append(home / "skills" / "generated" / skill_id)

        firstparty = Path(__file__).parent / "manifests" / f"{skill_id}.json"
        if firstparty.exists():
            candidates.append(firstparty)

        for candidate in candidates:
            try:
                if candidate.is_file() and candidate.suffix == ".json":
                    self.load_from_file(candidate)
                    logger.info("reload_skill(%s): loaded first-party manifest", skill_id)
                    return True
                if candidate.is_dir() and (candidate / "manifest.json").exists():
                    pkg = SkillPackage(candidate)
                    if not (pkg.load() and pkg.manifest):
                        continue
                    self.skills.pop(skill_id, None)
                    self._tool_cache.pop(skill_id, None)
                    self.register(pkg.manifest)
                    self._reimport_dynamic_impl(candidate, skill_id)
                    logger.info("reload_skill(%s): hot-reloaded from %s", skill_id, candidate)
                    return True
            except Exception as exc:
                logger.warning("reload_skill(%s) from %s failed: %s", skill_id, candidate, exc)
        logger.warning("reload_skill(%s): no manifest found in %s", skill_id, candidates)
        return False

    @staticmethod
    def _reimport_dynamic_impl(skill_dir: Path, skill_id: str):
        """Force re-import of impl.py for the given skill, replacing the cached instance."""
        impl_path = skill_dir / "impl.py"
        if not impl_path.exists():
            return
        try:
            import importlib.util
            import sys as _sys
            module_name = f"feral_skill_{skill_id}"
            # If previously imported, drop it so we get the new source.
            _sys.modules.pop(module_name, None)
            spec = importlib.util.spec_from_file_location(module_name, str(impl_path))
            if not spec or not spec.loader:
                return
            mod = importlib.util.module_from_spec(spec)
            _sys.modules[module_name] = mod
            spec.loader.exec_module(mod)

            from skills.base import BaseSkill
            from skills.impl import register_instance
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if isinstance(obj, type) and issubclass(obj, BaseSkill) and obj is not BaseSkill:
                    register_instance(skill_id, obj())
                    logger.info("reload_skill(%s): reimported impl from %s", skill_id, impl_path)
                    return
        except Exception as exc:
            logger.warning("reload_skill(%s): impl reimport failed: %s", skill_id, exc)

    def get_skill(self, skill_id: str):
        """Return the executable BaseSkill implementation for a given skill_id, or None."""
        from skills.impl import get_implementation
        if skill_id not in self.skills:
            return None
        impl = get_implementation(skill_id)
        if impl:
            return impl
        # Try loading dynamic implementation from marketplace
        skills_dir = feral_home() / "skills" / skill_id
        if skills_dir.is_dir():
            self._try_load_dynamic_impl(skills_dir, skill_id)
            return get_implementation(skill_id)
        return None

    def get_all_tools(self) -> list[dict]:
        """Get all tools from all skills in LLM-compatible format."""
        tools = []
        for skill_id, skill_tools in self._tool_cache.items():
            tools.extend(skill_tools)
        return tools

    def find_skills_for_query(self, query: str, top_k: int = 5) -> list[SkillManifest]:
        """
        Find the most relevant skills for a user query.
        
        v1: Improved keyword/trigger phrase matching with tiered scoring.
        v2: Embedding-based semantic search (future).
        """
        scored: list[tuple[float, SkillManifest]] = []

        query_lower = query.lower().strip()
        query_words = set(query_lower.split())

        for skill in self.skills.values():
            score = 0.0

            # Check trigger phrases — highest priority
            best_trigger_score = 0.0
            for phrase in skill.trigger_phrases:
                phrase_lower = phrase.lower()

                # Exact match: query IS the trigger phrase
                if phrase_lower == query_lower:
                    best_trigger_score = max(best_trigger_score, 25.0)
                # Trigger phrase fully contained in query
                elif phrase_lower in query_lower:
                    best_trigger_score = max(best_trigger_score, 20.0)
                # Query fully contained in trigger phrase
                elif query_lower in phrase_lower:
                    best_trigger_score = max(best_trigger_score, 15.0)
                else:
                    # Partial word overlap, normalized by phrase length
                    phrase_words = set(phrase_lower.split())
                    overlap = phrase_words & query_words
                    if overlap:
                        overlap_ratio = len(overlap) / max(len(phrase_words), 1)
                        phrase_score = len(overlap) * 3.0 * overlap_ratio
                        best_trigger_score = max(best_trigger_score, phrase_score)

            score += best_trigger_score

            # Check categories — strong signal
            for cat in skill.categories:
                if cat.lower() in query_lower:
                    score += 5.0

            # Check description — weak signal, heavily normalized
            desc_words = set(skill.description.lower().split())
            # Remove common stop words to avoid noise
            stop_words = {"the", "a", "an", "and", "or", "for", "to", "in", "on", "of", "is", "it", "get", "from", "your", "with"}
            meaningful_desc = desc_words - stop_words
            meaningful_query = query_words - stop_words
            desc_overlap = meaningful_desc & meaningful_query
            score += len(desc_overlap) * 0.5  # Very low weight to prevent noise

            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [skill for _, skill in scored[:top_k]]

    def get_tools_for_skills(self, skills: list[SkillManifest]) -> list[dict]:
        """Get LLM tool definitions for a subset of skills."""
        tools = []
        for skill in skills:
            tools.extend(self._tool_cache.get(skill.skill_id, []))
        return tools

    def _manifest_to_tools(self, manifest: SkillManifest) -> list[dict]:
        """
        Convert a skill manifest to LLM function-calling tool definitions.
        This is what gets injected into the LLM's tool list.
        Compatible with OpenAI function calling format.
        """
        tools = []
        for endpoint in manifest.endpoints:
            properties = {}
            required = []
            for param in endpoint.params:
                prop: dict = {
                    "type": param.type if param.type != "array" else "array",
                    "description": param.description,
                }
                if param.type == "array" and param.items:
                    prop["items"] = param.items
                if param.enum:
                    prop["enum"] = param.enum
                if param.default:
                    prop["default"] = param.default
                properties[param.name] = prop
                if param.required:
                    required.append(param.name)

            tool = {
                "type": "function",
                "function": {
                    "name": f"{manifest.skill_id}__{endpoint.id}",
                    "description": f"[{manifest.brand.name}] {endpoint.description}",
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
                "_feral_meta": {
                    "skill_id": manifest.skill_id,
                    "endpoint_id": endpoint.id,
                    "method": endpoint.method,
                    "url": endpoint.url,
                    "ui_hint": endpoint.ui_hint,
                    "brand": manifest.brand.model_dump(),
                },
            }
            tools.append(tool)

        return tools
