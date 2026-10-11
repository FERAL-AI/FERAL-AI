"""Bounded process-local replay of output received from a Responses wire.

Opaque attributes never enter JSON history, websocket frames, or logs. Rebuilt
transcripts have ordinary dicts and cannot authorize native provider replay.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

MAX_ITEMS = 64
MAX_BYTES = 262_144


class _Messages(list):
    __slots__ = ('owner', 'session')


class _Envelope(dict):
    __slots__ = ('continuation',)


class _Assistant(dict):
    __slots__ = ('continuation',)


@dataclass(frozen=True, repr=False)
class _Origin:
    owner: object = field(repr=False)
    session: str
    provider: str
    model: str
    endpoint: str
    credential_digest: str = field(repr=False)


@dataclass(frozen=True, repr=False)
class _Continuation:
    origin: _Origin = field(repr=False)
    items_json: str = field(repr=False)
    message_digest: str


def bind_messages(messages, owner, session):
    bound = _Messages(messages)
    bound.owner, bound.session = owner, session
    return bound


def origin_for(messages, owner, model, client, provider=None):
    if type(messages) is not _Messages or messages.owner is not owner or not messages.session:
        return None
    endpoint = str(getattr(client, 'base_url', ''))
    authorization = str(getattr(client, 'headers', {}).get('Authorization', ''))
    if not endpoint or not authorization:
        return None
    return _Origin(owner, messages.session, provider or getattr(owner, 'provider', ''), model, endpoint, hashlib.sha256(authorization.encode()).hexdigest())


def _digest(message):
    try:
        calls = []
        for call in message.get('tool_calls', []) or []:
            function = call.get('function', {})
            arguments = function.get('arguments', '{}')
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    pass
            calls.append({'id': call.get('id'), 'name': function.get('name'), 'arguments': arguments})
        value = {'content': message.get('content') or '', 'tool_calls': calls}
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    except (ValueError, TypeError, AttributeError, RecursionError):
        return ''


def capture_wire(result, payload, origin):
    """Called only after successful HTTP decoding, never by a normalizer."""
    if origin is None or not isinstance(payload, dict) or payload.get('error') or result.get('error'):
        return result
    resolved_model = payload.get('model', origin.model)
    same_model = resolved_model == origin.model or (
        isinstance(resolved_model, str)
        and re.fullmatch(re.escape(origin.model) + r'-\d{4}-\d{2}-\d{2}', resolved_model) is not None
    )
    if payload.get('status') != 'completed' or not same_model:
        return result
    output = payload.get('output')
    if not isinstance(output, list) or not output or len(output) > MAX_ITEMS:
        return result
    items = []
    for item in output:
        if not isinstance(item, dict) or item.get('type') not in {'message', 'function_call', 'reasoning'}:
            return result
        kind = item['type']
        if item.get('status') not in {None, 'completed'}:
            return result
        if kind == 'message' and (item.get('role') != 'assistant' or item.get('phase') not in {None, 'commentary', 'final_answer'}):
            return result
        if kind == 'reasoning' and (not isinstance(item.get('encrypted_content'), str) or not item['encrypted_content']):
            # A server-stored reasoning id is not portable across provider accounts.
            return result
        if kind == 'function_call' and not all(isinstance(item.get(k), str) and item[k] for k in ('call_id', 'name', 'arguments')):
            return result
        items.append(item)
    try:
        encoded = json.dumps(items, ensure_ascii=False)
    except (ValueError, TypeError, RecursionError):
        return result
    if len(encoded.encode()) > MAX_BYTES:
        return result
    message = result.get('choices', [{}])[0].get('message', {})
    digest = _digest(message)
    if not digest:
        return result
    envelope = _Envelope(result)
    envelope.continuation = _Continuation(origin, encoded, digest)
    return envelope


def attach_history(message, response, owner, session):
    if type(response) is not _Envelope:
        return message
    continuation = response.continuation
    if continuation.origin.owner is not owner or continuation.origin.session != session or _digest(message) != continuation.message_digest:
        return message
    history = _Assistant(message)
    history.continuation = continuation
    return history


def replay_items(message, origin):
    if origin is None or type(message) is not _Assistant:
        return None
    continuation = message.continuation
    previous = continuation.origin
    if (previous.owner is not origin.owner or previous.session != origin.session
            or previous.provider != origin.provider or previous.model != origin.model or previous.endpoint != origin.endpoint
            or previous.credential_digest != origin.credential_digest
            or _digest(message) != continuation.message_digest):
        return None
    return json.loads(continuation.items_json)


def stream_terminal(done, response):
    if type(response) is not _Envelope:
        return done
    envelope = _Envelope(done)
    envelope.continuation = response.continuation
    return envelope
