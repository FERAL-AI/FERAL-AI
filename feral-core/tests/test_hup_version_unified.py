"""HUP version unification — single canonical version across 5 surfaces.

This is the CI gate for Lane 11 acceptance item "HUP unified across
spec, Python SDK, TS SDK, Swift SDK, iOS Info.plist". Any drift between
these five surfaces — even a hypothetical "we only forgot to bump the
manifest" — fails this test and the build.

The five surfaces (and the one regex / parse line we use for each):

1. Brain canonical (``feral-core/models/protocol.py``) → ``HUP_VERSION``
   Python constant. Authoritative — every other surface mirrors this.
2. Public spec (``feral-nodes/HUP_SPEC.md``) → ``**Version:** `HUP v<X>``
   bold-version line in the header.
3. Python node SDK (``feral-nodes/python-node-sdk/src/feral_node_sdk/
   schemas.py``) → ``HUP_VERSION`` literal.
4. TypeScript node SDK (``feral-nodes/ts-node-sdk/src/schemas.ts``) →
   ``HUP_VERSION`` string literal.
5. Swift node SDK (``feral-nodes/ios-node-sdk/Sources/FeralNodeSDK/
   Info.swift``) → ``hupVersion`` string literal. The companion iOS app
   re-vendors this same file under ``feral-companion-ios/`` — the test
   walks that file too when the path is present on disk (it is for the
   parallel-repo developer workflow described in
   ``ASOS/AUDIT-r14/phase2/prompts/11-nodes-ios-hardware.md``).

Why a literal-scrape test rather than importing each surface: TS + Swift
cannot be imported into the Python interpreter, so the unification gate
has to live as a text-level coherence check.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from models.protocol import HUP_VERSION as BRAIN_HUP_VERSION


REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract(pattern: str, text: str, *, source: str) -> str:
    m = re.search(pattern, text, flags=re.MULTILINE)
    if not m:
        raise AssertionError(
            f"could not find HUP version literal in {source} using pattern {pattern!r}"
        )
    return m.group(1)


def test_hup_version_is_1_4_0_in_brain():
    """The brain's canonical constant is the source of truth.

    Bumped to 1.4.0 on 2026-08-22 for the somatic and physiology
    additions: `somatic_state`, the `hrv` / `activity` device_event
    types, and the optional `moments` block on `ambient_transcript`.
    Additive throughout, which is a MINOR per §1 of the spec.
    """
    assert BRAIN_HUP_VERSION == "1.4.0", (
        "Brain HUP_VERSION must be '1.4.0'. Every other surface mirrors "
        "this. If you intentionally bumped the protocol, update every "
        "surface in this test and bump the v1.0 release gate evidence."
    )


def test_hup_spec_header_matches_brain():
    spec = _read(REPO_ROOT / "feral-nodes" / "HUP_SPEC.md")
    version = _extract(
        r"\*\*Version:\*\*\s+`HUP\s+v([0-9]+\.[0-9]+\.[0-9]+)`",
        spec,
        source="feral-nodes/HUP_SPEC.md header",
    )
    assert version == BRAIN_HUP_VERSION, (
        f"HUP_SPEC.md header advertises v{version} but brain canonical is "
        f"v{BRAIN_HUP_VERSION}. The spec is the operator-facing source of "
        "truth — drift here is the same kind of bug that motivated this test."
    )


def test_python_node_sdk_version_matches_brain():
    schemas = _read(
        REPO_ROOT
        / "feral-nodes"
        / "python-node-sdk"
        / "src"
        / "feral_node_sdk"
        / "schemas.py"
    )
    version = _extract(
        r'^HUP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        schemas,
        source="feral_node_sdk/schemas.py",
    )
    assert version == BRAIN_HUP_VERSION


def test_python_node_sdk_init_version_matches_brain():
    init = _read(
        REPO_ROOT
        / "feral-nodes"
        / "python-node-sdk"
        / "src"
        / "feral_node_sdk"
        / "__init__.py"
    )
    version = _extract(
        r'^HUP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        init,
        source="feral_node_sdk/__init__.py",
    )
    assert version == BRAIN_HUP_VERSION


def test_ts_node_sdk_version_matches_brain():
    schemas = _read(
        REPO_ROOT / "feral-nodes" / "ts-node-sdk" / "src" / "schemas.ts"
    )
    version = _extract(
        r'export\s+const\s+HUP_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        schemas,
        source="ts-node-sdk schemas.ts",
    )
    assert version == BRAIN_HUP_VERSION


def test_swift_node_sdk_version_matches_brain():
    info = _read(
        REPO_ROOT
        / "feral-nodes"
        / "ios-node-sdk"
        / "Sources"
        / "FeralNodeSDK"
        / "Info.swift"
    )
    version = _extract(
        r'hupVersion\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        info,
        source="ios-node-sdk Info.swift",
    )
    assert version == BRAIN_HUP_VERSION


def test_seed_manifests_match_brain():
    """Track-B daemon manifests (wristband + w300) must mirror the brain."""
    for manifest in [
        REPO_ROOT / "feral-nodes" / "wristband_daemon" / "manifest.json",
        REPO_ROOT / "feral-nodes" / "w300_daemon" / "manifest.json",
    ]:
        raw = _read(manifest)
        version = _extract(
            r'"hup_version"\s*:\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
            raw,
            source=str(manifest.relative_to(REPO_ROOT)),
        )
        assert version == BRAIN_HUP_VERSION, (
            f"{manifest.relative_to(REPO_ROOT)}: hup_version is v{version} "
            f"but brain canonical is v{BRAIN_HUP_VERSION}"
        )


@pytest.mark.parametrize(
    "rel_path",
    [
        # Co-located companion-ios checkout used during the parallel-repo
        # dev workflow described in
        # ASOS/AUDIT-r14/phase2/prompts/11-nodes-ios-hardware.md.
        Path("..") / "feral-companion-ios" / "Sources" / "FeralNodeSDK" / "Info.swift",
        Path("..") / ".." / "feral-companion-ios" / "Sources" / "FeralNodeSDK" / "Info.swift",
    ],
)
def test_companion_ios_swift_version_matches_brain_when_present(rel_path: Path):
    """If the companion-ios checkout is next to ASOS, its vendored SDK
    must mirror the brain.

    Skipped when the directory isn't present so CI runners without the
    companion repo cloned alongside don't fail. The parallel-PR
    coordination in WORK_LOG ensures the iOS PR's CI re-asserts this
    against its own canonical Info.swift in the iOS repo's tests.
    """
    candidate = (REPO_ROOT / rel_path).resolve()
    if not candidate.is_file():
        pytest.skip(f"companion-ios checkout not present at {candidate}")
    info = _read(candidate)
    version = _extract(
        r'hupVersion\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
        info,
        source=str(candidate),
    )
    assert version == BRAIN_HUP_VERSION


def test_companion_ios_info_plist_when_present():
    """If the companion-ios Info.plist is co-located, the FERALHUPVersion
    key MUST match the brain. The key is added by Lane 11 to make the
    iOS app self-describe the protocol it speaks so operators can verify
    a mismatched build without launching the app.
    """
    for candidate in [
        REPO_ROOT.parent / "feral-companion-ios" / "App" / "Info.plist",
        REPO_ROOT.parent.parent / "feral-companion-ios" / "App" / "Info.plist",
    ]:
        if candidate.is_file():
            plist = _read(candidate)
            m = re.search(
                r"<key>FERALHUPVersion</key>\s*<string>([0-9]+\.[0-9]+\.[0-9]+)</string>",
                plist,
            )
            assert m is not None, (
                f"{candidate}: FERALHUPVersion key missing — Lane 11 contract "
                "is that the iOS app self-describes the HUP version it speaks."
            )
            assert m.group(1) == BRAIN_HUP_VERSION
            return
    pytest.skip("companion-ios Info.plist not present co-located with ASOS")


# ─────────────────────────────────────────────
# The envelope, not just the version literal
# ─────────────────────────────────────────────
# Everything above checks that five surfaces AGREE on the version string.
# None of it ever looked at a frame. HUP_SPEC.md section 5 says every HUP
# frame carries ``hup_version``, ``type``, ``ts`` and ``payload``; six
# brain-to-node sends spelled that out by hand and about twenty did not,
# including every ``hup_action_request`` -- the actuator command frame.
#
# Impact was nil while no shipping SDK validated envelopes (the Swift
# decoder documents the omission and tolerates it by name). It bites the
# first third-party daemon that follows the published spec, which is the
# audience the spec exists for.
#
# Two gates below. The AST scan is the regression guard: a new send that
# bypasses ``hup_frame`` / ``stamp_hup_envelope`` / a gated
# ``build_action_request`` frame fails the build. The behaviour tests in
# ``tests/test_hup_envelope_and_grants.py`` drive the real code and assert
# what lands on the wire.

import ast

#: (module, function names whose ``send_json`` calls go to a NODE).
#: Client-bound sends are out of scope -- browsers read ``FeralMessage``,
#: which is a different envelope. The list is explicit rather than
#: inferred because "is this websocket a node?" is not decidable from the
#: syntax, and a wrong inference either misses a leak or fails the build
#: on a browser frame.
NODE_BOUND_SENDERS: tuple = (
    ("api/server.py", (
        "_send_protocol_error",
        "daemon_session",
        "_handle_ambient_transcript",
        "_handle_ambient_digest_request",
    )),
    ("api/state.py", ("send_to_daemon", "_send_dict_to_node")),
    ("api/routes/dashboard.py", ("_push_health_update",)),
    ("agents/orchestrator.py", ("request_frame",)),
    ("agents/tool_runner.py", (
        "execute_daemon_command",
        "execute_daemon_command_with_ack",
    )),
    ("hardware/mesh.py", ("invoke", "execute")),
    ("hardware/protocol.py", ("execute",)),
    ("gateway/protocol.py", ("node_invoke",)),
)

#: Calls whose return value is a complete HUP frame.
ENVELOPE_BUILDERS: frozenset = frozenset({"hup_frame", "stamp_hup_envelope"})

BRAIN_ROOT = Path(__file__).resolve().parents[1]


def _function_nodes(tree: ast.AST, names) -> list:
    """Every def/async def in ``tree`` whose name is in ``names``."""
    wanted = set(names)
    return [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in wanted
    ]


def _is_enveloped(expr: ast.AST, enveloped_names: set) -> bool:
    # A dict literal that spells the envelope out by hand is compliant on
    # the wire, so it passes. Six sends did exactly this and were the only
    # correct ones in the tree. It is still worse than the builder --
    # `time.time()` and the version constant get retyped at every site,
    # which is how five of them ended up with the version and none of the
    # twenty others did -- but a gate that failed correct frames would be
    # asserting a style rule while claiming to assert the spec.
    if isinstance(expr, ast.Dict):
        keys = {
            k.value for k in expr.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        return {"hup_version", "ts", "type"} <= keys
    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Name) and func.id in ENVELOPE_BUILDERS:
            return True
        if isinstance(func, ast.Attribute) and func.attr in ENVELOPE_BUILDERS:
            return True
        return False
    # ``gate.frame`` -- hardware.action_frames.ActionRequest.frame is
    # built by hup_frame and is None when the capability gate refused.
    if isinstance(expr, ast.Attribute) and expr.attr == "frame":
        return True
    if isinstance(expr, ast.Name):
        return expr.id in enveloped_names
    return False


def _collect_enveloped_names(fn: ast.AST) -> set:
    """Locals assigned an enveloped expression anywhere in ``fn``.

    Whole-function rather than flow-sensitive: the shape in the tree is
    always ``msg = gate.frame`` a few lines above ``send_json(msg)``, and
    a scan that demanded ordering would reject nothing extra while being
    far easier to get wrong.
    """
    names: set = set()
    # Two passes so ``a = hup_frame(...)`` then ``b = a`` both land.
    for _ in range(2):
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and _is_enveloped(node.value, names):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return names


def _send_json_calls(fn: ast.AST) -> list:
    return [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send_json"
    ]


def test_every_brain_to_node_send_carries_the_hup_envelope():
    """HUP_SPEC.md section 5, enforced on the brain's own source.

    A send that builds its dict inline is a send that will be missing
    ``hup_version`` and ``ts``, because that is what all twenty of them
    did. Route it through ``models.protocol.hup_frame`` (building a new
    frame), ``stamp_hup_envelope`` (a dict that already exists), or
    ``hardware.action_frames.build_action_request`` (which does both and
    applies the section 6 capability gate).
    """
    offenders: list = []
    scanned = 0
    for rel_path, fn_names in NODE_BOUND_SENDERS:
        path = BRAIN_ROOT / rel_path
        assert path.is_file(), f"{rel_path} moved; update NODE_BOUND_SENDERS"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = _function_nodes(tree, fn_names)
        found_names = {f.name for f in found}
        missing = set(fn_names) - found_names
        assert not missing, (
            f"{rel_path}: {sorted(missing)} no longer exist; the scan would "
            "silently stop covering them. Update NODE_BOUND_SENDERS."
        )
        for fn in found:
            enveloped = _collect_enveloped_names(fn)
            for call in _send_json_calls(fn):
                scanned += 1
                arg = call.args[0] if call.args else None
                if arg is None or not _is_enveloped(arg, enveloped):
                    offenders.append(
                        f"{rel_path}:{call.lineno} in {fn.name}()"
                    )

    assert scanned >= 12, (
        f"only {scanned} node-bound send_json calls found; the scan has "
        "probably stopped matching real code"
    )
    assert not offenders, (
        "brain -> node frames missing the HUP_SPEC.md section 5 envelope "
        "(hup_version + ts). Route each through models.protocol.hup_frame "
        "(a new frame), stamp_hup_envelope (a dict that already exists), "
        "or hardware.action_frames.build_action_request (both, plus the "
        "section 6 capability gate):\n  " + "\n  ".join(offenders)
    )


def test_the_removed_command_alias_is_gone_from_every_sender():
    """``{"type": "command"}`` has not been a HUP frame since 2026.7.0.

    ``tests/test_hup_protocol.py`` asserted this for ``hardware/mesh.py``
    and ``agents/tool_runner.py`` and nothing else, and the gateway's
    ``node.invoke`` fallback went on sending it -- top-level ``command``
    and ``args``, no ``payload``, no envelope -- into a socket where no
    SDK has a branch for it, while returning ``{"dispatched": true}``.
    """
    for rel_path, _ in NODE_BOUND_SENDERS:
        tree = ast.parse((BRAIN_ROOT / rel_path).read_text(encoding="utf-8"))
        # AST, not a substring scan: the note in gateway/protocol.py that
        # records what the fallback used to send would trip a text match,
        # and deleting the note to satisfy the test would delete the
        # explanation of why the fallback looks the way it does.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant) and key.value == "type"
                    and isinstance(value, ast.Constant) and value.value == "command"
                ):
                    raise AssertionError(
                        f"{rel_path}:{node.lineno} still builds the removed "
                        "`command` alias; HUP_SPEC.md section 5.5 maps it to "
                        "hup_action_request."
                    )
