# Selected scene image cost admission

Actual native9.60 capture succeeded but scene analysis was refused before HTTP
with `pricing_unavailable`. GPT-5.4 rates were already present; budget admission
classified every image as unpriceable. Active spending caps therefore blocked
the selected vision request.

The repair adds conservative per-request image and prompt bounds only for the
vision cost site, exact OpenAI GPT-5.4/published snapshot and the standard
OpenAI origin. The Responses dispatcher supplies the actual selected provider
and client endpoint. Encoded image bytes are excluded from text estimation;
the wire payload is unchanged. Existing atomic reservation and settlement
remain active. No cap, saved provider choice or permission is disabled.

The bound follows the documented patch ceilings and multiplier. Unsupported
models, endpoints, media, service tiers, hidden history, tools and requests above
the long-context threshold remain unpriceable under caps. Conservative holds
can reject a very tight cap even when actual usage would be smaller.
[Image billing basis](https://developers.openai.com/api/docs/guides/images-vision),
[GPT-5.4 pricing and long-context rules](https://developers.openai.com/api/docs/models/gpt-5.4).

Failure-first testing reproduces the original refusal through actual
SceneAnalyzer, selected vision adapter, Responses and SQLite budget code using
an inert provider transport. After integration185 focused tests pass with six
existing warnings, and full-core CI Ruff passes. Tests cover actual settlement,
concurrent reservations, tight-cap refusal before HTTP, unsupported selections,
and dispatched failure retaining unknown outcome without fallback. These checks
do not establish live screenshot recognition or browser workflow completion.
