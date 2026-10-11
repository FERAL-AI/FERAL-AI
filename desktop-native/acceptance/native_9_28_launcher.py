"""Launch only the audited 9.28 candidate in the existing disposable profile."""

import hashlib
from pathlib import Path
import plistlib
import sys

import populated_profile_probe

EXPECTED_SHA = "dba3785d53634fb699986c2ade9290f42ff5c0631c63224510bfb34350d165f0"
ROOT = "/private/tmp/feral-native-populated-20261002"


def main():
    app = Path(__file__).resolve().parents[1] / "build/FERAL Native Preview.app"
    with (app / "Contents/Info.plist").open("rb") as file:
        info = plistlib.load(file)
    if info.get("CFBundleShortVersionString") != "2026.9.28" or info.get("CFBundleVersion") != "2026100202":
        raise SystemExit("Not the immutable expected 9.28 candidate")
    digest = hashlib.sha256((app / "Contents/MacOS/feral-native").read_bytes()).hexdigest()
    if digest != EXPECTED_SHA:
        raise SystemExit("Candidate hash differs from coordinator READY receipt")
    sys.argv = [sys.argv[0], "--root", ROOT, "--app", str(app), "--hold"]
    populated_profile_probe.main()


if __name__ == "__main__":
    main()
