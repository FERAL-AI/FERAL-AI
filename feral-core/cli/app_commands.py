"""
feral app CLI — scaffold, validate, build, install, publish a GenUI app.

Subcommands
-----------
* ``feral app init <name>``     — scaffold manifest.yaml + surfaces/ +
                                  interactions.yaml + brand/logo.svg +
                                  README.md under ``./<name>/``.
* ``feral app validate <dir>``  — parse the manifest via AppManifest and
                                  re-run every validator (cross-refs,
                                  action contracts, schemas).
* ``feral app build <dir>``     — produce a reproducible tarball under
                                  ``<dir>/dist/<app_id>-<version>.tar.gz``.
* ``feral app install <path>``  — POST /api/apps/install {path: ...} so
                                  the running brain loads the app into
                                  AppRegistry.
* ``feral app publish <dir>``   — sign the tarball with the publisher's
                                  Ed25519 key and POST to
                                  registry.feral.sh/api/v1/publish with
                                  kind=app.

Shares the signing + tokening infrastructure already in
``feral-core/cli/publish.py`` so publishers don't need a second keypair.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tarfile
import textwrap
from hashlib import sha256
from pathlib import Path
from typing import Optional

try:
    import httpx
except ImportError:  # pragma: no cover — optional for offline subcommands
    httpx = None  # type: ignore


# This used to read "Install feral-ai[cli]", which was wrong twice over. There
# is no `cli` extra, and pip does not fail on an unknown one: it warns to
# stderr and installs the base package, so the user saw no error and got
# nothing. And httpx is a BASE dependency (pyproject.toml `dependencies`), so
# no extra could ever have supplied it. Reaching here means the install is
# incomplete, which is a different problem with a different remedy.
_HTTPX_MISSING = (
    "  httpx is required for `{command}` but is not importable.\n"
    "  httpx is a base dependency of feral-ai, so this means the install is\n"
    "  incomplete rather than missing an optional extra. Repair it with:\n"
    "      pip install --force-reinstall feral-ai"
)


def _print(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------
# init
# ---------------------------------------------------------------------


_SCAFFOLD_MANIFEST = """\
app_id: {app_id}
version: 0.1.0
author: your-github-handle
description: A GenUI app built on FERAL.

brand:
  name: {title}
  primary_color: "#5B21B6"
  secondary_color: "#A78BFA"

permissions: []

data_schemas:
  - schema_id: home_data
    schema:
      type: object
      properties:
        greeting:
          type: string

entry_surface_id: home

surfaces:
  - surface_id: home
    title: Home
    kind: authored
    data_schema_ref: home_data
    template_root:
      type: VStack
      spacing: md
      padding: md
      children:
        - type: Text
          value: "$data.greeting"
          style: headline
        - type: Button
          label: "Tap me"
          action_id: hello
          style: primary
    action_contract:
      - action_id: hello
        handler: app_event
        description: Primary call to action.

interactions:
  button_style_priority: ["primary", "secondary", "ghost"]
  destructive_confirmation_required: true
  list_render_preference: auto
  accessibility_notes:
    - "Respect the user's color-contrast preference."
  prose_guidance: |
    Never show raw IDs. Localise timestamps. Use brand primary for
    affirmative actions only.
"""

_SCAFFOLD_README = """\
# {title}

A FERAL GenUI app.

## Build + install locally

    feral app validate ./
    feral app install ./

## Publish

    feral publisher login
    feral app publish ./

## Structure

    manifest.yaml         # AppManifest — brand + schemas + surfaces + rules
    surfaces/             # (optional) split large templates into files referenced
                          # from manifest.yaml via relative paths.
    brand/                # logo, screenshots, etc. — shipped with the bundle.
"""


def cmd_app_init(name: str) -> None:
    slug = _slugify(name)
    if not slug:
        _print("  App name must contain at least 3 letters or digits.")
        sys.exit(2)
    dest = Path(slug).resolve()
    if dest.exists():
        _print(f"  {dest} already exists; pick another name or remove it first.")
        sys.exit(2)
    dest.mkdir(parents=True)
    (dest / "surfaces").mkdir()
    (dest / "brand").mkdir()
    manifest_text = _SCAFFOLD_MANIFEST.format(app_id=slug, title=name.title())
    (dest / "manifest.yaml").write_text(manifest_text)
    (dest / "README.md").write_text(_SCAFFOLD_README.format(title=name.title()))
    (dest / ".feralignore").write_text("dist/\nnode_modules/\n__pycache__/\n")
    _print(f"  Scaffolded FERAL app at {dest}")
    _print("  Edit manifest.yaml, then: feral app validate ./ && feral app install ./")


def _slugify(name: str) -> str:
    cleaned = []
    for ch in name.strip().lower():
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in ("-", "_", " "):
            cleaned.append("-")
    slug = "".join(cleaned).strip("-")
    # Collapse repeat dashes
    while "--" in slug:
        slug = slug.replace("--", "-")
    # Must start with a letter
    while slug and not slug[0].isalpha():
        slug = slug[1:]
    return slug[:64]


# ---------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------


def cmd_app_validate(path: str) -> None:
    source = Path(path).expanduser().resolve()
    if not source.is_dir():
        _print(f"  Not a directory: {source}")
        sys.exit(2)
    try:
        manifest = _load_manifest(source)
    except Exception as exc:
        _print(f"  Manifest failed to load: {exc}")
        sys.exit(1)
    try:
        from models.app_manifest import AppManifest
    except Exception as exc:
        _print(f"  AppManifest model unavailable in this install: {exc}")
        sys.exit(1)
    try:
        model = AppManifest(**manifest)
    except Exception as exc:
        _print(f"  Manifest validation failed: {exc}")
        sys.exit(1)
    _print(f"  OK. {model.app_id} v{model.version} — {len(model.surfaces)} surface(s).")
    _print(f"  Entry surface: {model.entry_surface_id}")
    for s in model.surfaces:
        _print(
            f"    - {s.surface_id} (kind={s.kind}, actions={len(s.action_contract)})"
        )


# ---------------------------------------------------------------------
# build
# ---------------------------------------------------------------------


def cmd_app_build(path: str, out: Optional[str] = None) -> None:
    source = Path(path).expanduser().resolve()
    if not source.is_dir():
        _print(f"  Not a directory: {source}")
        sys.exit(2)
    try:
        manifest = _load_manifest(source)
        from models.app_manifest import AppManifest
        model = AppManifest(**manifest)
    except Exception as exc:
        _print(f"  Can't build — manifest invalid: {exc}")
        sys.exit(1)
    out_path = Path(out) if out else source / "dist" / f"{model.app_id}-{model.version}.tar.gz"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    patterns = _load_ignore_patterns(source)
    size = _build_tarball(source, out_path, patterns)
    _print(f"  Built {out_path} ({size} bytes).")


def _build_tarball(source: Path, out_path: Path, patterns: list[str]) -> int:
    if out_path.exists():
        out_path.unlink()
    with tarfile.open(out_path, "w:gz") as tar:
        for root, dirs, files in os.walk(source):
            # Respect .feralignore
            rel_root = Path(root).relative_to(source)
            if _is_ignored(str(rel_root), patterns):
                dirs[:] = []
                continue
            for fname in files:
                rel = (rel_root / fname).as_posix()
                if _is_ignored(rel, patterns):
                    continue
                full = Path(root) / fname
                tar.add(full, arcname=rel)
    return out_path.stat().st_size


def _load_ignore_patterns(source: Path) -> list[str]:
    patterns: list[str] = []
    path = source / ".feralignore"
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line.rstrip("/"))
    return patterns


def _is_ignored(rel_path: str, patterns: list[str]) -> bool:
    if not rel_path or rel_path == ".":
        return False
    for pattern in patterns:
        if rel_path == pattern or rel_path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


# ---------------------------------------------------------------------
# install (local)
# ---------------------------------------------------------------------


def cmd_app_install(path: str, host: Optional[str] = None, port: Optional[str] = None) -> None:
    if httpx is None:
        _print(_HTTPX_MISSING.format(command="feral app install"))
        sys.exit(1)
    source = Path(path).expanduser().resolve()
    if not source.is_dir():
        _print(f"  Not a directory: {source}")
        sys.exit(2)
    base = _brain_base_url(host, port)
    url = f"{base.rstrip('/')}/api/apps/install"
    body = {"path": str(source), "overwrite": True}
    try:
        resp = httpx.post(url, json=body, timeout=30.0)
    except httpx.HTTPError as exc:
        _print(f"  Could not reach brain at {url}: {exc}")
        sys.exit(1)
    if resp.status_code >= 400:
        _print(f"  Install rejected ({resp.status_code}): {resp.text[:400]}")
        sys.exit(1)
    try:
        data = resp.json()
    except Exception:
        data = {}
    app = data.get("app") or {}
    _print(f"  Installed {app.get('app_id', '<unknown>')} v{app.get('version', '?')}.")


# ---------------------------------------------------------------------
# sign / verify (manifest-level Ed25519, see roadmap §3.3 #1)
# ---------------------------------------------------------------------


def _resolve_manifest_path(manifest_path: str) -> Path:
    target = Path(manifest_path).expanduser().resolve()
    if target.is_dir():
        for name in ("manifest.json", "manifest.yaml", "manifest.yml"):
            candidate = target / name
            if candidate.is_file():
                return candidate
        _print(f"  No manifest.json/.yaml found under {target}.")
        sys.exit(2)
    if not target.is_file():
        _print(f"  Manifest not found: {target}")
        sys.exit(2)
    return target


def _read_manifest_dict(manifest_path: Path) -> dict:
    text = manifest_path.read_text()
    if manifest_path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            _print("  pyyaml is required to read YAML manifests.")
            sys.exit(2)
        data = yaml.safe_load(text) or {}
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            _print(f"  Manifest is not valid JSON: {exc}")
            sys.exit(2)
    if not isinstance(data, dict):
        _print("  Manifest must be a JSON/YAML object at the top level.")
        sys.exit(2)
    return data


def cmd_app_sign(manifest_path: str, key_id: str) -> None:
    """Sign a manifest in place and write ``<name>.signed.json`` next to it.

    Uses the publisher's existing ``~/.feral/publisher.key`` (created
    on demand by ``cli.publish.load_or_create_signing_key``). This is
    the same key the registry uses to authenticate publishes — a
    single keypair per publisher, not one per signing surface.
    """
    target = _resolve_manifest_path(manifest_path)
    if not key_id:
        _print("  --key-id is required (use a stable identifier per publisher key).")
        sys.exit(2)

    try:
        from cli.publish import load_or_create_signing_key
    except Exception as exc:
        _print(f"  Publisher signing key tooling unavailable: {exc}")
        sys.exit(1)
    try:
        from genui.manifest_signing import sign as sign_manifest
    except Exception as exc:
        _print(f"  Manifest signing module unavailable: {exc}")
        sys.exit(1)

    manifest_dict = _read_manifest_dict(target)
    sk = load_or_create_signing_key(verbose=False)
    private_bytes = bytes(sk)
    signed = sign_manifest(manifest_dict, private_bytes, key_id=key_id)

    out_name = target.stem.replace(".signed", "") + ".signed.json"
    out_path = target.with_name(out_name)
    out_path.write_text(signed.model_dump_json(indent=2))
    _print(f"  Signed manifest written to {out_path}.")
    _print(f"  key_id={key_id}  alg={signed.alg}  signed_at={signed.signed_at.isoformat()}")


def cmd_app_verify(manifest_path: str) -> None:
    """Verify a ``*.signed.json`` envelope and exit non-zero on failure."""
    target = Path(manifest_path).expanduser().resolve()
    if target.is_dir():
        for name in ("manifest.signed.json",):
            candidate = target / name
            if candidate.is_file():
                target = candidate
                break
    if not target.is_file():
        _print(f"  Signed manifest not found: {target}")
        sys.exit(2)

    try:
        from genui.manifest_signing import SignedManifest, verify as verify_manifest
    except Exception as exc:
        _print(f"  Manifest signing module unavailable: {exc}")
        sys.exit(1)

    try:
        envelope = SignedManifest.model_validate_json(target.read_text())
    except Exception as exc:
        _print(f"  Signed envelope invalid: {exc}")
        sys.exit(1)

    ok, reason = verify_manifest(envelope)
    if ok:
        _print(
            f"  OK. key_id={envelope.key_id} alg={envelope.alg} "
            f"signed_at={envelope.signed_at.isoformat()}"
        )
        sys.exit(0)
    _print(f"  FAILED: {reason}")
    sys.exit(1)


# ---------------------------------------------------------------------
# publish (signed network)
# ---------------------------------------------------------------------


def cmd_app_publish(path: str, registry: Optional[str] = None) -> None:
    if httpx is None:
        _print(_HTTPX_MISSING.format(command="feral app publish"))
        sys.exit(1)
    try:
        from cli.publish import (
            _load_token_or_exit,
            _sha256_file,
            load_or_create_signing_key,
            registry_base_url,
        )
    except Exception as exc:
        _print(f"  Publisher tooling unavailable: {exc}")
        sys.exit(1)

    source = Path(path).expanduser().resolve()
    if not source.is_dir():
        _print(f"  Not a directory: {source}")
        sys.exit(2)

    try:
        manifest = _load_manifest(source)
        from models.app_manifest import AppManifest
        model = AppManifest(**manifest)
    except Exception as exc:
        _print(f"  Manifest invalid: {exc}")
        sys.exit(1)

    # Registry's Manifest model expects top-level kind/name/version in
    # addition to the app-specific fields. Wrap the AppManifest dump
    # inside that envelope so the registry's validator is happy.
    registry_manifest = {
        "kind": "app",
        "name": model.app_id,
        "version": model.version,
        "description": model.description,
        "author": model.author,
        "app_id": model.app_id,
        "brand": model.brand.model_dump(),
        "entry_surface_id": model.entry_surface_id,
        "surfaces": [s.model_dump() for s in model.surfaces],
    }

    dist_dir = source / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    out_path = dist_dir / f"{model.app_id}-{model.version}.tar.gz"
    patterns = _load_ignore_patterns(source)
    _build_tarball(source, out_path, patterns)

    token = _load_token_or_exit()
    signing_key = load_or_create_signing_key(verbose=False)

    # The registry verifies the detached signature over the SHA-256
    # *hex digest as ASCII bytes*, not the raw 32-byte digest, to
    # match feral_registry/signing.py::verify_bundle_signature. The
    # skill-publish path in cli/publish.py already does this; the
    # GenUI app-publish path was signing the raw digest, so every
    # `feral app publish` returned 400 "signature verification failed"
    # against any registry that ran the canonical verifier (i.e.
    # production). Mirror the skill-publish behaviour here.
    digest = _sha256_file(out_path)
    sha_hex = digest.hex()
    signature = signing_key.sign(sha_hex.encode("ascii")).signature
    sig_b64 = base64.b64encode(signature).decode("ascii")

    base = registry_base_url(registry)
    url = f"{base}/api/v1/publish"
    _print(f"  Uploading app '{model.app_id}' v{model.version} to {url}...")

    with open(out_path, "rb") as fp:
        files = {"bundle": (out_path.name, fp, "application/gzip")}
        data = {
            "signature": sig_b64,
            "manifest_json": json.dumps(registry_manifest),
        }
        headers = {"Authorization": f"Bearer {token}"}
        try:
            resp = httpx.post(url, files=files, data=data, headers=headers, timeout=30.0)
        except httpx.HTTPError as exc:
            _print(f"  Upload failed: {exc}")
            sys.exit(1)

    if resp.status_code >= 400:
        _print(f"  Registry rejected publish ({resp.status_code}): {resp.text[:400]}")
        sys.exit(1)
    try:
        body = resp.json()
    except Exception:
        body = {}
    _print(f"  Published! item_id: {body.get('id', '<unknown>')}")


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _load_manifest(source: Path) -> dict:
    yaml_path = source / "manifest.yaml"
    json_path = source / "manifest.json"
    if yaml_path.exists():
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise RuntimeError("pyyaml required to parse manifest.yaml") from exc
        raw = yaml.safe_load(yaml_path.read_text()) or {}
    elif json_path.exists():
        raw = json.loads(json_path.read_text())
    else:
        raise FileNotFoundError(f"no manifest.yaml or manifest.json in {source}")
    # Inline surface templates referenced as relative file paths, same
    # behaviour as AppRegistry.install_from_dir so `validate` + `build`
    # match install-time semantics.
    surfaces = raw.get("surfaces")
    if isinstance(surfaces, list):
        for surface in surfaces:
            if isinstance(surface, dict):
                template_root = surface.get("template_root")
                if isinstance(template_root, str):
                    candidate = (source / template_root).resolve()
                    if candidate.is_file():
                        surface["template_root"] = json.loads(candidate.read_text())
    return raw


def _brain_base_url(host: Optional[str], port: Optional[str]) -> str:
    if host or port:
        h = host or "localhost"
        p = port or "9090"
        scheme = "https" if p == "443" else "http"
        return f"{scheme}://{h}:{p}"
    env = os.environ.get("FERAL_BRAIN_URL")
    if env:
        return env
    return "http://localhost:9090"


# ---------------------------------------------------------------------
# Argparse wiring
# ---------------------------------------------------------------------


def register_app_subparser(sub) -> None:
    """Attach `feral app ...` subcommands onto the main argparse registry."""
    app_p = sub.add_parser("app", help="FERAL GenUI app: init / validate / build / install / publish")
    app_sub = app_p.add_subparsers(dest="app_subcommand", required=True)

    init_p = app_sub.add_parser("init", help="Scaffold a new GenUI app folder.")
    init_p.add_argument("name", help="Human name for the app (becomes the slug).")

    val_p = app_sub.add_parser("validate", help="Validate an app bundle folder against the AppManifest schema.")
    val_p.add_argument("path", nargs="?", default=".")

    build_p = app_sub.add_parser("build", help="Produce a reproducible tarball under <path>/dist/.")
    build_p.add_argument("path", nargs="?", default=".")
    build_p.add_argument("--out", default=None)

    inst_p = app_sub.add_parser("install", help="Install a local app bundle into the running brain.")
    inst_p.add_argument("path", nargs="?", default=".")
    inst_p.add_argument("--host", default=None)
    inst_p.add_argument("--port", default=None)

    pub_p = app_sub.add_parser("publish", help="Sign + publish an app bundle to registry.feral.sh.")
    pub_p.add_argument("path", nargs="?", default=".")
    pub_p.add_argument("--registry", default=None)

    sign_p = app_sub.add_parser(
        "sign",
        help="Sign a manifest in place; writes <name>.signed.json next to it.",
    )
    sign_p.add_argument(
        "manifest_path",
        help="Path to manifest.json/.yaml or to a folder containing one.",
    )
    sign_p.add_argument(
        "--key-id",
        required=True,
        help="Stable identifier for this publisher key (e.g. 'feral-team:2026-04').",
    )

    verify_p = app_sub.add_parser(
        "verify",
        help="Verify a *.signed.json envelope; exits non-zero on failure.",
    )
    verify_p.add_argument(
        "manifest_path",
        help=(
            "Path to a *.signed.json file (or folder containing manifest.signed.json)."
        ),
    )


def dispatch_app_subcommand(args: argparse.Namespace) -> None:
    sub = getattr(args, "app_subcommand", "")
    if sub == "init":
        cmd_app_init(args.name)
    elif sub == "validate":
        cmd_app_validate(args.path)
    elif sub == "build":
        cmd_app_build(args.path, out=args.out)
    elif sub == "install":
        cmd_app_install(args.path, host=args.host, port=args.port)
    elif sub == "publish":
        cmd_app_publish(args.path, registry=args.registry)
    elif sub == "sign":
        cmd_app_sign(args.manifest_path, args.key_id)
    elif sub == "verify":
        cmd_app_verify(args.manifest_path)
    else:
        _print("  Unknown `feral app` subcommand.")
        sys.exit(2)
