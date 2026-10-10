"""Server-owned node ingress restriction and private exact review provenance.

An authenticated device is not the owner of every pending review. Node free-text
ingress remains denied; explicit decisions additionally require a bound exact
review and its originating principal. Child tasks inherit the restriction, which
is never derived from client context or a SID.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class PairedDevicePrincipal:
    """Private server admission, with immutable identity and live peer fencing.

    Construct only from verified pairing credentials, never from message fields.
    The callback retains connection/store authority; it is not persisted or sent
    to a client. Persisted identity alone never grants permission to act.
    """

    device_id: str
    is_current: Callable[[], bool] = field(repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.device_id, str) or str(UUID(self.device_id)) != self.device_id:
            raise ValueError("Invalid paired device identity")
        if not callable(self.is_current):
            raise ValueError("Missing paired device authority")

    def require_current(self) -> None:
        try:
            current = self.is_current() is True
        except Exception:
            current = False
        if not current:
            raise asyncio.CancelledError("Paired device authority changed")

    def storage_binding(self) -> dict:
        return {"version": 1, "kind": "paired_device", "device_id": self.device_id}


def canonical_device_binding(binding: dict) -> str:
    """Validate stored identity only; a stored identity never admits a caller."""
    if (not isinstance(binding, dict) or set(binding) != {"version", "kind", "device_id"}
            or type(binding.get("version")) is not int or binding["version"] != 1
            or binding.get("kind") != "paired_device"):
        raise ValueError("Invalid private review identity")
    device_id = binding.get("device_id")
    if not isinstance(device_id, str) or str(UUID(device_id)) != device_id:
        raise ValueError("Invalid private review identity")
    return json.dumps(binding, sort_keys=True, separators=(",", ":"), allow_nan=False)


def review_terms_digest(pending: dict, surface: str) -> str:
    """Exact review identity; this digest is correlation, never an action grant."""
    terms = {key: pending.get(key) for key in ("request_id", "session_id", "tool_name", "args",
                                               "browser_resource", "created_at", "expires_at", "taskflow")}
    terms["surface"] = surface
    return hashlib.sha256(json.dumps(terms, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def exact_review_card_matches(submitted, expected: dict) -> bool:
    """Canonical comparison distinguishes true/1 and refuses extra fields."""
    try:
        return (type(submitted) is dict and json.dumps(submitted, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, allow_nan=False) == json.dumps(expected, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False, allow_nan=False))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True)
class PhoneReviewOrigin:
    """Private immutable issued review; excludes owner identity from projections."""
    source_binding_json: str = field(repr=False)
    origin_session_id: str
    task_review_json: str
    surface: str
    terms_digest: str

    def descriptor(self) -> dict:
        return {"source_binding": json.loads(self.source_binding_json),
                "origin_session_id": self.origin_session_id,
                "task_review": json.loads(self.task_review_json)}


_node_ingress: ContextVar[bool | PairedDevicePrincipal] = ContextVar("feral_node_approval_ingress", default=False)


def begin_node_approval_ingress(principal: PairedDevicePrincipal | None = None) -> Token[bool | PairedDevicePrincipal]:
    """Bind only at the server's admitted node connection boundary."""
    if principal is not None and type(principal) is not PairedDevicePrincipal:
        raise ValueError("Invalid paired device authority")
    return _node_ingress.set(principal if principal is not None else True)


def end_node_approval_ingress(token: Token[bool | PairedDevicePrincipal]) -> None:
    _node_ingress.reset(token)


def device_approval_authority_unavailable() -> bool:
    return _node_ingress.get() is not False


def current_node_principal() -> PairedDevicePrincipal | None:
    value = _node_ingress.get()
    return value if type(value) is PairedDevicePrincipal else None
