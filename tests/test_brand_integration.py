"""Brand assets must stay wired into the three surfaces that show them.

These are static checks on committed files: the web favicon, the topbar lockup,
and the Windows executable icon. They deliberately do not import the server or
touch user data.
"""
import json
import re
import struct
from io import BytesIO
from pathlib import Path
from unittest import TestCase, main

from PIL import IcoImagePlugin

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
STYLE = ROOT / "static" / "style.css"
SPEC = ROOT / "BiliFavOrganizer.spec"
ICO = ROOT / "packaging" / "BiliFavOrganizer.ico"
BRAND_COLORS = json.loads((ROOT / "static" / "brand" / "tokens.json").read_text(encoding="utf-8"))["color"]
# canvas/white are the plate, not the mark — counting them would pass on an empty icon.
MARK_COLORS = tuple(BRAND_COLORS[name] for name in ("deep", "teal", "mint", "ink"))

# tools/brand/export_ico.py owns this frame plan; keep the two in step.
ICON_FRAMES = [16, 24, 32, 48, 64, 128, 256]


class FaviconTests(TestCase):
    def test_index_declares_svg_favicon_from_static_brand(self):
        html = INDEX.read_text(encoding="utf-8")
        self.assertRegex(
            html,
            r'<link\s+rel="icon"[^>]*href="/static/brand/favicon\.svg"',
            "index.html must point the tab icon at the brand favicon master",
        )

    def test_favicon_asset_exists_and_is_vector_only(self):
        asset = ROOT / "static" / "brand" / "favicon.svg"
        self.assertTrue(asset.is_file(), f"{asset} is missing")
        body = asset.read_text(encoding="utf-8")
        for forbidden in ("<image", "script", "foreignObject", "xlink:href"):
            self.assertNotIn(forbidden, body, f"favicon.svg must not embed {forbidden}")


class TopbarBrandTests(TestCase):
    def test_topbar_replaces_the_play_glyph_with_the_mark(self):
        html = INDEX.read_text(encoding="utf-8")
        brand = re.search(r'<div class="brand">(.*?)</div>', html, re.S)
        self.assertIsNotNone(brand, "topbar .brand block not found")
        block = brand.group(1)
        self.assertIn('src="/static/brand/mark-flat.svg"', block)
        self.assertIn("B站收藏夹智能整理", block)
        self.assertNotIn("▶", block, "the old ▶ glyph must not survive next to the real mark")

    def test_mark_is_decorative_and_sized_by_css(self):
        html = INDEX.read_text(encoding="utf-8")
        img = re.search(r'<img class="brand-mark"[^>]*>', html)
        self.assertIsNotNone(img, "brand mark <img> missing")
        tag = img.group(0)
        self.assertIn('alt=""', tag, "the product name follows the mark, so the image must be decorative")
        css = STYLE.read_text(encoding="utf-8")
        rule = re.search(r"\.brand-mark\s*{([^}]*)}", css)
        self.assertIsNotNone(rule, "style.css must size .brand-mark")
        self.assertIn("flex: none", rule.group(1), "a shrinking logo distorts at narrow widths")

    def test_flat_mark_carries_its_own_colors_for_both_themes(self):
        body = (ROOT / "static" / "brand" / "mark-flat.svg").read_text(encoding="utf-8")
        self.assertNotIn("currentColor", body, "an <img> cannot inherit page color; the flat master must be self-colored")
        self.assertNotIn("<rect", body, "the topbar mark must stay transparent, not carry a plate")


class ExecutableIconTests(TestCase):
    def test_spec_points_pyinstaller_at_the_ico(self):
        spec = SPEC.read_text(encoding="utf-8")
        self.assertRegex(spec, r"icon=str\(APP_ICON\)")
        self.assertRegex(spec, r'APP_ICON\s*=\s*ROOT\s*/\s*"packaging"\s*/\s*"BiliFavOrganizer\.ico"')

    def test_ico_is_committed_with_every_required_frame(self):
        self.assertTrue(ICO.is_file(), f"{ICO} is missing — run python tools/brand/export_ico.py")
        found = set(IcoImagePlugin.IcoFile(BytesIO(ICO.read_bytes())).sizes())
        self.assertEqual(found, {(size, size) for size in ICON_FRAMES})

    def test_ico_frames_are_not_blank(self):
        """Each frame must paint the mark: a scaled-down empty plate passes a size check only.

        Small frames come from the favicon master (deep teal, tuned for legibility),
        large ones from the gradient app icon, so accept any brand palette color.
        """
        palette = [
            tuple(int(text[index : index + 2], 16) for index in (1, 3, 5))
            for text in MARK_COLORS
        ]
        ico = IcoImagePlugin.IcoFile(BytesIO(ICO.read_bytes()))
        for size in ICON_FRAMES:
            image = ico.getimage((size, size)).convert("RGB")
            raw = image.tobytes()
            painted = sum(
                1
                for offset in range(0, len(raw), 3)
                if any(all(abs(raw[offset + channel] - color[channel]) <= 40 for channel in range(3)) for color in palette)
            )
            share = painted / (size * size)
            self.assertGreater(share, 0.05, f"{size}px frame has only {share:.1%} brand-colored pixels")

    def test_ico_container_header_is_well_formed(self):
        payload = ICO.read_bytes()
        reserved, kind, count = struct.unpack("<HHH", payload[:6])
        self.assertEqual((reserved, kind), (0, 1), "ICO must start with reserved=0, type=1")
        self.assertEqual(count, len(ICON_FRAMES))


if __name__ == "__main__":
    main()
