# Adding Skills to FERAL

Extending FERAL relies on mapping Python or JSON boundaries onto LLM function descriptors.

## Defining the Capability (JSON Manifest)

Every capability inside FERAL must be governed by a schema explicitly readable by the Anthropic/OpenAI APIs.

**Create `feral-core/skills/manifests/new_skill.json`:**
```json
{
  "skill_id": "iot_light",
  "requires_daemon": true, 
  "trigger_phrases": ["turn on the light", "make it brighter"],
  "categories": ["iot", "smart_home"],
  "description": "Standardized control for the connected smart bulb.",
  "brand": {
    "name": "Smart Bulb",
    "primary_color": "#f1c40f"
  },
  "endpoints": [
    {
      "id": "set_color",
      "method": "WS_EXECUTE",
      "url": "local_daemon",
      "description": "Sets the RGB color of the light bulb.",
      "params": [
        {
          "name": "rgb_hex",
          "type": "string",
          "description": "The exact hex color excluding the '#', e.g. 'FF0000'.",
          "required": true
        }
      ],
      "ui_hint": "card"
    }
  ]
}
```

The `requires_daemon: true` field explicitly tells the Orchestrator to bypass HTTP routing and instead pack the execution as an `execute` payload directed at a connected WebSocket node.

### `skill_id` and `brand` are both required, and both are load-bearing

`skill_id` is the skill's **name** everywhere it is referred to: the
directory it installs into (`~/.feral/skills/<skill_id>/`), the string a
user types (`feral install iot_light`), the string an app writes in
`skill_dependencies`, and the `name` the registry publishes it under.
Leave it out and `SkillManifest` generates a UUID, the skill installs
under a random directory name, and nothing can name it again. The
registry refuses to publish a bundle without one.

`brand.name` is what the install dialog shows as the skill's name before
anything is written to the user's disk. `SkillManifest.brand` has no
default, so a manifest without it does not load, and the registry
refuses that bundle at publish rather than letting it fail on a user's
machine.

## Defining the Hardware Code

To handle the incoming payload, modify your daemon code (e.g. `robot_template.py`) to map against the `skill_id` endpoints.

```python
        if executor == "set_color":
            hex_color = args.get("rgb_hex", "FFFFFF")
            # --- Insert GPIO/Serial Code Here ---
            set_bulb_color(hex_color)
            result_msg = f"Bulb changed to {hex_color}"
```

## Defining Pure Python Cloud Skills

If you want the API server to perform the execution (e.g., hitting Stripe or Gmail instead of local hardware):
1. Create a `.py` file inside `feral-core/skills/impl/`.
2. Inherit from `BaseSkill`.
3. Set `requires_daemon: false` in the corresponding manifest.
4. Define the execution override explicitly, handling the exact signature provided by your `endpoints`.

The Orchestrator automatically handles the translation from LLM Tool Calling -> Payload Structuring -> Skill Invocation.

## Declaring Permissions

`permissions` is a closed vocabulary, not free text. It is what the
install dialog shows the user before your skill is written to their
disk, so every entry has to be a capability FERAL can describe in a
sentence. A manifest naming anything else **fails to load**, with the
allowed values in the error.

The vocabulary is defined once, in
`feral-core/models/skill_manifest.py::SkillPermission`:

| Permission | Shown to the user as |
|---|---|
| `filesystem` | Your files |
| `network` | Internet access |
| `code_execution` | Run programs |
| `screen` | Screen contents |
| `input_control` | Keyboard and mouse |
| `camera` | Camera |
| `vision` | Image analysis |
| `messaging` | Messages |
| `contacts` | Contacts |
| `calendar` | Calendar |
| `health_data` | Health data |
| `smart_home` | Smart home devices |
| `hardware` | Connected hardware |
| `system_settings` | System settings |
| `memory` | Stored memory |
| `identity` | Your profile |
| `notifications` | Notifications |
| `scheduling` | Background schedules |
| `autonomy` | Acting on its own |
| `llm` | Model providers |
| `browser` | Your web browser |
| `commerce` | Purchases |

The one-line explanation attached to each is in `PERMISSION_DESCRIPTIONS`
in the same file, and is served to clients with the permission list so
the wording is reviewed in one place. Adding a member is a deliberate
act: write the label and the sentence at the same time.

Declare what your skill actually reaches. Under-declaring does not gain
you any access (nothing grants capability from this list today; it is a
disclosure), and it misleads the person deciding whether to install.

## Publishing

```bash
feral publisher register           # registers your Ed25519 public key
feral publish --skill ./my_skill   # signs the bundle and uploads it
```

`cli/publish.py` signs the SHA-256 of the tarball with your private key,
as its **hex digest encoded ASCII**, which is the message
`feral_registry/signing.py` verifies and the one `cli/install.py`
re-verifies on the way back in. Both install paths check it before
anything lands on disk: `feral install <name>` from a shell, and the
Marketplace page in the web client, which additionally shows the user
your permission list and the signature status and will not install until
they confirm. An unsigned or tampered bundle is refused rather than
installed with a warning.

`feral publish` posts two documents: the tarball, and a metadata
envelope derived from your manifest (`kind`, `name`, `version`,
description, author, plus `skill_id`). You do not write the envelope;
`registry_envelope()` builds it, and `name` is your `skill_id`, not your
`brand.name`. See
[Marketplace, publish flow](mintlify/marketplace/overview.mdx) for the
per-kind table.

The bundle is your skill directory as it stands: `manifest.json` at the
root next to `impl.py`. That `manifest.json` must be the `SkillManifest`
itself, not a registry envelope wrapping it. Publish reads the tarball
and rejects a bundle whose `manifest.json` FERAL could not load, naming
the missing key.

Your item is then addressable by `skill_id`. `feral install iot_light`
and `skill_dependencies: ["iot_light"]` both resolve through
`GET /api/v1/item/iot_light?kind=skill`; the registry's UUID also
resolves, but nothing you write by hand should carry one. If two
versions of your skill are published, a bare name resolves to the
highest; see the
[marketplace overview](mintlify/marketplace/overview.mdx) for the full
rule, including the two cases the registry refuses to guess at.

A successful publish is a **submission**, not a release. The item lands
`submitted` / `private` and `feral install` answers 404 until a reviewer
approves it; `feral publish` prints the status it got back so you are
not left testing against a 404. Track it with:

```bash
curl -H "Authorization: Bearer $(cat ~/.feral/publisher.token)" \
  https://registry.feral.sh/api/v1/publisher/submissions
```

For local iteration, `MarketplaceClient.install(skill_id, source_url=...)`
still installs straight from a URL or a git clone with no verification.
It logs a loud warning and is not reachable from the HTTP API; use it
from the CLI while you are developing your own skill.
