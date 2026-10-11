"""Launch only the disposable native accessibility probe and record its exit."""

import argparse
import json
from pathlib import Path
import plistlib
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["selectable", "plain", "explicit-label", "appkit"])
    args = parser.parse_args()
    root = Path("/private/tmp/feral-native-populated-20261002")
    app = root / "FERAL Accessibility Probe.app"
    binary = app / "Contents/MacOS/feral-ax-probe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "feral-ax-probe", binary)
    with (app / "Contents/Info.plist").open("wb") as output:
        plistlib.dump({"CFBundleExecutable": "feral-ax-probe", "CFBundleIdentifier": "ai.feral.native.acceptance.ax-probe", "CFBundleName": "FERAL Accessibility Probe", "CFBundlePackageType": "APPL"}, output)
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(root / "user-home"), "TMPDIR": str(root / "tmp"), "FERAL_AX_PROBE_MODE": args.mode}
    child = subprocess.Popen([str(binary)], env=environment, start_new_session=True)
    print(json.dumps({"pid": child.pid, "mode": args.mode}), flush=True)
    status = child.wait()
    result = {"pid": child.pid, "mode": args.mode, "exit_code": status}
    (root / ("ax-" + args.mode + "-exit.json")).write_text(json.dumps(result))
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
