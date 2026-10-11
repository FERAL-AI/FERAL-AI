"""Package the supplied FERAL logo into application icon formats.

The canonical image is kept unchanged. Generated assets only change size or
file format; the logo's white background and geometry are preserved.
"""
from pathlib import Path
import subprocess

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets/brand/feral-logo-original.png"


def main():
    with Image.open(SOURCE) as source:
        if source.width != source.height:
            raise ValueError("The canonical app logo must be square")
        logo = source.convert("RGBA")
    desktop = ROOT / "desktop/src-tauri/icons"
    desktop.mkdir(parents=True, exist_ok=True)
    native = ROOT / "desktop-native/assets"
    native.mkdir(parents=True, exist_ok=True)
    iconset = native / "Feral.iconset"
    iconset.mkdir(exist_ok=True)

    def png(path, size):
        path.parent.mkdir(parents=True, exist_ok=True)
        logo.resize((size, size), Image.Resampling.LANCZOS).save(path)

    for size in (16, 32, 128, 256, 512):
        png(iconset / f"icon_{size}x{size}.png", size)
        png(iconset / f"icon_{size}x{size}@2x.png", size * 2)
    subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(native / "Feral.icns")], check=True)
    png(native / "FeralLogo.png", 1024)
    for name, size in (("32x32.png", 32), ("64x64.png", 64), ("128x128.png", 128), ("128x128@2x.png", 256), ("icon.png", 512)):
        png(desktop / name, size)
    (desktop / "icon.icns").write_bytes((native / "Feral.icns").read_bytes())
    logo.save(desktop / "icon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    png(ROOT / "desktop/src-tauri/app-icon.png", 1024)
    png(ROOT / "desktop/src/feralLogo.png", 256)
    for size in (192, 512):
        png(ROOT / f"feral-client-v2/public/icons/icon-{size}.png", size)
    png(ROOT / "feral-client-v2/public/icons/icon-maskable-512.png", 512)
    print("Packaged FERAL logo: native ICNS/PNG, desktop ICNS/ICO/PNG, web icons.")


if __name__ == "__main__":
    main()
