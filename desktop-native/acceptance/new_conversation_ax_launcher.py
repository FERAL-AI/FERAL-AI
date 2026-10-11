"""Run only the synthetic New conversation AX probe with an observed exit."""

import argparse
import json
from pathlib import Path
import plistlib
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["original", "appkit"])
    args = parser.parse_args()
    root = Path("/private/tmp/feral-native-populated-20261002")
    app = root / "FERAL New Conversation Probe.app"
    binary = app / "Contents/MacOS/feral-new-conversation-probe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "feral-new-conversation-probe", binary)
    with (app / "Contents/Info.plist").open("wb") as output:
        plistlib.dump({"CFBundleExecutable": binary.name, "CFBundleIdentifier": "ai.feral.native.acceptance.new-conversation-probe", "CFBundleName": "FERAL New Conversation Probe", "CFBundlePackageType": "APPL"}, output)
    subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(root / "user-home"), "TMPDIR": str(root / "tmp"), "FERAL_AX_PROBE_MODE": args.mode}
    child = subprocess.Popen([str(binary)], env=env, start_new_session=True)
    print(json.dumps({"pid": child.pid, "mode": args.mode}), flush=True)
    status = child.wait()
    result = {"pid": child.pid, "mode": args.mode, "exit_code": status}
    (root / ("new-conversation-" + args.mode + "-exit.json")).write_text(json.dumps(result))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
