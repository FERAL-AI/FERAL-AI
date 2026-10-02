"""Assemble only known generated resources into the pure SwiftUI Mac app."""
from pathlib import Path
import plistlib
import shutil

root = Path(__file__).resolve().parent
app = root / 'build' / 'FERAL Native Preview.app'
if app.exists():
    shutil.rmtree(app)
contents = app / 'Contents'
resources = contents / 'Resources'
(contents / 'MacOS').mkdir(parents=True)
resources.mkdir()
shutil.copy2(root / 'build' / 'feral-native', contents / 'MacOS' / 'feral-native')
for name in ('python', 'feral-core', 'opencode'):
    source = root.parent / 'desktop' / 'src-tauri' / 'resources' / name
    if not source.is_dir():
        raise RuntimeError(f'Missing staged {name}; run desktop/scripts/stage_bundle.sh first')
    shutil.copytree(source, resources / name, symlinks=True)
shutil.copy2(root / 'assets' / 'FeralAvatar.png', resources / 'FeralAvatar.png')
shutil.copy2(root / 'assets' / 'FeralLogo.png', resources / 'FeralLogo.png')
shutil.copy2(root / 'native_backend_launcher.py', resources / 'native_backend_launcher.py')
icon = root / 'assets' / 'Feral.icns'
if icon.is_file():
    shutil.copy2(icon, resources / 'icon.icns')
info = {
    'CFBundleName': 'FERAL Native Preview', 'CFBundleDisplayName': 'FERAL Native Preview',
    'CFBundleIdentifier': 'ai.feral.native.preview', 'CFBundleExecutable': 'feral-native',
    'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '2026.9.27',
    'CFBundleVersion': '2026100201', 'LSMinimumSystemVersion': '13.0',
    'NSMicrophoneUsageDescription': 'FERAL uses your microphone only when you start a voice conversation.',
    'CFBundleIconFile': 'icon', 'NSHighResolutionCapable': True,
    'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True},
}
(contents / 'Info.plist').write_bytes(plistlib.dumps(info))
print(app)
