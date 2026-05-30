"""
FERAL GenUI AppManifest — the third-party app contract.

A *GenUI app* is orthogonal to a `SkillManifest`:

* **Skills** expose *tools* (endpoints) the agent can call. The agent
  decides when to call them; the UI (if any) is assembled via GenUI on
  the tool result.
* **Apps** expose *branded surfaces* the user navigates. The publisher
  supplies (a) brand, (b) data schemas, (c) interaction rules, and
  optionally (d) authored SDUI surface trees. The user's local agent
  fills authored surfaces with live data; for surfaces the publisher
  doesn't ship, the agent generates SDUI from the publisher's rules
  and caches the result per-user. Every user-initiated event flows
  back over ``ui_event`` scoped by ``app_id``.

This module defines the Pydantic contract and a validator that a
``feral app validate`` CLI or the registry's publish handler can run
against any submitted bundle. No runtime side-effects here — see
`feral-core/agents/app_registry.py` for the install path and
`feral-core/agents/hybrid_genui.py` for the render path.

Key invariants the validator enforces
-------------------------------------
* ``entry_surface_id`` must exist in ``surfaces``.
* Every ``authored`` / ``hybrid`` surface must carry a non-empty
  ``template_root``.
* Every ``generated`` surface must carry a non-empty
  ``generation_prompt`` and the app must declare non-empty
  ``interactions`` so the LLM has a style contract.
* Every ``action_id`` referenced inside a surface's ``template_root``
  must be declared in that surface's ``action_contract``. This is the
  primary guard against drift — a button that isn't in the contract
  will be rejected at publish time.
* ``data_schemas`` referenced by an action's ``value_schema_ref`` must
  exist.
* ``app_id`` must be a DNS-safe slug (lowercase letters, digits,
  ``-``), 3–64 chars, so ``/apps/<app_id>`` route segments don't need
  escaping.

This file is hand-read by publishers and by the LLM generator. Keep
the docstrings and field descriptions clear.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from models.skill_manifest import BrandProfile


APP_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
SEMVER_LIKE_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")

SurfaceKind = Literal["authored", "generated", "hybrid"]
ActionHandler = Literal[
    "skill_call",       # dispatch to a FERAL skill endpoint
    "app_event",        # dispatch to an in-app handler (see `app_events`)
    "navigate",         # navigate to another surface in this app
    "patch",            # mutate the current data_model w/ a JSON-Patch
    "close",            # close the current surface / modal
]

NotificationPriority = Literal["low", "normal", "high", "critical"]


def _parse_semver_like(value: str) -> tuple[int, int, int]:
    """Parse `major.minor` or `major.minor.patch` into a numeric tuple."""
    if not SEMVER_LIKE_RE.match(value):
        raise ValueError(
            f"version {value!r} must match 'major.minor' or 'major.minor.patch'"
        )
    parts = [int(p) for p in value.split(".")]
    if len(parts) == 2:
        parts.append(0)
    return parts[0], parts[1], parts[2]


# ----------------------------------------------------------------------
# Action contract
# ----------------------------------------------------------------------


class ActionSpec(BaseModel):
    """One `action_id` a surface is allowed to emit.

    Clients must not send any `ui_event` with an `action_id` that
    isn't declared here. The dispatcher in `AppRegistry` refuses the
    event and returns a 400, so a misbehaving (or compromised) client
    can't invoke arbitrary skill endpoints by guessing action names.
    """

    action_id: str = Field(..., min_length=1, max_length=120)
    handler: ActionHandler = "app_event"
    description: str = ""
    # When handler == "skill_call", this is the skill endpoint ref
    # (e.g. "calendar_google/list_events"). When handler == "navigate",
    # this is the target `surface_id`.
    target: str = ""
    # Optional JSON Schema the client's event `value` is validated
    # against. Use a `$ref` into `data_schemas` or an inline schema.
    value_schema: Optional[dict[str, Any]] = None
    value_schema_ref: Optional[str] = None
    # If True, the brain routes the event through the user-confirmation
    # path (autonomy_tier=user_confirm equivalent) before executing.
    requires_confirmation: bool = False

    @field_validator("action_id")
    @classmethod
    def _valid_action_id(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,119}$", v):
            raise ValueError(
                f"action_id {v!r} must match [a-zA-Z][a-zA-Z0-9_.:-]{{0,119}}"
            )
        return v


# ----------------------------------------------------------------------
# Data schemas
# ----------------------------------------------------------------------


class DataSchemaSpec(BaseModel):
    """A named JSON Schema the app uses for both templates + events.

    Schemas are stored in `AppManifest.data_schemas` and referenced by
    id from `SurfaceSpec.data_schema_ref` (for the surface-level data
    binding shape) and `ActionSpec.value_schema_ref` (for event
    payloads). The validator resolves `$ref` strings of the form
    `#/data_schemas/<id>` at publish time.

    The Pydantic field is named ``schema`` because that's the
    publisher-facing key in manifest YAML/JSON. Pydantic warns about
    the BaseModel.schema() shadowing — silence it with model_config
    so the warning doesn't pollute test output.
    """

    model_config = {"protected_namespaces": ()}

    schema_id: str = Field(..., min_length=1)
    schema: dict[str, Any]
    description: str = ""


# ----------------------------------------------------------------------
# Interaction rules (publisher style contract for the LLM)
# ----------------------------------------------------------------------


class InteractionRules(BaseModel):
    """Publisher's style + behaviour guidance for agent-generated surfaces.

    This is the contract the LLM sees in the system prompt when it
    has to generate a missing surface. The rules are strict enough
    that the same publisher gets consistent UI across different users
    and brains.

    Nothing here controls authored templates — they render exactly as
    shipped. These rules only govern surfaces with kind ``generated``
    or ``hybrid`` (when the hybrid branch falls through to the LLM).
    """

    button_style_priority: list[str] = Field(
        default_factory=lambda: ["primary", "secondary", "ghost"],
        description="Preferred button style order for the LLM.",
    )
    destructive_confirmation_required: bool = Field(
        default=True,
        description=(
            "Surfaces with destructive actions (cancel ride, delete "
            "thread, etc.) must show a confirm modal before emitting "
            "the ui_event."
        ),
    )
    list_render_preference: Literal["list", "grid", "auto"] = "auto"
    accessibility_notes: list[str] = Field(default_factory=list)
    # Free-form guidance injected verbatim into the LLM system prompt
    # so the publisher can say "never show raw IDs", "always localise
    # timestamps", "use only currency from the user's locale", etc.
    prose_guidance: str = ""
    # Optional hard deny-list of SDUI component types. Useful for
    # publishers that want to force a specific aesthetic (e.g. no
    # Tables on a messaging app).
    forbidden_components: list[str] = Field(default_factory=list)

    def to_system_prompt_chunk(self) -> str:
        """Render these rules as a LLM system-prompt snippet."""
        lines: list[str] = ["## App interaction rules"]
        if self.prose_guidance:
            lines.append(self.prose_guidance.strip())
        if self.destructive_confirmation_required:
            lines.append(
                "Any destructive action (delete/cancel) MUST be gated by a "
                "Modal confirmation. Never emit a destructive action_id "
                "from a plain Button."
            )
        if self.button_style_priority:
            lines.append(
                f"Preferred button styles, in order: "
                f"{', '.join(self.button_style_priority)}."
            )
        if self.list_render_preference != "auto":
            lines.append(
                f"Default list rendering: {self.list_render_preference}."
            )
        if self.forbidden_components:
            lines.append(
                "Never use these SDUI components: "
                + ", ".join(self.forbidden_components)
            )
        if self.accessibility_notes:
            lines.append("Accessibility notes:")
            for note in self.accessibility_notes:
                lines.append(f"- {note}")
        return "\n".join(lines)


# ----------------------------------------------------------------------
# Surface spec
# ----------------------------------------------------------------------


class SurfaceSpec(BaseModel):
    """One navigable screen inside the app.

    * ``authored`` — publisher ships the full SDUI tree in
      ``template_root``. `$data.*` placeholders hydrate at render time
      with live data. No LLM call ever.
    * ``generated`` — no template; the agent generates it once from
      ``generation_prompt`` + app ``interactions`` + ``data_schema_ref``,
      then caches the result per-user. Regenerated only if
      ``schema_version`` changes or the user invalidates the cache.
    * ``hybrid`` — authored template is the default; the agent may
      regenerate a personalised version on customisation signals
      (e.g. user pinned/reordered). Always falls back to the authored
      template if the LLM is unavailable.
    """

    surface_id: str = Field(..., min_length=1)
    title: str = ""
    kind: SurfaceKind = "authored"
    template_root: Optional[dict[str, Any]] = None
    generation_prompt: str = ""
    # Optional short summary surfaced in the app's nav + used by the
    # LLM when it has to explain where a user is.
    description: str = ""
    # Reference to a DataSchemaSpec by id. The surface-level data
    # binding is validated against this schema at render time.
    data_schema_ref: Optional[str] = None
    # Increment to invalidate cached agent-generated renders for this
    # surface. Part of the per-user cache key.
    schema_version: int = Field(
        default=1,
        ge=1,
        description=(
            "Surface contract version. Bump this when action ids, "
            "bindings, or required shape changes."
        ),
    )
    action_contract: list[ActionSpec] = Field(default_factory=list)

    def action_ids(self) -> set[str]:
        return {a.action_id for a in self.action_contract}


# ----------------------------------------------------------------------
# Supplementary specs
# ----------------------------------------------------------------------


class JobSpec(BaseModel):
    """A background job the app runs (e.g. poll, notification dispatch)."""

    job_id: str = Field(..., min_length=1)
    description: str = ""
    cron: str = ""
    handler: str = ""


class NotificationSchema(BaseModel):
    """How the app emits notifications into FERAL's proactive pipeline."""

    enabled: bool = True
    default_priority: NotificationPriority = "normal"
    channels: list[str] = Field(
        default_factory=list,
        description="Optional messaging channels to also mirror to.",
    )
    # Optional deep-link surface_id a notification tap should open.
    default_deep_link_surface_id: Optional[str] = None


class ContractSpec(BaseModel):
    """Publisher-declared contract versions for compatibility checks.

    `manifest_schema_version` governs the AppManifest shape.
    `a2ui_version` pins the message/render contract expected by this app.
    """

    manifest_schema_version: str = Field(
        default="1.0",
        description="Publisher manifest contract version (major.minor[.patch]).",
    )
    a2ui_version: str = Field(
        default="1.0",
        description="A2UI contract version expected by this app bundle.",
    )
    compatibility_mode: Literal["strict", "backward_compatible"] = Field(
        default="backward_compatible",
        description="Whether runtime should allow additive contract drift.",
    )
    notes: str = ""

    @field_validator("manifest_schema_version", "a2ui_version")
    @classmethod
    def _valid_contract_version(cls, v: str) -> str:
        _parse_semver_like(v)
        return v


# ----------------------------------------------------------------------
# Root manifest
# ----------------------------------------------------------------------


class AppManifest(BaseModel):
    """The complete FERAL GenUI app definition.

    This is what a publisher writes. The validator enforces every
    cross-reference so a misconfigured bundle is rejected at publish
    time, never surprising a user later.
    """

    app_id: str = Field(..., description="DNS-safe slug, 3-64 chars.")
    version: str = "1.0.0"
    author: str = ""
    description: str = ""
    contract: ContractSpec = Field(default_factory=ContractSpec)

    brand: BrandProfile
    # Permissions accept either the legacy opaque-tag list shape
    # (``["calendar:read"]``) or the structured dict shape
    # (``{"network": [...], "justification": "..."}``) that the
    # GenUI sandbox + signing layer () reads. Both round-trip;
    # ``list(manifest.permissions)`` still works on both shapes
    # because dict iteration yields its keys.
    permissions: list[str] | dict[str, Any] = Field(default_factory=list)
    data_schemas: list[DataSchemaSpec] = Field(default_factory=list)
    surfaces: list[SurfaceSpec] = Field(default_factory=list)
    interactions: InteractionRules = Field(default_factory=InteractionRules)
    # Optional runtime dependencies on FERAL skills. The app install path
    # resolves these at install-time so app actions don't fail later due to
    # missing tool surfaces.
    skill_dependencies: list[str] = Field(default_factory=list)
    entry_surface_id: str = Field(...)
    background_jobs: list[JobSpec] = Field(default_factory=list)
    notifications: NotificationSchema = Field(default_factory=NotificationSchema)

    # Ed25519 signature, sha256, publisher fingerprint; filled by the
    # registry at publish time, ignored during local install.
    signatures: dict[str, Any] = Field(default_factory=dict)

    @field_validator("app_id")
    @classmethod
    def _valid_app_id(cls, v: str) -> str:
        if not APP_ID_RE.match(v):
            raise ValueError(
                f"app_id {v!r} must be a DNS-safe slug "
                "(lowercase letters, digits, '-'; 3-64 chars; "
                "must start with a letter)."
            )
        return v

    @model_validator(mode="after")
    def _validate_cross_refs(self) -> "AppManifest":
        manifest_major, _, _ = _parse_semver_like(self.contract.manifest_schema_version)
        if manifest_major != 1:
            raise ValueError(
                "unsupported contract.manifest_schema_version: "
                f"{self.contract.manifest_schema_version!r}. "
                "This runtime currently supports major version 1 only."
            )

        surface_ids = {s.surface_id for s in self.surfaces}
        if not surface_ids:
            raise ValueError("AppManifest must declare at least one surface")
        if self.entry_surface_id not in surface_ids:
            raise ValueError(
                f"entry_surface_id {self.entry_surface_id!r} is not in "
                f"declared surfaces {sorted(surface_ids)}"
            )

        schema_ids = {s.schema_id for s in self.data_schemas}

        for surface in self.surfaces:
            if surface.kind in ("authored", "hybrid"):
                if not surface.template_root:
                    raise ValueError(
                        f"Surface {surface.surface_id!r} has kind "
                        f"{surface.kind!r} but is missing template_root"
                    )
            if surface.kind == "generated" and not surface.generation_prompt:
                raise ValueError(
                    f"Surface {surface.surface_id!r} has kind 'generated' "
                    "but is missing generation_prompt"
                )
            if surface.kind in ("generated", "hybrid"):
                if not self.interactions.prose_guidance and not self.interactions.accessibility_notes and not self.interactions.forbidden_components:
                    # We allow defaults, but explicitly reject an entirely
                    # empty InteractionRules for generated surfaces — the
                    # LLM needs *some* style contract.
                    if not self.interactions.button_style_priority:
                        raise ValueError(
                            f"Surface {surface.surface_id!r} relies on the "
                            "agent generator but `interactions` is empty."
                        )
            if surface.data_schema_ref and surface.data_schema_ref not in schema_ids:
                raise ValueError(
                    f"Surface {surface.surface_id!r} references "
                    f"data_schema {surface.data_schema_ref!r} which "
                    f"is not declared in data_schemas"
                )

            declared_action_ids = surface.action_ids()
            for action_id_in_template in _collect_action_ids(surface.template_root):
                if action_id_in_template not in declared_action_ids:
                    raise ValueError(
                        f"Surface {surface.surface_id!r} template references "
                        f"action_id {action_id_in_template!r} which is not "
                        "declared in action_contract"
                    )
            for action in surface.action_contract:
                if action.handler == "navigate" and action.target:
                    if action.target not in surface_ids:
                        raise ValueError(
                            f"Action {action.action_id!r} on surface "
                            f"{surface.surface_id!r} navigates to "
                            f"unknown surface {action.target!r}"
                        )
                if action.value_schema_ref:
                    ref_id = action.value_schema_ref
                    if ref_id.startswith("#/data_schemas/"):
                        ref_id = ref_id.split("/", 2)[-1]
                    if ref_id not in schema_ids:
                        raise ValueError(
                            f"Action {action.action_id!r} references "
                            f"value_schema {action.value_schema_ref!r} "
                            "which is not declared in data_schemas"
                        )

        if self.notifications.default_deep_link_surface_id:
            if self.notifications.default_deep_link_surface_id not in surface_ids:
                raise ValueError(
                    f"notifications.default_deep_link_surface_id "
                    f"{self.notifications.default_deep_link_surface_id!r} "
                    f"is not in declared surfaces"
                )

        job_ids = [j.job_id for j in self.background_jobs]
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("background_jobs contains duplicate job_id values")
        schema_id_list = [s.schema_id for s in self.data_schemas]
        if len(schema_id_list) != len(set(schema_id_list)):
            raise ValueError("data_schemas contains duplicate schema_id values")
        surface_id_list = [s.surface_id for s in self.surfaces]
        if len(surface_id_list) != len(set(surface_id_list)):
            raise ValueError("surfaces contains duplicate surface_id values")
        dep_ids = [str(dep) for dep in self.skill_dependencies]
        if len(dep_ids) != len(set(dep_ids)):
            raise ValueError("skill_dependencies contains duplicate ids")

        return self

    # --------------------------------------------------------------
    # Convenience helpers
    # --------------------------------------------------------------

    def get_surface(self, surface_id: str) -> Optional[SurfaceSpec]:
        for s in self.surfaces:
            if s.surface_id == surface_id:
                return s
        return None

    def get_data_schema(self, schema_id: str) -> Optional[DataSchemaSpec]:
        for s in self.data_schemas:
            if s.schema_id == schema_id:
                return s
        return None


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _collect_action_ids(node: Any) -> Iterable[str]:
    """Yield every `action_id` literal found inside a template tree.

    Does NOT resolve `$data.*` references — only literal strings, since
    runtime bindings don't appear until a render fills them in. The
    goal is to catch static drift between the template and the
    declared action contract.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "action_id" and isinstance(value, str):
                yield value
            else:
                yield from _collect_action_ids(value)
    elif isinstance(node, list):
        for item in node:
            yield from _collect_action_ids(item)
