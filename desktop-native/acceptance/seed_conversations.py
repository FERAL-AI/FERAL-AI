"""Seed synthetic histories through an explicitly launched disposable backend."""
import argparse
import json
from pathlib import Path
import subprocess
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--check-target-only", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if not str(root).startswith("/private/tmp/feral-native-populated-"):
        parser.error("Only named disposable populated-profile acceptance roots are supported")
    launch = json.loads((root / "launch.json").read_text())
    if launch["root"] != str(root):
        parser.error("Launch receipt does not match this disposable root")
    runtime = json.loads((root / "feral-home/runtime.json").read_text())
    if runtime["port"] != launch["port"]:
        parser.error("The disposable runtime endpoint differs from the launch receipt")
    resources = Path(launch["candidate"]) / "Contents/Resources"
    expected = str(resources / "python/bin/python3") + " " + str(resources / "native_backend_launcher.py")
    owner = subprocess.run(["/bin/ps", "-p", str(int(runtime["pid"])), "-o", "ppid=,command="],
                           check=True, capture_output=True, text=True, timeout=5).stdout.strip().split(None, 1)
    if len(owner) != 2 or int(owner[0]) != launch["pid"] or owner[1] != expected:
        parser.error("The recorded backend is not the exact child of this disposable host")
    if args.check_target_only:
        print(json.dumps({"owned_target_verified": True, "port": runtime["port"]}))
        return
    base = "http://127.0.0.1:" + str(int(launch["port"]))

    def request(path, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        with urlopen(Request(base + path, data=payload,
                            headers={"Content-Type": "application/json"}), timeout=15) as response:
            value = json.load(response)
        if isinstance(value, dict) and value.get("error"):
            raise RuntimeError(value["error"])
        return value

    health = request("/health")
    if health.get("agent_ready") is not True:
        raise RuntimeError("Disposable backend is not agent-ready")
    rich = [
        {"role": "user", "content": "Synthetic populated-profile question", "id": "seed-user",
         "attachments": [{"id": "seed-attachment", "name": "synthetic-attachment.txt",
                          "content_type": "text/plain", "source": "synthetic-only"}]},
        {"role": "assistant", "content": "Synthetic rich response with **bold** text.",
         "id": "seed-assistant", "reasoning": "Synthetic retained reasoning metadata",
         "usage": {"input_tokens": 12, "output_tokens": 8},
         "custom_acceptance_marker": {"preserve": "FERAL_RICH_20261002"}},
        {"role": "tool", "content": {"status": "success", "synthetic": True},
         "id": "seed-tool", "tool_call_id": "seed-call", "name": "synthetic_read"},
    ]
    request("/api/conversations/save", {"id": "acceptance-rich", "title": "Acceptance rich history", "messages": rich})
    for number in range(55):
        request("/api/conversations/save", {"id": f"acceptance-history-{number:02d}",
            "title": f"Acceptance history {number:02d}",
            "messages": [{"role": "user", "content": f"Synthetic page item {number:02d}"},
                         {"role": "assistant", "content": f"Synthetic historical response {number:02d}"}]})
    readback = request("/api/conversations/acceptance-rich")
    if readback.get("messages") != rich:
        raise RuntimeError("Seed rich records did not survive actual backend readback")
    page = request("/api/conversations?limit=25")
    result = {"synthetic_threads_added": 56, "actual_total": page["total"],
        "first_page": len(page["conversations"]), "has_more": page["has_more"],
        "rich_readback_matches": True}
    (root / "seed.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
