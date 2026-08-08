"""Agent-facing surface for the per-domain browser knowledge store.

Separate module from ``skills/impl/browser_use.py`` on purpose: the browser
controller is a 1300-line CDP driver under active concurrent edit, and this
needs none of it. The only coupling is the small recall/capture wrapper in
``api.state.BrainState._execute_browser_action``.

Safety: every endpoint here reads or writes prose. Nothing in this module
compiles, evals, execs, imports-by-name or otherwise runs stored text.
``genesis_candidates`` deliberately returns proposal text and stops there —
turning a proposal into a running tool is ``ToolGenesisEngine``'s job, behind
its AST check and its human approval step.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from skills.base import BaseSkill
from skills.impl import register_skill

logger = logging.getLogger("feral.skill.browser_memory")


def _store():
    # Imported per call rather than at module load: get_store() re-resolves
    # against feral_data_home(), so a changed FERAL_HOME (tests, relocated
    # install) is picked up instead of frozen at import time.
    from memory.browser_domain_memory import get_store

    return get_store()


def _ok(data: Any) -> Dict[str, Any]:
    return {"success": True, "status_code": 200, "data": data, "error": None}


def _err(message: str, code: int = 400) -> Dict[str, Any]:
    return {"success": False, "status_code": code, "data": None, "error": message}


@register_skill
class BrowserMemorySkill(BaseSkill):
    def __init__(self):
        super().__init__("browser_memory")

    async def execute(
        self, endpoint_id: str, args: Dict[str, Any], vault: Dict[str, str]
    ) -> Dict[str, Any]:
        args = args or {}
        try:
            store = _store()
        except Exception as exc:
            logger.warning("browser_memory store unavailable: %s", exc)
            return _err(f"site-knowledge store unavailable: {exc}", 500)

        try:
            if endpoint_id == "recall":
                url = str(args.get("url") or "").strip()
                if not url:
                    return _err("url is required")
                result = store.recall(
                    url,
                    limit=int(args.get("limit") or 12),
                    topic=(str(args["topic"]) if args.get("topic") else None),
                )
                return _ok(result)

            if endpoint_id == "remember":
                url = str(args.get("url") or "").strip()
                title = str(args.get("title") or "").strip()
                body = str(args.get("body") or "").strip()
                if not url or not title or not body:
                    return _err("url, title and body are all required")
                tags = args.get("tags") or []
                if isinstance(tags, str):
                    tags = [t.strip() for t in tags.split(",") if t.strip()]
                result = store.add_note(
                    scope=url,
                    topic=str(args.get("topic") or "note"),
                    title=title,
                    body=body,
                    kind="note",
                    source="agent",
                    confidence=0.6,
                    tags=tags,
                )
                return _ok(result)

            if endpoint_id == "search":
                query = str(args.get("query") or "").strip()
                if not query:
                    return _err("query is required")
                notes = store.search(query, limit=int(args.get("limit") or 20))
                return _ok({"count": len(notes), "notes": notes})

            if endpoint_id == "list_domains":
                scopes = store.list_scopes()
                return _ok({"count": len(scopes), "scopes": scopes})

            if endpoint_id == "stats":
                return _ok(store.stats())

            if endpoint_id == "genesis_candidates":
                candidates = store.genesis_candidates(
                    min_observations=int(args.get("min_observations") or 3)
                )
                return _ok(
                    {
                        "count": len(candidates),
                        "candidates": candidates,
                        # Restated in the payload so a UI or another agent
                        # reading only the response cannot mistake these for
                        # something that already ran.
                        "note": (
                            "Proposals only. No code was generated and nothing was "
                            "executed. Building a real tool from a candidate goes "
                            "through Tool Genesis review and explicit approval."
                        ),
                    }
                )

            if endpoint_id == "forget":
                note_id = str(args.get("note_id") or "").strip()
                if not note_id:
                    return _err("note_id is required")
                return _ok({"forgotten": store.forget(note_id)})

            return _err(f"unknown endpoint: {endpoint_id}", 404)
        except Exception as exc:
            logger.warning("browser_memory %s failed: %s", endpoint_id, exc)
            return _err(str(exc), 500)
