"""
FERAL Notion Integration — Knowledge & Notes
===============================================
Real Notion API integration for reading/writing pages and databases.
Parses Notion blocks into LLM-friendly text and creates blocks from
LLM output.  Uses OAuth2 or internal integration token.
"""

from __future__ import annotations
import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("feral.integrations.notion")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

NOT_CONNECTED_ERROR = (
    "Not connected to Notion. Authorise it in Settings > Integrations, "
    "or set NOTION_TOKEN to an internal integration token."
)


class NotionIntegration:
    """
    Native Notion API integration.
    Uses OAuthManager for token lifecycle.
    """

    def __init__(self, oauth_manager=None):
        self._oauth = oauth_manager
        self._token = os.getenv("NOTION_TOKEN", "")
        self._http: Optional[httpx.AsyncClient] = None

    async def _resolve_token(self) -> str:
        """The Notion token, from the env override or the OAuth manager."""
        token = self._token
        if not token and self._oauth:
            try:
                token = await self._oauth.get_token("notion") or ""
            except Exception as exc:
                logger.debug("Notion token lookup failed: %s", exc)
                token = ""
        return token or ""

    async def _ensure_client(self) -> Optional[dict]:
        """Ready the HTTP client, or return a "not connected" envelope.

        With no token this used to build a client anyway and send
        ``Authorization: Bearer `` with nothing after the space. httpx
        rejects that header before the request leaves the process, so
        every Notion endpoint answered with ``Illegal header value
        b'Bearer '`` — a message about header encoding for what is
        simply an integration nobody has authorised. ``microsoft365``
        already gets this right (``_headers`` returns None and each
        method answers "Not connected to Microsoft 365"); this is the
        same shape.

        The client is also no longer built once and kept forever with a
        token baked into its headers: authorising Notion after the first
        call would otherwise leave the stale header in place for the
        life of the process.
        """
        token = await self._resolve_token()
        if not token:
            return {
                "success": False,
                "status_code": 401,
                "data": None,
                "error": NOT_CONNECTED_ERROR,
            }
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=NOTION_API,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": NOTION_VERSION,
                    "Content-Type": "application/json",
                },
                timeout=15.0,
            )
        else:
            self._http.headers["Authorization"] = f"Bearer {token}"
        return None

    @property
    def connected(self) -> bool:
        from integrations._probe_status import is_connected_cached

        token_present = bool(self._token) or (
            self._oauth is not None and self._oauth.is_connected("notion")
        )
        return is_connected_cached("notion", fallback=token_present)

    async def probe_connected(self) -> bool:
        """Force a live Notion ``/v1/users/me`` probe."""
        from integrations._probe_status import refresh

        result = await refresh("notion",
                               vault=getattr(self._oauth, "_vault", None))
        if result is None:
            return self.connected
        return result

    async def execute(self, endpoint_id: str, args: dict, vault: dict = None) -> dict:
        """Skill executor interface."""
        dispatch = {
            "search_pages": self.search_pages,
            "read_page": self.read_page,
            "create_page": self.create_page,
            "update_page": self.update_page,
            "query_database": self.query_database,
            "create_database_entry": self.create_database_entry,
        }
        fn = dispatch.get(endpoint_id)
        if not fn:
            return {"success": False, "error": f"Unknown endpoint: {endpoint_id}"}
        return await fn(**args)

    async def search_pages(self, query: str = "", **kwargs) -> dict:
        guard = await self._ensure_client()
        if guard is not None:
            return guard
        try:
            body = {"query": query, "page_size": 10}
            resp = await self._http.post("/search", json=body)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("results", []):
                obj_type = item.get("object", "")
                title = self._extract_title(item)
                results.append({
                    "id": item.get("id", ""),
                    "type": obj_type,
                    "title": title,
                    "url": item.get("url", ""),
                    "last_edited": item.get("last_edited_time", ""),
                })
            return {"success": True, "data": {"results": results, "query": query}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def read_page(self, page_id: str = "", **kwargs) -> dict:
        guard = await self._ensure_client()
        if guard is not None:
            return guard
        try:
            page_resp = await self._http.get(f"/pages/{page_id}")
            page_resp.raise_for_status()
            page = page_resp.json()

            blocks_resp = await self._http.get(f"/blocks/{page_id}/children", params={"page_size": 50})
            blocks_resp.raise_for_status()
            blocks = blocks_resp.json()

            title = self._extract_title(page)
            content = self._blocks_to_text(blocks.get("results", []))

            return {
                "success": True,
                "data": {
                    "id": page_id,
                    "title": title,
                    "content": content,
                    "url": page.get("url", ""),
                    "properties": self._extract_properties(page),
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def create_page(self, parent_id: str = "", title: str = "", content: str = "", **kwargs) -> dict:
        guard = await self._ensure_client()
        if guard is not None:
            return guard
        try:
            children = self._text_to_blocks(content) if content else []
            body = {
                "parent": {"page_id": parent_id},
                "properties": {
                    "title": {
                        "title": [{"text": {"content": title}}],
                    },
                },
                "children": children,
            }
            resp = await self._http.post("/pages", json=body)
            resp.raise_for_status()
            result = resp.json()
            return {
                "success": True,
                "data": {
                    "id": result.get("id", ""),
                    "url": result.get("url", ""),
                    "title": title,
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def update_page(self, page_id: str = "", properties: dict = None, **kwargs) -> dict:
        guard = await self._ensure_client()
        if guard is not None:
            return guard
        try:
            body = {}
            if properties:
                body["properties"] = properties
            resp = await self._http.patch(f"/pages/{page_id}", json=body)
            resp.raise_for_status()
            return {"success": True, "data": {"updated": page_id}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def query_database(self, database_id: str = "", filter: dict = None, sorts: list = None, **kwargs) -> dict:
        guard = await self._ensure_client()
        if guard is not None:
            return guard
        try:
            body = {"page_size": 20}
            if filter:
                body["filter"] = filter
            if sorts:
                body["sorts"] = sorts

            resp = await self._http.post(f"/databases/{database_id}/query", json=body)
            resp.raise_for_status()
            data = resp.json()

            entries = []
            for item in data.get("results", []):
                entries.append({
                    "id": item.get("id", ""),
                    "properties": self._extract_properties(item),
                    "url": item.get("url", ""),
                })
            return {"success": True, "data": {"entries": entries, "total": len(entries)}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def create_database_entry(self, database_id: str = "", properties: dict = None, **kwargs) -> dict:
        guard = await self._ensure_client()
        if guard is not None:
            return guard
        try:
            body = {
                "parent": {"database_id": database_id},
                "properties": properties or {},
            }
            resp = await self._http.post("/pages", json=body)
            resp.raise_for_status()
            result = resp.json()
            return {
                "success": True,
                "data": {"id": result.get("id", ""), "url": result.get("url", "")},
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _extract_title(self, page: dict) -> str:
        props = page.get("properties", {})
        for key, val in props.items():
            if val.get("type") == "title":
                title_arr = val.get("title", [])
                if title_arr:
                    return title_arr[0].get("plain_text", "")
        return ""

    def _extract_properties(self, page: dict) -> dict:
        result = {}
        for key, val in page.get("properties", {}).items():
            prop_type = val.get("type", "")
            if prop_type == "title":
                arr = val.get("title", [])
                result[key] = arr[0].get("plain_text", "") if arr else ""
            elif prop_type == "rich_text":
                arr = val.get("rich_text", [])
                result[key] = arr[0].get("plain_text", "") if arr else ""
            elif prop_type == "number":
                result[key] = val.get("number")
            elif prop_type == "select":
                sel = val.get("select")
                result[key] = sel.get("name", "") if sel else ""
            elif prop_type == "multi_select":
                result[key] = [s.get("name", "") for s in val.get("multi_select", [])]
            elif prop_type == "checkbox":
                result[key] = val.get("checkbox", False)
            elif prop_type == "date":
                d = val.get("date")
                result[key] = d.get("start", "") if d else ""
            elif prop_type == "url":
                result[key] = val.get("url", "")
            elif prop_type == "status":
                s = val.get("status")
                result[key] = s.get("name", "") if s else ""
        return result

    def _blocks_to_text(self, blocks: list) -> str:
        lines = []
        for block in blocks:
            btype = block.get("type", "")
            bdata = block.get(btype, {})

            if btype in ("paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item", "numbered_list_item", "quote", "callout"):
                rich_text = bdata.get("rich_text", [])
                text = "".join(rt.get("plain_text", "") for rt in rich_text)
                prefix = ""
                if btype == "heading_1":
                    prefix = "# "
                elif btype == "heading_2":
                    prefix = "## "
                elif btype == "heading_3":
                    prefix = "### "
                elif btype == "bulleted_list_item":
                    prefix = "- "
                elif btype == "numbered_list_item":
                    prefix = "1. "
                elif btype == "quote":
                    prefix = "> "
                lines.append(f"{prefix}{text}")

            elif btype == "to_do":
                checked = bdata.get("checked", False)
                rich_text = bdata.get("rich_text", [])
                text = "".join(rt.get("plain_text", "") for rt in rich_text)
                lines.append(f"[{'x' if checked else ' '}] {text}")

            elif btype == "code":
                rich_text = bdata.get("rich_text", [])
                text = "".join(rt.get("plain_text", "") for rt in rich_text)
                lang = bdata.get("language", "")
                lines.append(f"```{lang}\n{text}\n```")

            elif btype == "divider":
                lines.append("---")

        return "\n".join(lines)

    def _text_to_blocks(self, text: str) -> list:
        blocks = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("# "):
                blocks.append(self._heading_block(line[2:], 1))
            elif line.startswith("## "):
                blocks.append(self._heading_block(line[3:], 2))
            elif line.startswith("### "):
                blocks.append(self._heading_block(line[4:], 3))
            elif line.startswith("- "):
                blocks.append(self._list_block(line[2:], "bulleted"))
            else:
                blocks.append(self._paragraph_block(line))
        return blocks

    def _paragraph_block(self, text: str) -> dict:
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": text}}],
            },
        }

    def _heading_block(self, text: str, level: int) -> dict:
        btype = f"heading_{level}"
        return {
            "object": "block",
            "type": btype,
            btype: {
                "rich_text": [{"type": "text", "text": {"content": text}}],
            },
        }

    def _list_block(self, text: str, style: str = "bulleted") -> dict:
        btype = f"{style}_list_item"
        return {
            "object": "block",
            "type": btype,
            btype: {
                "rich_text": [{"type": "text", "text": {"content": text}}],
            },
        }

    async def close(self):
        if self._http:
            await self._http.aclose()
