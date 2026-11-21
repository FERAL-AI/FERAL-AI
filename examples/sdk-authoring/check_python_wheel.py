"""Import the supplied SDK wheel from a disposable artifact directory, without core."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import zipfile


def main():
    wheel = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="feral-sdk-wheel-") as folder:
        artifact = Path(folder) / "package"
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if name.startswith("/") or ".." in Path(name).parts:
                    raise ValueError("Unsafe package member")
            archive.extractall(artifact)
        script = '''
import sys, importlib.abc, json
from pathlib import Path
sys.path.insert(0, sys.argv[1])
class BlockCore(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "skills" or name.startswith("skills."):
            raise ModuleNotFoundError("Core unavailable", name=name)
sys.meta_path.insert(0, BlockCore())
import feral_sdk
assert Path(feral_sdk.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
class Plugin(feral_sdk.FeralPlugin):
    name = "artifact_probe"
    @feral_sdk.feral_tool()
    async def sum(self, a: int, b: int = 29): return a + b
assert Plugin().to_manifest()["endpoints"][0]["params"][0]["type"] == "integer"
try: Plugin.runtime_skill()
except RuntimeError: pass
else: raise AssertionError("Missing core not reported")
print(json.dumps({"wheel_import": True, "standalone_without_core": True, "missing_runtime_explicit": True}))
'''
        result = subprocess.run([sys.executable, "-I", "-c", script, str(artifact)],
                                cwd=folder, capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise RuntimeError("Artifact import verification failed: " + result.stderr)
        print(json.dumps(json.loads(result.stdout), sort_keys=True))


if __name__ == "__main__":
    main()
