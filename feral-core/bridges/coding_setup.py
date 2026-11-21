"""Explicit consumer coding setup. No project plugins or ambient model keys."""
import json
import hashlib
from pathlib import Path
from urllib.parse import urlparse

from config.loader import feral_home


def setup_path():
    return feral_home() / "coding-workspace.json"


def read_setup():
    path = setup_path()
    if not path.exists():
        return {"workspaces": [], "provider": None}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Coding setup is invalid")
    workspaces = value.get("workspaces", [])
    if not isinstance(workspaces, list) or any(not isinstance(p, str) or not Path(p).is_absolute() for p in workspaces):
        raise ValueError("Coding project grants are invalid")
    provider = value.get("provider")
    if provider is not None:
        if not isinstance(provider, dict) or not isinstance(provider.get("prepared", False), bool):
            raise ValueError("Coding provider setup is invalid")
        validate_provider(provider)
    return value


def save_setup(value):
    path = setup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def validate_workspace(path):
    """The pinned binary still discovers .opencode plugins despite flags.

    Block their automatic execution; never remove or rewrite project files.
    This preflight is not an OS filesystem sandbox.
    """
    directory = Path(path).resolve()
    if not directory.is_dir():
        raise ValueError("Project folder does not exist on the brain computer")
    for ancestor in (directory, *directory.parents):
        config_dir = ancestor / ".opencode"
        if config_dir.exists():
            raise ValueError(f"OpenCode project settings or plugins require review: {config_dir}. Choose a checkout without .opencode settings; this workspace cannot safely auto-load them.")
    return str(directory)


def validate_provider(body):
    model = str(body.get("model") or "").strip()
    base = str(body.get("base_url") or "http://127.0.0.1:11434/v1").rstrip("/")
    parsed = urlparse(base)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Choose a local Ollama HTTP endpoint on localhost")
    if parsed.path != "/v1":
        raise ValueError("Ollama endpoint must end in /v1")
    if not model or len(model) > 160 or any(c.isspace() for c in model):
        raise ValueError("Enter an installed Ollama model name, for example qwen2.5:3b")
    return {"kind": "ollama", "base_url": base, "model": model}


async def prepare_provider(provider):
    """Explicitly create an alias with sufficient context; never pull weights."""
    import httpx
    root = provider["base_url"][:-3]
    async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
        shown = await client.post(root + "/api/show", json={"model": provider["model"]})
        shown.raise_for_status()
        details = shown.json()
        if details.get("remote_model") or details.get("remote_host") or "cloud" in provider["model"].lower():
            raise ValueError("Choose a downloaded local model; cloud Ollama models are not supported here")
        if "tools" not in details.get("capabilities", []):
            raise ValueError("This model does not support coding tool calls. Choose a tool-capable Ollama model")
        contexts = [value for key, value in details.get("model_info", {}).items() if key.endswith(".context_length")]
        if contexts and max(contexts) < 16384:
            raise ValueError("This model needs at least 16384 context tokens for the coding workspace")
        alias = "theora-coding-" + hashlib.sha256(provider["model"].encode()).hexdigest()[:16]
        created = await client.post(root + "/api/create", json={"from": provider["model"], "model": alias,
            "parameters": {"num_ctx": 16384}, "stream": False})
        created.raise_for_status()
        if created.json().get("status") != "success":
            raise ValueError("Ollama did not confirm the coding model was prepared")
    return {**provider, "source_model": provider["model"], "model": alias, "prepared": True, "context_tokens": 16384}


def opencode_environment():
    provider = read_setup().get("provider")
    if not provider:
        return {}
    provider = validate_provider(provider)
    config = {
        "model": "theora-local/" + provider["model"],
        "enabled_providers": ["theora-local"],
        "provider": {"theora-local": {
            "npm": "@ai-sdk/openai-compatible", "name": "Local Ollama",
            "options": {"baseURL": provider["base_url"]},
            "models": {provider["model"]: {"name": provider["model"], "limit": {"context": 16384, "output": 2048}}},
        }},
        "permission": "ask",
        "agent": {"title": {"disable": True}},
    }
    return {"OPENCODE_CONFIG_CONTENT": json.dumps(config),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1", "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            "OPENCODE_DISABLE_MODELS_FETCH": "1", "OPENCODE_DISABLE_AUTOUPDATE": "1"}
