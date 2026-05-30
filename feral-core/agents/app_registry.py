"""AppRegistry — SQLite-backed index of installed third-party GenUI apps.

Keeps one row per installed app + an on-disk directory under
``~/.feral/apps/<app_id>/`` holding the manifest and any static
bundle assets (logos, pre-rendered surface defaults, readmes).

Install sources
---------------
1. **Local directory** — ``install_from_dir(path)``. The directory must
   contain a ``manifest.yaml`` or ``manifest.json`` that parses into an
   :class:`models.app_manifest.AppManifest`. Assets are copied, not
   referenced, so a subsequent edit in the source tree doesn't change
   the installed surface.
2. **Registry download** — ``install_from_registry(item_id)``.
   Delegates to the existing ``MarketplaceClient`` tarball download
   path and then calls :meth:`install_from_dir` on the unpacked
   content. See Commit 6 for the CLI-side publish path.

Data lifecycle
--------------
The registry never holds live data for an app. It exposes
:meth:`open_surface` which:

1. Looks up the surface spec in the stored manifest.
2. Asks the :class:`HybridGenerator` to render it with whatever data
   binding the caller provides.
3. Returns a plain SDUI tree ready to ship over the ``sdui`` wire
   message.

Every action dispatched from the rendered surface must flow through
:meth:`handle_app_action`, which validates the action against the
surface's declared contract before invoking the orchestrator. That
contract + per-user cache are the two invariants Commit 4 relies on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from base64 import b64decode
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, unquote

from pydantic import ValidationError

from genui.manifest_signing import SignedManifest, verify as verify_signed_manifest
from genui.permissions_policy import PolicyViolation, enforce_install_policy
from models.app_manifest import ActionSpec, AppManifest, SurfaceSpec

try:  # pragma: no cover - optional dependency in constrained test envs
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

try:  # pragma: no cover - optional dependency in constrained test envs
    from nacl.encoding import HexEncoder  # type: ignore
    from nacl.exceptions import BadSignatureError  # type: ignore
    from nacl.signing import VerifyKey  # type: ignore
except Exception:  # pragma: no cover
    HexEncoder = None  # type: ignore
    BadSignatureError = Exception  # type: ignore
    VerifyKey = None  # type: ignore

logger = logging.getLogger("feral.app_registry")


DEFAULT_APPS_DIR_NAME = "apps"
DEFAULT_APPS_DB_NAME = "apps.db"

SIGNED_MANIFEST_FILENAMES = ("manifest.signed.json",)
REGISTRY_DEFAULT_URL = "https://registry.feral.sh"
MANIFEST_FILENAMES = ("manifest.signed.json", "manifest.json", "manifest.yaml", "manifest.yml")


@dataclass
class InstalledApp:
    app_id: str
    version: str
    manifest: AppManifest
    install_dir: Path
    installed_at: float


class AppRegistryError(RuntimeError):
    """Raised when install/open fails in a way the caller should see."""


class UnverifiedManifestError(AppRegistryError):
    """Raised when ``install_app`` refuses to install an unsigned/invalid bundle.

    The exception message is meant to be surfaced verbatim to the
    publisher / installer so they understand why the install was
    refused (e.g. missing signature, tampered manifest, key mismatch).
    """


class UnapprovedRegistryItemError(AppRegistryError):
    """Raised when the registry exposes an item that is not approved+public.

    The acceptance gate on the registry side already returns 404 for
    non-approved items to anonymous callers, so this exception is
    primarily a defence-in-depth layer for cases where a misconfigured
    or out-of-band response leaks the moderation state to the install
    path. It is also raised when an explicit override is required but
    not supplied. The message is safe to show to users.
    """


class AppRegistry:
    """SQLite-backed store of installed FERAL GenUI apps."""

    _DDL = """
    CREATE TABLE IF NOT EXISTS installed_apps (
        app_id        TEXT PRIMARY KEY,
        version       TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        install_dir   TEXT NOT NULL,
        installed_at  REAL NOT NULL
    );
    """

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        apps_dir: str | Path | None = None,
        hybrid_generator: Optional["HybridGenerator"] = None,
    ):
        self._db_path = str(db_path) if db_path else ":memory:"
        self._apps_dir = Path(apps_dir) if apps_dir else Path("/tmp/feral-apps-mem")
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._apps_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self._DDL)
        self._hybrid: Optional[HybridGenerator] = hybrid_generator

    @property
    def apps_dir(self) -> Path:
        return self._apps_dir

    def set_hybrid_generator(self, hybrid: "HybridGenerator") -> None:
        self._hybrid = hybrid

    # ------------------------------------------------------------------
    # Install / uninstall
    # ------------------------------------------------------------------

    def install_from_dir(self, source_dir: str | Path, *, overwrite: bool = True) -> InstalledApp:
        """Install an app from a local directory containing a manifest + assets.

        Copies everything under *source_dir* into
        ``<apps_dir>/<app_id>/`` so a later edit to the source tree
        doesn't mutate the installed bundle.
        """
        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir():
            raise AppRegistryError(f"not a directory: {source}")
        manifest = _load_manifest_from_dir(source)
        app_id = manifest.app_id

        existing = self.get(app_id)
        if existing and not overwrite:
            raise AppRegistryError(f"app {app_id!r} already installed")

        dest = self._apps_dir / app_id
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        _copy_tree(source, dest)

        now = time.time()
        manifest_blob = manifest.model_dump_json()
        self._conn.execute(
            """INSERT INTO installed_apps
                (app_id, version, manifest_json, install_dir, installed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(app_id) DO UPDATE SET
                    version       = excluded.version,
                    manifest_json = excluded.manifest_json,
                    install_dir   = excluded.install_dir,
                    installed_at  = excluded.installed_at""",
            (app_id, manifest.version, manifest_blob, str(dest), now),
        )
        self._conn.commit()
        logger.info("Installed app %s@%s at %s", app_id, manifest.version, dest)
        return InstalledApp(
            app_id=app_id,
            version=manifest.version,
            manifest=manifest,
            install_dir=dest,
            installed_at=now,
        )

    def install_app(
        self,
        source_dir: str | Path,
        *,
        allow_unsigned: bool = False,
        user_high_trust: bool = False,
        overwrite: bool = True,
        vault: Optional[Any] = None,
        supervisor: Optional[Any] = None,
        audit_callback: Optional[Callable[[dict], None]] = None,
    ) -> InstalledApp:
        """Install with manifest signing + permissions policy enforced.

        This is the path the brain's ``/api/apps/install`` REST handler
        and the ``feral app install`` CLI both go through. It wraps
        :meth:`install_from_dir` with three guards:

        1. Looks for ``manifest.signed.json`` next to the manifest.
        2. Verifies the Ed25519 signature via
           :func:`genui.manifest_signing.verify`. A vault, when
           provided, pins the public key to the publisher's known
           ``key_id`` (so a perfectly valid signature from the *wrong*
           publisher key is still rejected with ``key_mismatch``).
        3. Runs :func:`genui.permissions_policy.enforce_install_policy`
           — most notably refuses ``permissions.network=["*"]`` unless
           the manifest is signed AND the user opted in via
           ``user_high_trust=True`` AND the publisher supplied a
           justification.

        Audit:
          Every decision (verified install, unsigned install, refused
          install) is forwarded to the supervisor (when one is wired)
          and to ``audit_callback`` (used by tests). The
          :data:`logger` always receives a structured ``"unsigned_install"``
          / ``"verified_install"`` / ``"signature_invalid"`` line so
          the audit trail survives even when no supervisor is wired
          at install time.
        """
        source = Path(source_dir).expanduser().resolve()
        if not source.is_dir():
            raise AppRegistryError(f"not a directory: {source}")

        signed_path = _find_signed_manifest(source)
        signed: Optional[SignedManifest] = None
        verification_reason: Optional[str] = None

        if signed_path is not None:
            try:
                signed = _load_signed_manifest(signed_path)
            except Exception as exc:
                self._audit_install(
                    "signature_invalid",
                    source=source,
                    detail={"reason": f"envelope_unreadable:{exc}"},
                    supervisor=supervisor,
                    callback=audit_callback,
                )
                if not allow_unsigned:
                    raise UnverifiedManifestError(
                        f"signed manifest envelope unreadable: {exc}"
                    ) from exc
            else:
                expected_pk = None
                if vault is not None:
                    expected_pk = _vault_lookup_key(vault, signed.key_id)
                ok, reason = verify_signed_manifest(
                    signed, expected_public_key_b64=expected_pk
                )
                if not ok:
                    verification_reason = reason
                    self._audit_install(
                        "signature_invalid",
                        source=source,
                        detail={"reason": reason, "key_id": signed.key_id},
                        supervisor=supervisor,
                        callback=audit_callback,
                    )
                    if not allow_unsigned:
                        raise UnverifiedManifestError(
                            f"signature verification failed: {reason}"
                        )

        if signed is None:
            if not allow_unsigned:
                self._audit_install(
                    "unsigned_install_refused",
                    source=source,
                    detail={"reason": "no_signed_manifest"},
                    supervisor=supervisor,
                    callback=audit_callback,
                )
                raise UnverifiedManifestError(
                    "manifest is unsigned (no manifest.signed.json found) "
                    "and allow_unsigned=False"
                )
            self._audit_install(
                "unsigned_install",
                source=source,
                detail={"reason": "allow_unsigned=True"},
                supervisor=supervisor,
                callback=audit_callback,
            )

        # Resolve the manifest dict the policy gate + install both see.
        if signed is not None:
            manifest_dict = dict(signed.manifest)
        else:
            manifest_dict = _load_manifest_dict(source)

        try:
            enforce_install_policy(
                manifest_dict,
                allow_unsigned=allow_unsigned,
                user_high_trust=user_high_trust,
            )
        except PolicyViolation as exc:
            self._audit_install(
                "policy_refused",
                source=source,
                detail={"reason": str(exc)},
                supervisor=supervisor,
                callback=audit_callback,
            )
            raise

        # Stage to a temp dir whose manifest.json IS the verified one,
        # then delegate to install_from_dir for the actual disk copy +
        # SQLite row write.
        with tempfile.TemporaryDirectory(prefix="feral-app-stage-") as staging:
            staging_path = Path(staging)
            for entry in source.iterdir():
                if entry.name in SIGNED_MANIFEST_FILENAMES:
                    continue
                if entry.is_dir():
                    shutil.copytree(entry, staging_path / entry.name)
                else:
                    shutil.copy2(entry, staging_path / entry.name)
            (staging_path / "manifest.json").write_text(
                json.dumps(manifest_dict)
            )
            for legacy in ("manifest.yaml", "manifest.yml"):
                stale = staging_path / legacy
                if stale.exists():
                    stale.unlink()
            installed = self.install_from_dir(staging_path, overwrite=overwrite)

        if signed is not None:
            try:
                shutil.copy2(signed_path, installed.install_dir / signed_path.name)
            except OSError:
                pass

        self._audit_install(
            "verified_install" if signed is not None and verification_reason is None
            else "unsigned_install",
            source=source,
            detail={
                "app_id": installed.app_id,
                "version": installed.version,
                "key_id": signed.key_id if signed is not None else None,
                "signature_reason": verification_reason,
            },
            supervisor=supervisor,
            callback=audit_callback,
        )
        return installed

    def install_from_registry(
        self,
        registry_id: str,
        *,
        registry_url: Optional[str] = None,
        allow_unsigned: bool = False,
        user_high_trust: bool = False,
        overwrite: bool = True,
        supervisor: Optional[Any] = None,
        audit_callback: Optional[Callable[[dict], None]] = None,
        internal_override: bool = False,
    ) -> InstalledApp:
        """Install an app bundle from the remote registry.

        Expected registry response shape:
            GET /api/v1/item/{registry_id} -> {
                "kind": "app",
                "download_url": "...",
                "sha256": "<hex>",
                "signature_b64": "...",          # optional when allow_unsigned
                "publisher_pubkey_hex": "...",   # optional when allow_unsigned
                "status": "approved",            # acceptance gate
                "visibility": "public",          # acceptance gate
            }

        Acceptance gate (defence in depth): the registry already 404s
        non-approved items to anonymous callers, but if a response
        does include moderation metadata we additionally refuse any
        item that is not ``status=approved`` AND ``visibility=public``.
        Only a caller that explicitly passes ``internal_override=True``
        AND has set the env flag ``FERAL_INTERNAL_ALLOW_UNAPPROVED=1``
        can bypass this -- both must agree, so neither a bare env flag
        nor a stray API parameter alone can install pending bundles.

        The tarball SHA-256 is always enforced. Detached signature
        verification is enforced unless ``allow_unsigned=True``.
        """
        if not registry_id:
            raise AppRegistryError("registry_id is required")
        if httpx is None:
            raise AppRegistryError("httpx is required for registry installs")

        # Build the ordered list of candidate base URLs (primary +
        # fallbacks for networks that can't resolve our IPv6-only
        # canonical host). cli.publish.registry_base_urls is the
        # single source of truth so install/CLI/marketplace agree.
        try:
            from cli.publish import registry_base_urls
        except Exception:  # defensive: cli/publish optional in some configs
            registry_base_urls = None  # type: ignore[assignment]
        if registry_url is not None:
            bases = [registry_url.rstrip("/")]
        elif registry_base_urls is not None:
            bases = registry_base_urls()
        else:
            bases = [
                (os.environ.get("FERAL_REGISTRY_URL") or REGISTRY_DEFAULT_URL).rstrip("/")
            ]

        stage_root = Path(tempfile.mkdtemp(prefix="feral-app-registry-"))
        tarball = stage_root / "bundle.tar.gz"
        extract_root = stage_root / "extract"

        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:  # type: ignore[union-attr]
                item_resp = None
                base = bases[0]
                last_lookup_err: Exception | None = None
                last_status: int | None = None
                for candidate in bases:
                    try:
                        item_resp = client.get(f"{candidate}/api/v1/item/{registry_id}")
                    except Exception as exc:
                        last_lookup_err = exc
                        continue
                    if item_resp.status_code == 404:
                        # 404 is authoritative: the item really doesn't
                        # exist on this registry. Bail rather than try
                        # fallbacks (which would return the same 404).
                        base = candidate
                        break
                    if item_resp.status_code >= 400:
                        last_status = item_resp.status_code
                        item_resp = None
                        continue
                    base = candidate
                    break
                if item_resp is None:
                    if last_lookup_err is not None:
                        raise AppRegistryError(
                            f"registry lookup failed: {last_lookup_err}"
                        ) from last_lookup_err
                    raise AppRegistryError(
                        f"registry lookup failed ({last_status})"
                    )
                if item_resp.status_code == 404:
                    raise AppRegistryError(f"registry item {registry_id!r} not found")
                if item_resp.status_code >= 400:
                    raise AppRegistryError(
                        f"registry lookup failed ({item_resp.status_code}): {item_resp.text[:240]}"
                    )
                try:
                    item = item_resp.json()
                except Exception as exc:
                    raise AppRegistryError(
                        f"registry returned non-JSON metadata for {registry_id!r}"
                    ) from exc
                if not isinstance(item, dict):
                    raise AppRegistryError("registry item metadata must be a JSON object")

                kind = str(item.get("kind") or (item.get("manifest") or {}).get("kind") or "app").lower()
                if kind not in {"app", "genui_app", "genui-app"}:
                    raise AppRegistryError(
                        f"registry item {registry_id!r} is kind {kind!r}, expected 'app'"
                    )

                # Acceptance gate. Older registries that don't expose
                # ``status``/``visibility`` are treated as ``approved``/
                # ``public`` so we don't break clients pointing at a
                # legacy registry that has already vetted its items;
                # any registry that exposes the fields must show
                # approved + public to be installable.
                item_status = str(item.get("status") or "approved").lower()
                item_visibility = str(item.get("visibility") or "public").lower()
                if item_status != "approved" or item_visibility != "public":
                    env_allow = os.environ.get(
                        "FERAL_INTERNAL_ALLOW_UNAPPROVED", ""
                    ).strip().lower() in {"1", "true", "yes", "on"}
                    if not (internal_override and env_allow):
                        logger.info(
                            "app_install rejected_unapproved %s",
                            json.dumps(
                                {
                                    "registry_id": registry_id,
                                    "registry_url": base,
                                    "status": item_status,
                                    "visibility": item_visibility,
                                }
                            ),
                        )
                        raise UnapprovedRegistryItemError(
                            f"registry item {registry_id!r} is not yet approved "
                            f"for public install (status={item_status!r}, "
                            f"visibility={item_visibility!r}); wait for FERAL "
                            "org reviewers to approve this submission, or set "
                            "FERAL_INTERNAL_ALLOW_UNAPPROVED=1 and pass "
                            "internal_override=True for an internal-only "
                            "override"
                        )

                download_url = str(item.get("download_url") or "")
                if not download_url:
                    raise AppRegistryError("registry item is missing download_url")

                try:
                    with client.stream("GET", download_url) as bundle_resp:
                        if bundle_resp.status_code >= 400:
                            raise AppRegistryError(
                                f"bundle download failed ({bundle_resp.status_code}) from {download_url}"
                            )
                        with open(tarball, "wb") as f:
                            for chunk in bundle_resp.iter_bytes():
                                f.write(chunk)
                except AppRegistryError:
                    raise
                except Exception as exc:
                    raise AppRegistryError(f"bundle download failed: {exc}") from exc

            verified_signature = _verify_registry_bundle(
                item,
                tarball,
                allow_unsigned=allow_unsigned,
            )
            _safe_extract_tarball(tarball, extract_root)
            source_dir = _locate_manifest_source_dir(extract_root)
            manifest_dict = _load_manifest_dict(source_dir)
            enforce_install_policy(
                manifest_dict,
                allow_unsigned=not verified_signature,
                user_high_trust=user_high_trust,
            )
            installed = self.install_from_dir(source_dir, overwrite=overwrite)
            self._audit_install(
                "verified_install" if verified_signature else "unsigned_install",
                source=stage_root,
                detail={
                    "registry_id": registry_id,
                    "registry_url": base,
                    "app_id": installed.app_id,
                    "version": installed.version,
                },
                supervisor=supervisor,
                callback=audit_callback,
            )
            return installed
        except PolicyViolation:
            raise
        except AppRegistryError:
            raise
        except Exception as exc:
            raise AppRegistryError(f"registry install failed: {exc}") from exc
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)

    @staticmethod
    def _audit_install(
        event: str,
        *,
        source: Path,
        detail: dict[str, Any],
        supervisor: Optional[Any] = None,
        callback: Optional[Callable[[dict], None]] = None,
    ) -> None:
        record = {
            "event": event,
            "source": str(source),
            **detail,
        }
        logger.info("app_install %s %s", event, json.dumps(detail))
        if supervisor is not None and hasattr(supervisor, "record"):
            try:
                supervisor.record(
                    source="app_install",
                    kind=event,
                    actor="installer",
                    payload=record,
                    decision="allowed" if event in (
                        "verified_install", "unsigned_install"
                    ) else "denied",
                    detail=detail,
                )
            except Exception as exc:
                logger.debug("supervisor.record failed: %s", exc)
        if callback is not None:
            try:
                callback(record)
            except Exception as exc:
                logger.debug("audit_callback failed: %s", exc)

    def uninstall(self, app_id: str, *, purge_cache: bool = True) -> bool:
        existing = self.get(app_id)
        if existing is None:
            return False
        if existing.install_dir.exists():
            shutil.rmtree(existing.install_dir, ignore_errors=True)
        self._conn.execute("DELETE FROM installed_apps WHERE app_id = ?", (app_id,))
        self._conn.commit()
        if purge_cache and self._hybrid is not None:
            try:
                self._hybrid.purge_app_cache(app_id)
            except Exception as exc:
                logger.debug("purge_app_cache failed: %s", exc)
        return True

    def list(self) -> list[InstalledApp]:
        rows = self._conn.execute(
            "SELECT * FROM installed_apps ORDER BY installed_at DESC"
        ).fetchall()
        return [self._row_to_installed(r) for r in rows]

    def get(self, app_id: str) -> Optional[InstalledApp]:
        row = self._conn.execute(
            "SELECT * FROM installed_apps WHERE app_id = ?", (app_id,)
        ).fetchone()
        return self._row_to_installed(row) if row else None

    # ------------------------------------------------------------------
    # Surface access
    # ------------------------------------------------------------------

    async def open_surface(
        self,
        app_id: str,
        surface_id: str,
        *,
        session_id: str = "",
        data: Optional[dict] = None,
        user_fingerprint: str = "default",
        regenerate: bool = False,
    ) -> dict:
        """Return a ready-to-render SDUI tree for *surface_id*."""
        app = self.get(app_id)
        if app is None:
            raise AppRegistryError(f"app {app_id!r} not installed")
        surface = app.manifest.get_surface(surface_id)
        if surface is None:
            raise AppRegistryError(
                f"surface {surface_id!r} not declared on app {app_id!r}"
            )
        if self._hybrid is None:
            raise AppRegistryError("HybridGenerator not configured")
        tree = await self._hybrid.render(
            app_id=app_id,
            manifest=app.manifest,
            surface=surface,
            data=data or {},
            user_fingerprint=user_fingerprint,
            regenerate=regenerate,
            bundle_dir=app.install_dir,
        )
        return {
            "app_id": app_id,
            "surface_id": surface_id,
            "screen_id": self.build_screen_id(
                app_id=app_id,
                surface_id=surface_id,
                scope=session_id or user_fingerprint,
            ),
            "root": tree,
        }

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    def validate_action(
        self,
        app_id: str,
        surface_id: str,
        action_id: str,
        value: Any = None,
    ) -> ActionSpec:
        """Raise if action_id isn't in the surface's contract; otherwise
        return the matching spec.

        Used by :func:`feral-core/agents/ui_handlers.handle_ui_event`
        and by the /api/apps dispatch path.
        """
        app = self.get(app_id)
        if app is None:
            raise AppRegistryError(f"app {app_id!r} not installed")
        surface = app.manifest.get_surface(surface_id)
        if surface is None:
            raise AppRegistryError(
                f"surface {surface_id!r} not declared on app {app_id!r}"
            )
        for action in surface.action_contract:
            if action.action_id == action_id:
                schema = _resolve_action_value_schema(app.manifest, action)
                if schema is not None:
                    _validate_json_like_schema(value, schema, path="$")
                return action
        raise AppRegistryError(
            f"action {action_id!r} is not declared on surface {surface_id!r} "
            f"of app {app_id!r}"
        )

    @staticmethod
    def build_screen_id(app_id: str, surface_id: str, scope: str) -> str:
        """Build a canonical app screen id used across REST/WS/phone."""
        return (
            f"{quote(str(app_id), safe='-._~')}:"
            f"{quote(str(surface_id), safe='-._~')}:"
            f"{quote(str(scope or 'default'), safe='-._~')}"
        )

    @staticmethod
    def parse_screen_id(screen_id: str) -> Optional[tuple[str, str, str]]:
        """Parse canonical + legacy ``<app>:<surface>:<scope>`` screen ids."""
        if not screen_id or ":" not in screen_id:
            return None
        parts = screen_id.split(":", 2)
        if len(parts) != 3:
            return None
        app_id, surface_id, scope = parts
        return unquote(app_id), unquote(surface_id), unquote(scope)

    def resolve_app_and_surface(
        self, screen_id: str
    ) -> Optional[tuple[str, str]]:
        """Parse an app-scoped ``screen_id`` back into (app_id, surface_id)."""
        parsed = self.parse_screen_id(screen_id)
        if parsed is not None:
            return parsed[0], parsed[1]
        # Legacy fallback for malformed pre-canonical ids.
        if not screen_id or ":" not in screen_id:
            return None
        first, second, *_ = screen_id.split(":")
        if not first or not second:
            return None
        return first, second

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_installed(row: sqlite3.Row) -> InstalledApp:
        manifest_data = json.loads(row["manifest_json"])
        manifest = AppManifest(**manifest_data)
        return InstalledApp(
            app_id=row["app_id"],
            version=row["version"],
            manifest=manifest,
            install_dir=Path(row["install_dir"]),
            installed_at=row["installed_at"],
        )


# ----------------------------------------------------------------------
# Filesystem helpers
# ----------------------------------------------------------------------


def _load_manifest_from_dir(source: Path) -> AppManifest:
    """Parse the manifest from `source/manifest.yaml` or `manifest.json`."""
    yaml_path = source / "manifest.yaml"
    json_path = source / "manifest.json"
    if yaml_path.exists():
        try:
            import yaml
        except ImportError as exc:
            raise AppRegistryError("pyyaml is required to parse manifest.yaml") from exc
        raw = yaml.safe_load(yaml_path.read_text()) or {}
    elif json_path.exists():
        raw = json.loads(json_path.read_text())
    else:
        raise AppRegistryError(
            f"no manifest.yaml or manifest.json found in {source}"
        )
    # Inline `template_root` can live beside the manifest as a
    # separate .sdui.json file; resolve any ``"$ref"`` style shorthand.
    _inline_surface_templates(raw, source)
    try:
        return AppManifest(**raw)
    except ValidationError as exc:
        raise AppRegistryError(f"invalid manifest in {source}: {exc}") from exc


def _inline_surface_templates(raw: dict[str, Any], source: Path) -> None:
    """If a surface's template_root is a relative-path string, load it."""
    surfaces = raw.get("surfaces")
    if not isinstance(surfaces, list):
        return
    for surface in surfaces:
        if not isinstance(surface, dict):
            continue
        template_root = surface.get("template_root")
        if not isinstance(template_root, str):
            continue
        candidate = (source / template_root).resolve()
        if candidate.is_file():
            surface["template_root"] = json.loads(candidate.read_text())


def _copy_tree(src: Path, dst: Path) -> None:
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copy2(entry, target)


def _find_signed_manifest(source: Path) -> Optional[Path]:
    for name in SIGNED_MANIFEST_FILENAMES:
        candidate = source / name
        if candidate.is_file():
            return candidate
    return None


def _load_signed_manifest(path: Path) -> SignedManifest:
    raw = json.loads(path.read_text())
    return SignedManifest.model_validate(raw)


def _load_manifest_dict(source: Path) -> dict[str, Any]:
    """Load the raw manifest dict (yaml or json) without pydantic-coercing.

    Used by install_app's policy gate so the unverified manifest is
    treated as opaque data — we only enforce the ``permissions``
    block at install time and let the publisher / consumer of the
    manifest do their own structural validation downstream.
    """
    yaml_path = source / "manifest.yaml"
    json_path = source / "manifest.json"
    if yaml_path.exists():
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise AppRegistryError(
                "pyyaml is required to parse manifest.yaml"
            ) from exc
        return yaml.safe_load(yaml_path.read_text()) or {}
    if json_path.exists():
        return json.loads(json_path.read_text())
    raise AppRegistryError(
        f"no manifest.yaml or manifest.json found in {source}"
    )


def _vault_lookup_key(vault: Any, key_id: str) -> Optional[str]:
    """Return the stored public key for ``key_id`` if the vault has one.

    Tries the new namespaced API first (``vault.get_namespace``) and
    falls back to the flat ``retrieve`` shape so this helper works
    against both the  additive interface and any earlier vault
    fixture used in tests.
    """
    try:
        get_ns = getattr(vault, "get_namespace", None)
        if callable(get_ns):
            value = get_ns("publisher_keys", key_id)
            if value:
                return value
        retrieve = getattr(vault, "retrieve", None)
        if callable(retrieve):
            return retrieve(f"publisher_keys::{key_id}")
    except Exception as exc:
        logger.debug("vault publisher_keys lookup failed: %s", exc)
    return None


def _sha256_file(path: Path) -> bytes:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.digest()


def _verify_registry_bundle(
    item: dict[str, Any],
    tarball: Path,
    *,
    allow_unsigned: bool,
) -> bool:
    """Return True when detached signature verification succeeds."""
    expected_hex = str(item.get("sha256") or "").lower().strip()
    if not expected_hex:
        raise AppRegistryError("registry item is missing sha256")
    actual_hex = _sha256_file(tarball).hex()
    if actual_hex != expected_hex:
        raise AppRegistryError(
            f"registry bundle sha256 mismatch ({actual_hex} != {expected_hex})"
        )

    sig_b64 = str(item.get("signature_b64") or item.get("signature") or "").strip()
    pub_hex = str(item.get("publisher_pubkey_hex") or item.get("publisher_pubkey") or "").strip()

    if not sig_b64 or not pub_hex:
        if allow_unsigned:
            return False
        raise UnverifiedManifestError(
            "registry bundle missing signature and/or publisher public key"
        )
    if VerifyKey is None or HexEncoder is None:
        raise AppRegistryError("pynacl is required for registry signature verification")

    try:
        vk = VerifyKey(pub_hex, encoder=HexEncoder)
        # Registry signs over the ASCII sha256 hex digest.
        vk.verify(expected_hex.encode("ascii"), b64decode(sig_b64))
        return True
    except (BadSignatureError, ValueError, TypeError) as exc:
        if allow_unsigned:
            logger.warning("registry signature verification failed but allow_unsigned=True: %s", exc)
            return False
        raise UnverifiedManifestError(f"registry signature verification failed: {exc}") from exc


def _safe_extract_tarball(tarball: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    dest_abs = dest.resolve()
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            for member in tf.getmembers():
                target = (dest / member.name).resolve()
                if not str(target).startswith(str(dest_abs) + os.sep) and target != dest_abs:
                    raise AppRegistryError(
                        f"registry bundle path traversal blocked: {member.name}"
                    )
            tf.extractall(dest)
    except AppRegistryError:
        raise
    except Exception as exc:
        raise AppRegistryError(f"failed to extract registry bundle: {exc}") from exc


def _locate_manifest_source_dir(extracted_root: Path) -> Path:
    """Find the bundle directory containing manifest metadata."""
    candidates: list[Path] = [extracted_root]
    candidates.extend(p for p in extracted_root.iterdir() if p.is_dir())
    for candidate in candidates:
        for name in MANIFEST_FILENAMES:
            if (candidate / name).is_file():
                return candidate
    for name in MANIFEST_FILENAMES:
        matches = list(extracted_root.rglob(name))
        if matches:
            return matches[0].parent
    raise AppRegistryError("registry bundle did not contain a manifest file")


def _resolve_action_value_schema(
    manifest: AppManifest,
    action: ActionSpec,
) -> Optional[dict[str, Any]]:
    if isinstance(action.value_schema, dict):
        return action.value_schema
    ref = action.value_schema_ref or ""
    if not ref:
        return None
    schema_id = ref
    if schema_id.startswith("#/data_schemas/"):
        schema_id = schema_id.split("/", 2)[-1]
    schema_spec = manifest.get_data_schema(schema_id)
    if schema_spec is None:
        raise AppRegistryError(
            f"action {action.action_id!r} references unknown schema {ref!r}"
        )
    if not isinstance(schema_spec.schema, dict):
        raise AppRegistryError(
            f"data schema {schema_id!r} is not a JSON object schema"
        )
    return schema_spec.schema


def _validate_json_like_schema(value: Any, schema: dict[str, Any], *, path: str) -> None:
    if not isinstance(schema, dict):
        raise AppRegistryError(f"invalid schema at {path}: schema must be an object")

    expected_type = schema.get("type")
    if expected_type is not None:
        _validate_type(value, expected_type, path=path)

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values and value not in enum_values:
        raise AppRegistryError(f"{path} must be one of {enum_values!r}")

    if isinstance(value, str):
        min_len = schema.get("minLength")
        max_len = schema.get("maxLength")
        if isinstance(min_len, int) and len(value) < min_len:
            raise AppRegistryError(f"{path} must be at least {min_len} chars")
        if isinstance(max_len, int) and len(value) > max_len:
            raise AppRegistryError(f"{path} must be at most {max_len} chars")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise AppRegistryError(f"{path} must be >= {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise AppRegistryError(f"{path} must be <= {maximum}")

    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for field in required:
                if isinstance(field, str) and field not in value:
                    raise AppRegistryError(f"{path}.{field} is required")

        props = schema.get("properties")
        if isinstance(props, dict):
            for key, child_schema in props.items():
                if key in value and isinstance(child_schema, dict):
                    _validate_json_like_schema(
                        value[key],
                        child_schema,
                        path=f"{path}.{key}",
                    )

        additional = schema.get("additionalProperties", True)
        if additional is False and isinstance(props, dict):
            extra = [k for k in value.keys() if k not in props]
            if extra:
                raise AppRegistryError(
                    f"{path} has unexpected keys: {', '.join(sorted(extra))}"
                )
        if isinstance(additional, dict):
            for key, child in value.items():
                if not isinstance(props, dict) or key not in props:
                    _validate_json_like_schema(
                        child,
                        additional,
                        path=f"{path}.{key}",
                    )

    if isinstance(value, list):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise AppRegistryError(f"{path} must contain at least {min_items} items")
        if isinstance(max_items, int) and len(value) > max_items:
            raise AppRegistryError(f"{path} must contain at most {max_items} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(value):
                _validate_json_like_schema(
                    item,
                    item_schema,
                    path=f"{path}[{idx}]",
                )


def _validate_type(value: Any, expected: Any, *, path: str) -> None:
    """Validate JSON-schema-like ``type`` value(s)."""
    if isinstance(expected, list):
        errors: list[str] = []
        for one in expected:
            try:
                _validate_type(value, one, path=path)
                return
            except AppRegistryError as exc:
                errors.append(str(exc))
        raise AppRegistryError(errors[0] if errors else f"{path} has invalid type")

    if expected == "object":
        if not isinstance(value, dict):
            raise AppRegistryError(f"{path} must be an object")
        return
    if expected == "array":
        if not isinstance(value, list):
            raise AppRegistryError(f"{path} must be an array")
        return
    if expected == "string":
        if not isinstance(value, str):
            raise AppRegistryError(f"{path} must be a string")
        return
    if expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise AppRegistryError(f"{path} must be a number")
        return
    if expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise AppRegistryError(f"{path} must be an integer")
        return
    if expected == "boolean":
        if not isinstance(value, bool):
            raise AppRegistryError(f"{path} must be a boolean")
        return
    if expected == "null":
        if value is not None:
            raise AppRegistryError(f"{path} must be null")
        return
    # Unknown type keywords are ignored for forward compatibility.


# ----------------------------------------------------------------------
# HybridGenerator
# ----------------------------------------------------------------------


class HybridGenerator:
    """Render a surface by combining authored templates + LLM fallback.

    Flow per ``render()`` call:

    1. **authored** surface → fill ``template_root`` with ``$data.*``
       bindings and return. No LLM.
    2. **generated** surface → look up cached LLM render per
       ``(app_id, surface_id, user_fingerprint, schema_version)``; if
       present, hydrate with data and return. If missing, try the
       publisher-default bundled at ``bundle_dir/surfaces/<surface>.default.json``
       before calling the LLM.
    3. **hybrid** surface → authored template is the default. Only
       regenerates when ``regenerate=True`` (user-customization signal)
       or when the cached personalised render exists.

    The cache directory is structured as::

        <cache_dir>/
            <app_id>/
                <surface_id>/
                    <fingerprint>.v<schema_version>.json
    """

    def __init__(
        self,
        *,
        genui_engine=None,
        cache_dir: str | Path | None = None,
    ):
        self._genui = genui_engine
        self._cache_dir = Path(cache_dir) if cache_dir else Path("/tmp/feral-hybrid-cache")
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._trace_dir = self._cache_dir / "_render_traces"
        self._trace_dir.mkdir(parents=True, exist_ok=True)

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    def set_genui_engine(self, engine) -> None:
        self._genui = engine

    async def render(
        self,
        *,
        app_id: str,
        manifest: AppManifest,
        surface: SurfaceSpec,
        data: dict,
        user_fingerprint: str = "default",
        regenerate: bool = False,
        bundle_dir: Optional[Path] = None,
    ) -> dict:
        kind = surface.kind
        cache_path = self._cache_path_for(
            app_id, surface.surface_id, user_fingerprint, surface.schema_version
        )

        if kind == "authored":
            assert surface.template_root is not None
            rendered = _hydrate(surface.template_root, data)
            self._trace_render(
                app_id=app_id,
                manifest=manifest,
                surface=surface,
                user_fingerprint=user_fingerprint,
                regenerate=regenerate,
                source="authored_template",
                cache_path=cache_path,
                data=data,
                output_tree=rendered,
            )
            return rendered

        if kind == "generated":
            if not regenerate and cache_path.is_file():
                cached_tree = _safe_read_json(cache_path)
                if cached_tree is not None:
                    rendered = _hydrate(cached_tree, data)
                    self._trace_render(
                        app_id=app_id,
                        manifest=manifest,
                        surface=surface,
                        user_fingerprint=user_fingerprint,
                        regenerate=regenerate,
                        source="generated_cache_hit",
                        cache_path=cache_path,
                        data=data,
                        output_tree=rendered,
                    )
                    return rendered
            # Prefer publisher default before we hit the LLM.
            default_tree = _read_publisher_default(bundle_dir, surface.surface_id)
            if default_tree is not None and not regenerate:
                self._write_cache(cache_path, default_tree)
                rendered = _hydrate(default_tree, data)
                self._trace_render(
                    app_id=app_id,
                    manifest=manifest,
                    surface=surface,
                    user_fingerprint=user_fingerprint,
                    regenerate=regenerate,
                    source="publisher_default",
                    cache_path=cache_path,
                    data=data,
                    output_tree=rendered,
                )
                return rendered
            generated, gen_source = await self._llm_generate(manifest, surface, data)
            self._write_cache(cache_path, generated)
            rendered = _hydrate(generated, data)
            self._trace_render(
                app_id=app_id,
                manifest=manifest,
                surface=surface,
                user_fingerprint=user_fingerprint,
                regenerate=regenerate,
                source=gen_source,
                cache_path=cache_path,
                data=data,
                output_tree=rendered,
            )
            return rendered

        if kind == "hybrid":
            assert surface.template_root is not None
            if regenerate:
                # Try the publisher's shipped default first when present.
                # That's the "signed default" path the plan calls out.
                default_tree = _read_publisher_default(bundle_dir, surface.surface_id)
                if default_tree is not None and self._genui is None:
                    self._write_cache(cache_path, default_tree)
                    rendered = _hydrate(default_tree, data)
                    self._trace_render(
                        app_id=app_id,
                        manifest=manifest,
                        surface=surface,
                        user_fingerprint=user_fingerprint,
                        regenerate=regenerate,
                        source="publisher_default_no_llm",
                        cache_path=cache_path,
                        data=data,
                        output_tree=rendered,
                    )
                    return rendered
                generated, gen_source = await self._llm_generate(manifest, surface, data)
                self._write_cache(cache_path, generated)
                rendered = _hydrate(generated, data)
                self._trace_render(
                    app_id=app_id,
                    manifest=manifest,
                    surface=surface,
                    user_fingerprint=user_fingerprint,
                    regenerate=regenerate,
                    source=gen_source,
                    cache_path=cache_path,
                    data=data,
                    output_tree=rendered,
                )
                return rendered
            if cache_path.is_file():
                cached_tree = _safe_read_json(cache_path)
                if cached_tree is not None:
                    rendered = _hydrate(cached_tree, data)
                    self._trace_render(
                        app_id=app_id,
                        manifest=manifest,
                        surface=surface,
                        user_fingerprint=user_fingerprint,
                        regenerate=regenerate,
                        source="hybrid_cache_hit",
                        cache_path=cache_path,
                        data=data,
                        output_tree=rendered,
                    )
                    return rendered
            rendered = _hydrate(surface.template_root, data)
            self._trace_render(
                app_id=app_id,
                manifest=manifest,
                surface=surface,
                user_fingerprint=user_fingerprint,
                regenerate=regenerate,
                source="hybrid_authored_template",
                cache_path=cache_path,
                data=data,
                output_tree=rendered,
            )
            return rendered

        # Fallback — should be caught by manifest validator but safe-guard anyway.
        raise AppRegistryError(f"unknown surface kind {kind!r}")

    # --------------------------------------------------------------
    # Cache ops
    # --------------------------------------------------------------

    def purge_app_cache(self, app_id: str) -> int:
        """Remove every cached render for *app_id*. Returns file count."""
        app_dir = self._cache_dir / app_id
        if not app_dir.is_dir():
            return 0
        count = 0
        for surface_dir in app_dir.iterdir():
            if surface_dir.is_dir():
                for f in surface_dir.iterdir():
                    if f.is_file():
                        f.unlink()
                        count += 1
                surface_dir.rmdir()
        try:
            app_dir.rmdir()
        except OSError:
            pass
        return count

    def _cache_path_for(
        self, app_id: str, surface_id: str, fingerprint: str, schema_version: int
    ) -> Path:
        safe_finger = self._fingerprint_token(fingerprint)
        path = (
            self._cache_dir
            / app_id
            / surface_id
            / f"{safe_finger}.v{schema_version}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _write_cache(self, path: Path, tree: dict) -> None:
        payload = {
            "generated_at": time.time(),
            "tree": tree,
        }
        path.write_text(json.dumps(payload))

    @staticmethod
    def _fingerprint_token(fingerprint: str) -> str:
        return hashlib.sha1(str(fingerprint).encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _data_summary(data: dict) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {"type": type(data).__name__}
        keys = sorted(str(k) for k in data.keys())
        return {
            "keys": keys[:40],
            "key_count": len(keys),
        }

    def _trace_render(
        self,
        *,
        app_id: str,
        manifest: AppManifest,
        surface: SurfaceSpec,
        user_fingerprint: str,
        regenerate: bool,
        source: str,
        cache_path: Path,
        data: dict,
        output_tree: dict,
    ) -> None:
        """Append one deterministic render trace line for replay/audits."""
        try:
            trace_path = (
                self._trace_dir
                / app_id
                / f"{surface.surface_id}.jsonl"
            )
            trace_path.parent.mkdir(parents=True, exist_ok=True)

            action_ids = [a.action_id for a in surface.action_contract]
            record = {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "app_id": app_id,
                "app_version": manifest.version,
                "surface_id": surface.surface_id,
                "surface_kind": surface.kind,
                "surface_schema_version": surface.schema_version,
                "manifest_schema_version": manifest.contract.manifest_schema_version,
                "a2ui_version": manifest.contract.a2ui_version,
                "compatibility_mode": manifest.contract.compatibility_mode,
                "user_fingerprint_hash": self._fingerprint_token(user_fingerprint),
                "regenerate": bool(regenerate),
                "source": source,
                "cache_path": str(cache_path),
                "action_ids": action_ids,
                "data_summary": self._data_summary(data),
                "output_root_type": (
                    output_tree.get("type")
                    if isinstance(output_tree, dict)
                    else type(output_tree).__name__
                ),
            }
            with trace_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        except Exception as exc:
            logger.debug("HybridGenerator trace write failed: %s", exc)

    async def _llm_generate(
        self, manifest: AppManifest, surface: SurfaceSpec, data: dict
    ) -> tuple[dict, str]:
        """Generate SDUI via the shared GenUIEngine with publisher style."""
        if self._genui is None:
            return _deterministic_fallback(manifest, surface), "deterministic_fallback_no_llm"
        prompt = _build_llm_prompt(manifest, surface, data)
        try:
            tree = await self._genui.generate_from_prompt(
                prompt, context={"brand": manifest.brand.model_dump()}
            )
            if isinstance(tree, dict) and tree.get("type"):
                return tree, "llm_generated"
        except Exception as exc:
            logger.warning(
                "HybridGenerator LLM call failed for %s/%s: %s — falling back",
                manifest.app_id, surface.surface_id, exc,
            )
            return _deterministic_fallback(manifest, surface), "deterministic_fallback_llm_error"
        return _deterministic_fallback(manifest, surface), "deterministic_fallback_invalid_llm_output"


# ----------------------------------------------------------------------
# Template hydration helpers
# ----------------------------------------------------------------------


def _hydrate(tree: Any, data: dict) -> Any:
    """Walk *tree* and replace ``$data.foo.bar`` strings with data values.

    Strings of the form ``$data.a.b`` (and ``${data.a.b}``) resolve to
    the matching path in ``data``. Lists walk recursively; dicts walk
    every value. Non-string leaves are returned as-is. Missing paths
    render as ``""`` to avoid crashes on optional bindings.
    """
    if isinstance(tree, str):
        return _resolve_placeholder(tree, data)
    if isinstance(tree, list):
        return [_hydrate(item, data) for item in tree]
    if isinstance(tree, dict):
        out = {}
        for key, value in tree.items():
            out[key] = _hydrate(value, data)
        return out
    return tree


def _resolve_placeholder(text: str, data: dict) -> str | Any:
    if not text:
        return text
    stripped = text.strip()
    # Match either `$data.a.b.c` or `${data.a.b.c}`.
    if stripped.startswith("${") and stripped.endswith("}"):
        inner = stripped[2:-1]
    elif stripped.startswith("$"):
        inner = stripped[1:]
    else:
        return text
    if not inner.startswith("data"):
        return text
    parts = inner.split(".")[1:]
    current: Any = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list):
            # Numeric segment indexes into the list (e.g. contacts.0.name).
            try:
                idx = int(part)
            except ValueError:
                return ""
            if 0 <= idx < len(current):
                current = current[idx]
            else:
                return ""
        else:
            return ""
    return current


# ----------------------------------------------------------------------
# Publisher default + LLM prompt + fallback
# ----------------------------------------------------------------------


def _read_publisher_default(bundle_dir: Optional[Path], surface_id: str) -> Optional[dict]:
    if bundle_dir is None:
        return None
    candidate = bundle_dir / "surfaces" / f"{surface_id}.default.json"
    if not candidate.is_file():
        return None
    try:
        data = json.loads(candidate.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _safe_read_json(path: Path) -> Optional[dict]:
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return None
    if isinstance(raw, dict) and "tree" in raw and isinstance(raw["tree"], dict):
        return raw["tree"]
    return raw if isinstance(raw, dict) else None


def _build_llm_prompt(manifest: AppManifest, surface: SurfaceSpec, data: dict) -> str:
    interactions_chunk = manifest.interactions.to_system_prompt_chunk()
    brand = manifest.brand
    prompt_parts = [
        f"App: {manifest.app_id} (v{manifest.version}).",
        f"Brand: {brand.name}, primary color {brand.primary_color}.",
        "",
        interactions_chunk,
        "",
        f"Generate SDUI for surface '{surface.surface_id}': {surface.description or surface.title}.",
        "",
        f"Generation prompt: {surface.generation_prompt}",
        "",
    ]
    if surface.action_contract:
        prompt_parts.append(
            "Allowed action_ids: "
            + ", ".join(a.action_id for a in surface.action_contract)
        )
    if data:
        prompt_parts.append(f"Render with this data: {json.dumps(data)[:1000]}")
    return "\n".join(prompt_parts)


def _deterministic_fallback(manifest: AppManifest, surface: SurfaceSpec) -> dict:
    """Non-LLM fallback so the UI is never empty when the VLM is unavailable.

    Renders a simple Card with the app brand + a note that the agent
    couldn't generate the surface. Matches the "never fake" contract —
    it's honest, not pretending to be a real surface.
    """
    return {
        "type": "Card",
        "children": [
            {"type": "Text", "value": manifest.brand.name, "style": "headline", "color": manifest.brand.primary_color},
            {"type": "Divider"},
            {"type": "Text", "value": f"{surface.title or surface.surface_id}", "style": "subtitle"},
            {
                "type": "Text",
                "value": (
                    "The agent couldn't generate this surface right now. "
                    "Please open the app's main screen or try again in a moment."
                ),
                "style": "body",
            },
        ],
    }


# ----------------------------------------------------------------------
# Default on-disk paths
# ----------------------------------------------------------------------


def default_apps_db_path() -> str:
    from config.loader import feral_home

    return str(feral_home() / DEFAULT_APPS_DB_NAME)


def default_apps_dir() -> Path:
    from config.loader import feral_home

    return feral_home() / DEFAULT_APPS_DIR_NAME


def default_hybrid_cache_dir() -> Path:
    from config.loader import feral_home

    return feral_home() / ".cache" / "hybrid_genui"
