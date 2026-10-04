"""Launch the existing immutable candidate with an explicitly disposable profile.

This is a manual native acceptance aid, not a GUI test or a release certification.
It never discovers personal profiles, credentials, or model directories.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--model", default="qwen3:4b")
    parser.add_argument("--model-port", type=int, default=11436)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--hold", action="store_true", help="Observe the actual host exit code")
    args = parser.parse_args()
    root = args.root.resolve()
    if not str(root).startswith("/private/tmp/feral-native-populated-"):
        parser.error("root must be a named /private/tmp/feral-native-populated-* disposable directory")
    app = args.app.resolve()
    binary = app / "Contents/MacOS/feral-native"
    if not binary.is_file():
        parser.error("app must contain the existing feral-native candidate")
    root.mkdir(exist_ok=True)
    previous_receipt = root / "launch.json"
    if previous_receipt.exists():
        old_pid = int(json.loads(previous_receipt.read_text())["pid"])
        try:
            os.kill(old_pid, 0)
        except ProcessLookupError:
            pass
        else:
            parser.error("the earlier recorded host PID is still alive; do not launch a duplicate")
    home = root / "feral-home"
    for folder in [home, root / "user-home", root / "tmp", root / "project"]:
        folder.mkdir(exist_ok=True)
    marker = root / "acceptance-only.txt"
    marker.write_text("Disposable FERAL populated native acceptance profile.\n")
    attachment = root / "synthetic-attachment.txt"
    attachment.write_text("Synthetic acceptance attachment. Unique marker: FERAL_ATTACHMENT_20261002\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = {"llm": {"provider": "ollama", "model": args.model,
        "base_url": f"http://127.0.0.1:{args.model_port}/v1", "fallback_providers": []},
        "features": {"multi_agent": False, "proactive": False, "self_learning": False, "vision": False},
        "vision": {"enabled": False}, "memory": {"sync": {"enabled": False}}}
    settings_path = home / "settings.json"
    if not settings_path.exists():
        settings_path.write_text(json.dumps(settings))
    suite = "ai.feral.native.acceptance." + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    env = {"HOME": str(root / "user-home"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": str(root / "tmp"), "LANG": "en_US.UTF-8", "FERAL_HOME": str(home),
        "FERAL_DATA_HOME": str(home / "data"), "FERAL_NATIVE_PREFS_SUITE": suite,
        "FERAL_PORT": str(port), "FERAL_OLLAMA_BASE_URL": f"http://127.0.0.1:{args.model_port}",
        "PYTHONNOUSERSITE": "1", "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "FERAL_SYNC_PASSPHRASE": "disposable-acceptance-synthetic-passphrase"}
    if args.model_dir:
        if not args.model_dir.is_dir():
            parser.error("the explicitly supplied model directory must already exist")
        model_env = {**os.environ, "OLLAMA_HOST": f"127.0.0.1:{args.model_port}",
            "OLLAMA_MODELS": str(args.model_dir.resolve()), "OLLAMA_MAX_LOADED_MODELS": "1",
            "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_KEEP_ALIVE": "2m"}
        # Fail before launch if another listener owns this requested port.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", args.model_port))
        with (root / "ollama.log").open("a") as log:
            model = subprocess.Popen(["/usr/local/bin/ollama", "serve"], env=model_env,
                cwd=root, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        (root / "ollama.pid").write_text(str(model.pid))
    with (root / "native.log").open("a") as log:
        child = subprocess.Popen([str(binary)], env=env, cwd=root,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
    evidence = {"pid": child.pid, "root": str(root), "port": port, "preferences_suite": suite,
        "candidate": str(app), "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    (root / "launch.json").write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence), flush=True)
    if args.hold:
        exit_code = child.wait()
        (root / "native-exit.json").write_text(json.dumps({"pid": child.pid, "exit_code": exit_code}))
        print(json.dumps({"host_pid": child.pid, "host_exit_code": exit_code}), flush=True)


if __name__ == "__main__":
    main()
