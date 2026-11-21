"""Actual raw-socket refusal must be identifiable without granting replay."""
import pytest

from models.protocol import parse_message
from tests.test_client_managed_chained_voice import managed_client as _managed_client
from tests.test_client_managed_chained_voice import configure, until
from tests.test_runtime_context_ingress import context_client as _context_client

managed_client = _managed_client
context_client = _context_client
pytestmark = pytest.mark.no_auto_feral_home


@pytest.mark.parametrize("kind", ["voice_utterance_begin", "voice_utterance_finish"])
def test_raw_refusal_is_bound_to_exact_request_without_speech_or_task(managed_client, kind):
    client, _, _, _, requests, stt, speakers = managed_client
    with client.websocket_connect('/v1/session?session_id=voice-A&context_checkpoint_version=1') as ws:
        identity = configure(ws)
        ws.send_json({"type": kind, "payload": {**identity, "context_revision": identity['context_revision'] + 1}})
        reply, observed = until(ws, lambda frame: frame.get('type') == kind + '_ack')
        payload = parse_message(reply)[1].model_dump()
        assert payload['status'] == 'error' and payload['retry_safe'] is False
        assert payload['request_id'] == identity['request_id']
        assert payload['voice_attempt_id'] == identity['voice_attempt_id']
        assert payload['managed_chained_voice_version'] == 1
        assert not {'turn_id', 'context_checkpoint'} & payload.keys()
        assert not requests and all(recognizer.flushes == 0 for recognizer in stt)
        assert all(not speaker.texts for speaker in speakers)
        assert all(frame.get('type') not in {'chat_turn_accepted', 'chat_turn_terminal', 'audio_chunk'} for frame in observed)
