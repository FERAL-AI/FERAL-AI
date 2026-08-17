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
* ``feral app install <path>``  : POST /api/apps/preview, show what the
                                  app reaches and which skills it pulls
                                  in, ask, then POST /api/apps/install
                                  with the ``install_token`` the preview
                                  minted.
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
    feral app install ./ --unsigned

`install` shows what the app reaches and which skills it pulls into the
brain, then asks. `--unsigned` is needed until the manifest is signed
(`feral app sign ./ --key-id <your-key-id>`), because nothing otherwise
proves who wrote the bundle. Add `--yes` to answer in advance from a
script.

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
    _print("  Edit manifest.yaml, then:")
    _print(f"      feral app validate {dest}")
    _print(f"      feral app install {dest} --unsigned")
    _print("  (--unsigned until you sign it: `feral app sign <dir> --key-id <id>`)")


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


def cmd_app_install(
    path: str,
    host: Optional[str] = None,
    port: Optional[str] = None,
    *,
    assume_yes: bool = False,
    unsigned: bool = False,
    high_trust: bool = False,
) -> None:
    """Preview an app install, show what it brings, ask, then install it.

    ``POST /api/apps/install`` requires an ``install_token`` minted by
    ``POST /api/apps/preview`` and answers 403 without one, because an
    app's ``skill_dependencies`` install skills and a skill executes
    Python inside the brain. This used to post the ungated shape and got
    that 403.

    The token is not the point of this function; the disclosure is. A
    terminal is a consent surface, so the same three decisions the web
    sheet presents are presented here: the app's own reach, the skills
    that will be installed (new code), the skills already present
    (nothing new runs), and the skills FERAL could not verify (named,
    with the brain's reason, what breaks, and what to do). Every word of
    that copy comes from the brain, which reads it out of
    ``models/skill_manifest.py``, so the terminal cannot drift from the
    dialog.
    """
    if httpx is None:
        _print(_HTTPX_MISSING.format(command="feral app install"))
        sys.exit(1)
    source = Path(path).expanduser().resolve()
    if not source.is_dir():
        _print(f"  Not a directory: {source}")
        sys.exit(2)

    base = _brain_base_url(host, port)
    preview = _preview_app(
        base, source, path=path, unsigned=unsigned, high_trust=high_trust
    )
    _print_disclosure(preview, unsigned=unsigned)
    _decide(
        preview,
        path=path,
        assume_yes=assume_yes,
        unsigned=unsigned,
        high_trust=high_trust,
    )
    _install_previewed(base, preview)


# --- step one: preview -------------------------------------------------


def _preview_app(
    base: str,
    source: Path,
    *,
    path: str,
    unsigned: bool,
    high_trust: bool,
) -> dict:
    """Ask the brain what installing *source* would do. Installs nothing."""
    url = f"{base.rstrip('/')}/api/apps/preview"
    body = {
        "path": str(source),
        "overwrite": True,
        "unsigned": bool(unsigned),
        "user_high_trust": bool(high_trust),
    }
    try:
        resp = httpx.post(url, json=body, timeout=60.0, headers=_brain_auth_headers())
    except httpx.HTTPError as exc:
        _print(f"  Could not reach the brain at {url}: {exc}")
        _print("  Start it with `feral start`, or name another one with --host/--port.")
        sys.exit(1)

    if resp.status_code >= 400:
        _print_preview_refusal(resp, path=path, unsigned=unsigned, high_trust=high_trust)
        sys.exit(1)

    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict) or not data.get("install_token"):
        # An install without a token is refused by the brain, so there is
        # nothing to fall back to and saying "installed" would be a lie.
        _print(f"  The brain answered {resp.status_code} with no install_token, so")
        _print("  there is nothing to install with. Response:")
        _print(f"      {resp.text[:400]}")
        sys.exit(1)
    return data


def _detail_of(resp) -> tuple[dict, str]:
    """Split an error body into its structured detail and a readable message."""
    try:
        body = resp.json()
    except Exception:
        return {}, (resp.text or "").strip()[:400]
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        return detail, str(detail.get("message") or detail.get("error") or "")
    if isinstance(detail, str):
        return {}, detail
    return {}, (resp.text or "").strip()[:400]


def _print_preview_refusal(resp, *, path: str, unsigned: bool, high_trust: bool) -> None:
    """Say why the brain will not install this, and what to do next.

    Only next steps that were run against this build are printed. There
    is deliberately no advice on a signature failure of the app bundle
    itself: every install path runs the same verifier, so re-running one
    would send the user in a circle.
    """
    detail, message = _detail_of(resp)
    error = str(detail.get("error") or "")
    _print("")
    _print(f"  The brain refused to preview this app ({resp.status_code}).")
    if message:
        for line in _wrap(message, indent="  "):
            _print(line)

    low = message.lower()
    if error == "unverified_manifest" and (
        "verification failed" in low or "unreadable" in low
    ):
        # A bundle that HAS a signature and fails it is not the same as
        # one that has none. Offering --unsigned here would be offering a
        # way around a check that just caught something, and every other
        # install path runs the same verifier, so nothing is printed to
        # run. This mirrors the web sheet, which shows no command either.
        _print("")
        _print("  These files carry a signature and it does not match them, so FERAL")
        _print("  cannot tell who wrote them or whether they were altered on the way.")
        _print("  Retrying will not help: every install path runs this same check.")
        _print("  Get an intact copy from the publisher.")
        return
    if error == "unverified_manifest" and not unsigned:
        _print("")
        _print("  Nothing here proves who wrote these files. Two ways forward:")
        _print("    Sign it with your publisher key, then install the signed bundle:")
        _print(f"        feral app sign {path} --key-id <your-key-id>")
        _print(f"        feral app install {path}")
        _print("    Or install it unsigned, on your own judgement, having read the")
        _print("    disclosure it prints:")
        _print(f"        feral app install {path} --unsigned")
        return
    if error == "permissions_policy_violation":
        _print("")
        for line in _wrap(str(detail.get("remediation") or ""), indent="  "):
            _print(line)
        if not high_trust:
            _print("  The last of those is --high-trust, and it only counts on a signed")
            _print("  bundle whose manifest carries a justification:")
            _print(f"        feral app install {path} --high-trust")
        return
    remediation = str(detail.get("remediation") or "")
    if remediation:
        _print("")
        for line in _wrap(remediation, indent="  "):
            _print(line)


# --- the disclosure ----------------------------------------------------


def _wrap(text: str, *, indent: str = "  ", width: int = 78) -> list[str]:
    """Wrap server copy for a terminal, keeping it readable at 80 columns."""
    text = " ".join(str(text or "").split())
    if not text:
        return []
    return textwrap.wrap(
        text, width=width, initial_indent=indent, subsequent_indent=indent
    ) or [indent + text]


def _permission_lines(rows, fallback_ids=None, *, indent: str = "    ") -> list[str]:
    """Render ``[{id, label, description}]`` as consent copy.

    The labels and descriptions are the brain's, out of
    ``models/skill_manifest.PERMISSION_LABELS`` /
    ``PERMISSION_DESCRIPTIONS``. The CLI never writes its own sentence
    for a capability; when the brain sends only ids it shows the ids.
    """
    out: list[str] = []
    rows = list(rows or [])
    if not rows:
        rows = [{"id": str(i), "label": str(i), "description": ""} for i in (fallback_ids or [])]
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or row.get("id") or "")
        if not label:
            continue
        out.append(f"{indent}- {label}")
        out.extend(_wrap(row.get("description") or "", indent=indent + "  "))
    return out


def _app_name(preview: dict) -> str:
    app = preview.get("app") or {}
    brand = app.get("brand") or {}
    return str(brand.get("name") or app.get("app_id") or "this app")


def _print_disclosure(preview: dict, *, unsigned: bool = False) -> None:
    """Print what installing this app would do, before anything is asked."""
    from cli import ui_kit

    app = preview.get("app") or {}
    name = _app_name(preview)
    version = str(app.get("version") or "?")
    ui_kit.brand_panel(
        f"feral app install: {app.get('app_id') or name} v{version}",
        body=str(app.get("description") or ""),
    )

    source = preview.get("source") or {}
    origin = str(source.get("origin") or "")
    origin_label = {
        "path": "local directory",
        "git_url": "git clone",
        "registry_id": "registry",
    }.get(origin, origin or "source")
    _print(f"  Source: {source.get('value') or '?'} ({origin_label})")
    if app.get("author"):
        _print(f"  Author: {app['author']}")

    sig = preview.get("signature") or {}
    if sig.get("verified"):
        signer = str(sig.get("key_id") or sig.get("publisher") or "").strip()
        by = f" by {signer}" if signer else ""
        _print(f"  Signature: verified{by}. These are the bytes that were signed.")
    else:
        reason = str(sig.get("reason") or "").strip()
        _print("  Signature: none. Nothing here proves who wrote these files or")
        _print("  whether they were altered.")
        if unsigned:
            _print("  You asked for that with --unsigned.")
        if reason:
            for line in _wrap(f"Reason: {reason}", indent="  "):
                _print(line)

    _print("")
    _print("  What the app itself can reach")
    _print("  (its screens run in a sandbox, so this is where data may go)")
    lines = _permission_lines(preview.get("permission_details"), preview.get("permissions"))
    for line in lines or ["    - Nothing declared."]:
        _print(line)

    deps = preview.get("skill_dependencies") or {}
    _print_new_skills(list(deps.get("to_install") or []))
    _print_existing_skills(list(deps.get("already_installed") or []))
    _print_unavailable_skills(list(deps.get("unavailable") or []), app_name=name)
    _print("")


def _print_new_skills(rows: list) -> None:
    if not rows:
        return
    _print("")
    _print(f"  Skills it will install ({len(rows)})")
    _print("  (these are not screens: each one runs its own Python inside FERAL,")
    _print("   and each was checked against its publisher's signature)")
    for dep in rows:
        skill_id = str(dep.get("skill_id") or "?")
        title = str(dep.get("name") or skill_id)
        meta = [skill_id]
        if dep.get("version"):
            meta.append(f"v{dep['version']}")
        if dep.get("publisher"):
            meta.append(f"by {dep['publisher']}")
        _print(f"    - {title} ({', '.join(meta)})")
        perms = _permission_lines(
            dep.get("permission_details"), dep.get("permissions"), indent="      "
        )
        if perms:
            _print("      It can reach:")
            for line in perms:
                _print(line)
        else:
            _print("      It declares no permissions, which is not the same as doing")
            _print("      nothing: it still runs its own code inside FERAL. Install it")
            _print("      only if you trust its publisher.")


def _print_existing_skills(rows: list) -> None:
    if not rows:
        return
    _print("")
    _print(f"  Skills you already have ({len(rows)})")
    _print("  (nothing new is installed for these; they are listed so the app's")
    _print("   full reach is visible)")
    for dep in rows:
        skill_id = str(dep.get("skill_id") or "?")
        title = str(dep.get("name") or skill_id)
        version = f" v{dep['version']}" if dep.get("version") else ""
        labels = [
            str(r.get("label") or r.get("id") or "")
            for r in (dep.get("permission_details") or [])
            if isinstance(r, dict)
        ]
        labels = [lbl for lbl in labels if lbl] or list(dep.get("permissions") or [])
        suffix = f" reaches: {', '.join(str(x) for x in labels)}" if labels else ""
        _print(f"    - {title} ({skill_id}{version}){suffix}")


def _print_unavailable_skills(rows: list, *, app_name: str) -> None:
    if not rows:
        return
    _print("")
    _print(f"  Skills FERAL cannot install ({len(rows)})")
    for dep in rows:
        skill_id = str(dep.get("skill_id") or "?")
        remediation = dep.get("remediation") or {}
        _print(f"    - {skill_id}")
        why = str(remediation.get("message") or dep.get("reason") or "")
        for line in _wrap(why, indent="      "):
            _print(line)
        impact = [a for a in (dep.get("impact") or []) if isinstance(a, dict)]
        if impact:
            _print(f"      Without it, {app_name} cannot:")
            for action in impact:
                what = str(action.get("description") or action.get("action_id") or "")
                where = str(action.get("surface_id") or "")
                _print(f"        - {what}" + (f" ({where})" if where else ""))
        for line in _wrap(str(remediation.get("action") or ""), indent="      "):
            _print(line)
        command = str(remediation.get("command") or "").strip()
        if command:
            # Empty on purpose for a signature failure and for a package
            # the brain refused: every install path runs the same check,
            # so a command there would be a loop. Do not invent one.
            _print(f"          {command}")


# --- the decision ------------------------------------------------------


def _decide(
    preview: dict,
    *,
    path: str,
    assume_yes: bool,
    unsigned: bool,
    high_trust: bool,
) -> None:
    """Get consent, or exit without installing anything.

    ``--yes`` is consent given in advance, so it installs, and the
    disclosure above is printed either way: a scripted run leaves the
    same record on stdout as an answered one.

    Without ``--yes`` and without a TTY there is nobody to answer, and
    the prompt is not asked at all rather than read off a pipe. An empty
    pipe would answer "no" and a pipe carrying an unrelated line could
    answer "yes"; neither is consent. It exits 2, which is what this CLI
    uses for "invoked in a way that cannot work", and names the exact
    command that would work.
    """
    from cli import ui_kit

    name = _app_name(preview)
    missing = list((preview.get("skill_dependencies") or {}).get("unavailable") or [])
    if missing:
        plural = "" if len(missing) == 1 else "s"
        question = f"  Install {name} without {len(missing)} skill{plural}?"
    else:
        question = f"  Install {name}?"

    if assume_yes:
        _print(f"{question}  --yes, so it installs without asking.")
        return

    if not ui_kit.is_interactive():
        flags = "".join(
            f" {flag}"
            for flag, on in (("--unsigned", unsigned), ("--high-trust", high_trust))
            if on
        )
        _print(f"{question}  not asked: stdin is not a terminal.")
        _print("  Nothing was installed. There is nobody here to answer, and an app")
        _print("  install installs code, so the answer is not assumed. Re-run where")
        _print("  you can answer, or accept the disclosure above in advance:")
        _print(f"      feral app install {path}{flags} --yes")
        sys.exit(2)

    if not ui_kit.confirm(question, default=False):
        _print("  Nothing was installed.")
        sys.exit(1)


# --- step two: install -------------------------------------------------


def _install_previewed(base: str, preview: dict) -> None:
    """Spend the token on exactly the app the disclosure described.

    The source is not sent: it lives inside the token, bound to the bytes
    the brain staged and verified during the preview, so nothing can be
    swapped between the disclosure and the install.
    """
    url = f"{base.rstrip('/')}/api/apps/install"
    try:
        resp = httpx.post(
            url,
            json={"install_token": preview["install_token"]},
            timeout=120.0,
            headers=_brain_auth_headers(),
        )
    except httpx.HTTPError as exc:
        _print(f"  Could not reach the brain at {url}: {exc}")
        _print("  Nothing was installed.")
        sys.exit(1)

    if resp.status_code >= 400:
        _print_install_refusal(resp)
        sys.exit(1)

    try:
        data = resp.json()
    except Exception:
        data = {}
    app = data.get("app") or {}
    deps = data.get("skill_dependencies") or {}
    _print("")
    _print(f"  Installed {app.get('app_id', '<unknown>')} v{app.get('version', '?')}.")
    installed = [str(s) for s in (deps.get("installed") or [])]
    present = [str(s) for s in (deps.get("already_present") or [])]
    if installed:
        _print(f"  Skills installed: {', '.join(installed)}")
    if present:
        _print(f"  Skills already present: {', '.join(present)}")

    unavailable = [
        str(d.get("skill_id") or "?")
        for d in (deps.get("unavailable") or [])
        if isinstance(d, dict)
    ]
    if unavailable:
        _print("")
        _print(f"  Installed without {', '.join(unavailable)}.")
        _print("  The Apps page keeps showing what is missing, what it costs and how")
        _print("  to get it: the brain recomputes that on every listing, so it clears")
        _print("  itself the moment the skill is installed.")
    else:
        _print("  It is loaded in the running brain; open it from the Apps page.")


def _print_install_refusal(resp) -> None:
    detail, message = _detail_of(resp)
    error = str(detail.get("error") or "")
    _print("")
    _print(f"  Install rejected ({resp.status_code}).")
    if message:
        for line in _wrap(message, indent="  "):
            _print(line)

    if error == "preview_required":
        # The common cause is elapsed time: a preview lasts
        # APP_PREVIEW_TTL_SECONDS (300s) and is single-use.
        _print("  Previews are single-use and expire after 5 minutes. Run the same")
        _print("  command again to see the disclosure fresh and answer it.")
    for failed in detail.get("failed") or []:
        if not isinstance(failed, dict):
            continue
        _print(f"    - {failed.get('skill_id', '?')}: {failed.get('error', '')}")
        remediation = failed.get("remediation") or {}
        for line in _wrap(str(remediation.get("action") or ""), indent="      "):
            _print(line)
        command = str(remediation.get("command") or "").strip()
        if command:
            _print(f"          {command}")
    remediation = detail.get("remediation")
    if isinstance(remediation, str) and remediation:
        for line in _wrap(remediation, indent="  "):
            _print(line)

    if error == "skill_dependency_install_failed":
        # Precise on purpose. The brain uninstalls the app
        # (``registry.uninstall`` in the route) but does not uninstall the
        # dependencies that installed successfully before one failed, so
        # "nothing was installed" would be false here.
        _print("  The app was rolled back and is not installed. Any skill that did")
        _print("  install before the failure is still installed.")
    else:
        _print("  Nothing was installed.")


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


def _brain_auth_headers() -> dict:
    """Bearer header for a brain that is not on loopback.

    Loopback bypasses HTTP auth (``api/server.APIKeyMiddleware``), so the
    usual local install needs nothing. A brain named with --host or
    FERAL_BRAIN_URL does need the key, and there is exactly one answer to
    "which key", in ``cli/install.py``. Reused rather than restated so
    the two install commands cannot disagree about it.
    """
    try:
        from cli.install import _brain_auth_headers as _headers

        return dict(_headers() or {})
    except Exception:
        return {}


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

    inst_p = app_sub.add_parser(
        "install",
        help=(
            "Show what a local app bundle installs, ask, then install it into "
            "the running brain."
        ),
    )
    inst_p.add_argument("path", nargs="?", default=".")
    inst_p.add_argument("--host", default=None)
    inst_p.add_argument("--port", default=None)
    # dest= is spelled out because `feral app` shares one argparse
    # namespace with 30-odd other subcommands; a bare `--yes` would
    # collide with the `key`/`integration` ones that already own that
    # attribute name.
    inst_p.add_argument(
        "--yes", "-y",
        action="store_true",
        dest="app_assume_yes",
        help=(
            "Consent in advance: print the disclosure, then install without "
            "prompting. Required when stdin is not a terminal."
        ),
    )
    inst_p.add_argument(
        "--unsigned",
        action="store_true",
        dest="app_unsigned",
        help=(
            "Install a bundle with no publisher signature. Nothing then proves "
            "who wrote it; the disclosure says so."
        ),
    )
    inst_p.add_argument(
        "--high-trust",
        action="store_true",
        dest="app_high_trust",
        help=(
            "Opt into the permissions the install policy otherwise refuses "
            "(network: '*'), which also needs a signed manifest carrying a "
            "justification."
        ),
    )

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
        cmd_app_install(
            args.path,
            host=args.host,
            port=args.port,
            assume_yes=bool(getattr(args, "app_assume_yes", False)),
            unsigned=bool(getattr(args, "app_unsigned", False)),
            high_trust=bool(getattr(args, "app_high_trust", False)),
        )
    elif sub == "publish":
        cmd_app_publish(args.path, registry=args.registry)
    elif sub == "sign":
        cmd_app_sign(args.manifest_path, args.key_id)
    elif sub == "verify":
        cmd_app_verify(args.manifest_path)
    else:
        _print("  Unknown `feral app` subcommand.")
        sys.exit(2)
