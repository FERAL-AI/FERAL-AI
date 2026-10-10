# Computer and browser vision acceptance, October 10

The new physical trace reached desktop tool execution without an approval wait.
It exposed three independent capability failures: the selected Ollama vision
model was unavailable, the 128-tool realtime catalog retained browser navigation
while dropping inspection/editing tools, and captured images were omitted from
realtime tool outputs without a separate image message reaching the model.
The physical phone also continued to report VoiceProcessingIO render failures.

## Implemented source repair

Nine browser inspection and editing endpoints are pinned within the existing
128-tool limit. Existing schemas, per-skill coverage and central authorization
remain intact. The catalog does not authorize an unrelated Chrome profile.

Realtime capture receipts can deliver one validated bounded PNG/JPEG as a user
image input between the tool receipt and response continuation. Exact callback
ownership, response epoch and the selected vision destination are checked across
awaits. Direct image delivery requires explicit OpenAI vision selection with the
standard OpenAI endpoint; local or separate destinations retain truthful unseen
receipts. No saved personal provider choice is changed. Desktop command/path
receipts do not become images. Guidance directs the model to registered DOM or
Accessibility controls and requires outcome readback.
[Official image-input schema](https://developers.openai.com/api/docs/guides/realtime-conversations).

Explicit cloud scene selection uses the shared selected-vision adapter instead
of falling through to primary chat. This preserves provider-specific credentials,
budget admission and no-fallback behavior. Screen capture reuses the initialized
scene runtime and reports empty analysis explicitly.

## Verification boundaries

The catalog worker passed 85 focused regressions. Realtime integration passed
165 focused regressions; its final screenshot suite passed 27 cases. The parent
passed 43 scene/capture regressions and a 42-case combined new-suite gate.
Full-core CI Ruff passes. Configured Mac mypy retains 788 baseline diagnostics
with zero additions or removals after line-number normalization.

The live harness uses one synthetic local application in regular Chrome and one
tracked FERAL task. Its graphical reference code requires actual image reading;
the task also exercises a deliberate validation failure, internal page tabs,
dynamic activity rows, itinerary save and local draft save. Server persistence
and independent DOM readback verify completion. Python, JavaScript and harness
preflight checks prepare this run; they do not count as live completion.

Live Chrome inspection confirms remote debugging is disabled. Temporary
permission confirmation is pending because enabling it exposes saved browser
data to connected applications. No browser setting has been changed. The new
bundle and physical audio acceptance remain pending; the previously installed
9.56 does not contain this source repair.
