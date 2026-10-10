# Active cloud connection and catalogue parity

Supported cloud catalogue adapters now bind to the actual resolved runtime
endpoint and credential after startup hydration and successful configuration
activation. The same rule covers setup save, provider switch, Settings updates,
active labeled-key selection and adding a new active key. Provider aliases are
matched by their resolved catalogue identity.

Binding makes no HTTP request and writes no credential. Identical connections
retain their adapter and cached inventory. Inactive provider overrides stay
separate. Key-only changes preserve the existing provider endpoint rather than
resetting it to a catalogue default. Active catalogue publication follows verified
runtime activation; a failed activation does not publish the new key as though
the running model had accepted it. Credentials or settings may already be saved
when activation fails, so the response requires refresh rather than blind retry.

Captured state, orchestrator, runtime and catalogue identity must still match
after awaited activation. Replacing a connection invalidates old in-flight probes;
they refuse stale results rather than retrying or caching them. New binding errors
and affected activation diagnostics use fixed messages, excluding private keys
and endpoints. The existing generic Settings failure retains its HTTP500 contract.

Local providers retain the existing separate binding path. CLI-only and unsupported
catalogue entries are not promoted to HTTP runtime support by this helper. No
inference, tool or voice readiness follows from endpoint binding or an inventory
probe. This does not implement model installation or subscription tool bridging.

Tests use actual adapters, startup initialization and API routes with inert vault
fixtures and MockTransport. They cover supported cloud adapters, custom/default
endpoints, keyless save, labeled-key rotation and aliases, inactive overrides,
failure ordering, owner replacement, late probes, redaction and passive reads.
No provider account or live inference is used. Final source checks and package
identity are recorded in [WORK_STATE](WORK_STATE.md).
