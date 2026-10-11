# Mac local-reply investigation

Updated October 3, 2026. This is a failed actual-app journey and its bounded
diagnosis, not successful local-model or distribution acceptance.

## Actual app observation

Unmodified native9.34/build2026100301, source4eded179e, was launched with a fresh
synthetic profile and a separate preferences suite. Computer Use exercised avatar
selection, onboarding, reviewed provider configuration and the connected main
window. The first synthetic text request produced the empty-response fallback
after a retry. The app stayed connected; Quit removed the native process and its
owned profile children. The pre-existing local model service was reused and left
running. This test establishes neither a crash nor an OS Keychain problem.

Onboarding also presented a second provider-setup flow after its initial start-chat
action. That duplication remains a usability issue to resolve. Physical glasses,
microphone, cloud accounts and personal profiles were not used.

## Provider evidence

The cached acceptance alias resolved to Qwen2.5 3B, architectureqwen2, with
completion/tools capabilities and context16384. It was not a verified thinking
model. The actual raw HTTP experiments used only synthetic requests:

| Request | Observed raw outcome |
| --- | --- |
| Minimal synthetic text | HTTP200, normal stop, visible answer, no tools or reasoning |
| Captured normal FERAL request with nine tool definitions | HTTP200, normal stop, visible answer, no tools or reasoning |
| Original GUI wording in an isolated prepared request | HTTP200, normal stop, zero visible text, zero tools, zero reasoning |

The last request contained11917 schema bytes and106 history bytes, matching those
logged in the GUI failure. Its system content differed by15 bytes, so it is not
the lost original wire body. Reported usage was6649 prompt tokens and25 completion
tokens; this does not establish where those completion tokens went. It rules out
an exhausted1024 output budget for this reproduction. The provider response was
already empty before FERAL's display sanitizer; a parser defect is not established.

The capture used the unchanged bundled runtime behind a loopback proxy. The
capture phase returned synthetic text and never forwarded tool calls or executed
tools. Owned processes stopped, listeners closed and all487 bundled production
Python hashes remained unchanged. One capture wrapper failed after successful
capture/shutdown because its final receipt filename already existed; its original
receipt and a separate recovered proof were retained. Sandbox bind refusals
occurred before process launch and were not reported as app failures.

## Repair and remaining acceptance

Streaming and nonstreaming paths now emit `provider_empty_response` after the
existing empty-generation retry. The turn is failed, with an actionable retry or
provider-selection message; no empty assistant history or completed-turn receipt
is manufactured. Valid replies, tool-only results, partial streams and existing
provider errors retain their behavior. The focused regression integration passed
87 tests, including15 new cases; the parent voice/chat integration passed471.

The unchanged bundled parser passed four synthetic SSE cases: empty and short
unpunctuated text, each with explicit completion and EOF. This distinguishes
parser fixtures from the actual upstream empty result. A valid raw replay does
not certify the GUI journey, and an honest error does not resolve model
reliability. An exact-source rebuilt Mac journey remains required. No raw
synthetic capture or runtime profile is checked in.
