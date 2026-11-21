"""Run only a backend-free synthetic selectable layout probe with exit evidence."""

import argparse
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["original", "measured-copy", "security-original"])
    args = parser.parse_args()
    root = Path("/private/tmp/feral-native-populated-20261002")
    app = root / "FERAL Selectable Layout Probe.app"
    binary = app / "Contents/MacOS/feral-selectable-layout-probe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "feral-selectable-layout-probe", binary)
    with (app / "Contents/Info.plist").open("wb") as file:
        plistlib.dump({"CFBundleExecutable": binary.name,
                      "CFBundleIdentifier": "ai.feral.native.acceptance.selectable-layout-probe",
                      "CFBundleName": "FERAL Selectable Layout Probe",
                      "CFBundlePackageType": "APPL"}, file)
    subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(root / "user-home"),
           "TMPDIR": str(root / "tmp"), "FERAL_LAYOUT_MODE": args.mode}
    server = None
    if args.mode == "security-original":
        fixtures = {"/api/security/grants": {"grants": [{"path": str(root / "project"), "mode": "readwrite"}]},
                    "/api/security/permissions": {"max_tier": "active", "tiers": ["passive", "active", "privileged", "dangerous"]},
                    "/api/security/audit": {"entries": []}, "/api/policy": {"execution": {"full_authority": False}},
                    "/api/config": {"cost": {}}}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps(fixtures.get(self.path, {"error": "Synthetic unknown path"})).encode()
                self.send_response(200 if self.path in fixtures else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env["FERAL_LAYOUT_SECURITY_URL"] = "http://127.0.0.1:" + str(server.server_port)
    with (root / ("layout-" + args.mode + ".log")).open("w") as output:
        child = subprocess.Popen([str(binary)], env=env, stdout=output, stderr=output, start_new_session=True)
        print(json.dumps({"pid": child.pid, "mode": args.mode}), flush=True)
        status = child.wait()
    result = {"pid": child.pid, "mode": args.mode, "exit_code": status}
    (root / ("layout-" + args.mode + "-exit.json")).write_text(json.dumps(result))
    print(json.dumps(result), flush=True)
    if server is not None:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
