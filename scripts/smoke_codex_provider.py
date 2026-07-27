#!/usr/bin/env python3
"""Run one real Codex-provider turn using the current Codex login."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from providers.base import ChatMessage
from providers.codex_provider import CodexProvider


async def _run(prompt: str, model: str, runtime: bool) -> int:
    provider = CodexProvider()
    models = await provider.refresh_models()
    selected = model or models[0]
    if runtime:
        os.environ["FERAL_LLM_PROVIDER"] = "codex"
        os.environ["FERAL_LLM_MODEL"] = selected
        from agents.llm_provider import LLMProvider

        llm = LLMProvider()
        try:
            response = await llm.chat([{"role": "user", "content": prompt}])
        finally:
            await llm.close()
        print(json.dumps(response, indent=2))
        return 0 if response.get("choices") else 1

    response = await provider.chat(
        [ChatMessage(role="user", content=prompt)],
        model=selected,
    )
    print(
        json.dumps(
            {
                "model": response.model,
                "text": response.text,
                "usage": response.usage,
                "finish_reason": response.finish_reason,
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="?", default="Reply with codex-provider-ok")
    parser.add_argument("--model", default="")
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="Exercise FERAL's LLMProvider routing in addition to the adapter",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.prompt, args.model, args.runtime))


if __name__ == "__main__":
    raise SystemExit(main())
