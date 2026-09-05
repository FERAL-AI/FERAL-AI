"""Pluggable context engine with formal lifecycle for FERAL."""
from __future__ import annotations
import asyncio
import hashlib
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from agents.token_estimate import estimate_message_tokens, estimate_tokens

logger = logging.getLogger("feral.context_engine")


@dataclass
class AssembleResult:
    messages: list[dict]
    system_prompt_addition: str = ""
    estimated_tokens: int = 0


@dataclass
class CompactResult:
    messages: list[dict]
    summary: str = ""
    tokens_before: int = 0
    tokens_after: int = 0


@dataclass
class CompactionCheckpoint:
    session_id: str
    timestamp: float
    messages_before: list[dict]
    tokens_before: int
    tokens_after: int
    summary: str = ""

MAX_CHECKPOINTS_PER_SESSION = 25


class ContextEngine(ABC):
    """Abstract context engine with formal lifecycle."""

    async def bootstrap(self, session_id: str) -> None:
        """One-time init for a session."""
        pass

    @abstractmethod
    async def ingest(self, session_id: str, message: dict) -> None:
        """Feed a new message into the engine."""
        ...

    async def ingest_batch(self, session_id: str, messages: list[dict]) -> None:
        for msg in messages:
            await self.ingest(session_id, msg)

    @abstractmethod
    async def assemble(self, session_id: str, max_tokens: int) -> AssembleResult:
        """Build model context under token budget."""
        ...

    @abstractmethod
    async def compact(self, session_id: str, target_tokens: int) -> CompactResult:
        """Reduce token usage via summarization or pruning."""
        ...

    async def maintain(self, session_id: str) -> None:
        """Periodic maintenance between turns."""
        pass

    async def after_turn(self, session_id: str) -> None:
        """Post-turn hook."""
        pass

    async def dispose(self, session_id: str) -> None:
        """Cleanup when session ends."""
        pass


class DefaultContextEngine(ContextEngine):
    """Default context engine with token estimation, summarization compaction, and checkpointing."""

    def __init__(self, llm=None, max_history_per_session: int = 200):
        self._sessions: dict[str, list[dict]] = {}
        self._checkpoints: dict[str, list[CompactionCheckpoint]] = {}
        self._llm = llm
        self._max_history = max_history_per_session
        self._prompt_cache: dict[str, str] = {}  # session -> SHA-256 of last system prompt

    async def ingest(self, session_id: str, message: dict) -> None:
        if session_id not in self._sessions:
            self._sessions[session_id] = []
        self._sessions[session_id].append(message)
        if len(self._sessions[session_id]) > self._max_history:
            self._sessions[session_id] = self._sessions[session_id][-self._max_history:]

    async def assemble(self, session_id: str, max_tokens: int) -> AssembleResult:
        messages = list(self._sessions.get(session_id, []))
        estimated = self._estimate_tokens(messages)

        if estimated > max_tokens:
            result = await self.compact(session_id, int(max_tokens * 0.7))
            messages = result.messages
            estimated = result.tokens_after

        return AssembleResult(messages=messages, estimated_tokens=estimated)

    async def compact(self, session_id: str, target_tokens: int) -> CompactResult:
        messages = list(self._sessions.get(session_id, []))
        tokens_before = self._estimate_tokens(messages)

        # Save checkpoint before compaction
        self._save_checkpoint(session_id, messages, tokens_before)

        if self._llm and len(messages) > 10:
            compacted = await self._summarize_and_compact(session_id, messages, target_tokens)
        else:
            compacted = self._prune_to_budget(messages, target_tokens)

        tokens_after = self._estimate_tokens(compacted)
        self._sessions[session_id] = compacted

        return CompactResult(
            messages=compacted,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )

    def observe_prompt_cache(self, session_id: str, system_prompt: str) -> Optional[dict]:
        """Track prompt cache state for observability."""
        current_hash = hashlib.sha256(system_prompt.encode()).hexdigest()[:16]
        prev_hash = self._prompt_cache.get(session_id)
        self._prompt_cache[session_id] = current_hash

        if prev_hash and prev_hash != current_hash:
            return {"cache_break": True, "prev": prev_hash, "current": current_hash}
        return {"cache_break": False, "hash": current_hash}

    async def dispose(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._checkpoints.pop(session_id, None)
        self._prompt_cache.pop(session_id, None)

    def _save_checkpoint(self, session_id: str, messages: list[dict], tokens: int):
        if session_id not in self._checkpoints:
            self._checkpoints[session_id] = []
        cp = CompactionCheckpoint(
            session_id=session_id,
            timestamp=time.time(),
            messages_before=messages[:],
            tokens_before=tokens,
            tokens_after=0,
        )
        self._checkpoints[session_id].append(cp)
        if len(self._checkpoints[session_id]) > MAX_CHECKPOINTS_PER_SESSION:
            self._checkpoints[session_id] = self._checkpoints[session_id][-MAX_CHECKPOINTS_PER_SESSION:]

    async def _summarize_and_compact(self, session_id: str, messages: list[dict], target_tokens: int) -> list[dict]:
        """LLM-based staged summarization: chunk old messages, summarize each, preserve recent."""
        recent_count = min(10, len(messages) // 3)
        recent = messages[-recent_count:]
        to_summarize = messages[:-recent_count] if recent_count < len(messages) else []

        if not to_summarize or not self._llm:
            return self._prune_to_budget(messages, target_tokens)

        chunk_text = "\n".join(
            f"{m.get('role', 'unknown')}: {m.get('content', '')[:300]}"
            for m in to_summarize
        )

        try:
            summary_prompt = [
                {"role": "system", "content": "Summarize the following conversation history concisely. PRIORITIZE recent context and decisions. Preserve key facts, tool results, and user preferences."},
                {"role": "user", "content": chunk_text[:8000]},
            ]
            response = await self._llm.chat(summary_prompt)
            text, _ = self._llm.extract_response(response)
            if not text:
                # Provider failure or empty summary: a summary row that
                # reads "None" (or an HTTP error) would poison every
                # later turn's context. Prune instead.
                from agents.llm_provider import llm_response_error
                raise RuntimeError(
                    llm_response_error(response) or "empty summary",
                )
            summary_msg = {"role": "system", "content": f"[Compacted history summary]\n{text}"}
            return [summary_msg] + recent
        except Exception as e:
            logger.warning(f"Summarization failed, falling back to pruning: {e}")
            return self._prune_to_budget(messages, target_tokens)

    @staticmethod
    def _prune_to_budget(messages: list[dict], target_tokens: int) -> list[dict]:
        """Drop oldest messages until under budget, preserving tool-call/result pairs."""
        # Was `len(str(content)) // 4`, which is calibrated for English prose
        # only. Measured against cl100k/o200k, that read Chinese at 0.21x its
        # real token count and emoji at 0.09x, so this loop decided a context
        # five times over the window was inside it and pruned nothing. See
        # agents/token_estimate.py for the corpus and the weights.
        total = estimate_message_tokens(messages)
        result = list(messages)
        while total > target_tokens and len(result) > 2:
            removed = result.pop(0)
            total -= estimate_tokens(removed.get("content", ""))
        return result

    @staticmethod
    def _estimate_tokens(messages: list[dict]) -> int:
        return estimate_message_tokens(messages)


# Global registry
_engine_registry: dict[str, ContextEngine] = {}
_default_engine: Optional[DefaultContextEngine] = None


def register_context_engine(engine_id: str, engine: ContextEngine):
    _engine_registry[engine_id] = engine


def get_context_engine(engine_id: str = "default") -> ContextEngine:
    if engine_id in _engine_registry:
        return _engine_registry[engine_id]
    global _default_engine
    if _default_engine is None:
        _default_engine = DefaultContextEngine()
    return _default_engine


def set_default_engine(engine: DefaultContextEngine):
    global _default_engine
    _default_engine = engine
