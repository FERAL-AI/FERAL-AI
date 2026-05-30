"""Sandbox image build helper, version pinning, and tag computation.

Cooperates with :mod:`feral-core.security.docker_sandbox` — that module
owns the *runtime* invocation (container flags, mount layout, watchdog
wiring); this module owns the *upstream image*: what version we pin,
what tag the launcher should reference, and how to (re)build it locally
or push to the shared GHCR registry.

There are two image kinds:

  - ``"minimal"`` → ``ghcr.io/feral-ai/sandbox:<version>`` (or local
    ``feral-sandbox:<version>``). Built from ``Dockerfile.sandbox``.
    Used for tool-genesis code ( GenUI app code execution slot) and
     subagent worker code.

  - ``"browser"`` → ``ghcr.io/feral-ai/sandbox-browser:<version>``
    (or local ``feral-sandbox-browser:<version>``). Built from
    ``Dockerfile.sandbox-browser``. Used for the Chromium-backed
    AppSurface iframe runtime.

Both kinds ``FROM`` the common base built from
``Dockerfile.sandbox-common``.

Version pinning
---------------
``SANDBOX_IMAGE_VERSION`` is a ``<calver>-<sha8>`` string. The calver
matches feral-core's package version (so a pinned brain build ships
against a known sandbox image); the hex suffix is the first 8 chars of
``sha256(Dockerfile.sandbox-common ‖ Dockerfile.sandbox ‖
Dockerfile.sandbox-browser)``. When ANY of those three Dockerfiles
changes, the version string changes — so cache keys, audit log entries,
and image tags can never silently drift behind the on-disk recipes.

The launcher in :mod:`docker_sandbox` is expected to call
:func:`resolve_image_tag` instead of hard-coding ``"feral-sandbox:latest"``
so a partial upgrade can never produce a brain talking to a stale
sandbox recipe.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Literal

logger = logging.getLogger("feral.security.sandbox_image")

ImageKind = Literal["minimal", "browser"]

# feral-core/security/sandbox_image.py → repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]

DOCKERFILE_COMMON = _REPO_ROOT / "Dockerfile.sandbox-common"
DOCKERFILE_MINIMAL = _REPO_ROOT / "Dockerfile.sandbox"
DOCKERFILE_BROWSER = _REPO_ROOT / "Dockerfile.sandbox-browser"

REGISTRY_NAMESPACE = "ghcr.io/feral-ai"
LOCAL_NAMESPACE_MINIMAL = "feral-sandbox"
LOCAL_NAMESPACE_BROWSER = "feral-sandbox-browser"
LOCAL_NAMESPACE_COMMON = "feral-sandbox-common"

# Pinned to feral-core's package calver. Bump in lock-step with
# feral-core/pyproject.toml::project.version when ANY sandbox image
# semantics changes that warrants a new image series.
_FERAL_CALVER = "2026.4.32"


def _content_hash() -> str:
    """sha256(Dockerfile-common || Dockerfile.sandbox || Dockerfile.sandbox-browser)[:8]."""
    h = hashlib.sha256()
    for path in (DOCKERFILE_COMMON, DOCKERFILE_MINIMAL, DOCKERFILE_BROWSER):
        if path.exists():
            h.update(b"---" + path.name.encode() + b"---\n")
            h.update(path.read_bytes())
        else:
            # An import-time missing Dockerfile would normally be a hard
            # error, but this module is also imported by the test suite
            # in environments where docker isn't installed and CI may
            # check out a partial worktree. We surface the absence in
            # the version string so tests can assert on it.
            h.update(b"<missing:" + path.name.encode() + b">")
    return h.hexdigest()[:8]


SANDBOX_IMAGE_VERSION: str = f"{_FERAL_CALVER}-{_content_hash()}"
"""Pinned, deterministic version string: ``<calver>-<sha8-of-Dockerfiles>``."""


def _local_repo(kind: ImageKind) -> str:
    if kind == "minimal":
        return LOCAL_NAMESPACE_MINIMAL
    if kind == "browser":
        return LOCAL_NAMESPACE_BROWSER
    raise ValueError(f"unknown image kind: {kind!r}")


def _registry_repo(kind: ImageKind) -> str:
    if kind == "minimal":
        return f"{REGISTRY_NAMESPACE}/sandbox"
    if kind == "browser":
        return f"{REGISTRY_NAMESPACE}/sandbox-browser"
    raise ValueError(f"unknown image kind: {kind!r}")


def resolve_image_tag(kind: ImageKind, *, prefer_registry: bool = False) -> str:
    """Return the image reference :mod:`docker_sandbox` should use.

    By default we prefer the local tag (e.g. ``feral-sandbox:<version>``)
    so the launcher works fully offline. Pass ``prefer_registry=True``
    to get the GHCR tag (e.g. ``ghcr.io/feral-ai/sandbox:<version>``)
    when the operator has explicitly opted into pulling from the
    shared registry.
    """
    repo = _registry_repo(kind) if prefer_registry else _local_repo(kind)
    return f"{repo}:{SANDBOX_IMAGE_VERSION}"


def common_image_tag() -> str:
    """Image tag for the common base (consumed via Dockerfile ``ARG``)."""
    return f"{LOCAL_NAMESPACE_COMMON}:{SANDBOX_IMAGE_VERSION}"


def _docker_bin() -> str:
    bin_path = shutil.which("docker")
    if not bin_path:
        raise RuntimeError(
            "docker CLI not found on PATH. Install Docker Desktop or "
            "the docker engine to (re)build sandbox images."
        )
    return bin_path


def _run(argv: list[str]) -> None:
    logger.info("sandbox_image.run: %s", " ".join(argv))
    proc = subprocess.run(argv, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"sandbox image build step failed (exit={proc.returncode}): "
            f"{' '.join(argv)}"
        )


def _image_digest(docker: str, tag: str) -> str:
    proc = subprocess.run(
        [docker, "image", "inspect", "--format", "{{.Id}}", tag],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker image inspect {tag!r} failed: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def build_image(kind: ImageKind, *, push: bool = False) -> str:
    """Build the requested sandbox image and return its sha256 digest.

    Build order is enforced: the common base is always built first
    (no-op if already cached) so the kind-specific build can resolve
    its ``FROM feral-sandbox-common:<tag>`` directive. Both the kind-
    specific image and the common base are tagged with
    ``SANDBOX_IMAGE_VERSION`` so tag-mismatch debugging is trivial.

    ``push=True`` re-tags the local image as the GHCR reference and
    runs ``docker push``. The caller is responsible for prior
    ``docker login ghcr.io`` — this helper does NOT manage credentials.
    """
    docker = _docker_bin()

    if not DOCKERFILE_COMMON.exists():
        raise FileNotFoundError(f"missing {DOCKERFILE_COMMON}")
    if kind == "minimal" and not DOCKERFILE_MINIMAL.exists():
        raise FileNotFoundError(f"missing {DOCKERFILE_MINIMAL}")
    if kind == "browser" and not DOCKERFILE_BROWSER.exists():
        raise FileNotFoundError(f"missing {DOCKERFILE_BROWSER}")

    common_versioned = common_image_tag()
    common_latest = f"{LOCAL_NAMESPACE_COMMON}:latest"
    _run([
        docker, "build",
        "-f", str(DOCKERFILE_COMMON),
        "-t", common_versioned,
        "-t", common_latest,
        str(_REPO_ROOT),
    ])

    dockerfile = DOCKERFILE_MINIMAL if kind == "minimal" else DOCKERFILE_BROWSER
    local_tag = resolve_image_tag(kind)

    _run([
        docker, "build",
        "-f", str(dockerfile),
        "-t", local_tag,
        "--build-arg", f"SANDBOX_COMMON_TAG={SANDBOX_IMAGE_VERSION}",
        str(_REPO_ROOT),
    ])

    digest = _image_digest(docker, local_tag)

    if push:
        registry_tag = resolve_image_tag(kind, prefer_registry=True)
        _run([docker, "tag", local_tag, registry_tag])
        _run([docker, "push", registry_tag])

    logger.info(
        "sandbox_image.build_image kind=%s tag=%s digest=%s",
        kind, local_tag, digest,
    )
    return digest


__all__ = [
    "SANDBOX_IMAGE_VERSION",
    "DOCKERFILE_COMMON",
    "DOCKERFILE_MINIMAL",
    "DOCKERFILE_BROWSER",
    "ImageKind",
    "build_image",
    "resolve_image_tag",
    "common_image_tag",
]
