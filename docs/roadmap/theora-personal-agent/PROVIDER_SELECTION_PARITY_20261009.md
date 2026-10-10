# Provider selection and runtime identity

October 9, 2026. These source changes extend the existing configuration and
runtime. They are outside immutable Mac 9.47 until exact-source packaging.

Native setup and settings consume strict runtime_supported/setup_selectable
flags. Automatic choices require both to be true. Catalog-only and older
unclassified entries remain explicitly configurable as custom gateways; saved
gateway endpoints are preserved, not replaced by catalog defaults. Saved keys,
configuration and chat_ready metadata do not prove successful inference,
FERAL tool execution or audio readiness.

Settings and onboarding request the same recommended chat-model projection.
Explicit live discovery now decodes the backend's string model IDs. Provider
probes accept identified, consistent negative receipts while withholding private
error details. Mismatched IDs and contradictory probe results refuse acceptance.
Ambient reads still use cached suggestions and do not contact providers.

Moonshot is the catalog ID for the Kimi runtime. The provider-list flag previously
checked the catalog ID against the runtime registry, while save/switch and runtime
startup also rejected that identity. A shared exact alias conversion now applies
to startup, activation, fallback configuration and routed candidates. Configuration
retains the catalog ID, runtime uses kimi, and alias duplicates cannot create a
second fallback attempt against the same provider. Unknown providers remain
unsupported; explicitly configured gateways retain their existing behavior.

Executed isolated checks:

- Native provider fixtures: 66 assertions; onboarding: 89 assertions. These are
  controlled HTTP/provenance fixtures, without a real provider request.
- Backend API, registry, runtime alias and base-URL suites: 89 passed, seven
  warnings, 2.44 seconds. Actual LLMProvider startup/switch/reconfigure and
  registered API routes verify alias activation, persistence and fallback
  deduplication. No cloud inference or credential access is accepted.
- Changed backend files pass the repository Ruff rules.

The earlier parent test invocation omitted isolated environment variables and
failed at state initialization. A corrected disposable run then exposed a
test-only assertion naming nonexistent catalog entries. Both failed attempts
remain separate; the final isolated run above passes. Full frozen integration,
packaged native interaction and actual inference/tool/voice acceptance remain
required. Ollama is installed and reachable on the reviewed Mac, but its observed
model inventory is empty; configuration alone cannot satisfy those gates.
