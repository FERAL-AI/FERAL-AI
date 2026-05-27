# Contributing a FERAL Channel

This document covers the manifest schema, the bundled-channel discovery
loop, the signing path, and the SDK-barrel architectural rule for
in-tree channels. The third-party extension SDK and external discovery
path are tracked separately and not yet shipped.

## 1. What a FERAL channel is

A **channel** is the bridge between FERAL and a messaging surface
(Telegram, Slack, Discord, WhatsApp, …). The runtime implementation
lives under `feral-core/channels/` and is built on the abstract
`Channel` base class in
[`feral-core/channels/base.py`](../feral-core/channels/base.py). Every
channel must ship a **manifest** — `feral-channel.manifest.json` —
beside its adapter, declaring the providers it speaks to, the env vars
its auth needs, and the capabilities it advertises.

The manifest is the seam that makes channels addressable from outside
the in-tree Python surface (CLI, Settings dropdown, capability planner,
third-party catalogs).

## 2. Manifest schema reference

Authoritative schema: [`feral-core/channels/manifest_schema.json`](../feral-core/channels/manifest_schema.json) (JSON Schema draft-07).

Required fields:

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | `^[a-z][a-z0-9_-]*$`, ≤ 64 chars. Stable manifest id. |
| `providers` | array&lt;string&gt; | Underlying provider IDs (often `[id]`). |
| `providerAuthEnvVars` | object | Map *provider → list of env var names*. Keys MUST appear in `providers`. |
| `capabilities` | object&lt;string, bool&gt; | At least one capability MUST be `true`. Known keys: `messagingProvider`, `voiceProvider`, `fileProvider`, `webhookProvider`, `videoProvider`, `presenceProvider`. |

Optional fields:

| Field | Type | Notes |
|-------|------|-------|
| `providerAuthChoices` | array | Auth menu (`oauth` / `device-code` / `api-key`) exposed to the user-facing picker. |
| `modelSupport` | object | `modelPrefixes`, `preferredModels` — hint for the model picker. |
| `contracts` | object | Capability-contract version pins (e.g. `{"messaging": "v1"}`). |
| `signature` | object | Ed25519 signature envelope (see §4). |

Bundled example: [`feral-core/channels/telegram/feral-channel.manifest.json`](../feral-core/channels/telegram/feral-channel.manifest.json).

## 3. Adding a new in-tree channel

Bundled manifests live beside an existing in-tree adapter:

1. Implement the adapter under `feral-core/channels/<id>.py` (or a
   subpackage), subclassing `Channel` from `channels.base`.
2. Add the manifest at `feral-core/channels/<id>/feral-channel.manifest.json`.
3. Sign the manifest using
   `feral_core.channels.manifest.sign_manifest(...)` — see §4.
4. Register the adapter in
   `feral-core/tests/test_channel_manifest_contract.py`'s
   `_ADAPTER_BY_MANIFEST_ID` map so the contract test exercises it.
5. Run:

   ```bash
   cd feral-core
   python -m pytest tests/test_channel_manifest_*.py -v --no-cov
   ```

That's the full acceptance loop for an in-tree channel.

## 4. Signing a manifest

Manifests use the **same Ed25519 signer** as GenUI manifests —
`feral_core.genui.manifest_signing` (PyNaCl). The channel manifest's
signature envelope is shaped to match the rest of the manifest
(camelCase keys, ISO-8601 timestamp), but the bytes signed are produced
by `genui.manifest_signing.canonical_json(...)` over the manifest dict
**with the `signature` field removed**.

Programmatically:

```python
from feral_core.channels.manifest import sign_manifest, load_manifest_dict
from feral_core.genui.manifest_signing import generate_keypair

priv, pub = generate_keypair()
signed = sign_manifest(unsigned_dict, priv, public_key_id="my-publisher-key")
manifest = load_manifest_dict(signed)            # validates schema
ok, reason = manifest.is_signed, None
```

Verification:

```python
from feral_core.channels.manifest import verify_signature
ok, reason = verify_signature(manifest)          # trust embedded key
ok, reason = verify_signature(
    manifest,
    public_key_provider=lambda kid: vault.get(kid),  # pin via vault
)
```

The loader-level dial is `load_with_verification(allow_unsigned=...)`:

* `allow_unsigned=False` (default; production): unsigned manifests are
  refused; tampered signatures are refused.
* `allow_unsigned=True` (dev): unsigned manifests are accepted; tampered
  signatures are STILL refused. A present-but-broken signature is
  always fatal.

### Wire-contract reasons

`verify_signature` returns `(False, reason)` strings: `format_error:...`,
`signature_mismatch`, `key_mismatch`, `unsupported_alg:...`, `unsigned`.
Tooling and the CLI rely on these verbatim — do not rename them silently.

## 5. SDK-barrel rule (architectural boundary)

> **Channel code reaches into core ONLY via `feral_core.channels.sdk`;
> everything else is private.** Core must not reach into channel
> internals; channels must not reach into core internals or into other
> channels' modules.

The current shipped surface is the manifest + loader + capability registry.
A formal `feral_core.channels.sdk` barrel (typed runtime context, allowed
helpers, permitted re-exports) is the next architectural step. Until then,
the rule applies prospectively: do not add `from api.state import state`
(or any other `feral-core/{api,services,...}` import) into
`feral-core/channels/manifest.py`, `feral-core/channels/loader.py`, or any
new bundled channel's adapter beyond what the existing in-tree adapters
already do. Treat anything not in `channels/` as **off-limits for new
channel code**.

## 6. Cross-references

* Signing primitive: [`feral-core/genui/manifest_signing.py`](../feral-core/genui/manifest_signing.py)
* Existing channel base class: [`feral-core/channels/base.py`](../feral-core/channels/base.py)
