"""Bounded actual 9.30 packaged-backend acceptance, not native GUI acceptance.

Only a fresh named synthetic home, loopback listeners/model, and owned processes.
No accounts, grants, external deliveries, replay, or downloads. Raw transcript
backup is synthetic and stays outside Git. Candidate identity is mandatory.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import time
from uuid import UUID, uuid4

import httpx
import websockets

from native_9_30_launcher import APP, candidate_identity, identity_arguments
from native_9_30_probe import load_checkpoint_codec, checkpoint_summary

ROOT = Path('/private/tmp/feral-native-9-30-headless-20261002')
CORE = APP / 'Contents/Resources/feral-core'
PYTHON = APP / 'Contents/Resources/python/bin/python3'
FACT = 'AMBER-LANTERN-731'
DECOY = 'COBALT-PAPER-946'
MODEL = 'theora-coding-ba1007eb4404ce93:latest'
MAX_LOG = 8 * 1024 * 1024


class ProbeFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ProbeFailure(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_new(name, value):
    path = ROOT / name
    require(path.resolve() == path and not path.exists(), 'Evidence path redirected or already present')
    encoded = json.dumps(value, indent=2, ensure_ascii=False).encode()
    require(len(encoded) <= 1024 * 1024, 'Evidence exceeds 1MiB')
    with path.open('xb') as file:
        file.write(encoded + b'\n')
    path.chmod(0o600)


def checkpoint(sid):
    db = ROOT / 'feral-home/memory.db'
    require(db.resolve() == db and db.is_file() and db.stat().st_size < 128 * 1024 * 1024,
            'Synthetic database absent, redirected or oversized')
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        row = conn.execute('SELECT session_id,generation,revision,attempt_id,state,format_version,'
                           'payload_bytes,updated_at,length(CAST(payload_json AS BLOB)) '
                           'FROM runtime_session_checkpoints WHERE session_id=?', (sid,)).fetchone()
        require(row is not None, 'Exact checkpoint absent')
        require(type(row[8]) is int and row[8] <= 2 * 1024 * 1024, 'Checkpoint exceeds read bound')
        encoded = conn.execute('SELECT payload_json FROM runtime_session_checkpoints WHERE session_id=?',
                               (sid,)).fetchone()[0]
        codec, source_digest = load_checkpoint_codec()
        summary = checkpoint_summary(row, encoded, codec)
        return {'row': row, 'encoded': encoded, 'summary': summary, 'codec_sha256': source_digest}


class Server:
    def __init__(self, token):
        self.token = token
        self.process = None
        self.log = None
        self.port = None
        self.launch_number = 0
        self.receipts = []
        self.monitor = None
        self.log_limit_hit = False

    async def start(self):
        require(self.process is None, 'Duplicate owned server start')
        self.launch_number += 1
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        log_path = ROOT / f'server-{self.launch_number}.log'
        self.log = log_path.open('xb')
        env = {'HOME': str(ROOT / 'user-home'), 'TMPDIR': str(ROOT / 'tmp'),
               'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8',
               'FERAL_HOME': str(ROOT / 'feral-home'), 'FERAL_DATA_HOME': str(ROOT / 'feral-home/data'),
               'FERAL_PORT': str(self.port), 'FERAL_API_KEY': self.token,
               'FERAL_NATIVE_DEFER_VAULT': '1', 'PYTHON_KEYRING_BACKEND': 'keyring.backends.null.Keyring',
               'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(CORE),
               'FERAL_EMBED_MODEL_CACHE_ONLY': '1', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
               'FERAL_OLLAMA_BASE_URL': 'http://127.0.0.1:11436', 'FERAL_LOCAL_BYPASS': '0',
               'FERAL_SYNC_PASSPHRASE': 'synthetic-headless-acceptance-only'}
        # The existing production untrusted listener enforces authentication even
        # on loopback. This does not expose any public/LAN listener.
        self.process = subprocess.Popen([str(PYTHON), '-B', '-m', 'uvicorn',
            'api.server:untrusted_app', '--host', '127.0.0.1', '--port', str(self.port),
            '--log-level', 'info'], cwd=CORE, env=env, stdin=subprocess.DEVNULL,
            stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        self.monitor = asyncio.create_task(self.watch_log(log_path, self.process))
        current = {'pid': self.process.pid, 'port': self.port, 'launch': self.launch_number,
                   'interpreter': str(PYTHON), 'application': 'api.server:untrusted_app'}
        write_new(f'launch-{self.launch_number}.json', current)
        print(json.dumps({'phase': 'owned_server_start', **current}), flush=True)
        deadline = time.monotonic() + 100
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            while time.monotonic() < deadline:
                require(self.process.poll() is None, 'Packaged server exited before readiness')
                require(log_path.stat().st_size <= MAX_LOG, 'Owned server log exceeds 8MiB')
                try:
                    response = await client.get(self.base + '/health')
                    if response.status_code == 200:
                        value = response.json()
                        if value.get('status') == 'ok':
                            print(json.dumps({'phase': 'health_ready', 'launch': self.launch_number}), flush=True)
                            return
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(.25)
        raise ProbeFailure('Packaged server readiness exceeded 100s')

    async def watch_log(self, path, process):
        while process.poll() is None:
            if path.stat().st_size > MAX_LOG:
                self.log_limit_hit = True
                if os.getpgid(process.pid) == process.pid:
                    os.killpg(process.pid, signal.SIGTERM)
                return
            await asyncio.sleep(.25)

    @property
    def base(self):
        return f'http://127.0.0.1:{self.port}'

    async def http(self, path, body=None):
        async with httpx.AsyncClient(trust_env=False, timeout=20,
                headers={'Authorization': 'Bearer ' + self.token}) as client:
            response = await (client.get(self.base + path) if body is None else client.post(self.base + path, json=body))
            require(len(response.content) <= MAX_LOG, 'HTTP readback exceeds bound')
            require(response.status_code == 200, 'HTTP endpoint did not return 200: ' + path)
            value = response.json()
            require(isinstance(value, dict) and not value.get('error'), 'HTTP application error: ' + path)
            return value

    async def stop(self):
        if self.process is None:
            return
        process = self.process
        forced = False
        if process.poll() is None:
            require(os.getpgid(process.pid) == process.pid, 'Owned process group identity changed')
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(asyncio.to_thread(process.wait), 30)
            except asyncio.TimeoutError:
                forced = True
                os.killpg(process.pid, signal.SIGKILL)
                await asyncio.wait_for(asyncio.to_thread(process.wait), 10)
        try:
            os.kill(process.pid, 0)
        except ProcessLookupError:
            absent = True
        else:
            absent = False
        with socket.socket() as sock:
            sock.settimeout(1)
            listener_absent = sock.connect_ex(('127.0.0.1', self.port)) != 0
        require(absent and listener_absent, 'Owned server or listener remained after shutdown')
        if self.monitor is not None:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
        receipt = {'log_limit_hit': self.log_limit_hit, 'pid': process.pid, 'launch': self.launch_number, 'exit_code': process.returncode,
                   'forced_kill': forced, 'pid_absent': absent, 'listener_absent': listener_absent}
        write_new(f'exit-{self.launch_number}.json', receipt)
        self.receipts.append(receipt)
        self.log.close()
        self.process = None
        print(json.dumps({'phase': 'owned_server_stopped', **receipt}), flush=True)

    async def socket(self, sid):
        ws = await websockets.connect(f'ws://127.0.0.1:{self.port}/v1/session?session_id={sid}&context_checkpoint_version=1',
                                      max_size=MAX_LOG, open_timeout=15, close_timeout=5, proxy=None)
        await ws.send(json.dumps({'type': 'auth', 'token': self.token}))
        return ws


async def receive_until(ws, predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.monotonic()))
        require(isinstance(frame, dict), 'Malformed gateway frame')
        if predicate(frame):
            return frame
    raise ProbeFailure('Bounded gateway response unavailable')


async def capability(ws, sid, expect_ready=True):
    request = str(uuid4())
    await ws.send(json.dumps({'type': 'req', 'id': request, 'method': 'chat.capabilities', 'params': {}}))
    frame = await receive_until(ws, lambda f: f.get('type') == 'res' and f.get('id') == request)
    require(frame.get('ok') is True, 'Capability request refused')
    payload = frame.get('payload', {})
    require(payload.get('session_id') == sid and payload.get('context_managed') is True,
            'Capability exact managed SID not confirmed')
    require(payload.get('turn_contract_versions') == [1] and payload.get('durable_receipts') is True
            and payload.get('whole_turn_terminal') is True, 'Whole-turn receipt capability not confirmed')
    require(payload.get('context_checkpoint_versions') == [1], 'Saved context v1 unavailable')
    if expect_ready:
        require(payload.get('context_ready') is True and payload.get('context_state') == 'ready', 'Managed context is not READY')
        cp = payload.get('context_checkpoint', {})
        require(cp.get('session_id') == sid and cp.get('contract_version') == 1 and cp.get('durable') is True
                and type(cp.get('revision')) is int and cp['revision'] > 0
                and str(UUID(cp.get('generation'))) == cp.get('generation'), 'Capability checkpoint fence invalid')
    return payload


async def turn(ws, sid, prompt, label):
    request = str(uuid4())
    await ws.send(json.dumps({'type': 'text_command', 'msg_id': request,
                             'payload': {'text': prompt, 'turn_contract_version': 1}}))
    accepted = await receive_until(ws, lambda f: f.get('type') in {'chat_turn_accepted', 'error'}
            and f.get('payload', {}).get('request_id') == request, 30)
    require(accepted.get('type') == 'chat_turn_accepted', 'Tracked turn refused: ' + label)
    ack = accepted.get('payload', {})
    require(ack.get('session_id') == sid and ack.get('request_id') == request
            and ack.get('durable') is True and ack.get('replayed') is False, 'Turn acceptance owner/durability invalid')
    print(json.dumps({'phase': 'actual_turn_accepted', 'label': label, 'request_id': request}), flush=True)
    terminal = await receive_until(ws, lambda f: f.get('type') == 'chat_turn_terminal'
            and f.get('payload', {}).get('request_id') == request, 180)
    payload = terminal.get('payload', {})
    require(terminal.get('session_id') == sid and payload.get('session_id') == sid
            and payload.get('turn_id') == ack.get('turn_id') and payload.get('request_id') == request
            and payload.get('durable') is True and payload.get('replayed') is False, 'Terminal identity/durability invalid')
    require(payload.get('processing_outcome') == 'completed' and payload.get('approval_request_ids') == [],
            'Actual provider turn did not complete without approvals: ' + label)
    text = payload.get('final_text')
    require(isinstance(text, str) and len(text.encode()) <= 16000, 'Final text malformed or exceeds synthetic bound')
    evidence = {key: payload.get(key) for key in ['contract_version', 'session_id', 'request_id', 'turn_id',
                'processing_outcome', 'action_outcome', 'durable', 'replayed']}
    evidence.update(label=label, synthetic_model_answer=text[:2000], text_bytes=len(text.encode()), text_sha256=digest(text.encode()))
    write_new(f'{label}-receipt.json', evidence)
    print(json.dumps({'phase': 'actual_turn_terminal', **evidence}), flush=True)
    return payload


def decoy_edit(sid, expected):
    before = checkpoint(sid)
    require(before['row'] == expected['row'] and before['encoded'] == expected['encoded'],
            'Checkpoint changed before isolated projection edit')
    db = ROOT / 'feral-home/memory.db'
    with sqlite3.connect(db, timeout=5) as conn:
        conn.execute('BEGIN IMMEDIATE')
        raw = conn.execute('SELECT messages_json FROM conversations WHERE id=?', (sid,)).fetchone()[0]
        require(isinstance(raw, str) and 0 < len(raw.encode()) < 1024 * 1024 and 1 <= raw.count(FACT) <= 8,
                'Synthetic projection lacks exact bounded known fact')
        write_new('original-synthetic-ui-projection.json', {'session_id': sid, 'raw_sha256': digest(raw.encode()),
                                                          'messages_json': raw})
        replacement = raw.replace(FACT, DECOY)
        require(conn.execute('UPDATE conversations SET messages_json=? WHERE id=? AND messages_json=?',
                             (replacement, sid, raw)).rowcount == 1, 'Projection compare-and-set lost exact row')
    after = checkpoint(sid)
    require(after['row'] == before['row'] and after['encoded'] == before['encoded'],
            'UI-only edit changed runtime checkpoint')
    receipt = {'session_id': sid, 'deliberate_synthetic_fixture_edit': True,
               'original_ui_sha256': digest(raw.encode()), 'decoy_ui_sha256': digest(replacement.encode()),
               'replacement_count': raw.count(FACT), 'runtime_checkpoint_unchanged': True,
               'checkpoint_before': before['summary'], 'checkpoint_after': after['summary']}
    write_new('ui-only-decoy-edit.json', receipt)
    return receipt


async def journey(args):
    identity = candidate_identity(args.expected_source, args.expected_sha256)
    require(not ROOT.exists() and ROOT.resolve() == ROOT, 'Headless root already exists or redirects; no reset allowed')
    ROOT.mkdir()
    for folder in ['feral-home', 'user-home', 'tmp', 'project']:
        (ROOT / folder).mkdir()
    settings = {'llm': {'provider': 'ollama', 'model': MODEL, 'base_url': 'http://127.0.0.1:11436/v1',
                       'fallback_providers': [], 'context_window': 32768, 'max_tokens': 512},
                'features': {'multi_agent': False, 'proactive': False, 'self_learning': False, 'vision': False},
                'vision': {'enabled': False}, 'memory': {'sync': {'enabled': False}}}
    (ROOT / 'feral-home/settings.json').write_text(json.dumps(settings))
    token = os.urandom(32).hex()
    server = Server(token)
    sid = 'thread-' + str(uuid4())
    evidence = {'candidate': identity, 'root': str(ROOT), 'session_id': sid,
                'native_gui_tested': False, 'headless_only': True, 'overall': 'running'}
    write_new('candidate-identity.json', evidence)
    try:
        await server.start()
        # Confirm production strict listener refuses missing/bad authentication.
        bad = await websockets.connect(f'ws://127.0.0.1:{server.port}/v1/session?session_id=acceptance-denied',
                                       open_timeout=10, close_timeout=3, proxy=None)
        await bad.send(json.dumps({'type': 'auth', 'token': 'deliberately-invalid-synthetic'}))
        try:
            await asyncio.wait_for(bad.recv(), 8)
        except websockets.exceptions.ConnectionClosed as error:
            require(error.rcvd is not None and error.rcvd.code == 4001, 'Bad authentication did not close with4001')
            evidence['invalid_auth_refused_4001'] = True
        else:
            raise ProbeFailure('Strict listener accepted invalid authentication')
        finally:
            await bad.close()
        ws = await server.socket(sid)
        try:
            caps = await capability(ws, sid)
            evidence['initial_context'] = caps
            created = await server.http('/api/conversations/new', {'id': sid, 'title': 'Synthetic saved context headless', 'create_if_missing': True})
            require(created.get('ok') is True and created.get('created') is True and created.get('id') == sid
                    and created.get('conversation', {}).get('messages') == [], 'Empty create-only UI record not confirmed')
            initial = checkpoint(sid)
            require(initial['summary'].get('state') == 'ready' and initial['summary'].get('payload_history_rows') == 0,
                    'Initial saved context already contains history')
            evidence['initial_checkpoint'] = initial['summary']
            first_prompt = f'This is a harmless synthetic test fact: the acceptance object codeword is {FACT}. Reply only ACK {FACT}. Do not use tools, notes, browsing or integrations.'
            first = await turn(ws, sid, first_prompt, 'first')
            await capability(ws, sid)
            second_prompt = 'Answer only the result of 23 plus 19. This is a synthetic text request; do not use tools.'
            second = await turn(ws, sid, second_prompt, 'second')
            await capability(ws, sid)
            ui = [{'role': 'user', 'content': first_prompt}, {'role': 'assistant', 'content': first['final_text']},
                  {'role': 'user', 'content': second_prompt}, {'role': 'assistant', 'content': second['final_text']}]
            saved = await server.http('/api/conversations/save', {'id': sid, 'title': 'Synthetic saved context headless', 'messages': ui})
            require(saved.get('id') == sid and saved.get('message_count') == 4, 'Synthetic display projection save not confirmed')
            readback = await server.http('/api/conversations/' + sid)
            require(readback.get('id') == sid and readback.get('messages') == ui, 'Exact synthetic projection readback differs')
            prior = checkpoint(sid)
            require(prior['summary'].get('state') == 'ready' and prior['summary'].get('payload_history_rows', 0) >= 4,
                    'Two actual turns not retained in ready checkpoint')
            evidence['before_shutdown_checkpoint'] = prior['summary']
        finally:
            await ws.close()
        await server.stop()
        require(not server.receipts[-1]['forced_kill'], 'First server required force kill')
        evidence['decoy_edit'] = decoy_edit(sid, prior)
        await server.start()
        require(candidate_identity(args.expected_source, args.expected_sha256) == identity, 'Candidate changed at restart')
        ws = await server.socket(sid)
        try:
            caps = await capability(ws, sid)
            restored = checkpoint(sid)
            require(restored['row'] == prior['row'] and restored['encoded'] == prior['encoded'],
                    'Checkpoint changed on attach before new prompt; UI may have influenced storage')
            evidence['after_restart_context'] = caps
            evidence['after_restart_checkpoint'] = restored['summary']
            recalled = await turn(ws, sid, 'What is the acceptance object codeword stated earlier in this chat? Reply only the codeword. Do not use tools, notes, browsing or integrations.', 'recall')
            evidence['actual_recall_original_fact'] = FACT in recalled['final_text'] and DECOY not in recalled['final_text']
            require(evidence['actual_recall_original_fact'], 'Actual model did not recall original runtime fact independently of UI decoy')
            evidence['after_recall_checkpoint'] = checkpoint(sid)['summary']
        finally:
            await ws.close()
        await server.stop()
        require(not server.receipts[-1]['forced_kill'], 'Restarted server required force kill')
        evidence.update(overall='passed_bounded_headless_journey', shutdowns=server.receipts,
                        cancellation_tested=False, native_gui_tested=False,
                        actual_model_answer_is_not_universal_memory_proof=True)
    except BaseException as error:
        evidence.update(overall='failed_or_incomplete', failure_type=type(error).__name__,
                        failure_message=str(error)[:500] if isinstance(error, ProbeFailure) else 'Transport/runtime failure; inspect bounded synthetic logs')
        raise
    finally:
        await server.stop()
        evidence['shutdowns'] = server.receipts
        write_new('final-result.json', evidence)
        print(json.dumps({'phase': 'final_result', 'overall': evidence['overall'],
                          'failure_type': evidence.get('failure_type'), 'evidence': str(ROOT / 'final-result.json')}), flush=True)



async def cancellation_journey(args):
    identity = candidate_identity(args.expected_source, args.expected_sha256)
    require(ROOT.resolve() == ROOT and ROOT.is_dir(), 'Prior exact headless root is required')
    prior_path = ROOT / 'final-result.json'
    require(prior_path.resolve() == prior_path and prior_path.stat().st_size < 1024 * 1024,
            'Prior result is redirected or oversized')
    prior = json.loads(prior_path.read_text())
    require(prior.get('candidate') == identity and prior.get('overall') == 'passed_bounded_headless_journey',
            'Prior bounded journey did not pass this exact candidate')
    require(not (ROOT / 'cancellation-result.json').exists(), 'Cancellation journey already recorded; no replay')
    require(all(item.get('pid_absent') is True and item.get('listener_absent') is True
                for item in prior.get('shutdowns', [])) and len(prior.get('shutdowns', [])) == 2,
            'Prior owned process cleanup not confirmed')
    for item in prior['shutdowns']:
        try:
            os.kill(item['pid'], 0)
        except ProcessLookupError:
            pass
        else:
            raise ProbeFailure('Prior PID is alive/reused; no new launch')
    sid = prior['session_id']
    require(sid == 'thread-' + str(UUID(sid.removeprefix('thread-'))), 'Prior synthetic SID invalid')
    server = Server(os.urandom(32).hex())
    server.launch_number = 2
    evidence = {'candidate': identity, 'session_id': sid, 'headless_only': True,
                'overall': 'running', 'original_journey_preserved': True}
    try:
        await server.start()
        ws = await server.socket(sid)
        try:
            await capability(ws, sid)
            request = str(uuid4())
            await ws.send(json.dumps({'type': 'text_command', 'msg_id': request,
                'payload': {'text': 'Write a long synthetic list of integers from 1 through 1000. Do not use tools or integrations.', 'turn_contract_version': 1}}))
            ack = (await receive_until(ws, lambda f: f.get('type') == 'chat_turn_accepted'
                   and f.get('payload', {}).get('request_id') == request))['payload']
            require(ack.get('session_id') == sid and ack.get('request_id') == request
                    and ack.get('durable') is True and ack.get('replayed') is False,
                    'Pending cancellation acceptance identity invalid')
            deadline = time.monotonic() + 15
            pending = None
            while time.monotonic() < deadline:
                pending = checkpoint(sid)
                if pending['summary'].get('state') == 'in_progress':
                    break
                await asyncio.sleep(.02)
            require(pending is not None and pending['summary'].get('state') == 'in_progress',
                    'No actual in-progress checkpoint observed; cancellation not exercised')
            abort = str(uuid4())
            await ws.send(json.dumps({'type': 'req', 'id': abort, 'method': 'chat.abort',
                                     'params': {'request_id': request, 'turn_id': ack['turn_id']}}))
            # The acknowledgement and terminal can arrive in either order.
            abort_response, terminal = None, None
            deadline = time.monotonic() + 45
            while abort_response is None or terminal is None:
                frame = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.monotonic()))
                if frame.get('type') == 'res' and frame.get('id') == abort:
                    abort_response = frame
                if frame.get('type') == 'chat_turn_terminal' and frame.get('payload', {}).get('request_id') == request:
                    terminal = frame['payload']
            require(abort_response.get('ok') is True and abort_response.get('payload', {}).get('cancel_requested') is True,
                    'Exact pending abort not acknowledged')
            require(terminal.get('session_id') == sid and terminal.get('request_id') == request
                    and terminal.get('turn_id') == ack['turn_id'] and terminal.get('durable') is True
                    and terminal.get('processing_outcome') == 'cancelled' and terminal.get('replayed') is False,
                    'Exact cancelled terminal not confirmed')
            after = checkpoint(sid)
            require(after['summary'].get('state') == 'in_progress', 'Cancelled context falsely became ready')
            caps = await capability(ws, sid, expect_ready=False)
            require(caps.get('context_ready') is False and caps.get('context_state') == 'in_progress'
                    and caps.get('context_checkpoint') is None, 'Cancelled context capability falsely READY')
            status_request = str(uuid4())
            await ws.send(json.dumps({'type': 'req', 'id': status_request, 'method': 'chat.status',
                                     'params': {'request_id': request}}))
            status = await receive_until(ws, lambda f: f.get('type') == 'res' and f.get('id') == status_request)
            receipt = status.get('payload', {}).get('receipt', {})
            require(status.get('ok') is True and status.get('payload', {}).get('found') is True
                    and receipt.get('session_id') == sid and receipt.get('request_id') == request
                    and receipt.get('turn_id') == ack['turn_id'] and receipt.get('processing_outcome') == 'cancelled',
                    'Read-only exact cancellation status not confirmed')
            blocked_request = str(uuid4())
            await ws.send(json.dumps({'type': 'req', 'id': blocked_request, 'method': 'chat.send',
                                     'params': {'text': 'Synthetic blocked new turn, not a replay.', 'turn_contract_version': 1}}))
            refused = await receive_until(ws, lambda f: f.get('type') == 'res' and f.get('id') == blocked_request)
            require(refused.get('ok') is False, 'In-progress managed context accepted a new task')
            with sqlite3.connect((ROOT / 'feral-home/memory.db').as_uri() + '?mode=ro', uri=True) as conn:
                conn.execute('PRAGMA query_only=ON')
                require(conn.execute('SELECT COUNT(*) FROM chat_turn_receipts WHERE session_id=? AND request_id=?',
                                     (sid, blocked_request)).fetchone()[0] == 0, 'Refused task gained an acceptance receipt')
                require(conn.execute('SELECT COUNT(*) FROM chat_turn_receipts WHERE session_id=? AND request_id=?',
                                     (sid, request)).fetchone()[0] == 1, 'Cancellation request duplicated')
            evidence.update(overall='passed_bounded_headless_cancellation', request_id=request,
                turn_id=ack['turn_id'], cancellation_terminal={key:terminal.get(key) for key in
                    ['processing_outcome','action_outcome','durable','replayed']},
                observed_pending_checkpoint=pending['summary'], after_cancellation_checkpoint=after['summary'],
                exact_status_confirmed=True, in_progress_new_task_refused=True,
                refused_request_receipt_absent=True, original_request_count=1,
                original_request_replayed=False)
        finally:
            await ws.close()
        await server.stop()
        require(not server.receipts[-1]['forced_kill'], 'Cancellation server required force kill')
    except BaseException as error:
        evidence.update(overall='failed_or_incomplete', failure_type=type(error).__name__,
            failure_message=str(error)[:500] if isinstance(error, ProbeFailure) else 'Transport/runtime failure; inspect synthetic logs')
        raise
    finally:
        await server.stop()
        evidence['shutdowns'] = server.receipts
        write_new('cancellation-result.json', evidence)
        print(json.dumps({'phase': 'cancellation_result', 'overall': evidence['overall'],
                          'failure_type': evidence.get('failure_type')}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    identity_arguments(parser)
    parser.add_argument('--cancel-only', action='store_true', help='Separate one-shot bounded cancellation after the passed original journey; preserve all evidence')
    args = parser.parse_args()
    try:
        asyncio.run(asyncio.wait_for(cancellation_journey(args) if args.cancel_only else journey(args), 900))
    except (Exception, KeyboardInterrupt):
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
