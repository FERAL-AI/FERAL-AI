"""
FERAL Skill Registry — Loading and Managing Skills
=====================================================
Loads skill manifests, provides embedding-based search,
and converts skills to LLM tool definitions.
"""

from __future__ import annotations
import json
import logging
import asyncio
import importlib.util
import inspect
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from collections.abc import Callable
from uuid import uuid4

from config.loader import feral_home
from models.skill_manifest import SkillManifest, WEATHER_SKILL
from skills.base import BaseSkill

logger = logging.getLogger("feral.skills")


class ReloadPreparationError(ValueError):
    """Bounded public failure; plugin exceptions/source never become API text."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code


@dataclass
class ReloadCandidate:
    registry: SkillRegistry
    skill_id: str
    manifest: SkillManifest
    tools: list[dict]
    implementation: BaseSkill | None
    replace_implementation: bool
    module: ModuleType | None
    source_terms: tuple[tuple[Path, bytes | None], ...]
    source_path: Path
    first_party: bool
    generation: int
    previous_manifest: SkillManifest | None
    previous_tools: list[dict] | None
    previous_implementation: BaseSkill | None
    previous_module: ModuleType | None
    previous_manifest_terms: str | None
    consumed: bool = False

    def discard(self) -> None:
        """Remove only this candidate's private staging module."""
        if self.consumed:
            return
        if self.module is not None and sys.modules.get(self.module.__name__) is self.module:
            sys.modules.pop(self.module.__name__, None)
        self.consumed = True


class SkillRegistry:
    """Manages all registered skills and provides fast lookup."""

    def __init__(self):
        self.skills: dict[str, SkillManifest] = {}
        self._tool_cache: dict[str, list[dict]] = {}  # skill_id → LLM tool defs
        self._cron_service = None
        # Monotonic counter bumped on every ``register()``. Consumers that
        # snapshot ``self.skills`` (notably ``ToolDispatchValidator``, which
        # precomputes per-endpoint schemas) compare this against the value
        # they built from and rebuild when it moves. Without it, anything
        # registered after the first tool call (marketplace installs,
        # ``reload_skill``, Tool Genesis output, hot-plugged hardware) was
        # permanently rejected as ``unknown_endpoint`` even though it was
        # present in ``self.skills``.
        self._generation = 0
        self._reload_metadata: dict[str, dict[str, str | bool]] = {}
        self._lazy_attempted: set[str] = set()
        self._reload_preparations: set[asyncio.Task[ReloadCandidate]] = set()

    @property
    def generation(self) -> int:
        """Registration generation. Increments whenever a manifest is
        registered or re-registered, so cached views can detect staleness."""
        return self._generation

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

        count = 0
        for directory in sorted(skills_dir.iterdir()):
            if directory.is_dir() and directory.name != "generated":
                ok, _code, _reason = self.reload_skill_detail(directory.name)
                count += int(ok)
                if not ok:
                    logger.warning("Marketplace package was not activated (%s)", _code)
        if count:
            logger.info("Loaded %s marketplace skills", count)

    def _try_load_dynamic_impl(self, skill_dir: Path, skill_id: str) -> bool:
        """Compatibility entry point using the same truthful staging contract."""
        selected, first_party = self._select_source(skill_id)
        if first_party or selected != skill_dir.resolve():
            return False
        return self.reload_skill(skill_id)

    def register(self, manifest: SkillManifest):
        """Register a skill manifest."""
        self.skills[manifest.skill_id] = manifest
        self._tool_cache[manifest.skill_id] = self._manifest_to_tools(manifest)
        self._generation += 1
        self._lazy_attempted.discard(manifest.skill_id)
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
                # A manifest trigger says "run this action when the condition
                # holds". There is no evaluator for that condition anywhere in
                # the tree, so this used to create a JobType.TRIGGERED job with
                # cron_expr "every 1m" and stash the condition in the payload
                # where nothing read it. The result was not a trigger, it was
                # an unconditional once-a-minute poll of the action.
                #
                # That is not a safe thing to create blind. The two such jobs on
                # the first install to hit this ran 4,766 times each; one of
                # them was a Telegram send gated on a high-stress reading, and
                # it stayed quiet only because the skill was never registered.
                #
                # So do not create the job. The manifest keeps its trigger and
                # this becomes a no-op that says why. Conditions ARE evaluated
                # now, by agents/trigger_conditions.py on the ProactiveEngine's
                # 15s tick, which notifies and does not dispatch the declared
                # action. Reviving the 1m cron poll would re-create the exact
                # unconditional-firing shape, so it stays dead.
                logger.info(
                    "Skill %s declares trigger %r, which is not auto-created: "
                    "conditions are evaluated on the proactive loop "
                    "(agents/trigger_conditions.py), not by a 1m poll that "
                    "would run the action unconditionally",
                    manifest.skill_id, event,
                )
                continue
        except Exception as e:
            # Not debug. Failing here means the skill's scheduled routines were
            # never created, which presents later as a routine that simply does
            # not exist, with nothing to connect it back to this.
            logger.warning(
                "Auto-routine creation failed for %s: %s", manifest.skill_id, e
            )

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

    @staticmethod
    def _first_party_manifest(skill_id: str) -> Path | None:
        """Path of the shipped manifest that DECLARES ``skill_id``.

        The file name is not the skill id. Eight of the shipped manifests
        declare an id that differs from their file stem (``calendar.json``
        -> ``calendar_google``, ``github.json`` -> ``github_api``,
        ``task.json`` -> ``background_task``, ``messaging.json`` ->
        ``messaging_sms``, ``notes.json`` -> ``notes_memory``,
        ``robot_action.json`` -> ``robot_ext``, ``smart_home.json`` ->
        ``smart_home_hue``, ``spotify.json`` -> ``spotify_music``).

        ``register()`` keys on the declared id, which is what ``/skills``
        reports and therefore what the Skills page sends back to
        ``/api/skills/reload``. Resolving the path as
        ``manifests/{skill_id}.json`` and nothing else meant a reload of
        any of those eight found no file and returned False, so the stem
        is only a fast path now and the declared id decides.
        """
        manifests_dir = Path(__file__).parent / "manifests"
        direct = manifests_dir / f"{skill_id}.json"
        if direct.is_file():
            return direct
        if not manifests_dir.is_dir():
            return None
        for path in sorted(manifests_dir.glob("*.json")):
            try:
                with open(path) as fh:
                    declared = json.load(fh).get("skill_id")
            except Exception as exc:
                logger.warning("manifest could not be read while resolving %s (%s)", skill_id, type(exc).__name__)
                continue
            if declared == skill_id:
                return path
        return None

    def reload_skill(self, skill_id: str) -> bool:
        """Re-load a skill from disk and hot-swap its implementation.

        ``True`` on success. Callers that need to tell the operator WHY a
        reload did nothing should use :meth:`reload_skill_detail`.
        """
        ok, _code, _reason = self.reload_skill_detail(skill_id)
        return ok

    @staticmethod
    def _validate_skill_id(skill_id: str) -> None:
        if not isinstance(skill_id, str) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}", skill_id):
            raise ReloadPreparationError("invalid_id", "skill_id must be a bounded identifier without path separators")

    def _select_source(self, skill_id: str) -> tuple[Path, bool]:
        self._validate_skill_id(skill_id)
        # Canonicalize the caller-selected home once, including macOS /var
        # aliases. Reject redirects beneath that root, not the root alias.
        home = feral_home().resolve()
        for directory in (home / "skills" / skill_id, home / "skills/generated" / skill_id):
            if directory.exists() or directory.is_symlink():
                if directory.resolve() != directory.absolute() or not directory.is_dir():
                    raise ReloadPreparationError("unloadable", "Selected package directory is redirected or invalid")
                return directory, False
        first_party = self._first_party_manifest(skill_id)
        if first_party is not None:
            return first_party, True
        raise ReloadPreparationError("no_source", f"nothing on disk to reload for '{skill_id}': no package or shipped manifest declares that skill id")

    @staticmethod
    def _source_bytes(path: Path, limit: int, *, optional: bool = False) -> bytes | None:
        try:
            if path.resolve() != path.absolute() or path.is_symlink():
                raise ReloadPreparationError("unloadable", "Selected package source is redirected")
            if not path.exists() and optional:
                return None
            if not path.is_file() or path.stat().st_size > limit:
                raise ReloadPreparationError("unloadable", "Selected package source is missing, invalid or exceeds its read bound")
            data = path.read_bytes()
        except OSError as exc:
            raise ReloadPreparationError("unloadable", "Selected package source could not be read") from exc
        if len(data) > limit:
            raise ReloadPreparationError("unloadable", "Selected package source exceeds its read bound")
        return data

    def prepare_reload(self, skill_id: str) -> ReloadCandidate:
        """Prepare trusted code off-thread; never mutate live registry entries.

        Imports may have arbitrary trusted-code effects. Only helper-mediated
        implementation publication is staged, and exceptions are redacted.
        """
        from skills.impl import capture_registrations, get_implementation

        self._validate_skill_id(skill_id)
        previous_manifest = self.skills.get(skill_id)
        previous_tools = self._tool_cache.get(skill_id)
        previous_implementation = get_implementation(skill_id)
        previous_module = sys.modules.get(f"feral_skill_{skill_id}")
        generation = self.generation
        previous_terms = previous_manifest.model_dump_json() if previous_manifest is not None else None
        source_path, first_party = self._select_source(skill_id)
        manifest_path = source_path if first_party else source_path / "manifest.json"
        manifest_bytes = self._source_bytes(manifest_path, 1024 * 1024)
        if manifest_bytes is None:
            raise ReloadPreparationError("unloadable", "Selected manifest is absent")
        try:
            manifest = SkillManifest.model_validate_json(manifest_bytes)
        except Exception as exc:
            raise ReloadPreparationError("unloadable", f"Selected manifest for '{skill_id}' is invalid") from exc
        if manifest.skill_id != skill_id:
            raise ReloadPreparationError("identity_mismatch", "Requested ID and selected manifest ID differ")
        tools = self._manifest_to_tools(manifest)
        implementation = previous_implementation if first_party else None
        if first_party and implementation is not None and implementation.skill_id != skill_id:
            raise ReloadPreparationError("identity_mismatch", "Wired implementation has a different skill ID")
        terms: tuple[tuple[Path, bytes | None], ...] = ((manifest_path, manifest_bytes),)
        module = None
        if not first_party:
            impl_path = source_path / "impl.py"
            impl_bytes = self._source_bytes(impl_path, 2 * 1024 * 1024, optional=True)
            terms += ((impl_path, impl_bytes),)
            if impl_bytes is None:
                if any(endpoint.method == "PYTHON" for endpoint in manifest.endpoints):
                    raise ReloadPreparationError("missing_implementation", "Python package requires an explicit BaseSkill implementation")
            else:
                name = "_feral_skill_staging_" + uuid4().hex
                spec = importlib.util.spec_from_file_location(name, impl_path)
                if spec is None:
                    raise ReloadPreparationError("unloadable", "Selected implementation cannot be imported")
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                try:
                    with capture_registrations(skill_id) as captured:
                        # Captured source avoids stale timestamp-based pyc loads.
                        exec(compile(impl_bytes, str(impl_path), "exec"), module.__dict__)
                        exported: dict[type[BaseSkill], Callable[[], BaseSkill]] = {}
                        for attribute, value in vars(module).items():
                            if isinstance(value, type) and issubclass(value, BaseSkill) and value is not BaseSkill:
                                exported[value] = module.__dict__[attribute]
                        if len(exported) != 1:
                            raise ReloadPreparationError("invalid_implementation", "Package must export one unique BaseSkill class")
                        skill_class = next(iter(exported))
                        implementation = captured.instances.get(skill_id)
                        if implementation is None:
                            implementation = exported[skill_class]()
                        elif type(implementation) is not skill_class:
                            raise ReloadPreparationError("invalid_implementation", "Exported class and staged implementation differ")
                        if not isinstance(implementation, BaseSkill) or implementation.skill_id != skill_id:
                            raise ReloadPreparationError("identity_mismatch", "Requested ID and implementation ID differ")
                        if skill_class.execute is BaseSkill.execute or not inspect.iscoroutinefunction(implementation.execute):
                            raise ReloadPreparationError("invalid_implementation", "Implementation must define async execute")
                        staged = captured.instances.get(skill_id)
                        if staged is not None and staged is not implementation:
                            raise ReloadPreparationError("invalid_implementation", "Constructor registered a different implementation")
                except BaseException as exc:
                    if sys.modules.get(name) is module:
                        sys.modules.pop(name, None)
                    if isinstance(exc, ReloadPreparationError):
                        raise
                    if isinstance(exc, Exception):
                        raise ReloadPreparationError("implementation_failed", "Selected Python implementation failed to load or construct") from exc
                    raise
        return ReloadCandidate(self, skill_id, manifest, tools, implementation, not first_party,
                               module, terms, source_path, first_party, generation,
                               previous_manifest, previous_tools, previous_implementation,
                               previous_module, previous_terms)

    def publish_reload(self, candidate: ReloadCandidate) -> tuple[bool, str, str]:
        """Compare-and-publish in one no-await section on the owning loop.

        This does not make arbitrary thread readers or plugin import effects
        transactional, nor does it bind existing reviews to a code hash.
        """
        from skills.impl import SKILL_IMPLEMENTATIONS, get_implementation

        skill_id = candidate.skill_id
        try:
            if candidate.registry is not self or candidate.consumed:
                raise ReloadPreparationError("conflict", "Reload candidate has already been consumed")
            current_manifest = self.skills.get(skill_id)
            current_terms = current_manifest.model_dump_json() if current_manifest is not None else None
            if (self.generation != candidate.generation or current_manifest is not candidate.previous_manifest
                    or current_terms != candidate.previous_manifest_terms
                    or self._tool_cache.get(skill_id) is not candidate.previous_tools
                    or get_implementation(skill_id) is not candidate.previous_implementation
                    or sys.modules.get(f"feral_skill_{skill_id}") is not candidate.previous_module):
                raise ReloadPreparationError("conflict", "Live registry changed while replacement was being prepared")
            selected, first_party = self._select_source(skill_id)
            if (selected, first_party) != (candidate.source_path, candidate.first_party):
                raise ReloadPreparationError("conflict", "Selected package source changed during preparation")
            for path, data in candidate.source_terms:
                limit = 1024 * 1024 if path.suffix == ".json" else 2 * 1024 * 1024
                if self._source_bytes(path, limit, optional=data is None) != data:
                    raise ReloadPreparationError("conflict", "Selected package bytes changed during preparation")
        except ReloadPreparationError as exc:
            candidate.discard()
            return False, exc.code, str(exc)
        if candidate.replace_implementation:
            if candidate.implementation is None:
                SKILL_IMPLEMENTATIONS.pop(skill_id, None)
            else:
                SKILL_IMPLEMENTATIONS[skill_id] = candidate.implementation
            stable = f"feral_skill_{skill_id}"
            previous = candidate.previous_module
            if candidate.module is None:
                sys.modules.pop(stable, None)
            else:
                sys.modules[stable] = candidate.module
            if (previous is not None and previous.__name__.startswith("_feral_skill_staging_")
                    and sys.modules.get(previous.__name__) is previous):
                sys.modules.pop(previous.__name__, None)
        self.skills[skill_id] = candidate.manifest
        self._tool_cache[skill_id] = candidate.tools
        self._generation += 1
        self._reload_metadata[skill_id] = {
            "refresh_kind": "manifest_inventory" if candidate.first_party else "package",
            "implementation_ready": (candidate.implementation is not None
                                     and type(candidate.implementation).execute is not BaseSkill.execute
                                     and inspect.iscoroutinefunction(candidate.implementation.execute)),
        }
        candidate.consumed = True
        self._auto_create_routines(candidate.manifest)
        return True, "", ""

    def reload_skill_detail(self, skill_id: str) -> tuple[bool, str, str]:
        try:
            candidate = self.prepare_reload(skill_id)
        except ReloadPreparationError as exc:
            return False, exc.code, str(exc)
        except Exception:
            return False, "unloadable", "Selected replacement could not be prepared"
        return self.publish_reload(candidate)

    async def reload_skill_detail_async(
        self, skill_id: str, *, publish_guard: Callable[[], bool] | None = None,
    ) -> tuple[bool, str, str]:
        """Cancellation cannot publish a worker-thread candidate that finishes late."""
        preparation = asyncio.create_task(asyncio.to_thread(self.prepare_reload, skill_id))
        self._reload_preparations.add(preparation)
        preparation.add_done_callback(self._reload_preparations.discard)
        try:
            candidate = await asyncio.shield(preparation)
        except asyncio.CancelledError:
            def discard_late(task: asyncio.Task[ReloadCandidate]) -> None:
                if not task.cancelled():
                    try:
                        task.result().discard()
                    except Exception as exc:
                        logger.warning("Cancelled reload preparation failed (%s)", type(exc).__name__)
            preparation.add_done_callback(discard_late)
            raise
        except ReloadPreparationError as exc:
            return False, exc.code, str(exc)
        except Exception:
            return False, "unloadable", "Selected replacement could not be prepared"
        if publish_guard is not None:
            try:
                owner_matches = publish_guard() is True
            except Exception as exc:
                logger.warning("Reload owner check failed (%s)", type(exc).__name__)
                owner_matches = False
            if not owner_matches:
                candidate.discard()
                return False, "conflict", "Live skill registry changed during preparation"
        return self.publish_reload(candidate)

    def _reimport_dynamic_impl(self, skill_dir: Path, skill_id: str) -> bool:
        return self._try_load_dynamic_impl(skill_dir, skill_id)

    def get_skill(self, skill_id: str) -> BaseSkill | None:
        """Return backing only for a registered manifest; lazy loads use staging."""
        from skills.impl import get_implementation
        if skill_id not in self.skills:
            return None
        implementation = get_implementation(skill_id)
        if implementation is not None:
            return implementation
        if skill_id in self._lazy_attempted:
            return None
        self._lazy_attempted.add(skill_id)
        directory = feral_home() / "skills" / skill_id
        if directory.is_dir():
            self._try_load_dynamic_impl(directory, skill_id)
            return get_implementation(skill_id)
        return None

    def get_all_tools(self) -> list[dict]:
        """Every tool from every registered skill, in LLM tool format.

        This is the INVENTORY, and it stays the inventory: a skill the
        operator has not connected yet is still installed, and
        ``GET /api/tools``, the Skills page and the MCP projection all
        have to keep saying so.

        Callers assembling the list a model will be offered pass the
        result through ``skills.availability.filter_unavailable_tools``,
        which withholds the skills whose prerequisite is absent (no key,
        no OAuth, no Docker, no robot: 79 of these 266 schemas on the
        operator's brain). Applied by the caller rather than here because
        the two questions are genuinely different.
        """
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
        """Get LLM tool definitions for a subset of skills.

        Inventory, like :meth:`get_all_tools`. See its docstring for
        where the availability gate is applied instead.
        """
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
                # ``is not None``, not truthiness. A declared default of
                # ``""``, ``0`` or ``false`` is a real default, and
                # ``if param.default:`` dropped it from the schema
                # entirely, eight shipped params (all empty-string
                # defaults, e.g. calendar_google__create_event.description
                # and smart_home_hue__get_entities.domain) were affected,
                # and the model was shown an optional param with no stated
                # default at all.
                #
                # Coerced for the same reason ``_schema_hint_for_endpoint``
                # coerces: ``EndpointParam.default`` is ``Optional[str]``,
                # so an ``integer`` param's default reached the model as
                # ``"7"`` and a ``boolean`` param's as the always-truthy
                # string ``"false"``. 59 of the 89 shipped defaults declare
                # a non-string type. ``SkillExecutor._apply_param_defaults``
                # injects the same coerced value at dispatch, so the
                # advertised contract and the executed one now agree.
                if param.default is not None:
                    from agents.tool_dispatch_validator import _coerce_default

                    prop["default"] = _coerce_default(param)
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
