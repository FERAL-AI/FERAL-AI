"""
FERAL Push Notification Channel — Firebase Cloud Messaging (Android) + APNs (iOS)
==================================================================================
Sends push notifications through FCM v1 HTTP API and APNs HTTP/2.
Device tokens are stored in a local SQLite database.
Gracefully degrades when credentials are not configured.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from config.loader import feral_data_home

logger = logging.getLogger("feral.channels.push")


def _db_path() -> Path:
    base = feral_data_home()
    base.mkdir(parents=True, exist_ok=True)
    return base / "push_tokens.db"


# Aliases an iOS/Android client would plausibly send. The send path only ever
# tested ``platform == "apns"`` and fell through to FCM for everything else,
# so a device registered as "ios" (the obvious value for an iOS app to post)
# would have had its APNs token handed to Firebase and silently rejected.
_PLATFORM_ALIASES = {
    "apns": "apns",
    "ios": "apns",
    "iphone": "apns",
    "fcm": "fcm",
    "android": "fcm",
    "firebase": "fcm",
}


def _normalize_platform(platform: str) -> str:
    """Map a client-supplied platform string onto a transport we implement."""
    key = (platform or "").strip().lower()
    resolved = _PLATFORM_ALIASES.get(key)
    if resolved is None:
        # Keep the historical fcm default so an existing registration keeps
        # working, but stop doing it silently -- routing an unknown platform
        # to Firebase is a guess, and the operator should see the guess.
        logger.warning(
            f"Unknown push platform {platform!r}; defaulting to fcm. "
            f"Known values: {sorted(set(_PLATFORM_ALIASES))}"
        )
        return "fcm"
    return resolved


class PushChannel:
    """Push notification dispatcher for FCM (Android) and APNs (iOS)."""

    def __init__(self) -> None:
        self._firebase_creds_path: str = os.environ.get("FERAL_FIREBASE_CREDENTIALS", "")
        self._apns_key_path: str = os.environ.get("FERAL_APNS_KEY_PATH", "")
        self._apns_team_id: str = os.environ.get("FERAL_APNS_TEAM_ID", "")
        self._apns_key_id: str = os.environ.get("FERAL_APNS_KEY_ID", "")
        self._apns_environment: str = os.environ.get("FERAL_APNS_ENVIRONMENT", "production")
        self._apns_topic: str = os.environ.get("FERAL_APNS_BUNDLE_ID", "com.feral.app")
        self._firebase_project_id: Optional[str] = None
        self._firebase_token: Optional[str] = None
        self._firebase_token_expiry: float = 0.0
        self._apns_token: Optional[str] = None
        self._apns_token_expiry: float = 0.0
        self._lock = threading.Lock()

        self._conn = sqlite3.connect(str(_db_path()), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

        if self._firebase_creds_path and Path(self._firebase_creds_path).exists():
            try:
                with open(self._firebase_creds_path) as f:
                    sa = json.load(f)
                self._firebase_project_id = sa.get("project_id")
                logger.info(f"Firebase credentials loaded (project: {self._firebase_project_id})")
            except Exception as exc:
                # Real failure (creds present but unreadable) -- keep WARN.
                logger.warning(f"Failed to load Firebase credentials: {exc}")
        else:
            # Expected on fresh installs that don't push to mobile devices.
            # INFO so it doesn't spook a first-time user reading the boot log.
            logger.info(
                "FCM disabled (set FERAL_FIREBASE_CREDENTIALS to enable Android push)."
            )

        if not self._apns_key_path or not Path(self._apns_key_path).exists():
            logger.info(
                "APNs disabled (set FERAL_APNS_KEY_PATH to enable iOS push)."
            )

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS device_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    token TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'fcm',
                    registered_at REAL NOT NULL,
                    UNIQUE(user_id, token)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_device_user ON device_tokens (user_id)"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ─── Device Registration ───

    def register_device(self, user_id: str, token: str, platform: str = "fcm") -> dict[str, Any]:
        # Normalize at the door so the stored row is already a transport name.
        # Otherwise every read path has to re-guess what "ios" meant.
        platform = _normalize_platform(platform)
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO device_tokens (user_id, token, platform, registered_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, token, platform, now),
            )
            self._conn.commit()
        logger.info(f"Registered device token for user={user_id} platform={platform}")
        return {"success": True, "user_id": user_id, "platform": platform}

    def get_tokens(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT token, platform, registered_at FROM device_tokens WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return [{"token": r["token"], "platform": r["platform"], "registered_at": r["registered_at"]} for r in rows]

    # ─── Sending ───

    def send_push(
        self,
        device_token: str,
        title: str,
        body: str,
        data: Optional[dict[str, str]] = None,
        platform: str = "fcm",
    ) -> dict[str, Any]:
        """Deliver one notification. BLOCKING: opens a real httpx connection.

        Stays synchronous on purpose -- ``_send_fcm`` / ``_send_apns`` use the
        sync ``httpx.Client``, so this must never be called directly from the
        event loop. Async callers go through ``broadcast``, which offloads it.
        """
        return self._send_one(device_token, title, body, data, platform)

    def _send_one(
        self,
        device_token: str,
        title: str,
        body: str,
        data: Optional[dict[str, str]],
        platform: str,
    ) -> dict[str, Any]:
        resolved = _normalize_platform(platform)
        if resolved == "apns":
            return self._send_apns(device_token, title, body, data)
        return self._send_fcm(device_token, title, body, data)

    def _broadcast_blocking(
        self,
        tokens: list[dict[str, Any]],
        title: str,
        body: str,
        data: Optional[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Fan out to every device, sequentially, on ONE worker thread.

        Sequential rather than one thread per device because the bearer-token
        caches (``_firebase_token`` / ``_apns_token``) are read-modify-written
        without holding ``_lock``; concurrent sends would race them into
        duplicate OAuth refreshes on the very first configured send.
        """
        results: list[dict[str, Any]] = []
        for entry in tokens:
            result = self._send_one(
                entry["token"], title, body, data, entry["platform"],
            )
            # Copy rather than mutate: the result dict belongs to the send
            # path, and annotating it in place aliases every caller that
            # reuses a dict. Only the last 6 chars go in -- a device token
            # is a credential and must not be echoed back over the API.
            results.append({**result, "token_suffix": str(entry["token"])[-6:]})
        return results

    async def broadcast(
        self,
        user_id: str,
        title: str,
        body: str,
        data: Optional[dict[str, str]] = None,
    ) -> list[dict[str, Any]]:
        """Send a push notification to all registered devices for a user.

        Async on purpose. This used to be a plain ``def`` returning a list
        while its only production caller (``POST /api/push/send`` in
        api/routes/timeline.py) awaited it, so every request that ever hit
        that endpoint died on ``TypeError: object list can't be used in
        'await' expression``. Nothing caught it because the route swallows
        exceptions into an error dict and ``device_tokens`` has never had a
        row in it, so the path was never exercised.

        Making the coroutine the real signature -- instead of dropping the
        ``await`` at the call site -- is the fix that stays fixed: the route
        is async, so a sync ``broadcast`` would be re-awaited by the next
        person who reads it, and ``inspect.iscoroutinefunction`` now gives
        tests something enforceable to assert on.

        The blocking network IO is pushed onto a worker thread. Calling
        ``send_push`` inline would park the whole brain's event loop for up
        to the 10s httpx timeout per device.
        """
        tokens = self.get_tokens(user_id)
        if not tokens:
            # WARN, not INFO: the caller asked for a delivery and got none.
            # An empty result list is indistinguishable from a successful
            # send unless somebody says so out loud.
            logger.warning(f"Push requested for user={user_id} but no device tokens are registered")
            return []
        return await asyncio.to_thread(
            self._broadcast_blocking, tokens, title, body, data,
        )

    def credentials_status(self) -> dict[str, Any]:
        """Report which transports could actually deliver right now.

        The route needs this to answer honestly when a send returns zero
        successes: "no credentials" and "Apple rejected the token" are
        different answers and the response shape has to distinguish them.
        """
        fcm_ready = bool(self._firebase_project_id)
        apns_ready = bool(self._apns_key_path and Path(self._apns_key_path).exists()
                          and self._apns_team_id and self._apns_key_id)
        return {
            "fcm": fcm_ready,
            "apns": apns_ready,
            "any": fcm_ready or apns_ready,
        }

    # ─── FCM v1 HTTP API ───

    def _get_fcm_bearer_token(self) -> Optional[str]:
        """Obtain an OAuth2 bearer token for FCM using google-auth, if available."""
        if time.time() < self._firebase_token_expiry and self._firebase_token:
            return self._firebase_token

        try:
            from google.oauth2 import service_account
            import google.auth.transport.requests

            scopes = ["https://www.googleapis.com/auth/firebase.messaging"]
            creds = service_account.Credentials.from_service_account_file(
                self._firebase_creds_path, scopes=scopes,
            )
            creds.refresh(google.auth.transport.requests.Request())
            self._firebase_token = creds.token
            self._firebase_token_expiry = time.time() + 3300  # ~55 min
            return self._firebase_token
        except ImportError:
            logger.warning("google-auth not installed — cannot authenticate FCM requests")
            return None
        except Exception as exc:
            logger.error(f"FCM token refresh failed: {exc}")
            return None

    def _send_fcm(
        self, token: str, title: str, body: str, data: Optional[dict[str, str]],
    ) -> dict[str, Any]:
        if not self._firebase_project_id:
            return {"success": False, "platform": "fcm", "error": "Firebase project not configured"}

        bearer = self._get_fcm_bearer_token()
        if not bearer:
            return {"success": False, "platform": "fcm", "error": "Could not obtain FCM bearer token"}

        url = f"https://fcm.googleapis.com/v1/projects/{self._firebase_project_id}/messages:send"
        message: dict[str, Any] = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
            }
        }
        if data:
            message["message"]["data"] = {k: str(v) for k, v in data.items()}

        try:
            import httpx
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, json=message, headers={"Authorization": f"Bearer {bearer}"})
            if resp.status_code == 200:
                logger.info(f"FCM push sent to token={token[:12]}…")
                return {"success": True, "platform": "fcm", "status_code": resp.status_code}
            logger.warning(f"FCM push failed ({resp.status_code}): {resp.text[:300]}")
            return {"success": False, "platform": "fcm", "status_code": resp.status_code, "error": resp.text[:300]}
        except ImportError:
            logger.warning("httpx not installed — cannot send FCM push")
            return {"success": False, "platform": "fcm", "error": "httpx not installed"}
        except Exception as exc:
            logger.error(f"FCM push error: {exc}")
            return {"success": False, "platform": "fcm", "error": str(exc)}

    # ─── APNs HTTP/2 ───

    def _get_apns_token(self) -> Optional[str]:
        """Return a cached APNs JWT, refreshing when older than 50 minutes."""
        if self._apns_token and time.time() < self._apns_token_expiry:
            return self._apns_token

        if not self._apns_team_id or not self._apns_key_id:
            logger.warning("FERAL_APNS_TEAM_ID or FERAL_APNS_KEY_ID not set")
            return None

        try:
            import jwt
        except ImportError:
            logger.warning("PyJWT not installed — cannot sign APNs token")
            return None

        key_path = Path(self._apns_key_path)
        if not key_path.exists():
            logger.warning(f"APNs .p8 key not found at {self._apns_key_path}")
            return None

        try:
            private_key = key_path.read_text()
            now = int(time.time())
            token = jwt.encode(
                {"iss": self._apns_team_id, "iat": now},
                private_key,
                algorithm="ES256",
                headers={"kid": self._apns_key_id},
            )
            self._apns_token = token
            self._apns_token_expiry = time.time() + 3000  # 50 min cache
            logger.info("APNs JWT signed and cached")
            return self._apns_token
        except Exception as exc:
            logger.error(f"APNs JWT signing failed: {exc}")
            return None

    def _send_apns(
        self, token: str, title: str, body: str, data: Optional[dict[str, str]],
    ) -> dict[str, Any]:
        if not self._apns_key_path or not Path(self._apns_key_path).exists():
            return {"success": False, "platform": "apns", "error": "APNs key not configured"}

        bearer = self._get_apns_token()
        if not bearer:
            return {"success": False, "platform": "apns", "error": "Could not obtain APNs bearer token"}

        # apns-topic must be the app's real bundle id or Apple answers 400
        # BadTopic. The only way to set it used to be smuggling "bundle_id"
        # through the notification data, which then also leaked into the
        # payload as a user-visible custom key. Prefer explicit config.
        topic = (data or {}).get("bundle_id") or self._apns_topic

        payload: dict[str, Any] = {
            "aps": {"alert": {"title": title, "body": body}, "sound": "default"},
        }
        if data:
            for k, v in data.items():
                if k == "bundle_id":
                    continue  # routing metadata, not payload
                payload[k] = v

        if self._apns_environment == "sandbox":
            host = "api.sandbox.push.apple.com"
        else:
            host = "api.push.apple.com"

        try:
            import httpx
            url = f"https://{host}/3/device/{token}"
            headers = {
                "Authorization": f"bearer {bearer}",
                "apns-topic": topic,
                "apns-push-type": "alert",
                "apns-priority": "10",
            }
            with httpx.Client(http2=True, timeout=10.0) as client:
                resp = client.post(url, json=payload, headers=headers)
            if resp.status_code == 200:
                logger.info(f"APNs push sent to token={token[:12]}…")
                return {"success": True, "platform": "apns", "status_code": resp.status_code}
            logger.warning(f"APNs push failed ({resp.status_code}): {resp.text[:300]}")
            return {"success": False, "platform": "apns", "status_code": resp.status_code, "error": resp.text[:300]}
        except ImportError:
            logger.warning("httpx with h2 not available — cannot send APNs push")
            return {"success": False, "platform": "apns", "error": "httpx with HTTP/2 support not installed"}
        except Exception as exc:
            logger.error(f"APNs push error: {exc}")
            return {"success": False, "platform": "apns", "error": str(exc)}
