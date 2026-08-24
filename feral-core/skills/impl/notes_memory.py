"""Python backing class for the ``notes_memory`` skill.

Replaces the legacy HTTP self-call indirection (the JSON manifest's
``save_note``/``search_notes``/``list_recent`` endpoints used to
target ``http://localhost:9090/internal/memory/*``, i.e. the brain
calling itself over the loopback REST surface). The HTTP routes
still exist for non-skill consumers (CLI, dashboard); this class
short-circuits the skill-executor path so the LLM's tool calls land
directly on the in-process ``MemoryStore``.

Adds the new ``fused_timeline`` endpoint that closes the S1 thesis
scenario (cut-list item #8): "what did I do yesterday?" → a single
chronological card fused from episodes + notes + knowledge graph +
calendar + health.

The orchestrator wires the brain-side singletons (memory, calendar,
health_aggregator) into ``api.state.state`` at boot, and we read
through that module so the skill picks them up regardless of which
order ``register_skill`` / brain wiring runs in.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from skills.base import BaseSkill
from skills.impl import register_skill
from skills.impl.timeline_fusion import (
    DEFAULT_PER_SOURCE_LIMIT,
    timeline_fusion,
)

logger = logging.getLogger("feral.skill.notes_memory")


def _state():
    """Late-binding accessor for ``api.state.state``.

    Importing at module-load time would create an import cycle with
    the FastAPI route module that also imports from ``api.state``.
    A per-call import is cheap (Python caches the module) and keeps
    the skill registry boot-order-independent.
    """
    from api.state import state  # local import — see docstring
    return state


@register_skill
class NotesMemorySkill(BaseSkill):
    """In-process backing for the notes/memory skill manifest."""

    def __init__(self) -> None:
        super().__init__(skill_id="notes_memory")

    async def execute(
        self,
        endpoint_id: str,
        args: Dict[str, Any],
        vault: Dict[str, str],
    ) -> Dict[str, Any]:
        dispatch = {
            "save_note": self._save_note,
            "search_notes": self._search_notes,
            "list_recent": self._list_recent,
            "fused_timeline": self._fused_timeline,
            # The ambient conversation read path. Without these the
            # agent had no way to ask whether anything was recorded, so
            # it answered from its self-model and said no while four
            # transcripts sat in the store. See
            # memory/ambient_conversations.py.
            "list_conversations": self._list_conversations,
            "search_conversations": self._search_conversations,
            "conversation_commitments": self._conversation_commitments,
        }
        fn = dispatch.get(endpoint_id)
        if not fn:
            return {
                "success": False,
                "status_code": 404,
                "data": None,
                "error": f"Unknown notes_memory endpoint: {endpoint_id}",
            }
        try:
            return await fn(args)
        except Exception as exc:
            logger.exception("notes_memory.%s failed", endpoint_id)
            return {
                "success": False,
                "status_code": 500,
                "data": None,
                "error": f"{exc.__class__.__name__}: {exc}",
            }

    # ── individual endpoint handlers ─────────────────────────────

    async def _save_note(self, args: Dict[str, Any]) -> Dict[str, Any]:
        memory = _state().memory
        if memory is None:
            return {"success": False, "status_code": 503, "data": None, "error": "memory unavailable"}
        content = args.get("content", "")
        if not content:
            return {"success": False, "status_code": 400, "data": None, "error": "content is required"}
        result = await memory.save(
            content=content,
            tags=args.get("tags") or [],
            importance=args.get("importance", "normal"),
        )
        return {"success": True, "status_code": 200, "data": result, "error": None}

    async def _search_notes(self, args: Dict[str, Any]) -> Dict[str, Any]:
        memory = _state().memory
        if memory is None:
            return {"success": False, "status_code": 503, "data": None, "error": "memory unavailable"}
        query = args.get("query") or args.get("q") or ""
        if not query:
            return {"success": True, "status_code": 200, "data": [], "error": None}
        limit = int(args.get("limit") or 10)
        rows = await memory.search(query=query, limit=limit)
        return {"success": True, "status_code": 200, "data": rows, "error": None}

    async def _list_recent(self, args: Dict[str, Any]) -> Dict[str, Any]:
        memory = _state().memory
        if memory is None:
            return {"success": False, "status_code": 503, "data": None, "error": "memory unavailable"}
        limit = int(args.get("limit") or 10)
        rows = await memory.list_recent(limit=limit)
        return {"success": True, "status_code": 200, "data": rows, "error": None}

    # ── ambient conversations ────────────────────────────────────
    #
    # sqlite reads on a worker thread. These are small (one row per
    # recorded conversation) but they are still blocking file I/O, and
    # blocking I/O inside `async def` is a bug in this codebase by
    # convention, not a judgement call.

    async def _list_conversations(self, args: Dict[str, Any]) -> Dict[str, Any]:
        import asyncio

        from memory.ambient_conversations import list_conversations

        data = await asyncio.to_thread(
            list_conversations,
            int(args.get("limit") or 20),
            include_detail=bool(args.get("include_detail")),
            with_commitments_only=bool(args.get("with_commitments_only")),
        )
        return {"success": True, "status_code": 200, "data": data, "error": None}

    async def _search_conversations(self, args: Dict[str, Any]) -> Dict[str, Any]:
        import asyncio

        from memory.ambient_conversations import search_conversations

        query = args.get("query") or args.get("q") or ""
        data = await asyncio.to_thread(
            search_conversations,
            str(query),
            int(args.get("limit") or 20),
            include_detail=bool(args.get("include_detail")),
        )
        return {"success": True, "status_code": 200, "data": data, "error": None}

    async def _conversation_commitments(self, args: Dict[str, Any]) -> Dict[str, Any]:
        import asyncio

        from memory.ambient_conversations import commitments_from_conversations

        data = await asyncio.to_thread(
            commitments_from_conversations, int(args.get("limit") or 50),
        )
        return {"success": True, "status_code": 200, "data": data, "error": None}

    async def _fused_timeline(self, args: Dict[str, Any]) -> Dict[str, Any]:
        st = _state()
        result = await timeline_fusion(
            query=args.get("query", ""),
            memory=getattr(st, "memory", None),
            calendar=getattr(st, "calendar", None),
            health_aggregator=getattr(st, "health_aggregator", None),
            window_label=args.get("window_label"),
            from_ts=args.get("from_ts"),
            to_ts=args.get("to_ts"),
            per_source_limit=int(
                args.get("per_source_limit") or DEFAULT_PER_SOURCE_LIMIT
            ),
        )
        return {
            "success": True,
            "status_code": 200,
            "data": result,
            "error": None,
        }
