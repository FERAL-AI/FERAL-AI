"""One negotiated utterance on the existing attachment and durable turn runner.

Configuration is not microphone admission. Historical status never supplies
speech, and local waiter cancellation is not a tracked task cancellation receipt.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agents.chat_turns import ChatTurnError, exact_uuid, get_chat_turn_manager
from agents.runtime_context_checkpoint import (
    RuntimeContextAttachment,
    RuntimeContextCoordinator,
)
from api.runtime_context import attachment_readiness, coordinator_for
from bridges.client_voice_attempt import (
    VoiceAttemptBinding,
    VoiceAttemptError,
    begin_voice_attempt,
    require_client_voice_producers,
    require_voice_attempt,
    voice_attempt_scope,
)
from memory.runtime_session_checkpoint import CheckpointFence
from models.protocol import AudioChunkPayload, FeralMessage

if TYPE_CHECKING:
    from api.state import BrainState
    from starlette.websockets import WebSocket
    from voice.router import VoiceRouter
    from voice.chained_pipeline import ChainedSession

Submit = Callable[
    [
        str,
        str,
        Callable[[str, dict], Awaitable[None]],
        CheckpointFence,
        Callable[[], bool],
    ],
    Awaitable[dict],
]
logger = logging.getLogger(__name__)
MANAGED_TERMINAL_TIMEOUT_SECONDS = 300


class ManagedChainedVoiceError(VoiceAttemptError):
    pass


def version(params: dict) -> None:
    if (
        type(params.get("managed_chained_voice_version")) is not int
        or params["managed_chained_voice_version"] != 1
    ):
        raise ManagedChainedVoiceError("managed_voice_contract_unsupported")


def reviewed_context(params: dict) -> tuple[str, int]:
    try:
        generation = exact_uuid(params.get("context_generation"))
    except ChatTurnError:
        raise ManagedChainedVoiceError("managed_voice_context_invalid") from None
    revision = params.get("context_revision")
    if type(revision) is not int or not 1 <= revision < 2**63 - 1:
        raise ManagedChainedVoiceError("managed_voice_context_invalid")
    return generation, revision


def public_fence(fence: CheckpointFence) -> dict:
    return {
        "contract_version": 1,
        "session_id": fence.session_id,
        "generation": fence.generation,
        "revision": fence.revision,
        "attempt_id": fence.attempt_id,
        "durable": True,
    }


@dataclass
class Utterance:
    request_id: str
    fence: CheckpointFence
    phase: str = "collecting"
    next_chunk: int = 0
    accepted: dict | None = None
    terminal: asyncio.Future[dict] | None = None
    cancel_before_acceptance: bool = False


class ClientManagedChainedVoice:
    def __init__(
        self,
        state: BrainState,
        session_id: str,
        owner: WebSocket,
        coordinator: RuntimeContextCoordinator,
        attachment: RuntimeContextAttachment,
        submit: Submit,
        surface_current: Callable[[], bool],
    ):
        self.state, self.session_id, self.owner = state, session_id, owner
        self.coordinator, self.attachment, self.submit = coordinator, attachment, submit
        self.surface_current = surface_current
        self.store, self.router = state.memory, state.voice_router
        self.binding: VoiceAttemptBinding | None = None
        self.selected: ChainedSession | None = None
        self.negotiated = False
        self.stopped = False
        self.config_fence: CheckpointFence | None = None
        self.utterance: Utterance | None = None
        self._lock = asyncio.Lock()

    def require_router(self) -> VoiceRouter:
        if self.router is None:
            raise ManagedChainedVoiceError("managed_voice_unavailable")
        return self.router

    def owner_current(self) -> bool:
        return (
            self.surface_current() is True
            and self.state.sessions.get(self.session_id) is self.owner
            and self.state.memory is self.store
            and coordinator_for(self.state) is self.coordinator
            and self.coordinator.store is self.store
            and self.state.voice_router is self.router
        )

    def assert_admission_current(self) -> None:
        if not self.owner_current() or self.stopped:
            raise ManagedChainedVoiceError("managed_voice_owner_superseded")
        if self.binding is not None and not self.binding.current():
            raise ManagedChainedVoiceError("voice_attempt_superseded")
        if (
            self.selected is not None
            and self.require_router()._chained.get_session(self.session_id)
            is not self.selected
        ):
            raise ManagedChainedVoiceError("voice_producer_superseded")
        if (
            self.config_fence is not None
            and not self.coordinator.review_generation_valid(
                self.session_id, self.config_fence.generation
            )
        ):
            raise ManagedChainedVoiceError("managed_voice_context_superseded")

    def supports(self) -> bool:
        checker = getattr(self.router, "supports_managed_chained_voice", None)
        return self.owner_current() and callable(checker) and checker() is True

    async def ready(self, expected: tuple[str, int] | None = None) -> CheckpointFence:
        self.assert_admission_current()
        if self.coordinator.has_writers(self.session_id):
            raise ManagedChainedVoiceError("managed_voice_context_busy")
        ready = await attachment_readiness(self.coordinator, self.attachment)
        self.assert_admission_current()
        if self.coordinator.has_writers(self.session_id):
            raise ManagedChainedVoiceError("managed_voice_context_busy")
        if not ready.ready or not ready.managed or ready.fence is None:
            raise ManagedChainedVoiceError("managed_voice_context_not_ready")
        if (
            expected is not None
            and (ready.fence.generation, ready.fence.revision) != expected
        ):
            raise ManagedChainedVoiceError("managed_voice_context_superseded")
        return ready.fence

    async def capabilities(self) -> list[int]:
        if not self.supports() or self.coordinator.has_writers(self.session_id):
            return []
        try:
            ready = await attachment_readiness(self.coordinator, self.attachment)
        except VoiceAttemptError:
            return []
        if (
            not self.supports()
            or not ready.ready
            or not ready.managed
            or ready.fence is None
        ):
            return []
        self.negotiated = True
        return [1]

    def identity(self, utterance: Utterance | None = None) -> dict:
        value = {
            "managed_chained_voice_version": 1,
            **(self.binding.identity() if self.binding else {}),
        }
        if utterance is not None:
            value["request_id"] = utterance.request_id
        return value

    async def configure(self, params: dict) -> dict:
        version(params)
        if not self.negotiated:
            raise ManagedChainedVoiceError("managed_voice_not_negotiated")
        if params.get("mode") == "disabled":
            return await self.stop(params)
        if not self.supports():
            raise ManagedChainedVoiceError("managed_voice_not_negotiated")
        if params.get("mode") != "chained" or params.get(
            "provider", "configured"
        ) not in {"configured", "chained"}:
            raise ManagedChainedVoiceError("managed_voice_mode_unsupported")
        async with self._lock:
            if self.stopped:
                if (
                    not self.owner_current()
                    or self.require_router()._chained.get_session(self.session_id)
                    is not None
                ):
                    raise ManagedChainedVoiceError("managed_voice_stop_unconfirmed")
                self.stopped, self.selected, self.binding, self.config_fence = (
                    False,
                    None,
                    None,
                    None,
                )
                self.utterance = None
            fence = await self.ready(reviewed_context(params))
            if self.selected is not None:
                raise ManagedChainedVoiceError(
                    "managed_voice_new_attempt_requires_stop"
                )
            require_client_voice_producers(self.state, self.session_id, None)
            self.binding = begin_voice_attempt(
                self.state, self.session_id, self.owner, params
            )
            if self.binding is None:
                raise ManagedChainedVoiceError("voice_attempt_required")
            self.config_fence = fence
            opened = None
            try:
                with voice_attempt_scope(self.binding):
                    opened = await self.require_router().open_chained_session(
                        self.session_id,
                        provider_opts=None,
                        submit_tracked_utterance=self.submit_tracked_utterance,
                        abort_tracked_utterance=self.abort_tracked_utterance,
                        assert_admission_current=self.assert_admission_current,
                        managed_send_frame=self.send_frame,
                    )
                self.assert_admission_current()
                self.selected = opened
                if (
                    opened is None
                    or self.require_router()._chained.get_session(self.session_id)
                    is not opened
                ):
                    raise ManagedChainedVoiceError("managed_voice_open_failed")
                await self.ready((fence.generation, fence.revision))
            except BaseException:
                # Retire only resources published by this exact open. A new
                # socket/producer must never be stopped by stale cleanup.
                if (
                    opened is not None
                    and self.require_router()._chained.get_session(self.session_id)
                    is opened
                ):
                    try:
                        await self.require_router()._chained.close_session(
                            self.session_id
                        )
                    except Exception as cleanup_error:
                        logger.warning(
                            "Managed voice open cleanup failed (%s)",
                            type(cleanup_error).__name__,
                        )
                self.binding.active = False
                self.stopped = True
                raise
            return {
                "mode": "chained",
                "provider": "configured",
                "status": "configured",
                **self.identity(),
                "context_checkpoint": public_fence(fence),
            }

    def require_utterance(self, params: dict) -> Utterance:
        version(params)
        self.assert_admission_current()
        if (
            require_voice_attempt(self.state, self.session_id, self.owner, params)
            is not self.binding
        ):
            raise ManagedChainedVoiceError("voice_attempt_superseded")
        utterance = self.utterance
        try:
            request = exact_uuid(params.get("request_id"))
        except ChatTurnError:
            raise ManagedChainedVoiceError("managed_voice_request_invalid") from None
        if utterance is None or request != utterance.request_id:
            raise ManagedChainedVoiceError("managed_voice_utterance_superseded")
        return utterance

    async def begin(self, params: dict) -> dict:
        version(params)
        async with self._lock:
            self.assert_admission_current()
            if (
                self.selected is None
                or self.binding is None
                or require_voice_attempt(
                    self.state, self.session_id, self.owner, params
                )
                is not self.binding
            ):
                raise ManagedChainedVoiceError("managed_voice_not_configured")
            if self.utterance is not None:
                raise ManagedChainedVoiceError("managed_voice_utterance_reused")
            try:
                request = exact_uuid(params.get("request_id"))
            except ChatTurnError:
                raise ManagedChainedVoiceError(
                    "managed_voice_request_invalid"
                ) from None
            fence = await self.ready(reviewed_context(params))
            if (
                await get_chat_turn_manager(self.state).status(
                    session_id=self.session_id, request_id=request
                )
                is not None
            ):
                raise ManagedChainedVoiceError("managed_voice_request_reused")
            self.assert_admission_current()
            await self.ready((fence.generation, fence.revision))
            self.utterance = Utterance(request, fence)
            await self.require_router()._chained.begin_utterance(self.selected, request)
            return {
                "status": "collecting",
                **self.identity(self.utterance),
                "context_checkpoint": public_fence(fence),
            }

    async def audio(self, params: dict) -> dict:
        async with self._lock:
            return await self._audio(params)

    async def _audio(self, params: dict) -> dict:
        utterance = self.require_utterance(params)
        if utterance.phase != "collecting":
            raise ManagedChainedVoiceError("managed_voice_audio_not_collecting")
        try:
            payload = AudioChunkPayload(**params)
        except ValueError:
            raise ManagedChainedVoiceError("voice_audio_invalid") from None
        if (
            payload.encoding != "pcm16"
            or payload.sample_rate != 24000
            or payload.channels != 1
            or payload.is_final
            or type(params.get("chunk_index")) is not int
            or type(params.get("sample_rate", 24000)) is not int
            or type(params.get("channels", 1)) is not int
            or type(params.get("is_final", False)) is not bool
            or payload.chunk_index != utterance.next_chunk
        ):
            raise ManagedChainedVoiceError("managed_voice_audio_invalid")
        await self.ready((utterance.fence.generation, utterance.fence.revision))
        try:
            with voice_attempt_scope(self.binding):
                await self.require_router()._chained.handle_audio_for_session(
                    self.selected,
                    payload.data_b64,
                    payload.chunk_index,
                    False,
                    wait_for_turn=False,
                    request_id=utterance.request_id,
                )
        except ValueError:
            raise ManagedChainedVoiceError("managed_voice_audio_invalid") from None
        self.assert_admission_current()
        utterance.next_chunk += 1
        return {"received": True, **self.identity(utterance)}

    async def finish(self, params: dict) -> dict:
        async with self._lock:
            utterance = self.require_utterance(params)
            if utterance.phase != "collecting":
                raise ManagedChainedVoiceError("managed_voice_utterance_reused")
            if reviewed_context(params) != (
                utterance.fence.generation,
                utterance.fence.revision,
            ):
                raise ManagedChainedVoiceError("managed_voice_context_superseded")
            await self.ready((utterance.fence.generation, utterance.fence.revision))
            utterance.phase = "submitted_processing"
            with voice_attempt_scope(self.binding):
                await self.require_router()._chained.finish_utterance(
                    self.selected, utterance.request_id
                )
            return {
                "status": "submitted_processing",
                **self.identity(utterance),
                "task_accepted": False,
            }

    async def submit_tracked_utterance(self, request_id: str, transcript: str) -> dict:
        utterance = self.utterance
        self.assert_admission_current()
        if (
            utterance is None
            or utterance.request_id != request_id
            or utterance.phase != "submitted_processing"
        ):
            raise ManagedChainedVoiceError("managed_voice_utterance_superseded")
        await self.ready((utterance.fence.generation, utterance.fence.revision))
        await self.send_frame(
            self.session_id,
            {
                "type": "transcript",
                "payload": {
                    "role": "user",
                    "text": transcript,
                    "is_partial": False,
                    "request_id": request_id,
                },
            },
        )
        await self.ready((utterance.fence.generation, utterance.fence.revision))
        utterance.terminal = asyncio.get_running_loop().create_future()

        async def receive(kind: str, receipt: dict) -> None:
            if not self.owner_current():
                raise ManagedChainedVoiceError("managed_voice_owner_superseded")
            if kind == "error":
                if receipt.get("request_id") != request_id:
                    raise ManagedChainedVoiceError("managed_voice_receipt_invalid")
                if utterance.terminal is not None and not utterance.terminal.done():
                    # A cancelled local waiter must not leave an unobserved
                    # Future exception. This is an error frame, never a receipt.
                    utterance.terminal.set_result(dict(receipt))
                await self.owner.send_json(
                    FeralMessage(
                        session_id=self.session_id,
                        hop="brain",
                        type=kind,
                        payload=receipt,
                    ).model_dump()
                )
                return
            if (
                receipt.get("session_id") != self.session_id
                or receipt.get("request_id") != request_id
                or type(receipt.get("contract_version")) is not int
                or receipt.get("contract_version") != 1
                or receipt.get("durable") is not True
                or receipt.get("replayed") is not False
                or kind not in {"chat_turn_accepted", "chat_turn_terminal"}
            ):
                raise ManagedChainedVoiceError("managed_voice_receipt_invalid")
            if kind == "chat_turn_accepted":
                exact_uuid(receipt.get("turn_id"))
                utterance.accepted = dict(receipt)
            elif kind == "chat_turn_terminal":
                if (
                    utterance.accepted is None
                    or receipt.get("turn_id") != utterance.accepted["turn_id"]
                ):
                    raise ManagedChainedVoiceError("managed_voice_receipt_invalid")
                if utterance.terminal is not None and not utterance.terminal.done():
                    utterance.terminal.set_result(dict(receipt))
            await self.owner.send_json(
                FeralMessage(
                    session_id=self.session_id, hop="brain", type=kind, payload=receipt
                ).model_dump()
            )

        binding = self.binding

        def admission_current() -> bool:
            self.assert_admission_current()
            return (
                self.utterance is utterance
                and self.binding is binding
                and not utterance.cancel_before_acceptance
            )

        accepted = await self.submit(
            request_id, transcript, receive, utterance.fence, admission_current
        )
        if (
            utterance.accepted is None
            or accepted.get("turn_id") != utterance.accepted["turn_id"]
        ):
            raise ManagedChainedVoiceError("managed_voice_receipt_invalid")
        if utterance.cancel_before_acceptance:
            await self.abort_tracked_utterance(request_id)
        try:
            terminal = await asyncio.wait_for(
                asyncio.shield(utterance.terminal), MANAGED_TERMINAL_TIMEOUT_SECONDS
            )
        except TimeoutError:
            utterance.phase = "terminal_unconfirmed"
            await self.send_frame(
                self.session_id,
                {
                    "type": "voice_state",
                    "payload": {
                        "state": "error",
                        "code": "managed_voice_terminal_unconfirmed",
                        "request_id": request_id,
                        "status_reconciliation_required": True,
                        "task_cancelled": False,
                    },
                },
            )
            raise ManagedChainedVoiceError(
                "managed_voice_terminal_unconfirmed"
            ) from None
        if terminal.get("code") is not None:
            raise ManagedChainedVoiceError("managed_voice_receipt_unavailable")
        self.assert_admission_current()
        if self.utterance is not utterance:
            raise ManagedChainedVoiceError("managed_voice_utterance_superseded")
        if terminal.get("processing_outcome") in {
            "completed",
            "awaiting_approval",
            "refused",
        }:
            fence = await self.ready()
            if terminal.get("context_checkpoint") != public_fence(fence):
                raise ManagedChainedVoiceError("managed_voice_context_superseded")
        return terminal

    async def abort_tracked_utterance(
        self, request_id: str, turn_id: str | None = None
    ) -> dict:
        if (
            not self.owner_current()
            or self.binding is None
            or not self.binding.owner_current()
            or not self.binding.ledger_current()
        ):
            raise ManagedChainedVoiceError("managed_voice_owner_superseded")
        utterance = self.utterance
        if utterance is None or request_id != utterance.request_id:
            raise ManagedChainedVoiceError("managed_voice_utterance_superseded")
        if utterance.accepted is None:
            utterance.cancel_before_acceptance = True
            return {
                "status": "acceptance_unconfirmed",
                "cancel_requested": False,
                "request_id": request_id,
                "status_reconciliation_required": True,
            }
        exact_turn = utterance.accepted["turn_id"]
        if turn_id is not None and turn_id != exact_turn:
            raise ManagedChainedVoiceError("managed_voice_utterance_superseded")
        return await get_chat_turn_manager(self.state).abort(
            owner=self.owner,
            session_id=self.session_id,
            request_id=request_id,
            turn_id=exact_turn,
        )

    async def send_frame(self, session_id: str, frame: dict) -> None:
        self.assert_admission_current()
        utterance = self.utterance
        payload = frame.get("payload", {})
        if (
            utterance is None
            and session_id == self.session_id
            and frame.get("type") == "voice_state"
            and payload.get("state") == "idle"
        ):
            await self.owner.send_json(
                FeralMessage(
                    session_id=session_id,
                    hop="brain",
                    type="voice_state",
                    payload={**payload, **self.identity()},
                ).model_dump()
            )
            self.assert_admission_current()
            return
        if (
            utterance is None
            or session_id != self.session_id
            or payload.get("request_id") != utterance.request_id
        ):
            raise ManagedChainedVoiceError("managed_voice_media_unbound")
        committed_media = frame.get("type") == "audio_chunk" or (
            frame.get("type") == "transcript" and payload.get("role") == "assistant"
        )
        if committed_media:
            if (
                utterance.terminal is None
                or not utterance.terminal.done()
                or utterance.terminal.cancelled()
            ):
                raise ManagedChainedVoiceError("managed_voice_media_unbound")
            terminal = utterance.terminal.result()
            if (
                terminal.get("processing_outcome")
                not in {"completed", "awaiting_approval", "refused"}
                or payload.get("turn_id") != terminal.get("turn_id")
                or payload.get("context_checkpoint")
                != terminal.get("context_checkpoint")
            ):
                raise ManagedChainedVoiceError("managed_voice_media_unbound")
            fence = await self.ready()
            if terminal.get("context_checkpoint") != public_fence(fence):
                raise ManagedChainedVoiceError("managed_voice_context_superseded")
        await self.owner.send_json(
            FeralMessage(
                session_id=session_id,
                hop="brain",
                type=frame["type"],
                payload={
                    "context_checkpoint": public_fence(utterance.fence),
                    **payload,
                    **self.identity(utterance),
                },
            ).model_dump()
        )
        self.assert_admission_current()
        if committed_media:
            fence = await self.ready()
            if terminal.get("context_checkpoint") != public_fence(fence):
                raise ManagedChainedVoiceError("managed_voice_context_superseded")

    async def stop(self, params: dict) -> dict:
        if (
            require_voice_attempt(
                self.state, self.session_id, self.owner, params, allow_stopped=True
            )
            is not self.binding
            or self.binding is None
        ):
            raise ManagedChainedVoiceError("voice_attempt_superseded")
        if (
            self.utterance is not None
            and "request_id" in params
            and params["request_id"] != self.utterance.request_id
        ):
            raise ManagedChainedVoiceError("managed_voice_utterance_superseded")
        # Fence queued admission before any awaited manager/producer cleanup.
        self.stopped = True
        self.binding.lifecycle_pending = True
        result = {"status": "not_submitted", "cancel_requested": False}
        if self.utterance is not None:
            result = await self.abort_tracked_utterance(self.utterance.request_id)
        with voice_attempt_scope(self.binding):
            await self.require_router().stop_session_voice(
                self.session_id, expected_attempt=self.binding, fenced=True
            )
        if (
            not self.owner_current()
            or self.require_router()._chained.get_session(self.session_id) is not None
        ):
            raise ManagedChainedVoiceError("managed_voice_stop_unconfirmed")
        self.binding.active = False
        self.binding.lifecycle_pending = False
        return {
            "mode": "disabled",
            "provider": "configured",
            "status": "ok",
            **self.identity(),
            "task_cancellation": result,
        }
