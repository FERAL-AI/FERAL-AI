"""Bounded REST fixtures/readback for the disposable 9.28 native acceptance."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.request import Request, urlopen

ROOT = Path("/private/tmp/feral-native-populated-20261002")
EXPECTED_SHA = "dba3785d53634fb699986c2ade9290f42ff5c0631c63224510bfb34350d165f0"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["inspect", "seed-formatting", "propose-deny", "propose-allow", "readback"])
    parser.add_argument("--thread", help="Exact synthetic acceptance thread to inspect")
    args = parser.parse_args()
    if args.thread and not re.fullmatch(r"(?:thread-[a-f0-9-]+|acceptance-[a-z0-9-]+)", args.thread):
        parser.error("Only exact synthetic acceptance thread IDs are supported")
    launch = json.loads((ROOT / "launch.json").read_text())
    runtime = json.loads((ROOT / "feral-home/runtime.json").read_text())
    candidate = Path(launch["candidate"])
    if launch["root"] != str(ROOT) or runtime["port"] != launch["port"]:
        parser.error("Disposable ownership/port receipt mismatch")
    if hashlib.sha256((candidate / "Contents/MacOS/feral-native").read_bytes()).hexdigest() != EXPECTED_SHA:
        parser.error("The audited immutable 9.28 executable changed")
    expected = str(candidate / "Contents/Resources/python/bin/python3") + " " + str(candidate / "Contents/Resources/native_backend_launcher.py")
    owner = subprocess.run(["/bin/ps", "-p", str(int(runtime["pid"])), "-o", "ppid=,command="], check=True, capture_output=True, text=True).stdout.strip().split(None, 1)
    if len(owner) != 2 or int(owner[0]) != launch["pid"] or owner[1] != expected:
        parser.error("The backend is not the exact owned native child")
    base = "http://127.0.0.1:" + str(int(runtime["port"]))

    def request(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        with urlopen(Request(base + path, data=data, headers={"Content-Type": "application/json"}), timeout=30) as response:
            return json.load(response)

    if args.mode == "seed-formatting":
        fixture = {"id": "acceptance-9-28-formatting", "title": "9.28 formatting fixture", "messages": [
            {"role": "user", "content": "Synthetic display fixture, not a generated model response."},
            {"role": "assistant", "content": "**Bold**, *italic*, `inline code`, [Public test link](https://example.com).\n```swift\nlet acceptance928 = true\n```"}]}
        print(json.dumps(request("/api/conversations/save", fixture)))
    elif args.mode.startswith("propose-"):
        kind = args.mode.removeprefix("propose-")
        path = ROOT / "project" / ("9-28-" + kind + "-only.txt")
        if path.exists():
            parser.error("Unique synthetic effect file already exists; refuse overwrite")
        body = {"tool_name": "coding_tools__write_file", "args": {"path": str(path), "content": "FERAL_9_28_" + kind.upper() + "_ONLY"}, "session_id": "acceptance-9-28-" + kind, "confirm": True}
        result = request("/api/tools/execute", body)
        (ROOT / ("9-28-" + kind + "-proposal.json")).write_text(json.dumps(result, indent=2))
        print(json.dumps({"proposal": result, "file_exists": path.exists()}))
    elif args.mode == "inspect":
        page = request("/api/conversations?limit=25")
        rich = request("/api/conversations/acceptance-rich")
        messages = rich["messages"]
        config = request("/api/llm/config")
        result = {"host_pid": launch["pid"], "backend_pid": runtime["pid"], "port": runtime["port"], "conversation_total": page["total"], "first_page": len(page["conversations"]), "rich_title": rich.get("title"), "rich_pinned": rich.get("pinned"), "rich_records": len(messages), "rich_initial_records": messages[:3], "llm": {k: config.get(k) for k in ["provider", "model", "base_url", "context_window"]}, "health": request("/health")}
        (ROOT / "9-28-initial-readback.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
    else:
        result = {"approvals": request("/api/approvals?limit=500"), "supervisor": request("/api/supervisor/stats"), "files": {kind: {"exists": (ROOT / "project" / ("9-28-" + kind + "-only.txt")).exists(), "content": (ROOT / "project" / ("9-28-" + kind + "-only.txt")).read_text() if (ROOT / "project" / ("9-28-" + kind + "-only.txt")).exists() else None} for kind in ["deny", "allow"]}}
        short = request("/api/conversations/acceptance-history-54")
        (ROOT / "9-28-short-chat-readback.json").write_text(json.dumps(short, indent=2))
        result["short_records"] = len(short["messages"])
        result["recent_short_messages"] = [{"role": row.get("role"), "content": str(row.get("content", ""))[:500]} for row in short["messages"][-4:]]
        formatting = request("/api/conversations/acceptance-9-28-formatting")
        (ROOT / "9-28-formatting-chat-readback.json").write_text(json.dumps(formatting, indent=2))
        result["formatting_records"] = len(formatting["messages"])
        result["recent_formatting_messages"] = [{"role": row.get("role"), "content": str(row.get("content", ""))[:500]} for row in formatting["messages"][-4:]]
        rich = request("/api/conversations/acceptance-rich")
        initial = json.loads((ROOT / "9-28-initial-readback.json").read_text())
        result["rich_retention"] = {"title": rich.get("title"), "pinned": rich.get("pinned"), "records": len(rich["messages"]), "original_three_unchanged": rich["messages"][:3] == initial["rich_initial_records"]}
        result["conversation_total"] = request("/api/conversations?limit=25")["total"]
        result["backend_pid"] = runtime["pid"]
        if args.thread:
            selected = request("/api/conversations/" + args.thread)
            (ROOT / "9-28-selected-chat-readback.json").write_text(json.dumps(selected, indent=2))
            result["selected_thread"] = {"id": selected["id"], "records": len(selected["messages"]), "recent_messages": [{"role": row.get("role"), "content": str(row.get("content", ""))[:500]} for row in selected["messages"][-8:]]}
        (ROOT / "9-28-review-readback.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result))


if __name__ == "__main__":
    main()
