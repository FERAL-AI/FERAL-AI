"""Read-only synthetic conversation/receipt observer for immutable 9.29.

GET and SQLite mode=ro only. No WebSocket, prompt, approval, abort or tool call.
By default text is represented only by UTF-8 byte count and SHA256.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess

from native_9_29_launcher import APP, ROOT, candidate_identity, identity_arguments

RECEIPT_KEYS = ("contract_version", "session_id", "request_id", "turn_id", "status",
                "state", "durable", "replayed", "processing_outcome", "action_outcome",
                "approval_request_ids")
FIXTURE_THREADS = ("acceptance-rich", "acceptance-history-54", "acceptance-9-28-formatting")


def validate_thread(value):
    if not re.fullmatch(r"(?:thread-[a-f0-9-]{1,100}|acceptance-[a-z0-9-]{1,100})", value or ""):
        raise ValueError("An exact synthetic acceptance thread ID is required")
    return value


def text_summary(value, include_text=False):
    if not isinstance(value, str):
        raise ValueError("Malformed synthetic transcript text")
    encoded = value.encode("utf-8")
    result = {"utf8_bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
    if include_text:
        result["synthetic_text"] = value[:500]
        result["truncated"] = len(value) > 500
    return result


def record_summary(record):
    if not isinstance(record, dict):
        raise ValueError("Malformed chat-turn record")
    return {key: record[key] for key in RECEIPT_KEYS if key in record}


def conversation_summary(row, include_text=False):
    messages = row["messages"]
    if not isinstance(messages, list) or len(messages) > 5000:
        raise ValueError("Synthetic transcript exceeds the native 5000-row bound")
    recent = []
    for message in messages[-10:]:
        marker = message.get("chat_turn") or message.get("metadata", {}).get("chat_turn")
        summary = {"role": message.get("role"),
                   "text": text_summary(message.get("content", message.get("text", "")), include_text)}
        if marker is not None:
            summary["chat_turn"] = record_summary(marker)
        recent.append(summary)
    return {"id": row["id"], "title_sha256": text_summary(row.get("title", ""))["sha256"],
            "pinned": bool(row.get("pinned")), "records": len(messages),
            "messages_sha256": hashlib.sha256(json.dumps(messages, sort_keys=True,
                ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(), "recent": recent}


def database_readback(thread=None, include_text=False):
    database = ROOT / "feral-home/memory.db"
    if database.resolve() != database or not database.is_file():
        raise ValueError("The existing exact disposable database must not be redirected")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
        connection.execute("PRAGMA query_only=ON")
        conversations = []
        for identifier in (*FIXTURE_THREADS, *((thread,) if thread else ())):
            row = connection.execute("SELECT id,title,pinned,messages_json FROM conversations WHERE id=?",
                                     (identifier,)).fetchone()
            if row is None:
                conversations.append({"id": identifier, "missing": True})
                continue
            conversations.append(conversation_summary({"id": row[0], "title": row[1],
                "pinned": row[2], "messages": json.loads(row[3])}, include_text and identifier == thread))
        receipts = []
        if thread:
            for row in connection.execute("SELECT turn_id,request_id,status,receipt_json,updated_at FROM "
                    "chat_turn_receipts WHERE session_id=? ORDER BY updated_at DESC LIMIT 20", (thread,)):
                receipt = json.loads(row[3])
                matching = (receipt.get("session_id"), receipt.get("turn_id"), receipt.get("request_id")) == (thread, row[0], row[1])
                receipts.append({"identity_matches_storage": matching, "stored_status": row[2],
                    "updated_at": row[4], "record": record_summary(receipt),
                    "output": text_summary(receipt.get("final_text", ""), include_text)})
        return {"conversations": conversations, "selected_receipts": receipts,
                "read_only": True, "receipt_query_limit": 20}


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
    for path in [ROOT / "9-29-launch-identity.json", ROOT / "launch.json", ROOT / "feral-home/runtime.json"]:
        if path.resolve() != path:
            raise ValueError("Disposable ownership receipt must not be redirected")
    saved = json.loads((ROOT / "9-29-launch-identity.json").read_text())
    if any(saved.get(key) != value for key, value in identity.items()):
        raise ValueError("Launch identity does not match the expected candidate")
    launch = json.loads((ROOT / "launch.json").read_text())
    runtime = json.loads((ROOT / "feral-home/runtime.json").read_text())
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
        observed = json.loads(exit_path.read_text())
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
    return {"health": {key: health.get(key) for key in ["status", "version"]},
            "llm": {key: config.get(key) for key in ["provider", "model", "base_url", "context_window"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["live", "offline"])
    identity_arguments(parser)
    parser.add_argument("--thread", type=validate_thread)
    parser.add_argument("--include-synthetic-text", action="store_true", help="Only the explicitly selected disposable thread; max500 chars per row")
    parser.add_argument("--output", type=Path, help="Optional NEW 9-29-*.json evidence file in the disposable root")
    args = parser.parse_args()
    try:
        if ROOT.resolve() != ROOT:
            raise ValueError("Redirected disposable root")
        result = launch_readback(args.expected_source, args.expected_sha256)
        if args.mode == "live":
            if not result["host_alive"] or not result["backend_alive"]:
                raise ValueError("Live readback requires the exact live owned host and backend")
            result["live"] = live_readback(result["port"])
        result["database"] = database_readback(args.thread, args.include_synthetic_text)
        encoded = json.dumps(result, indent=2)
        if args.output:
            path = args.output.absolute()
            if path.parent != ROOT or path.resolve() != path or not re.fullmatch(r"9-29-[a-z0-9-]+\.json", path.name):
                raise ValueError("Evidence output must be a new exact 9-29-*.json file in the disposable root")
            with path.open("x") as file:
                file.write(encoded + "\n")
        print(encoded)
    except (ValueError, OSError, KeyError, sqlite3.Error, json.JSONDecodeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
