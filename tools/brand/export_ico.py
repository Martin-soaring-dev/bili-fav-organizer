#!/usr/bin/env python3
"""Rasterize the brand SVG masters into a multi-frame Windows ICO.

Build-time only: unlike build.py, this needs Pillow and a Playwright Chromium,
neither of which is an application dependency. The generated ICO is committed,
so CI (which only installs requirements.txt + pyinstaller) just consumes it.

Per docs/brand/implementation.md §7, small frames come from the favicon master
and large frames from the light app icon; a single 256 px bitmap scaled down is
explicitly not acceptable, so every frame is rasterized from vector at its own
size.
"""
from io import BytesIO
from pathlib import Path
import argparse

from PIL import Image, IcoImagePlugin
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "static/brand"
OUTPUT = ROOT / "packaging/BiliFavOrganizer.ico"

# (master svg, pixel sizes) in rendering order.
FRAMES = [
    (ASSETS / "favicon.svg", (16, 24, 32)),
    (ASSETS / "app-icon-light.svg", (48, 64, 128, 256)),
]
SIZES = sorted(size for _, sizes in FRAMES for size in sizes)


def rasterize(master: Path, size: int) -> Image.Image:
    """Render one vector master at exactly this pixel size, alpha preserved."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": size, "height": size})
            page.goto(master.as_uri())
            shot = page.screenshot(omit_background=True)
        finally:
            browser.close()
    image = Image.open(BytesIO(shot)).convert("RGBA")
    if image.size != (size, size):
        raise SystemExit(f"{master.name} rendered as {image.size}, expected {(size, size)}")
    return image


def build() -> bytes:
    frames = {}
    for master, sizes in FRAMES:
        for size in sizes:
            frames[size] = rasterize(master, size)
    largest = frames[max(SIZES)]
    buffer = BytesIO()
    largest.save(
        buffer,
        format="ICO",
        sizes=[(size, size) for size in SIZES],
        append_images=[frames[size] for size in SIZES if size != max(SIZES)],
    )
    return buffer.getvalue()


def read_sizes(payload: bytes) -> set[tuple[int, int]]:
    return set(IcoImagePlugin.IcoFile(BytesIO(payload)).sizes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if the committed ICO no longer matches the frame plan")
    args = parser.parse_args()

    if args.check:
        if not OUTPUT.exists():
            raise SystemExit(f"Missing {OUTPUT.relative_to(ROOT)} — run tools/brand/export_ico.py")
        found = read_sizes(OUTPUT.read_bytes())
        expected = {(size, size) for size in SIZES}
        if found != expected:
            raise SystemExit(f"ICO frames {sorted(found)} != expected {sorted(expected)}")
        print("ICO frames match the plan.")
        return

    payload = build()
    found = read_sizes(payload)
    expected = {(size, size) for size in SIZES}
    if found != expected:
        raise SystemExit(f"Exported ICO frames {sorted(found)} != expected {sorted(expected)}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(payload)
    print(f"Wrote {OUTPUT.relative_to(ROOT)} with frames {SIZES} ({len(payload)} bytes).")


if __name__ == "__main__":
    main()
