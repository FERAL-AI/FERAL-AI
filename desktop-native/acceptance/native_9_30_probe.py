"""Read-only synthetic conversation/receipt observer for immutable 9.30.

GET and SQLite mode=ro only. No WebSocket, prompt, approval, abort or tool call.
Private payloads are represented only by byte counts, hashes and validated fences.
No raw-text opt-in exists; saved UI rows are distinct from runtime checkpoints.
"""

import argparse
import hashlib
import json
import importlib.util
import math
import sys
from pathlib import Path
import re
import sqlite3
import subprocess

from native_9_30_launcher import APP, ROOT, candidate_identity, identity_arguments, read_json

RECEIPT_KEYS = ("contract_version", "session_id", "request_id", "turn_id", "status",
                "state", "durable", "replayed", "processing_outcome", "action_outcome",
                )
FIXTURE_THREADS = ("acceptance-rich", "acceptance-history-54", "acceptance-9-28-formatting")


def validate_thread(value):
    if not re.fullmatch(r"(?:thread-[a-f0-9-]{1,100}|acceptance-[a-z0-9-]{1,100}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})", value or ""):
        raise ValueError("An exact synthetic acceptance thread ID is required")
    return value


def text_summary(value):
    if not isinstance(value, str):
        raise ValueError("Malformed synthetic transcript text")
    encoded = value.encode("utf-8")
    result = {"utf8_bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    return result


def record_summary(record):
    if not isinstance(record, dict):
        raise ValueError("Malformed chat-turn record")
    result = {}
    safe_states = {"accepted", "running", "completed", "awaiting_approval", "failed", "cancelled",
                   "outcome_unknown", "unavailable", "refused", "budget_exceeded", "terminal",
                   "ready", "in_progress", "deleted", "unknown", "not_asserted"}
    for key in RECEIPT_KEYS:
        value = record.get(key)
        if key in {"session_id", "request_id", "turn_id"}:
            if value is not None:
                result[key + "_sha256"] = text_summary(value)["sha256"]
        elif key in {"status", "state", "processing_outcome", "action_outcome"}:
            if value is not None:
                result[key] = value if isinstance(value, str) and value in safe_states else "invalid"
        elif key == "contract_version":
            if type(value) is int:
                result[key] = value
        elif type(value) is bool:
            result[key] = value
    approval_ids = record.get("approval_request_ids")
    if approval_ids is not None:
        if not isinstance(approval_ids, list) or len(approval_ids) > 1000:
            raise ValueError("Malformed approval ID metadata")
        result["approval_request_count"] = len(approval_ids)
    return result


def conversation_summary(row):
    messages = row["messages"]
    if not isinstance(messages, list) or len(messages) > 5000:
        raise ValueError("Synthetic transcript exceeds the native 5000-row bound")
    recent = []
    for message in messages[-10:]:
        if not isinstance(message, dict) or not isinstance(message.get("metadata", {}), dict):
            raise ValueError("Malformed synthetic message metadata")
        marker = message.get("chat_turn") or message.get("metadata", {}).get("chat_turn")
        summary = {"role": message.get("role") if message.get("role") in {"user", "assistant", "tool", "system"} else "invalid",
                   "text": text_summary(message.get("content", message.get("text", "")))}
        if marker is not None:
            summary["chat_turn"] = record_summary(marker)
        recent.append(summary)
    return {"id": row["id"], "title_sha256": text_summary(row.get("title", ""))["sha256"],
            "pinned": bool(row.get("pinned")), "records": len(messages),
            "messages_sha256": hashlib.sha256(json.dumps(messages, sort_keys=True,
                ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(), "recent": recent}


def database_readback(thread=None):
    if thread is not None:
        validate_thread(thread)
    database = ROOT / "feral-home/memory.db"
    if database.resolve() != database or not database.is_file():
        raise ValueError("The existing exact disposable database must not be redirected")
    if database.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("Disposable database exceeds the 128MiB read bound")
    for suffix in ["-wal", "-shm"]:
        sidecar = database.with_name(database.name + suffix)
        if sidecar.resolve() != sidecar or sidecar.is_symlink():
            raise ValueError("Disposable database sidecars must not be redirected")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        conversations = []
        for identifier in (*FIXTURE_THREADS, *((thread,) if thread else ())):
            row = connection.execute("SELECT id,title,pinned,CASE WHEN length(CAST(messages_json AS BLOB)) <= 8388608 THEN messages_json ELSE NULL END FROM conversations WHERE id=?",
                                     (identifier,)).fetchone()
            if row is None:
                conversations.append({"id": identifier, "missing": True})
                continue
            if row[3] is None:
                raise ValueError("Synthetic conversation exceeds the 8MiB payload bound")
            conversations.append(conversation_summary({"id": row[0], "title": row[1],
                "pinned": row[2], "messages": json.loads(row[3])}))
        receipts = []
        if thread:
            for row in connection.execute("SELECT turn_id,request_id,status,CASE WHEN length(CAST(receipt_json AS BLOB)) <= 8388608 THEN receipt_json ELSE NULL END,updated_at FROM "
                    "chat_turn_receipts WHERE session_id=? ORDER BY updated_at DESC LIMIT 20", (thread,)):
                if row[3] is None:
                    raise ValueError("Synthetic receipt exceeds the 8MiB payload bound")
                receipt = json.loads(row[3])
                if not isinstance(receipt, dict):
                    raise ValueError("Malformed synthetic receipt")
                matching = (receipt.get("session_id"), receipt.get("turn_id"), receipt.get("request_id")) == (thread, row[0], row[1])
                receipts.append({"identity_matches_storage": matching, "stored_status": row[2] if row[2] in {"accepted", "running", "terminal"} else "invalid",
                    "updated_at": row[4] if type(row[4]) in {int, float} and math.isfinite(row[4]) and row[4] >= 0 else None, "record": record_summary(receipt),
                    "output": text_summary(receipt.get("final_text", ""))})
        return {"conversations": conversations, "selected_receipts": receipts,
                "session_checkpoints": checkpoint_readback(connection, thread),
                "read_only": True, "receipt_query_limit": 20}


def load_checkpoint_codec():
    """Load only the immutable candidate's standalone, stdlib-only codec.

    Avoid memory/__init__.py, store initialization and pyc writes into the app.
    This is known runtime source, not generated/user-provided tool execution.
    """
    path = APP / "Contents/Resources/feral-core/memory/runtime_session_checkpoint.py"
    if path.resolve() != path or not path.is_file() or path.is_symlink() or path.stat().st_size > 256 * 1024:
        raise ValueError("Candidate checkpoint codec missing, redirected or oversized")
    source = path.read_bytes()
    name = "_feral_acceptance_checkpoint_codec"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None:
        raise ValueError("Candidate checkpoint codec cannot be inspected")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module, hashlib.sha256(source).hexdigest()


def checkpoint_summary(row, encoded, codec):
    """Validate actual runtime fence/codec and emit metadata only."""
    sid, generation, revision, attempt, state, version, stored_size, timestamp, actual_size = row
    result = {"session_id": sid, "state": state if state in {"ready", "in_progress", "deleted"} else "invalid",
              "format_version": version if type(version) is int else None,
              "payload_bytes": stored_size if type(stored_size) is int else None,
              "actual_payload_bytes": actual_size,
              "updated_at": timestamp if type(timestamp) in {int, float} and math.isfinite(timestamp) and timestamp >= 0 else None,
              "context_restorable": False}
    try:
        fence = codec.CheckpointFence(sid, generation, revision, attempt)
        result.update(generation=fence.generation, revision=fence.revision, attempt_id=fence.attempt_id,
                      fence_valid=True)
    except codec.CheckpointValidationError:
        result["fence_valid"] = False
    if type(version) is int and version > codec.FORMAT_VERSION:
        result["codec_status"] = "unsupported"
        return result
    if (type(version) is not int or version != codec.FORMAT_VERSION or not result["fence_valid"]
            or state not in {"ready", "in_progress", "deleted"} or result["updated_at"] is None
            or type(stored_size) is not int or stored_size != actual_size or encoded is None):
        result["codec_status"] = "corrupt"
        return result
    try:
        if not isinstance(encoded, str) or len(encoded.encode("utf-8")) != stored_size:
            raise codec.CheckpointValidationError("Invalid checkpoint bytes")
        result["payload_sha256"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        context = codec.decode_context(encoded) if encoded else None
        if (state == "ready" and context is None) or (state == "deleted" and context is not None):
            raise codec.CheckpointValidationError("Invalid checkpoint payload state")
        result["codec_status"] = state
        if context is not None:
            history, working = context.history(), context.working()
            result.update(payload_history_rows=len(history), payload_working_rows=len(working),
                          omissions=context.omissions(),
                          payload_tool_call_count=sum(len(item.get("tool_calls", [])) for item in history),
                          payload_tool_result_count=sum(item.get("role") == "tool" for item in history))
        else:
            result.update(payload_history_rows=0, payload_working_rows=0,
                          payload_tool_call_count=0, payload_tool_result_count=0)
        result["context_restorable"] = state == "ready" and context is not None
    except codec.CheckpointFutureFormat:
        result["codec_status"] = "unsupported"
    except (codec.CheckpointValidationError, UnicodeError, TypeError, ValueError):
        result["codec_status"] = "corrupt"
    return result


def checkpoint_readback(connection, thread=None):
    if thread is not None:
        validate_thread(thread)
    codec, digest = load_checkpoint_codec()
    required = {"session_id", "generation", "revision", "attempt_id", "state", "format_version",
                "payload_json", "payload_bytes", "updated_at"}
    columns = {row[1] for row in connection.execute("PRAGMA table_info(runtime_session_checkpoints)")}
    if not required.issubset(columns):
        raise ValueError("Runtime checkpoint table/schema is absent or incompatible")
    result = []
    for sid in dict.fromkeys((*FIXTURE_THREADS, *((thread,) if thread else ()))):
        # Bound before fetching a possibly malformed large private payload.
        row = connection.execute("SELECT session_id,generation,revision,attempt_id,state,format_version,"
            "payload_bytes,updated_at,length(CAST(payload_json AS BLOB)) FROM runtime_session_checkpoints "
            "WHERE session_id=?", (sid,)).fetchone()
        if row is None:
            result.append({"session_id": sid, "codec_status": "absent", "context_restorable": False})
            continue
        encoded = None
        if type(row[8]) is int and 0 <= row[8] <= codec.CheckpointLimits().record_bytes:
            value = connection.execute("SELECT payload_json FROM runtime_session_checkpoints WHERE session_id=?", (sid,)).fetchone()
            encoded = value[0] if value else None
        result.append(checkpoint_summary(row, encoded, codec))
    return {"records": result, "selected_query_limit": len(result), "payload_read_limit": codec.CheckpointLimits().record_bytes,
            "codec_source_sha256": digest, "history_is_inert_data": True,
            "restoration_not_proven_by_readback": True}


def process_info(pid):
    if type(pid) is not int or pid < 1:
        raise ValueError("Invalid recorded process PID")
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "ppid=,command="],
                            capture_output=True, text=True, check=False)
    if result.returncode == 1 and not result.stdout.strip():
        return None
    result.check_returncode()
    fields = result.stdout.strip().split(None, 1)
    if len(fields) != 2:
        raise ValueError("Process ownership response is malformed")
    return int(fields[0]), fields[1]


def launch_readback(expected_source, expected_sha):
    identity = candidate_identity(expected_source, expected_sha)
    for path in [ROOT / "9-30-launch-identity.json", ROOT / "launch.json", ROOT / "feral-home/runtime.json", ROOT / "native-exit.json"]:
        if path.resolve() != path:
            raise ValueError("Disposable ownership receipt must not be redirected")
    saved = read_json(ROOT / "9-30-launch-identity.json", 65536)
    if (any(saved.get(key) != value for key, value in identity.items())
            or saved.get("root") != str(ROOT) or saved.get("candidate") != str(APP)):
        raise ValueError("Launch identity does not match the expected candidate")
    launch = read_json(ROOT / "launch.json", 65536)
    runtime = read_json(ROOT / "feral-home/runtime.json", 65536)
    suite = "ai.feral.native.acceptance." + hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    if (launch.get("root"), launch.get("candidate"), launch.get("executable_sha256"),
            launch.get("preferences_suite")) != (str(ROOT), str(APP), expected_sha, suite):
        raise ValueError("Disposable launch owner/candidate/prefs mismatch")
    port = launch.get("port")
    if type(port) is not int or not 1024 <= port <= 65535 or runtime.get("port") != port:
        raise ValueError("Disposable runtime port mismatch")
    host = process_info(launch["pid"])
    child = process_info(runtime["pid"])
    if host is not None and host[1] != str(APP / "Contents/MacOS/feral-native"):
        raise ValueError("Host PID belongs to a different process")
    expected_child = str(APP / "Contents/Resources/python/bin/python3") + " " + str(APP / "Contents/Resources/native_backend_launcher.py")
    if child is not None and child != (launch["pid"], expected_child):
        raise ValueError("Backend is not the exact recorded native child")
    result = {"candidate_identity": identity, "host_pid": launch["pid"], "backend_pid": runtime["pid"],
              "port": port, "host_alive": host is not None, "backend_alive": child is not None}
    exit_path = ROOT / "native-exit.json"
    if exit_path.exists():
        observed = read_json(exit_path, 65536)
        if observed.get("pid") == launch["pid"]:
            result["observed_host_exit_code"] = observed.get("exit_code")
    return result


def live_readback(port):
    def get(path):
        # Disable proxies: even local readback must remain on verified loopback.
        from urllib.request import ProxyHandler, build_opener
        with build_opener(ProxyHandler({})).open("http://127.0.0.1:" + str(port) + path, timeout=15) as response:
            body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise ValueError("Readback response exceeds the bounded 8MiB limit")
            return json.loads(body)
    config = get("/api/llm/config")
    health = get("/health")
    if not isinstance(config, dict) or not isinstance(health, dict):
        raise ValueError("Malformed live readback")
    if config.get("provider") != "ollama" or config.get("base_url") != "http://127.0.0.1:11436/v1":
        raise ValueError("Live model must remain the isolated loopback provider")
    if config.get("fallback_providers"):
        raise ValueError("Live profile has fallback providers")
    context = config.get("context_window")
    return {"health": {"status_sha256": text_summary(health.get("status", ""))["sha256"],
                       "version_sha256": text_summary(health.get("version", ""))["sha256"]},
            "llm": {"provider": "ollama", "base_url": "http://127.0.0.1:11436/v1",
                    "model_sha256": text_summary(config.get("model", ""))["sha256"],
                    "context_window": context if type(context) is int and context > 0 else None}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["live", "offline"])
    identity_arguments(parser)
    parser.add_argument("--thread", type=validate_thread)
    parser.add_argument("--output", type=Path, help="Optional NEW 9-30-*.json evidence file in the disposable root")
    args = parser.parse_args()
    try:
        if ROOT.resolve() != ROOT or not ROOT.is_dir():
            raise ValueError("Redirected disposable root")
        result = launch_readback(args.expected_source, args.expected_sha256)
        if args.mode == "live":
            if not result["host_alive"] or not result["backend_alive"]:
                raise ValueError("Live readback requires the exact live owned host and backend")
            result["live"] = live_readback(result["port"])
        result["database"] = database_readback(args.thread)
        encoded = json.dumps(result, indent=2)
        if args.output:
            path = args.output.absolute()
            if path.parent != ROOT or path.resolve() != path or not re.fullmatch(r"9-30-[a-z0-9-]+\.json", path.name):
                raise ValueError("Evidence output must be a new exact 9-30-*.json file in the disposable root")
            with path.open("x") as file:
                file.write(encoded + "\n")
        print(encoded)
    except (ValueError, OSError, KeyError, TypeError, UnicodeError, sqlite3.Error, json.JSONDecodeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
