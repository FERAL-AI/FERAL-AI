"""Local-provider helpers shared by the Ollama / LM Studio wizard paths.

Ollama pulls models via ``ollama pull <name>``. We stream the
subprocess output directly so the user sees real progress instead of
a silent hang. LM Studio has no CLI install path — the wizard just
surfaces the "load a model in the UI and press Enter" instruction.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import Awaitable, Callable, Optional


def _has_cli(binary: str) -> bool:
    return shutil.which(binary) is not None


def ollama_cli_installed() -> bool:
    return _has_cli("ollama")


async def ollama_pull_model(
    model: str,
    *,
    on_line: Optional[Callable[[str], None]] = None,
) -> int:
    """Run ``ollama pull <model>`` and stream output back.

    Returns the process exit code. ``on_line`` is invoked with each
    non-empty line of stdout so the caller can pipe to a Rich
    console (or the wizard's plain printer) without buffering.
    """
    if not ollama_cli_installed():
        raise RuntimeError(
            "The `ollama` CLI isn't on $PATH. Install it from "
            "https://ollama.com/download, then pick Ollama again."
        )

    proc = await asyncio.create_subprocess_exec(
        "ollama",
        "pull",
        model,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").rstrip()
        if text and on_line is not None:
            on_line(text)
    return await proc.wait()


STARTER_OLLAMA_MODELS = (
    "llama3.3:8b",
    "qwen2.5-coder:7b",
    "mistral:7b",
    "phi3:mini",
)


OLLAMA_INSTALL_HINT = (
    "Install Ollama:\n"
    "  macOS / Linux:  curl -fsSL https://ollama.com/install.sh | sh\n"
    "  Windows:        https://ollama.com/download\n"
    "Then run `ollama serve` in another terminal and retry."
)


LMSTUDIO_INSTRUCTIONS = (
    "LM Studio isn't responding on http://localhost:1234.\n"
    "  1. Install LM Studio from https://lmstudio.ai/\n"
    "  2. Download a model in the UI (e.g. 'Llama 3 8B Instruct').\n"
    "  3. Click the 'Local Server' tab and press Start.\n"
    # Ends at "pick it again here", not "re-run the wizard": this text is
    # read from inside step 1 of 16, and the provider picker re-probes
    # every time it is shown, so leaving setup is never the remedy.
    "  4. Then pick LM Studio again here so the loaded model is seen."
)
