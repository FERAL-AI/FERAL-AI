"""
FERAL Google Drive Integration — Drive API v3
================================================
File management: list, search, download, upload, create folders.
Uses OAuth token from OAuthManager ("google" provider).
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from typing import Any, Optional

import httpx

from integrations._http_errors import http_error_detail

logger = logging.getLogger("feral.integrations.google_drive")

DRIVE_API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"


class GoogleDriveIntegration:
    """Google Drive API v3 integration via httpx + OAuthManager."""

    def __init__(self, oauth_manager: Any = None):
        self._oauth = oauth_manager
        self._http = httpx.AsyncClient(timeout=30.0)

    async def _headers(self) -> Optional[dict[str, str]]:
        if not self._oauth:
            return None
        token = await self._oauth.get_token("google")
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    @property
    def connected(self) -> bool:
        from integrations._probe_status import is_connected_cached

        token_present = (
            self._oauth is not None and self._oauth.is_connected("google")
        )
        return is_connected_cached("google", fallback=token_present)

    async def probe_connected(self) -> bool:
        """Force a live Google ``/oauth2/v3/userinfo`` probe."""
        from integrations._probe_status import refresh

        result = await refresh("google",
                               vault=getattr(self._oauth, "_vault", None))
        if result is None:
            return self.connected
        return result

    async def execute(self, endpoint_id: str, args: dict[str, Any], vault: dict[str, str] | None = None) -> dict[str, Any]:
        dispatch = {
            "list_files": self.list_files,
            "search_files": self.search_files,
            "download_file": self.download_file,
            "upload_file": self.upload_file,
            "create_folder": self.create_folder,
        }
        fn = dispatch.get(endpoint_id)
        if not fn:
            return {"success": False, "error": f"Unknown endpoint: {endpoint_id}"}
        return await fn(**args)

    async def list_files(
        self, page_size: int = 20, folder_id: str = "", page_token: str = "", **_kw: Any
    ) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Drive"}
        try:
            params: dict[str, Any] = {
                "pageSize": min(page_size, 100),
                "fields": "nextPageToken, files(id, name, mimeType, size, modifiedTime, webViewLink)",
                "orderBy": "modifiedTime desc",
            }
            if folder_id:
                params["q"] = f"'{folder_id}' in parents and trashed = false"
            else:
                params["q"] = "trashed = false"
            if page_token:
                params["pageToken"] = page_token
            resp = await self._http.get(f"{DRIVE_API}/files", params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return {
                "success": True,
                "data": {
                    "files": data.get("files", []),
                    "next_page_token": data.get("nextPageToken"),
                },
            }
        except Exception as e:
            return {"success": False, "error": http_error_detail(e)}

    async def search_files(self, query: str = "", max_results: int = 20, **_kw: Any) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Drive"}
        try:
            q_param = f"name contains '{query}' and trashed = false"
            resp = await self._http.get(
                f"{DRIVE_API}/files",
                params={
                    "q": q_param,
                    "pageSize": min(max_results, 100),
                    "fields": "files(id, name, mimeType, size, modifiedTime, webViewLink)",
                },
                headers=headers,
            )
            resp.raise_for_status()
            return {"success": True, "data": {"files": resp.json().get("files", []), "query": query}}
        except Exception as e:
            return {"success": False, "error": http_error_detail(e)}

    async def download_file(self, file_id: str = "", **_kw: Any) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Drive"}
        try:
            meta_resp = await self._http.get(
                f"{DRIVE_API}/files/{file_id}",
                params={"fields": "id, name, mimeType, size"},
                headers=headers,
            )
            meta_resp.raise_for_status()
            meta = meta_resp.json()

            resp = await self._http.get(
                f"{DRIVE_API}/files/{file_id}",
                params={"alt": "media"},
                headers=headers,
            )
            resp.raise_for_status()
            content_b64 = base64.b64encode(resp.content).decode()
            return {
                "success": True,
                "data": {
                    "file_id": file_id,
                    "name": meta.get("name", ""),
                    "mime_type": meta.get("mimeType", ""),
                    "size": meta.get("size"),
                    "content_b64": content_b64,
                },
            }
        except Exception as e:
            return {"success": False, "error": http_error_detail(e)}

    async def upload_file(
        self, name: str = "", content_b64: str = "", mime_type: str = "", folder_id: str = "", **_kw: Any
    ) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Drive"}
        try:
            if not mime_type:
                mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"

            metadata: dict[str, Any] = {"name": name, "mimeType": mime_type}
            if folder_id:
                metadata["parents"] = [folder_id]

            content_bytes = base64.b64decode(content_b64) if content_b64 else b""

            # Simple upload for files under 5MB, multipart otherwise
            if len(content_bytes) < 5 * 1024 * 1024:
                import json as _json
                boundary = "feral_upload_boundary"
                body = (
                    f"--{boundary}\r\n"
                    f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
                    f"{_json.dumps(metadata)}\r\n"
                    f"--{boundary}\r\n"
                    f"Content-Type: {mime_type}\r\n\r\n"
                ).encode() + content_bytes + f"\r\n--{boundary}--".encode()

                upload_headers = {
                    **headers,
                    "Content-Type": f"multipart/related; boundary={boundary}",
                }
                resp = await self._http.post(
                    f"{UPLOAD_API}/files",
                    params={"uploadType": "multipart", "fields": "id, name, webViewLink"},
                    content=body,
                    headers=upload_headers,
                )
            else:
                resp = await self._http.post(
                    f"{UPLOAD_API}/files",
                    params={"uploadType": "media", "fields": "id, name, webViewLink"},
                    content=content_bytes,
                    headers={**headers, "Content-Type": mime_type},
                )
            resp.raise_for_status()
            return {"success": True, "data": resp.json()}
        except Exception as e:
            return {"success": False, "error": http_error_detail(e)}

    async def create_folder(self, name: str = "", parent_id: str = "", **_kw: Any) -> dict[str, Any]:
        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Google Drive"}
        try:
            metadata: dict[str, Any] = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
            }
            if parent_id:
                metadata["parents"] = [parent_id]
            resp = await self._http.post(
                f"{DRIVE_API}/files",
                json=metadata,
                params={"fields": "id, name, webViewLink"},
                headers=headers,
            )
            resp.raise_for_status()
            return {"success": True, "data": resp.json()}
        except Exception as e:
            return {"success": False, "error": http_error_detail(e)}

    async def close(self):
        await self._http.aclose()
