"""
FERAL Email Integration — Gmail API + IMAP Fallback
=====================================================
Inbox management, search, send, draft, and LLM-powered summarisation.
Falls back to IMAP when no Google OAuth is available.
"""

from __future__ import annotations

import base64
import email as email_stdlib
import imaplib
import logging
import os
from email.mime.text import MIMEText
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger("feral.integrations.email")

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


class EmailIntegration:
    """
    Gmail API integration with IMAP fallback.
    Uses OAuthManager for Google token management.
    """

    def __init__(self, oauth_manager=None):
        self._oauth = oauth_manager
        self._http = httpx.AsyncClient(base_url=GMAIL_API, timeout=15.0)
        self._imap_host: Optional[str] = os.environ.get("FERAL_EMAIL_IMAP_HOST")
        self._imap_user: Optional[str] = os.environ.get("FERAL_EMAIL_IMAP_USER")
        self._imap_pass: Optional[str] = os.environ.get("FERAL_EMAIL_IMAP_PASS")
        self._imap_port: int = int(os.environ.get("FERAL_EMAIL_IMAP_PORT", "993"))

    async def _headers(self) -> Optional[dict]:
        if not self._oauth:
            return None
        token = await self._oauth.get_token("google")
        if not token:
            return None
        return {"Authorization": f"Bearer {token}"}

    @property
    def connected(self) -> bool:
        """Live-verified Gmail connection.

        Pre-Lane-10 we returned ``True`` as soon as the IMAP host env
        var was present, lying about whether Gmail itself was actually
        usable. Now ``connected`` reflects the Google probe (or the
        OAuth token-presence baseline when no probe has run yet).
        IMAP readiness is exposed separately via
        :attr:`imap_configured` so the UI can advertise the IMAP
        fallback path without misrepresenting Gmail as connected.
        """
        from integrations._probe_status import is_connected_cached

        token_present = (
            self._oauth is not None and self._oauth.is_connected("google")
        )
        return is_connected_cached("google", fallback=token_present)

    @property
    def imap_configured(self) -> bool:
        return self._imap_host is not None

    async def probe_connected(self) -> bool:
        from integrations._probe_status import refresh

        result = await refresh("google",
                               vault=getattr(self._oauth, "_vault", None))
        if result is None:
            return self.connected
        return result

    @property
    def _use_imap(self) -> bool:
        return self._imap_host is not None and (
            self._oauth is None or not self._oauth.is_connected("google")
        )

    async def execute(self, endpoint_id: str, args: dict, vault: dict = None) -> dict:
        """Skill executor interface — called by SkillExecutor."""
        dispatch = {
            "list_inbox": self.list_inbox,
            "read_email": self.read_email,
            "search": self.search,
            "send_email": self.send_email,
            "draft_email": self.draft_email,
            "get_unread_count": self.get_unread_count,
            "summarize_inbox": self.summarize_inbox,
        }
        fn = dispatch.get(endpoint_id)
        if not fn:
            return {"success": False, "error": f"Unknown endpoint: {endpoint_id}"}
        return await fn(**args)

    # ── IMAP helpers ───────────────────────────────────────────────

    def _imap_connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self._imap_host, self._imap_port)
        conn.login(self._imap_user, self._imap_pass)
        return conn

    @staticmethod
    def _parse_imap_message(raw_bytes: bytes) -> dict[str, Any]:
        msg = email_stdlib.message_from_bytes(raw_bytes)
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode("utf-8", errors="replace")
                    break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", errors="replace")
        return {
            "subject": msg.get("Subject", ""),
            "from": msg.get("From", ""),
            "to": msg.get("To", ""),
            "date": msg.get("Date", ""),
            "body": body,
        }

    # ── Gmail helpers ──────────────────────────────────────────────

    @staticmethod
    def _parse_gmail_message(payload: dict) -> dict[str, Any]:
        headers_list = payload.get("payload", {}).get("headers", [])
        hdr: dict[str, str] = {}
        for h in headers_list:
            hdr[h["name"].lower()] = h["value"]

        body = ""
        parts = payload.get("payload", {}).get("parts", [])
        if parts:
            for p in parts:
                if p.get("mimeType") == "text/plain":
                    data = p.get("body", {}).get("data", "")
                    if data:
                        body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    break
        else:
            data = payload.get("payload", {}).get("body", {}).get("data", "")
            if data:
                body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        return {
            "id": payload.get("id", ""),
            "thread_id": payload.get("threadId", ""),
            "subject": hdr.get("subject", ""),
            "from": hdr.get("from", ""),
            "to": hdr.get("to", ""),
            "date": hdr.get("date", ""),
            "snippet": payload.get("snippet", ""),
            "body": body,
            "labels": payload.get("labelIds", []),
        }

    # ── Endpoints ──────────────────────────────────────────────────

    async def list_inbox(self, max_results: int = 20, **kwargs) -> dict:
        if self._use_imap:
            try:
                conn = self._imap_connect()
                conn.select("INBOX")
                _, data = conn.search(None, "ALL")
                ids = data[0].split()
                ids = ids[-max_results:] if len(ids) > max_results else ids
                ids.reverse()
                messages: list[dict] = []
                for mid in ids:
                    _, msg_data = conn.fetch(mid, "(RFC822)")
                    if msg_data and msg_data[0] and isinstance(msg_data[0], tuple):
                        parsed = self._parse_imap_message(msg_data[0][1])
                        parsed["id"] = mid.decode()
                        messages.append(parsed)
                conn.logout()
                return {"success": True, "data": {"messages": messages, "source": "imap"}}
            except Exception as e:
                return {"success": False, "error": str(e)}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Gmail"}
        try:
            resp = await self._http.get(
                "/messages",
                params={"maxResults": max_results, "labelIds": "INBOX"},
                headers=headers,
            )
            resp.raise_for_status()
            msg_stubs = resp.json().get("messages", [])
            messages = []
            for stub in msg_stubs:
                detail = await self._http.get(
                    f"/messages/{stub['id']}",
                    params={"format": "metadata", "metadataHeaders": "Subject,From,Date"},
                    headers=headers,
                )
                detail.raise_for_status()
                d = detail.json()
                hdr: dict[str, str] = {}
                for h in d.get("payload", {}).get("headers", []):
                    hdr[h["name"].lower()] = h["value"]
                messages.append({
                    "id": d.get("id", ""),
                    "subject": hdr.get("subject", ""),
                    "from": hdr.get("from", ""),
                    "date": hdr.get("date", ""),
                    "snippet": d.get("snippet", ""),
                })
            return {"success": True, "data": {"messages": messages, "source": "gmail"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def read_email(self, message_id: str = "", **kwargs) -> dict:
        if self._use_imap:
            try:
                conn = self._imap_connect()
                conn.select("INBOX")
                _, msg_data = conn.fetch(message_id.encode(), "(RFC822)")
                conn.logout()
                if msg_data and msg_data[0] and isinstance(msg_data[0], tuple):
                    parsed = self._parse_imap_message(msg_data[0][1])
                    parsed["id"] = message_id
                    return {"success": True, "data": parsed}
                return {"success": False, "error": "Message not found"}
            except Exception as e:
                return {"success": False, "error": str(e)}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Gmail"}
        try:
            resp = await self._http.get(
                f"/messages/{message_id}",
                params={"format": "full"},
                headers=headers,
            )
            resp.raise_for_status()
            return {"success": True, "data": self._parse_gmail_message(resp.json())}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def search(self, query: str = "", max_results: int = 10, **kwargs) -> dict:
        if self._use_imap:
            try:
                conn = self._imap_connect()
                conn.select("INBOX")
                _, data = conn.search(None, "SUBJECT", f'"{query}"')
                ids = data[0].split()
                ids = ids[-max_results:] if len(ids) > max_results else ids
                ids.reverse()
                messages: list[dict] = []
                for mid in ids:
                    _, msg_data = conn.fetch(mid, "(RFC822)")
                    if msg_data and msg_data[0] and isinstance(msg_data[0], tuple):
                        parsed = self._parse_imap_message(msg_data[0][1])
                        parsed["id"] = mid.decode()
                        messages.append(parsed)
                conn.logout()
                return {"success": True, "data": {"messages": messages, "query": query, "source": "imap"}}
            except Exception as e:
                return {"success": False, "error": str(e)}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Gmail"}
        try:
            resp = await self._http.get(
                "/messages",
                params={"q": query, "maxResults": max_results},
                headers=headers,
            )
            resp.raise_for_status()
            msg_stubs = resp.json().get("messages", [])
            messages = []
            for stub in msg_stubs:
                detail = await self._http.get(
                    f"/messages/{stub['id']}",
                    params={"format": "metadata", "metadataHeaders": "Subject,From,Date"},
                    headers=headers,
                )
                detail.raise_for_status()
                d = detail.json()
                hdr: dict[str, str] = {}
                for h in d.get("payload", {}).get("headers", []):
                    hdr[h["name"].lower()] = h["value"]
                messages.append({
                    "id": d.get("id", ""),
                    "subject": hdr.get("subject", ""),
                    "from": hdr.get("from", ""),
                    "date": hdr.get("date", ""),
                    "snippet": d.get("snippet", ""),
                })
            return {"success": True, "data": {"messages": messages, "query": query, "source": "gmail"}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def send_email(self, to: str = "", subject: str = "", body: str = "", **kwargs) -> dict:
        if self._use_imap:
            return {"success": False, "error": "Cannot send via IMAP — connect Gmail"}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Gmail"}
        try:
            mime = MIMEText(body, "plain")
            mime["To"] = to
            mime["Subject"] = subject
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
            resp = await self._http.post(
                "/messages/send",
                json={"raw": raw},
                headers=headers,
            )
            resp.raise_for_status()
            sent = resp.json()
            return {"success": True, "data": {"id": sent.get("id", ""), "threadId": sent.get("threadId", "")}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def draft_email(self, to: str = "", subject: str = "", body: str = "", **kwargs) -> dict:
        if self._use_imap:
            return {"success": False, "error": "Cannot draft via IMAP — connect Gmail"}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Gmail"}
        try:
            mime = MIMEText(body, "plain")
            mime["To"] = to
            mime["Subject"] = subject
            raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
            resp = await self._http.post(
                "/drafts",
                json={"message": {"raw": raw}},
                headers=headers,
            )
            resp.raise_for_status()
            draft = resp.json()
            return {"success": True, "data": {"draft_id": draft.get("id", ""), "message_id": draft.get("message", {}).get("id", "")}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_unread_count(self, **kwargs) -> dict:
        if self._use_imap:
            try:
                conn = self._imap_connect()
                conn.select("INBOX")
                _, data = conn.search(None, "UNSEEN")
                count = len(data[0].split()) if data[0] else 0
                conn.logout()
                return {"success": True, "data": {"unread": count, "source": "imap"}}
            except Exception as e:
                return {"success": False, "error": str(e)}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Gmail"}
        try:
            resp = await self._http.get(
                "/labels/UNREAD" if False else "/labels/INBOX",
                headers=headers,
            )
            resp.raise_for_status()
            label = resp.json()
            return {
                "success": True,
                "data": {
                    "unread": label.get("messagesUnread", 0),
                    "total": label.get("messagesTotal", 0),
                    "source": "gmail",
                },
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def summarize_inbox(self, llm: Optional[Callable] = None, max_emails: int = 10, **kwargs) -> dict:
        """Fetch recent unread and optionally pass to LLM for summarisation."""
        if self._use_imap:
            try:
                conn = self._imap_connect()
                conn.select("INBOX")
                _, data = conn.search(None, "UNSEEN")
                ids = data[0].split()
                ids = ids[-max_emails:] if len(ids) > max_emails else ids
                ids.reverse()
                messages: list[dict] = []
                for mid in ids:
                    _, msg_data = conn.fetch(mid, "(RFC822)")
                    if msg_data and msg_data[0] and isinstance(msg_data[0], tuple):
                        parsed = self._parse_imap_message(msg_data[0][1])
                        messages.append({"subject": parsed["subject"], "from": parsed["from"], "snippet": parsed.get("body", "")[:200]})
                conn.logout()
                source = "imap"
            except Exception as e:
                return {"success": False, "error": str(e)}
        else:
            headers = await self._headers()
            if not headers:
                return {"success": False, "error": "Not connected to Gmail"}
            try:
                resp = await self._http.get(
                    "/messages",
                    params={"maxResults": max_emails, "labelIds": "UNREAD"},
                    headers=headers,
                )
                resp.raise_for_status()
                stubs = resp.json().get("messages", [])
                messages = []
                for stub in stubs:
                    detail = await self._http.get(
                        f"/messages/{stub['id']}",
                        params={"format": "metadata", "metadataHeaders": "Subject,From"},
                        headers=headers,
                    )
                    detail.raise_for_status()
                    d = detail.json()
                    hdr: dict[str, str] = {}
                    for h in d.get("payload", {}).get("headers", []):
                        hdr[h["name"].lower()] = h["value"]
                    messages.append({
                        "subject": hdr.get("subject", ""),
                        "from": hdr.get("from", ""),
                        "snippet": d.get("snippet", ""),
                    })
                source = "gmail"
            except Exception as e:
                return {"success": False, "error": str(e)}

        if not messages:
            return {"success": True, "data": {"summary": "Inbox zero — no unread messages.", "count": 0, "source": source}}

        if llm:
            try:
                prompt = "Summarize these unread emails concisely:\n\n" + "\n".join(
                    f"- From: {m['from']} | Subject: {m['subject']} | {m.get('snippet', '')}" for m in messages
                )
                summary = await llm(prompt)
                return {"success": True, "data": {"summary": summary, "count": len(messages), "source": source}}
            except Exception as e:
                logger.warning("LLM summarisation failed, returning raw: %s", e)

        lines = [f"• {m['from']}: {m['subject']}" for m in messages]
        return {"success": True, "data": {"summary": "\n".join(lines), "count": len(messages), "source": source}}

    async def close(self):
        await self._http.aclose()
