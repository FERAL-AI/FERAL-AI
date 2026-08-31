"""
FERAL Local LLM Inference — MLX + llama.cpp + Ollama
======================================================
Embedded inference that runs in-process — no external server.

- MLXEngine: Apple Silicon Macs via mlx-lm (Metal-accelerated)
- LlamaCppEngine: Cross-platform CPU/CUDA via llama-cpp-python
- OllamaEngine: Delegates to a local Ollama daemon

Model management in ~/.feral/models/ with auto-download from HuggingFace.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import platform
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, AsyncGenerator

import httpx

from config.loader import feral_data_home

logger = logging.getLogger("feral.local_inference")

MODELS_DIR = feral_data_home() / "models"

RECOMMENDED_MODELS: dict[str, str] = {
    "mlx-default": "mlx-community/Qwen2.5-7B-Instruct-4bit",
    "gguf-default": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
    "ollama-default": "llama3.2:3b",
}

# Context window llama.cpp is asked for when a GGUF model is loaded.
#
# This is the one context size this repo actually pins, and it used to be
# a bare literal inside ``LlamaCppEngine.load_model``'s closure, visible
# to nothing outside it. Callers that need to size a prompt against the
# serving model had no way to read it and guessed instead. It is a
# module constant now, and ``LlamaCppEngine.context_window_tokens``
# publishes it, so the value is stated once and derived everywhere else.
LLAMA_CPP_N_CTX = 4096


class LocalLLMEngine(ABC):
    """Abstract interface for local inference engines."""

    def __init__(self, model_id: str):
        self.model_id = model_id
        self.loaded = False
        self.supports_vision = False
        # Usable context window of this engine's model, in tokens.
        #
        # 0 means UNKNOWN, not unlimited. Only llama.cpp pins a window in
        # this file; MLX and Ollama take theirs from the model's own
        # config or the daemon's ``num_ctx``, neither of which this
        # process reads, and inventing a number for them would be exactly
        # the guess this attribute exists to remove. Callers treat 0 as
        # "fall back to the configured window"
        # (``FERAL_CONTEXT_WINDOW_TOKENS``).
        self.context_window_tokens = 0

    @abstractmethod
    async def load_model(self):
        ...

    @abstractmethod
    async def generate(self, prompt: str, max_tokens: int = 512, temperature: float = 0.7) -> str:
        ...

    @abstractmethod
    async def generate_stream(self, prompt: str, max_tokens: int = 512, temperature: float = 0.7) -> AsyncGenerator[str, None]:
        ...

    @abstractmethod
    async def unload(self):
        ...

    def format_chat(self, messages: list[dict], tools: Optional[list[dict]] = None) -> str:
        """Convert chat messages to a plain prompt for local models."""
        parts = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            normalized, has_vision = self._normalize_content(content)
            if has_vision and not self.supports_vision:
                raise ValueError(
                    "Local model received image content but this local engine is text-only. "
                    "Use Ollama VLM via FERAL_VLM_PROVIDER=ollama (for scene/vision) or "
                    "switch to a provider/model that supports multimodal input."
                )
            if role == "system":
                parts.append(f"<|system|>\n{normalized}")
            elif role == "user":
                parts.append(f"<|user|>\n{normalized}")
            elif role == "assistant":
                parts.append(f"<|assistant|>\n{normalized}")
            elif role == "tool":
                parts.append(f"<|tool|>\n{normalized}")

        if tools:
            tool_desc = json.dumps([{"name": t.get("function", {}).get("name", ""), "description": t.get("function", {}).get("description", "")} for t in tools[:10]], indent=2)
            parts.insert(1, f"<|tools|>\n{tool_desc}")

        parts.append("<|assistant|>\n")
        return "\n".join(parts)

    @staticmethod
    def _normalize_content(content: object) -> tuple[str, bool]:
        if isinstance(content, str):
            return content, False

        if isinstance(content, list):
            text_parts: list[str] = []
            has_vision = False
            for block in content:
                if isinstance(block, str):
                    text_parts.append(block)
                    continue
                if not isinstance(block, dict):
                    text_parts.append(str(block))
                    continue

                block_type = str(block.get("type", ""))
                if block_type in ("text", "input_text"):
                    text_parts.append(str(block.get("text", "")))
                elif block_type in ("image_url", "input_image", "image", "image_base64"):
                    has_vision = True
                elif "text" in block:
                    text_parts.append(str(block.get("text", "")))
                else:
                    text_parts.append(json.dumps(block))
            return "\n".join(p for p in text_parts if p).strip(), has_vision

        if isinstance(content, dict):
            if content.get("type") in ("image_url", "input_image", "image", "image_base64"):
                return "", True
            if "text" in content:
                return str(content.get("text", "")), False
            return json.dumps(content), False

        return str(content), False

    def parse_tool_calls(self, text: str) -> tuple[str, list[dict]]:
        """Try to extract tool calls from model output."""
        tool_calls = []
        clean_text = text

        import re
        pattern = r'\{["\']?name["\']?\s*:\s*["\'](\w+)["\'].*?["\']?args["\']?\s*:\s*(\{.*?\})\s*\}'
        matches = re.finditer(pattern, text, re.DOTALL)
        for match in matches:
            try:
                name = match.group(1)
                args = json.loads(match.group(2))
                tool_calls.append({"id": f"local_{int(time.time())}", "name": name, "args": args})
                clean_text = text[:match.start()] + text[match.end():]
            except (json.JSONDecodeError, IndexError):
                continue

        return clean_text.strip(), tool_calls


def _mlx_sampler(temperature: float):
    """Build the sampler mlx-lm wants for a given temperature.

    mlx-lm used to take ``temp=`` on ``generate``/``stream_generate``. It now
    forwards unrecognised kwargs into ``generate_step``, which is keyword-only,
    has no ``temp`` and no ``**kwargs``, so the old call raised

        TypeError: generate_step() got an unexpected keyword argument 'temp'

    rather than ignoring the argument. Sampling moved behind an explicit
    sampler callable, so temperature has to be baked into one here.
    """
    from mlx_lm.sample_utils import make_sampler

    return make_sampler(temp=temperature)


class MLXEngine(LocalLLMEngine):
    """Apple Silicon inference via mlx-lm."""

    def __init__(self, model_id: str):
        super().__init__(model_id)
        self._model = None
        self._tokenizer = None

    async def load_model(self):
        def _load():
            try:
                from mlx_lm import load as mlx_load
                model, tokenizer = mlx_load(self.model_id)
                return model, tokenizer
            except ImportError:
                raise RuntimeError("mlx-lm not installed. Run: pip install mlx-lm")
            except Exception as e:
                raise RuntimeError(f"Failed to load MLX model {self.model_id}: {e}")

        loop = asyncio.get_event_loop()
        self._model, self._tokenizer = await loop.run_in_executor(None, _load)
        self.loaded = True
        logger.info(f"MLX model loaded: {self.model_id}")

    async def generate(self, prompt: str, max_tokens: int = 512, temperature: float = 0.7) -> str:
        if not self.loaded:
            await self.load_model()

        def _generate():
            from mlx_lm import generate as mlx_generate
            return mlx_generate(
                self._model, self._tokenizer, prompt=prompt,
                max_tokens=max_tokens, sampler=_mlx_sampler(temperature),
            )

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _generate)

    async def generate_stream(self, prompt: str, max_tokens: int = 512, temperature: float = 0.7) -> AsyncGenerator[str, None]:
        if not self.loaded:
            await self.load_model()

        def _stream():
            sampler = _mlx_sampler(temperature)
            try:
                from mlx_lm import stream_generate
                # stream_generate yields GenerationResponse dataclasses, not
                # strings. This used to `str()` whatever came out, which turns
                # each chunk into "GenerationResponse(text='hi', token=...)"
                # and streams dataclass reprs at the user. `.text` is the
                # segment.
                return [
                    response.text
                    for response in stream_generate(
                        self._model, self._tokenizer, prompt=prompt,
                        max_tokens=max_tokens, sampler=sampler,
                    )
                ]
            except ImportError:
                # Retained for an mlx-lm without stream_generate; generate()
                # returns the whole completion as one string.
                from mlx_lm import generate as mlx_generate
                result = mlx_generate(
                    self._model, self._tokenizer, prompt=prompt,
                    max_tokens=max_tokens, sampler=sampler,
                )
                return [result]

        loop = asyncio.get_event_loop()
        tokens = await loop.run_in_executor(None, _stream)
        for token in tokens:
            yield token if isinstance(token, str) else str(token)

    async def unload(self):
        self._model = None
        self._tokenizer = None
        self.loaded = False
        logger.info(f"MLX model unloaded: {self.model_id}")


class LlamaCppEngine(LocalLLMEngine):
    """Cross-platform inference via llama-cpp-python."""

    def __init__(self, model_id: str):
        super().__init__(model_id)
        self._llm = None
        self._model_path = None
        # Known, not guessed: load_model asks for exactly this window.
        self.context_window_tokens = LLAMA_CPP_N_CTX

    async def load_model(self):
        def _load():
            try:
                from llama_cpp import Llama
            except ImportError:
                raise RuntimeError("llama-cpp-python not installed. Run: pip install llama-cpp-python")

            model_path = self._resolve_model_path()
            n_gpu = -1 if self._has_gpu() else 0

            return Llama(
                model_path=str(model_path),
                n_ctx=LLAMA_CPP_N_CTX,
                n_gpu_layers=n_gpu,
                verbose=False,
            )

        loop = asyncio.get_event_loop()
        self._llm = await loop.run_in_executor(None, _load)
        self.loaded = True
        logger.info(f"llama.cpp model loaded: {self.model_id}")

    def _resolve_model_path(self) -> Path:
        """Find or download the GGUF model."""
        MODELS_DIR.mkdir(parents=True, exist_ok=True)

        # Check if model_id is already a path
        direct_path = Path(self.model_id)
        if direct_path.exists():
            return direct_path

        # Check models directory
        for f in MODELS_DIR.rglob("*.gguf"):
            if self.model_id.replace("/", "_") in f.stem:
                return f

        # Try HuggingFace download
        try:
            from huggingface_hub import hf_hub_download
            parts = self.model_id.split(":")
            repo = parts[0] if len(parts) > 0 else self.model_id
            filename = parts[1] if len(parts) > 1 else None

            if not filename:
                from huggingface_hub import list_repo_files
                files = list_repo_files(repo)
                gguf_files = [f for f in files if f.endswith(".gguf")]
                q4_files = [f for f in gguf_files if "q4" in f.lower() or "Q4" in f]
                filename = q4_files[0] if q4_files else (gguf_files[0] if gguf_files else None)

            if filename:
                path = hf_hub_download(repo, filename, local_dir=str(MODELS_DIR))
                return Path(path)
        except Exception as e:
            logger.warning(f"HuggingFace download failed: {e}")

        raise FileNotFoundError(f"Model not found: {self.model_id}. Place a .gguf file in {MODELS_DIR}")

    @staticmethod
    def _has_gpu() -> bool:
        try:
            import subprocess
            subprocess.run(["nvidia-smi"], capture_output=True, check=True)
            return True
        except Exception:
            return platform.system() == "Darwin"

    async def generate(self, prompt: str, max_tokens: int = 512, temperature: float = 0.7) -> str:
        if not self.loaded:
            await self.load_model()

        def _generate():
            result = self._llm(prompt, max_tokens=max_tokens, temperature=temperature)
            return result["choices"][0]["text"]

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _generate)

    async def generate_stream(self, prompt: str, max_tokens: int = 512, temperature: float = 0.7) -> AsyncGenerator[str, None]:
        if not self.loaded:
            await self.load_model()

        def _stream():
            chunks = []
            for token in self._llm(prompt, max_tokens=max_tokens, temperature=temperature, stream=True):
                text = token["choices"][0]["text"]
                chunks.append(text)
            return chunks

        loop = asyncio.get_event_loop()
        tokens = await loop.run_in_executor(None, _stream)
        for token in tokens:
            yield token

    async def unload(self):
        self._llm = None
        self.loaded = False
        logger.info(f"llama.cpp model unloaded: {self.model_id}")


class OllamaEngine(LocalLLMEngine):
    """Delegates inference to a local Ollama daemon."""

    def __init__(
        self,
        model_id: str = "llama3.2:3b",
        base_url: str | None = None,
    ):
        super().__init__(model_id)
        self.base_url = (
            base_url
            or os.getenv("FERAL_OLLAMA_URL", "").strip()
            or "http://localhost:11434"
        ).rstrip("/")

    async def load_model(self):
        if not await self.health_check():
            raise RuntimeError(
                f"Ollama is not reachable at {self.base_url}. "
                "Start it with `ollama serve` or set FERAL_OLLAMA_URL."
            )

        async with httpx.AsyncClient(base_url=self.base_url, timeout=10) as client:
            resp = await client.get("/api/tags")
            resp.raise_for_status()
            models = resp.json().get("models", [])
            names = [m.get("name", "") for m in models]

            if not any(self.model_id in n for n in names):
                logger.info("Pulling Ollama model %s (this may take a while)…", self.model_id)
                await self._pull_model()

        self.loaded = True
        logger.info("Ollama engine ready: %s @ %s", self.model_id, self.base_url)

    async def _pull_model(self):
        async with httpx.AsyncClient(base_url=self.base_url, timeout=600) as client:
            async with client.stream(
                "POST", "/api/pull", json={"name": self.model_id, "stream": True},
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        progress = json.loads(line)
                        status = progress.get("status", "")
                        if "pulling" in status or "downloading" in status:
                            logger.debug("Ollama pull: %s", status)
                    except json.JSONDecodeError:
                        pass

    async def generate(
        self, prompt: str, max_tokens: int = 512, temperature: float = 0.7,
    ) -> str:
        if not self.loaded:
            await self.load_model()

        async with httpx.AsyncClient(base_url=self.base_url, timeout=120) as client:
            resp = await client.post("/api/generate", json={
                "model": self.model_id,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": temperature},
            })
            resp.raise_for_status()
            return resp.json().get("response", "")

    async def generate_stream(
        self, prompt: str, max_tokens: int = 512, temperature: float = 0.7,
    ) -> AsyncGenerator[str, None]:
        if not self.loaded:
            await self.load_model()

        async with httpx.AsyncClient(base_url=self.base_url, timeout=120) as client:
            async with client.stream("POST", "/api/generate", json={
                "model": self.model_id,
                "prompt": prompt,
                "stream": True,
                "options": {"num_predict": max_tokens, "temperature": temperature},
            }) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                        token = chunk.get("response", "")
                        if token:
                            yield token
                        if chunk.get("done"):
                            break
                    except json.JSONDecodeError:
                        continue

    async def chat(
        self,
        messages: list[dict],
        max_tokens: int = 512,
        temperature: float = 0.7,
    ) -> str:
        if not self.loaded:
            await self.load_model()

        async with httpx.AsyncClient(base_url=self.base_url, timeout=120) as client:
            resp = await client.post("/api/chat", json={
                "model": self.model_id,
                "messages": messages,
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": temperature},
            })
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "")

    async def unload(self):
        self.loaded = False
        logger.info("Ollama engine unloaded (daemon still running): %s", self.model_id)

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=5) as client:
                resp = await client.get("/api/tags")
                return resp.status_code == 200
        except Exception:
            return False


VISION_MODELS = ("llava", "moondream", "bakllava", "llava-phi3", "minicpm-v", "qwen2-vl", "gemma3")


async def auto_setup_vision(ollama_url: str | None = None) -> dict:
    """Pull a vision model via Ollama if available.

    Returns ``{"available": bool, "model": str, "pulled": bool}``.
    """
    base = (
        ollama_url
        or os.getenv("FERAL_OLLAMA_URL", "").strip()
        or "http://localhost:11434"
    ).rstrip("/")
    result: dict[str, object] = {"available": False, "model": "", "pulled": False}

    try:
        async with httpx.AsyncClient(base_url=base, timeout=5) as client:
            resp = await client.get("/api/tags")
            if resp.status_code != 200:
                return result
            models = resp.json().get("models", [])
    except Exception:
        return result

    names = [m.get("name", "") for m in models]
    for name in names:
        if any(v in name.lower() for v in VISION_MODELS):
            result["available"] = True
            result["model"] = name
            return result

    target = "llava:7b"
    try:
        logger.info("Pulling vision model %s (this may take a while)…", target)
        async with httpx.AsyncClient(base_url=base, timeout=600) as client:
            async with client.stream(
                "POST", "/api/pull", json={"name": target, "stream": True},
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        progress = json.loads(line)
                        status = progress.get("status", "")
                        if "pulling" in status or "downloading" in status:
                            logger.debug("Vision model pull: %s", status)
                    except json.JSONDecodeError:
                        pass
        result["available"] = True
        result["model"] = target
        result["pulled"] = True
    except Exception as e:
        logger.warning("Failed to pull vision model %s: %s", target, e)

    return result


async def auto_setup_offline(
    model_id: str | None = None,
) -> OllamaEngine:
    """Detect or bootstrap Ollama, pull the recommended model, return an engine.

    Checks:
      1. Is `ollama` on PATH (or at known install locations)?
      2. Is the daemon already running?  If not, try to start it.
      3. Pull the recommended model if not present.
    """
    target_model = model_id or RECOMMENDED_MODELS.get("ollama-default", "llama3.2:3b")

    ollama_bin = shutil.which("ollama")
    if not ollama_bin:
        for candidate in (
            "/usr/local/bin/ollama",
            Path.home() / ".ollama" / "bin" / "ollama",
            "/opt/homebrew/bin/ollama",
        ):
            if Path(candidate).is_file():
                ollama_bin = str(candidate)
                break

    if not ollama_bin:
        logger.warning(
            "Ollama binary not found. Install from https://ollama.com — "
            "then run `ollama serve` to start the daemon."
        )
        raise RuntimeError(
            "Ollama is not installed. Install it from https://ollama.com "
            "and run `ollama serve` to enable offline inference."
        )

    engine = OllamaEngine(model_id=target_model)

    if not await engine.health_check():
        logger.info("Ollama not running — attempting to start via `%s serve`…", ollama_bin)
        try:
            proc = await asyncio.create_subprocess_exec(
                ollama_bin, "serve",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            for _ in range(15):
                await asyncio.sleep(1)
                if await engine.health_check():
                    logger.info("Ollama daemon started (pid=%d)", proc.pid)
                    break
            else:
                logger.warning(
                    "Started Ollama but it didn't become healthy within 15 s. "
                    "You may need to start it manually: `ollama serve`"
                )
        except Exception as e:
            logger.error("Failed to start Ollama daemon: %s", e)
            raise RuntimeError(f"Could not auto-start Ollama: {e}") from e

    await engine.load_model()
    return engine


def create_local_engine(model_spec: str = "") -> LocalLLMEngine:
    """
    Factory: create the right engine based on spec and platform.
    Spec format: "mlx:<model_id>" | "gguf:<model_id>" | "ollama:<model_id>" | "<model_id>"
    """
    if not model_spec:
        model_spec = os.getenv("FERAL_LOCAL_MODEL", "")

    if model_spec.startswith("mlx:"):
        return MLXEngine(model_spec[4:])
    elif model_spec.startswith("gguf:"):
        return LlamaCppEngine(model_spec[5:])
    elif model_spec.startswith("ollama:"):
        return OllamaEngine(model_spec[7:])

    # Auto-detect: prefer MLX on Apple Silicon
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        default = model_spec or RECOMMENDED_MODELS["mlx-default"]
        return MLXEngine(default)
    else:
        default = model_spec or RECOMMENDED_MODELS["gguf-default"]
        return LlamaCppEngine(default)
