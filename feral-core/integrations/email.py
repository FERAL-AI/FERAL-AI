"""
FERAL Email Integration — Gmail API + IMAP Fallback
=====================================================
Inbox management, search, send, draft, and LLM-powered summarisation.
Falls back to IMAP when no Google OAuth is available.
"""

from __future__ import annotations

import asyncio
import base64
import email as email_stdlib
import imaplib
import json
import logging
import os
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from typing import Any, Callable, Optional

import httpx

from integrations._http_errors import http_error_detail

logger = logging.getLogger("feral.integrations.email")

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# App-password (IMAP/SMTP) path — used when the operator pastes a Gmail
# address + 16-char App Password in Settings → Integrations instead of
# running the full Google OAuth flow.
GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_IMAP_PORT = 993
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587

# Vault key under which the Gmail address + App Password are persisted.
EMAIL_CRED_VAULT_KEY = "email_app_credential"


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
        # Cached App Password creds (address + app_password), loaded from the
        # vault. Resolved lazily so a Save in Settings takes effect without a
        # brain restart.
        self._app_creds: Optional[dict] = self._load_app_creds()

    # ── App Password credential store (IMAP/SMTP, no OAuth) ────────

    def _vault(self):
        return getattr(self._oauth, "_vault", None)

    def _load_app_creds(self) -> Optional[dict]:
        """Read the saved Gmail address + App Password from the vault."""
        vault = self._vault()
        if vault is None:
            return None
        try:
            raw = vault.retrieve(EMAIL_CRED_VAULT_KEY, requester="email")
        except Exception:
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if data.get("address") and data.get("app_password"):
            return {"address": data["address"], "app_password": data["app_password"]}
        return None

    def store_app_password(self, address: str, app_password: str) -> bool:
        """Persist Gmail App Password creds and refresh the in-memory cache."""
        data = {"address": address.strip(), "app_password": app_password.replace(" ", "")}
        vault = self._vault()
        if vault is not None:
            vault.store(EMAIL_CRED_VAULT_KEY, json.dumps(data), stored_by="email")
        self._app_creds = data
        # The Google probe cache keys off "google" OAuth; an App Password
        # connection should not be shadowed by a stale OAuth probe miss.
        return True

    def clear_app_password(self) -> None:
        vault = self._vault()
        if vault is not None:
            try:
                vault.remove(EMAIL_CRED_VAULT_KEY, removed_by="email")
            except Exception:
                pass
        self._app_creds = None

    def _resolve_imap(self) -> Optional[tuple[str, int, str, str]]:
        """Return (host, port, user, password) for the IMAP path.

        Explicit ``FERAL_EMAIL_IMAP_*`` env vars win; otherwise fall back
        to the Gmail App Password saved via Settings → Integrations.
        """
        if self._imap_host and self._imap_user and self._imap_pass:
            return (self._imap_host, self._imap_port, self._imap_user, self._imap_pass)
        creds = self._load_app_creds() or self._app_creds
        if creds:
            return (GMAIL_IMAP_HOST, GMAIL_IMAP_PORT, creds["address"], creds["app_password"])
        return None

    def _resolve_smtp(self) -> Optional[tuple[str, int, str, str]]:
        creds = self._load_app_creds() or self._app_creds
        if creds:
            return (GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, creds["address"], creds["app_password"])
        return None

    @staticmethod
    def probe_app_password(address: str, app_password: str) -> dict:
        """Live IMAP+SMTP login check. Blocking — call via asyncio.to_thread."""
        address = (address or "").strip()
        app_password = (app_password or "").replace(" ", "")
        result: dict[str, Any] = {"imap": False, "smtp": False}
        try:
            conn = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, GMAIL_IMAP_PORT)
            conn.login(address, app_password)
            conn.logout()
            result["imap"] = True
        except Exception as e:  # noqa: BLE001 — surface the real reason to UI
            result["imap_error"] = str(e)
        try:
            smtp = smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=15)
            smtp.starttls()
            smtp.login(address, app_password)
            smtp.quit()
            result["smtp"] = True
        except Exception as e:  # noqa: BLE001
            result["smtp_error"] = str(e)
        # Reading the inbox is the load-bearing capability; SMTP is best-effort.
        result["ok"] = result["imap"]
        return result

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

        # App Password (IMAP/SMTP) path: creds are only stored after a
        # successful live probe, so their presence means Gmail is usable.
        if self._resolve_imap() is not None:
            return True

        token_present = (
            self._oauth is not None and self._oauth.is_connected("google")
        )
        return is_connected_cached("google", fallback=token_present)

    @property
    def imap_configured(self) -> bool:
        # An IMAP host being configured advertises the fallback path even
        # before a full login is possible; saved App Password creds also count.
        return self._imap_host is not None or self._load_app_creds() is not None

    async def probe_connected(self) -> bool:
        from integrations._probe_status import refresh

        result = await refresh("google",
                               vault=getattr(self._oauth, "_vault", None))
        if result is None:
            return self.connected
        return result

    @property
    def _use_imap(self) -> bool:
        # Prefer Gmail OAuth (full API) when present; otherwise use the
        # IMAP/SMTP App Password path if any IMAP creds resolve.
        if self._oauth is not None and self._oauth.is_connected("google"):
            return False
        return self._resolve_imap() is not None

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
        resolved = self._resolve_imap()
        if resolved is None:
            raise RuntimeError("No IMAP credentials configured")
        host, port, user, password = resolved
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(user, password)
        return conn

    @staticmethod
    def _clamp_max_results(max_results: int, default: int = 10) -> int:
        try:
            n = int(max_results)
        except (TypeError, ValueError):
            n = default
        return min(max(1, n), 100)

    @staticmethod
    def _is_gmail_imap_host(host: str) -> bool:
        h = (host or "").lower()
        return h == GMAIL_IMAP_HOST or "gmail" in h

    @staticmethod
    def _normalize_imap_date(date_str: str) -> str:
        """Convert ISO YYYY-MM-DD or YYYY/MM/DD to IMAP SINCE/BEFORE form (DD-Mon-YYYY)."""
        cleaned = (date_str or "").strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(cleaned, fmt)
                return dt.strftime("%d-%b-%Y")
            except ValueError:
                continue
        raise ValueError(f"Invalid date '{date_str}'; use YYYY-MM-DD or YYYY/MM/DD")

    @staticmethod
    def _normalize_gmail_date(date_str: str) -> str:
        """Convert ISO date to Gmail after:/before: form (YYYY/MM/DD)."""
        cleaned = (date_str or "").strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(cleaned, fmt)
                return dt.strftime("%Y/%m/%d")
            except ValueError:
                continue
        raise ValueError(f"Invalid date '{date_str}'; use YYYY-MM-DD or YYYY/MM/DD")

    @classmethod
    def _compose_gmail_query(
        cls,
        query: str = "",
        *,
        from_: str = "",
        subject: str = "",
        since: str = "",
        before: str = "",
        body: str = "",
        has_attachment: bool = False,
    ) -> str:
        """Build a Gmail ``q`` / X-GM-RAW string from free-text and structured filters."""
        parts: list[str] = []
        if query.strip():
            parts.append(query.strip())
        if from_.strip():
            parts.append(f"from:{from_.strip()}")
        if subject.strip():
            parts.append(f"subject:{subject.strip()}")
        if since.strip():
            parts.append(f"after:{cls._normalize_gmail_date(since)}")
        if before.strip():
            parts.append(f"before:{cls._normalize_gmail_date(before)}")
        if body.strip():
            parts.append(body.strip())
        if has_attachment:
            parts.append("has:attachment")
        return " ".join(parts)

    @classmethod
    def _build_generic_imap_search(
        cls,
        query: str = "",
        *,
        from_: str = "",
        subject: str = "",
        since: str = "",
        before: str = "",
        body: str = "",
    ) -> str:
        """Build RFC3501 SEARCH criteria for non-Gmail IMAP hosts."""
        clauses: list[str] = []
        if from_.strip():
            clauses.append(f'FROM "{from_.strip()}"')
        if subject.strip():
            clauses.append(f'SUBJECT "{subject.strip()}"')
        if since.strip():
            clauses.append(f"SINCE {cls._normalize_imap_date(since)}")
        if before.strip():
            clauses.append(f"BEFORE {cls._normalize_imap_date(before)}")
        if body.strip():
            clauses.append(f'TEXT "{body.strip()}"')
        if query.strip() and not any((from_, subject, since, before, body)):
            clauses.append(f'TEXT "{query.strip()}"')
        elif query.strip():
            clauses.append(f'TEXT "{query.strip()}"')
        if not clauses:
            return "ALL"
        if len(clauses) == 1:
            return clauses[0]
        return f"({' '.join(clauses)})"

    @staticmethod
    def _parse_imap_headers(raw_bytes: bytes, flags_line: str = "") -> dict[str, Any]:
        """Parse a header-only IMAP fetch (no body) for search/list results."""
        msg = email_stdlib.message_from_bytes(raw_bytes)
        subject = msg.get("Subject", "") or ""
        return {
            "subject": subject,
            "from": msg.get("From", "") or "",
            "date": msg.get("Date", "") or "",
            "snippet": subject[:200],
            "thread_id": msg.get("Message-ID", "") or "",
        }

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

    def _imap_search_sync(
        self,
        query: str,
        max_results: int,
        from_: str = "",
        subject: str = "",
        since: str = "",
        before: str = "",
        body: str = "",
        folder: str = "INBOX",
        has_attachment: bool = False,
    ) -> dict[str, Any]:
        """Blocking IMAP search — call via ``asyncio.to_thread``.

        Gmail IMAP hosts use ``X-GM-RAW`` so free-text ``query`` and structured
        filters share Gmail search syntax. Generic IMAP hosts translate structured
        filters into RFC3501 SEARCH criteria; a lone ``query`` becomes ``TEXT``.
        """
        resolved = self._resolve_imap()
        if resolved is None:
            raise RuntimeError("No IMAP credentials configured")
        host, _port, _user, _password = resolved
        gmail_host = self._is_gmail_imap_host(host)

        if gmail_host:
            query_used = self._compose_gmail_query(
                query,
                from_=from_,
                subject=subject,
                since=since,
                before=before,
                body=body,
                has_attachment=has_attachment,
            )
            if not query_used:
                query_used = "in:inbox"
        else:
            query_used = self._build_generic_imap_search(
                query,
                from_=from_,
                subject=subject,
                since=since,
                before=before,
                body=body,
            )

        conn = self._imap_connect()
        try:
            conn.select(folder or "INBOX")
            if gmail_host:
                _, data = conn.uid("SEARCH", "X-GM-RAW", f'"{query_used}"')
                id_key = "uid"
            else:
                if query_used == "ALL":
                    _, data = conn.search(None, "ALL")
                else:
                    _, data = conn.search(None, query_used)
                id_key = "seq"

            raw_ids = data[0].split() if data and data[0] else []
            ids = raw_ids[-max_results:] if len(raw_ids) > max_results else raw_ids
            ids.reverse()

            header_fetch = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] FLAGS)"
            messages: list[dict[str, Any]] = []
            for mid in ids:
                if id_key == "uid":
                    _, msg_data = conn.uid("FETCH", mid, header_fetch)
                else:
                    _, msg_data = conn.fetch(mid, header_fetch)
                if not msg_data or not msg_data[0]:
                    continue
                item = msg_data[0]
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                meta = item[0]
                if isinstance(meta, bytes):
                    meta = meta.decode("utf-8", errors="replace")
                parsed = self._parse_imap_headers(item[1], flags_line=str(meta))
                parsed["id"] = mid.decode() if isinstance(mid, bytes) else str(mid)
                if has_attachment:
                    parsed["has_attachments"] = True
                messages.append(parsed)

            return {
                "messages": messages,
                "source": "imap",
                "query_used": query_used,
            }
        finally:
            try:
                conn.logout()
            except Exception:
                pass

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
        max_results = self._clamp_max_results(max_results, default=20)
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
                return {"success": False, "error": http_error_detail(e)}

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
            return {"success": False, "error": http_error_detail(e)}

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
                return {"success": False, "error": http_error_detail(e)}

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
            return {"success": False, "error": http_error_detail(e)}

    async def search(
        self,
        query: str = "",
        max_results: int = 10,
        from_: str = "",
        subject: str = "",
        since: str = "",
        before: str = "",
        body: str = "",
        folder: str = "INBOX",
        has_attachment: bool = False,
        **kwargs,
    ) -> dict:
        max_results = self._clamp_max_results(max_results, default=10)

        if self._use_imap:
            try:
                data = await asyncio.to_thread(
                    self._imap_search_sync,
                    query,
                    max_results,
                    from_,
                    subject,
                    since,
                    before,
                    body,
                    folder,
                    has_attachment,
                )
                return {"success": True, "data": data}
            except Exception as e:
                return {"success": False, "error": http_error_detail(e)}

        headers = await self._headers()
        if not headers:
            return {"success": False, "error": "Not connected to Gmail"}
        query_used = self._compose_gmail_query(
            query,
            from_=from_,
            subject=subject,
            since=since,
            before=before,
            body=body,
            has_attachment=has_attachment,
        )
        try:
            resp = await self._http.get(
                "/messages",
                params={"q": query_used, "maxResults": max_results},
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json()
            msg_stubs = payload.get("messages", [])
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
                entry: dict[str, Any] = {
                    "id": d.get("id", ""),
                    "thread_id": d.get("threadId", ""),
                    "subject": hdr.get("subject", ""),
                    "from": hdr.get("from", ""),
                    "date": hdr.get("date", ""),
                    "snippet": d.get("snippet", ""),
                }
                if has_attachment:
                    entry["has_attachments"] = True
                messages.append(entry)
            result: dict[str, Any] = {
                "messages": messages,
                "source": "gmail",
                "query_used": query_used,
            }
            if payload.get("nextPageToken"):
                result["next_page_token"] = payload["nextPageToken"]
            return {"success": True, "data": result}
        except Exception as e:
            return {"success": False, "error": http_error_detail(e)}

    def _smtp_send(self, to: str, subject: str, body: str) -> dict:
        resolved = self._resolve_smtp()
        if resolved is None:
            return {"success": False, "error": "No SMTP credentials configured"}
        host, port, user, password = resolved
        try:
            mime = MIMEText(body, "plain")
            mime["From"] = user
            mime["To"] = to
            mime["Subject"] = subject
            smtp = smtplib.SMTP(host, port, timeout=20)
            smtp.starttls()
            smtp.login(user, password)
            smtp.sendmail(user, [to], mime.as_string())
            smtp.quit()
            return {"success": True, "data": {"to": to, "subject": subject, "source": "smtp"}}
        except Exception as e:
            return {"success": False, "error": http_error_detail(e)}

    async def send_email(self, to: str = "", subject: str = "", body: str = "", **kwargs) -> dict:
        if self._use_imap:
            import asyncio
            if self._resolve_smtp() is not None:
                return await asyncio.to_thread(self._smtp_send, to, subject, body)
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
            return {"success": False, "error": http_error_detail(e)}

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
            return {"success": False, "error": http_error_detail(e)}

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
                return {"success": False, "error": http_error_detail(e)}

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
            return {"success": False, "error": http_error_detail(e)}

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
                return {"success": False, "error": http_error_detail(e)}
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
                return {"success": False, "error": http_error_detail(e)}

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
