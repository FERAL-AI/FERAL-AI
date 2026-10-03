"""One-shot 9.31 packaged-backend Stop/recovery/restart acceptance.

Fresh synthetic storage only; no GUI, accounts, model downloads, or task replay.
Requires the coordinator's exact immutable source/executable identity. Existing
9.30 transport/owned-process helpers are reused with an explicit fresh ROOT;
their 9.30 candidate validation and old journeys are never called.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import time
from uuid import UUID, uuid4

import saved_context_headless_probe as helpers
from native_9_30_launcher import identity_arguments, read_json, validate_expectations

ROOT = Path('/private/tmp/feral-native-9-31-recovery-20261002')
APP = Path(__file__).resolve().parents[1] / 'build/FERAL Native Preview.app'
MANIFEST = Path('/private/tmp/feral-candidate-9-31-manifest.json')
FACT = 'JADE-COMPASS-853'
CANCELLED_MARKER = 'CANCELLED-SYNTHETIC-DO-NOT-REPLAY-982'
MODEL = 'theora-coding-ba1007eb4404ce93:latest'
require = helpers.require


def candidate_identity(expected_source, expected_sha256):
    validate_expectations(expected_source, expected_sha256)
    paths = [APP / 'Contents/Info.plist', APP / 'Contents/MacOS/feral-native', MANIFEST]
    for path in paths:
        require(path.resolve() == path and path.is_file(), 'Candidate path absent or redirected')
    interpreter = APP / 'Contents/Resources/python/bin/python3'
    require(interpreter.is_file() and interpreter.resolve().parent == interpreter.parent
            and interpreter.resolve().name in {'python3', 'python3.11'}, 'Bundled interpreter escapes its owned bin directory')
    require(paths[0].stat().st_size <= 1024 * 1024 and paths[1].stat().st_size <= 128 * 1024 * 1024,
            'Candidate metadata/binary exceeds bound')
    with paths[0].open('rb') as file:
        info = plistlib.load(file)
    with paths[1].open('rb') as file:
        binary_sha = hashlib.file_digest(file, 'sha256').hexdigest()
    manifest = read_json(MANIFEST)
    expected = {'version': '2026.9.31', 'build': '2026100205',
                'runtime_source': expected_source, 'native_sha256': expected_sha256}
    require((info.get('CFBundleShortVersionString'), info.get('CFBundleVersion'), info.get('CFBundleIdentifier'))
            == ('2026.9.31', '2026100205', 'ai.feral.native.preview'), 'Wrong candidate version/build/identifier')
    require(binary_sha == expected_sha256 and all(manifest.get(key) == value for key, value in expected.items()),
            'Candidate differs from exact coordinator identity')
    require(type(manifest.get('core_equality_files')) is int and manifest['core_equality_files'] > 0,
            'Production-core comparison receipt absent')
    return {**expected, 'core_equality_files': manifest['core_equality_files']}


def prepare_helpers():
    require(helpers.APP == APP and helpers.CORE == APP / 'Contents/Resources/feral-core',
            'Reusable helper candidate path changed')
    helpers.ROOT = ROOT  # Explicitly redirect only this process's disposable evidence/storage.


def write(name, value):
    helpers.write_new(name, value)


def receipt_summary(payload):
    value = {key: payload.get(key) for key in ('contract_version', 'session_id', 'request_id', 'turn_id',
        'processing_outcome', 'action_outcome', 'durable', 'replayed')}
    text = payload.get('final_text', '')
    require(isinstance(text, str) and len(text.encode()) <= 16000, 'Response text malformed or oversized')
    value.update(text_bytes=len(text.encode()), text_sha256=helpers.digest(text.encode()),
                 contains_committed_fact=FACT in text, contains_cancelled_marker=CANCELLED_MARKER in text,
                 approval_request_count=len(payload.get('approval_request_ids', [])))
    return value


async def receive(ws, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.monotonic()))
        require(isinstance(frame, dict), 'Malformed frame')
        require(frame.get('type') not in {'tool_start', 'tool_result'}, 'Unexpected tool dispatch in text-only journey')
        if predicate(frame):
            return frame
    raise helpers.ProbeFailure('Bounded response unavailable')


async def rpc(ws, method, params):
    request = str(uuid4())
    await ws.send(json.dumps({'type': 'req', 'id': request, 'method': method, 'params': params}))
    return await receive(ws, lambda frame: frame.get('type') == 'res' and frame.get('id') == request)


def check_receipt(payload, sid, request, turn_id=None):
    require(type(payload.get('contract_version')) is int and payload['contract_version'] == 1 and payload.get('session_id') == sid
            and payload.get('request_id') == request and payload.get('durable') is True
            and payload.get('replayed') is False, 'Receipt identity/durability mismatch')
    require(str(UUID(payload.get('turn_id'))) == payload.get('turn_id'), 'Turn UUID invalid')
    if turn_id is not None:
        require(payload['turn_id'] == turn_id, 'Terminal turn identity changed')


async def start_turn(ws, sid, prompt):
    request = str(uuid4())
    await ws.send(json.dumps({'type': 'text_command', 'msg_id': request,
                             'payload': {'text': prompt, 'turn_contract_version': 1}}))
    frame = await receive(ws, lambda item: item.get('type') in {'chat_turn_accepted', 'error'}
                          and item.get('payload', {}).get('request_id') == request)
    require(frame.get('type') == 'chat_turn_accepted', 'Actual tracked turn refused')
    accepted = frame['payload']
    check_receipt(accepted, sid, request)
    return accepted


async def completed_turn(ws, sid, prompt, label):
    accepted = await start_turn(ws, sid, prompt)
    frame = await receive(ws, lambda item: item.get('type') == 'chat_turn_terminal'
        and item.get('payload', {}).get('request_id') == accepted['request_id'], 180)
    payload = frame['payload']
    check_receipt(payload, sid, accepted['request_id'], accepted['turn_id'])
    require(frame.get('session_id') == sid and payload.get('processing_outcome') == 'completed'
            and payload.get('approval_request_ids') == [], 'Actual text turn did not finish without approvals')
    summary = receipt_summary(payload)
    write(label + '-receipt.json', summary)
    print(json.dumps({'phase': label, **summary}), flush=True)
    return payload


def checkpoint():
    snapshot = helpers.checkpoint(SESSION_ID)
    require(snapshot['summary'].get('fence_valid') is True and snapshot['summary'].get('codec_status')
            in {'ready', 'in_progress'}, 'Runtime checkpoint invalid')
    return snapshot


def fence(snapshot):
    row = snapshot['row']
    return {'contract_version': 1, 'session_id': row[0], 'generation': row[1], 'revision': row[2], 'attempt_id': row[3]}


def storage_counts():
    path = ROOT / 'feral-home/memory.db'
    require(path.resolve() == path and path.is_file() and path.stat().st_size < 128 * 1024 * 1024,
            'Synthetic database absent, redirected or oversized')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
        conn.execute('PRAGMA query_only=ON')
        rows = conn.execute('SELECT request_id,status FROM chat_turn_receipts WHERE session_id=? ORDER BY request_id',
                            (SESSION_ID,)).fetchall()
        require(len(rows) <= 10, 'Unexpected number of synthetic turns')
        executions = conn.execute('SELECT COUNT(*) FROM execution_log WHERE session_id=?', (SESSION_ID,)).fetchone()[0]
        return {'receipt_count': len(rows), 'request_ids': [row[0] for row in rows],
                'all_terminal': all(row[1] == 'terminal' for row in rows), 'recorded_tool_executions': executions}


async def selected_model(server):
    config = await server.http('/api/llm/config')
    status = await server.http('/api/llm/status')
    require(config.get('provider') == 'ollama' and config.get('model') == MODEL
        and config.get('base_url') == 'http://127.0.0.1:11436/v1' and config.get('fallback_providers') == []
        and status.get('provider') == 'ollama' and status.get('model') == MODEL
        and status.get('available') is True and status.get('supported') is True,
        'Exact existing loopback model selection/no-fallback status not confirmed')
    return {'provider': 'ollama', 'model': MODEL, 'base_url': config['base_url'],
            'fallback_count': 0, 'available': True, 'supported': True}


async def cancel_active(ws, sid):
    accepted = await start_turn(ws, sid, CANCELLED_MARKER +
        ': Write a long synthetic list of integers from 1 through 1000. Do not use tools or integrations.')
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        pending = checkpoint()
        if pending['summary']['state'] == 'in_progress':
            break
        await asyncio.sleep(.02)
    require(pending['summary']['state'] == 'in_progress', 'No actual IN_PROGRESS writer observed')
    abort_id = str(uuid4())
    await ws.send(json.dumps({'type': 'req', 'id': abort_id, 'method': 'chat.abort',
        'params': {'request_id': accepted['request_id'], 'turn_id': accepted['turn_id']}}))
    abort, terminal = None, None
    deadline = time.monotonic() + 45
    while abort is None or terminal is None:
        frame = await receive(ws, lambda _: True, max(.001, deadline - time.monotonic()))
        require(time.monotonic() <= deadline, 'Stop exceeded45s')
        if frame.get('type') == 'res' and frame.get('id') == abort_id:
            abort = frame
        if frame.get('type') == 'chat_turn_terminal' and frame.get('payload', {}).get('request_id') == accepted['request_id']:
            terminal = frame['payload']
    require(abort.get('ok') is True and abort.get('payload', {}).get('cancel_requested') is True, 'Stop not acknowledged')
    check_receipt(terminal, sid, accepted['request_id'], accepted['turn_id'])
    require(terminal.get('processing_outcome') == 'cancelled', 'Terminal did not confirm cancellation')
    after = checkpoint()
    require(after['summary']['state'] == 'in_progress', 'Stop falsely marked model context READY')
    write('cancelled-receipt.json', receipt_summary(terminal))
    return terminal, after


SESSION_ID = 'thread-' + str(uuid4())


async def journey(args):
    identity = candidate_identity(args.expected_source, args.expected_sha256)
    require(ROOT.resolve() == ROOT and not ROOT.exists(), 'Fresh exact root required; existing evidence cannot be reused/reset')
    prepare_helpers()
    ROOT.mkdir(mode=0o700)
    for name in ('feral-home', 'user-home', 'tmp', 'project'):
        (ROOT / name).mkdir(mode=0o700)
    settings = {'llm': {'provider': 'ollama', 'model': MODEL, 'base_url': 'http://127.0.0.1:11436/v1',
        'fallback_providers': [], 'context_window': 16384, 'max_tokens': 512},
        'features': {'multi_agent': False, 'proactive': False, 'self_learning': False, 'vision': False},
        'vision': {'enabled': False}, 'memory': {'sync': {'enabled': False}}}
    (ROOT / 'feral-home/settings.json').write_text(json.dumps(settings))
    server = helpers.Server(os.urandom(32).hex())
    evidence = {'candidate': identity, 'root': str(ROOT), 'session_id': SESSION_ID,
                'headless_only': True, 'native_gui_tested': False, 'overall': 'running'}
    write('candidate-identity.json', evidence)
    try:
        await server.start()
        evidence['selected_model'] = await selected_model(server)
        ws = await server.socket(SESSION_ID)
        try:
            await helpers.capability(ws, SESSION_ID)
            first_prompt = f'This is a harmless synthetic test: the acceptance codeword is {FACT}. Reply only ACK {FACT}. Do not use tools, notes, browsing or integrations.'
            first = await completed_turn(ws, SESSION_ID, first_prompt, 'memorize')
            require(FACT in first['final_text'], 'Actual model did not acknowledge synthetic fact')
            await helpers.capability(ws, SESSION_ID)
            committed = checkpoint()
            require(committed['summary']['payload_tool_call_count'] == 0, 'Unexpected persisted tool call')
            cancelled, pending = await cancel_active(ws, SESSION_ID)
            caps = await helpers.capability(ws, SESSION_ID, expect_ready=False)
            review = fence(pending)
            require(caps.get('context_ready') is False and caps.get('context_state') == 'in_progress'
                and caps.get('context_recovery_versions') == [1]
                and all(caps.get('context_recovery', {}).get(key) == value for key, value in review.items()),
                'Recovery preview does not match actual pending fence')
            original_ui = [{'role': 'user', 'content': first_prompt}, {'role': 'assistant', 'content': first['final_text']},
                {'role': 'user', 'content': CANCELLED_MARKER + ': interrupted synthetic request'}]
            await server.http('/api/conversations/new', {'id': SESSION_ID, 'title': 'Synthetic recovery', 'create_if_missing': True})
            await server.http('/api/conversations/save', {'id': SESSION_ID, 'title': 'Synthetic recovery', 'messages': original_ui})
            require((await server.http('/api/conversations/' + SESSION_ID)).get('messages') == original_ui, 'UI projection readback failed')
            wrong = await server.socket('thread-' + str(uuid4()))
            try:
                refused = await rpc(wrong, 'session.context.recover', {**review, 'acknowledge_unknown_effects': True})
                require(refused.get('ok') is False and refused.get('error', {}).get('code') == 'context_recovery_session_mismatch',
                        'Foreign selected session recovery not refused')
            finally:
                await wrong.close()
            stale = {**review, 'revision': review['revision'] + 1, 'acknowledge_unknown_effects': True}
            refused = await rpc(ws, 'session.context.recover', stale)
            require(refused.get('ok') is False and refused.get('error', {}).get('code') == 'context_recovery_conflict', 'Stale recovery not refused')
            require(checkpoint()['row'] == pending['row'] and checkpoint()['encoded'] == pending['encoded'], 'Refused recovery changed checkpoint')
            recovered = await rpc(ws, 'session.context.recover', {**review, 'acknowledge_unknown_effects': True})
            payload = recovered.get('payload', {})
            target = payload.get('context_checkpoint', {})
            require(recovered.get('ok') is True and payload.get('status') == 'recovered'
                and payload.get('context_ready') is True and payload.get('durable') is True
                and payload.get('replayed') is False and payload.get('action_outcome') == 'unknown', 'Exact recovery not confirmed')
            require(target.get('session_id') == SESSION_ID and target.get('generation') != review['generation']
                and target.get('revision') == review['revision'] + 1 and target.get('attempt_id') != review['attempt_id'], 'Recovery target did not rotate exact fence')
            before_status = checkpoint()
            require(target.get('generation') == before_status['row'][1] and target.get('revision') == before_status['row'][2]
                and target.get('attempt_id') == before_status['row'][3] and target.get('durable') is True,
                'Recovery response differs from exact durable SQLite fence')
            status = await rpc(ws, 'session.context.recoveryStatus', review)
            require(status.get('ok') is True and status.get('payload', {}).get('status') == 'recovered'
                and status['payload'].get('recovered') is True and status['payload'].get('context_ready') is True
                and status['payload'].get('durable') is True and status['payload'].get('replayed') is False
                and status['payload'].get('action_outcome') == 'unknown'
                and status['payload'].get('context_checkpoint') == target, 'Old-fence read-only recovery status invalid')
            require(checkpoint()['row'] == before_status['row'] and checkpoint()['encoded'] == before_status['encoded'], 'Status mutated checkpoint')
            ready = await helpers.capability(ws, SESSION_ID)
            require(all(ready['context_checkpoint'].get(key) == target.get(key) for key in ('session_id', 'generation', 'revision')), 'Independent READY readback changed recovery fence')
            require((await server.http('/api/conversations/' + SESSION_ID)).get('messages') == original_ui, 'Recovery rewrote original UI transcript')
            require(FACT in before_status['encoded'] and CANCELLED_MARKER not in before_status['encoded'], 'Recovery promoted cancelled task or lost committed fact')
            recalled = await completed_turn(ws, SESSION_ID, 'What is the acceptance codeword stated earlier? Reply only that codeword. Do not use tools, notes, browsing or integrations.', 'after-recovery-recall')
            require(FACT in recalled['final_text'] and CANCELLED_MARKER not in recalled['final_text'], 'Actual model did not recall committed fact after recovery')
            saved = checkpoint()
            require(saved['summary']['generation'] == target['generation'] and CANCELLED_MARKER not in saved['encoded'], 'New turn lost recovered generation or replayed cancelled input')
            evidence.update(cancelled=receipt_summary(cancelled), before_recovery=pending['summary'], recovered=before_status['summary'],
                after_recall=saved['summary'], foreign_owner_refused=True, stale_fence_refused=True, status_read_only=True,
                ui_transcript_unchanged=True, actual_fact_recalled_after_recovery=True)
        finally:
            await ws.close()
        await server.stop()
        require(not server.receipts[-1]['forced_kill'], 'First owned backend required forced kill')
        require(candidate_identity(args.expected_source, args.expected_sha256) == identity, 'Candidate changed before restart')
        await server.start()
        require(await selected_model(server) == evidence['selected_model'], 'Model selection changed at restart')
        ws = await server.socket(SESSION_ID)
        try:
            await helpers.capability(ws, SESSION_ID)
            restored = checkpoint()
            require(restored['row'] == saved['row'] and restored['encoded'] == saved['encoded'], 'Restart altered saved context before new task')
            result = await completed_turn(ws, SESSION_ID, 'Reply only the acceptance codeword from earlier in this exact chat. Do not use tools, notes, browsing or integrations.', 'after-restart-recall')
            require(FACT in result['final_text'] and CANCELLED_MARKER not in result['final_text'], 'Actual restarted model did not recall committed fact')
            counts = storage_counts()
            require(counts['receipt_count'] == 4 and counts['all_terminal'] and counts['recorded_tool_executions'] == 0
                and counts['request_ids'].count(cancelled['request_id']) == 1, 'Unexpected duplicate/nonterminal/tool execution record')
            evidence.update(restarted_context=restored['summary'], final_context=checkpoint()['summary'],
                actual_fact_recalled_after_restart=True, cancelled_request_count=1, cancelled_input_not_restored=True,
                storage_counts=counts, overall='passed_bounded_packaged_backend_recovery')
        finally:
            await ws.close()
        await server.stop()
        require(not server.receipts[-1]['forced_kill'], 'Restarted owned backend required forced kill')
        require(candidate_identity(args.expected_source, args.expected_sha256) == identity, 'Candidate changed during journey')
    except BaseException as error:
        evidence.update(overall='failed_or_incomplete', failure_type=type(error).__name__,
            failure_message=str(error)[:500] if isinstance(error, helpers.ProbeFailure) else 'Bounded transport/runtime failure; inspect disposable logs')
        raise
    finally:
        cleanup_failed = False
        try:
            await server.stop()
        except BaseException as error:
            cleanup_failed = True
            evidence.update(overall='failed_or_incomplete', cleanup_failure_type=type(error).__name__)
        evidence['shutdowns'] = server.receipts
        write('final-result.json', evidence)
        print(json.dumps({'phase': 'final_result', 'overall': evidence['overall'],
                          'evidence': str(ROOT / 'final-result.json')}), flush=True)
        if cleanup_failed:
            raise helpers.ProbeFailure('Owned child cleanup not confirmed; failure evidence preserved') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    identity_arguments(parser)
    args = parser.parse_args()
    try:
        asyncio.run(asyncio.wait_for(journey(args), 900))
    except (Exception, KeyboardInterrupt):
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
