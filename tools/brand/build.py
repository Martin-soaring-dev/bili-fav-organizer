#!/usr/bin/env python3
"""Build the v1 brand assets. Python stdlib only; run from any directory."""
from pathlib import Path
import argparse
import json
from html import escape

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "static/brand"
DOCS = ROOT / "docs/brand"
WORDMARK = json.loads((Path(__file__).parent / "wordmark-paths.json").read_text())
DESCRIPTOR = json.loads((Path(__file__).parent / "descriptor-paths.json").read_text())

# The 512-unit master. The white play shape is a true cut-out in every mark.
OUTLINE = "M96 48H276C362 48 412 92 412 156C412 190 396 212 374 230C424 252 452 286 452 336C452 414 386 464 290 464H214Q199 464 188 452L128 382L84 428Q64 448 64 420V80Q64 48 96 48Z"
PLAY = "M244 200Q220 186 220 214V294Q220 322 244 308L318 266Q342 254 318 242Z"
RIBBON = "M96 48C122 76 152 105 188 128V452L128 382L84 428Q64 448 64 420V80Q64 48 96 48Z"
UPPER = "M96 48H276C362 48 412 92 412 156C412 190 396 212 374 230C298 200 186 146 96 48Z"
FOLD = "M188 128C247 169 309 204 374 230C410 251 412 274 326 310C278 330 225 353 188 388Z"
COLORS = {"ink": "#0F3D3A", "deep": "#0F766E", "teal": "#14B8A6", "mint": "#2DD4BF", "canvas": "#F0FDFA", "white": "#FFFFFF"}


def defs(prefix):
    return f'''<defs>
  <linearGradient id="{prefix}-body" gradientUnits="userSpaceOnUse" x1="112" y1="64" x2="440" y2="448"><stop stop-color="#0F766E"/><stop offset=".52" stop-color="#14B8A6"/><stop offset="1" stop-color="#2DD4BF"/></linearGradient>
  <linearGradient id="{prefix}-ribbon" gradientUnits="userSpaceOnUse" x1="70" y1="72" x2="188" y2="436"><stop stop-color="#14B8A6"/><stop offset="1" stop-color="#0F766E"/></linearGradient>
  <linearGradient id="{prefix}-upper" gradientUnits="userSpaceOnUse" x1="96" y1="48" x2="400" y2="224"><stop stop-color="#0F766E"/><stop offset="1" stop-color="#2DD4BF"/></linearGradient>
  <linearGradient id="{prefix}-fold" gradientUnits="userSpaceOnUse" x1="188" y1="150" x2="360" y2="308"><stop stop-color="#5EEAD4"/><stop offset=".62" stop-color="#14B8A6"/><stop offset="1" stop-color="#0F766E"/></linearGradient>
  <clipPath id="{prefix}-cut"><path fill-rule="evenodd" clip-rule="evenodd" d="{OUTLINE} {PLAY}"/></clipPath>
</defs>'''


def mark(mode="gradient", prefix="bfo", color=None):
    if mode in ("mono", "compact"):
        return f'<path fill="{color or COLORS["deep"]}" fill-rule="evenodd" d="{OUTLINE} {PLAY}"/>'
    layers = [(OUTLINE, "body", "#14B8A6"), (RIBBON, "ribbon", "#0F766E"),
              (UPPER, "upper", "#14B8A6"), (FOLD, "fold", "#2DD4BF")]
    shapes = "\n".join(f'<path d="{d}" fill="{flat if mode == "flat" else f"url(#{prefix}-{name})"}"/>' for d, name, flat in layers)
    return defs(prefix) + f'\n<g clip-path="url(#{prefix}-cut)">\n{shapes}\n</g>'


def svg(content, w=512, h=512, title="BiliFav Organizer", desc="书签、字母 B 和播放负空间组成的收藏整理标识。", prefix="bfo"):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" role="img" aria-labelledby="{prefix}-title {prefix}-desc">
<title id="{prefix}-title">{escape(title)}</title>
<desc id="{prefix}-desc">{escape(desc)}</desc>
{content}
</svg>
'''


def wordmark(inverse=False):
    return "\n".join(f'<path fill="{COLORS["white"] if inverse else COLORS["ink"] if i == 0 else COLORS["deep"]}" d="{p["d"]}"/>' for i, p in enumerate(WORDMARK["parts"]))


def lockup(stacked=False, inverse=False):
    # Both English and Chinese signatures are outlined, with no font dependency.
    cn_color = "#CCFBF1" if inverse else "#52656A"
    cn = f'<path transform="translate(0 36)" fill="{cn_color}" d="{DESCRIPTOR["d"]}"/>'
    if stacked:
        cn_x = (710 - DESCRIPTOR["advance"]) / 2
        return svg(f'<g transform="translate(189.88 6) scale(.64)">{mark(prefix="bfo-stack")}</g><g transform="translate(46 418)">{wordmark(inverse)}</g><g transform="translate({cn_x:.3f} 439)">{cn}</g>', 710, 520, prefix="bfo-stack-root")
    return svg(f'<g transform="translate(10 8) scale(.34)">{mark(prefix="bfo-horizontal")}</g><g transform="translate(204 98)">{wordmark(inverse)}{cn}</g>', 856, 190, prefix="bfo-horizontal-root")


ICON_PATHS = {
    "scan": '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/><circle cx="11" cy="11" r="4"/><path d="m14 14 3 3"/>',
    "folder": '<path d="M3 7V5h6l2 2h10v13H3Z"/>',
    "profile": '<path d="M3 7V5h6l2 2h10v13H3Z"/><path d="M7 11h10M7 15h6"/>',
    "classify": '<rect x="9" y="3" width="6" height="5" rx="1"/><path d="M12 8v4M5 16v-4h14v4"/><rect x="2" y="16" width="6" height="5" rx="1"/><rect x="16" y="16" width="6" height="5" rx="1"/>',
    "review": '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="m8 12 3 3 5-6"/>',
    "apply": '<path d="M5 4v16M9 12h11m-5-5 5 5-5 5"/>',
    "merge": '<path d="M4 4v3q0 5 8 5M20 4v3q0 5-8 5M12 12v8m-4-4 4 4 4-4"/>',
    "archive": '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v12h14V8M9 12h6"/>',
    "pause": '<path d="M8 5v14M16 5v14"/>',
    "warning": '<path d="m12 3 10 18H2Z M12 9v5M12 17v.1"/>',
    "settings": '<path d="M3 6h18M3 12h18M3 18h18"/><circle cx="8" cy="6" r="2" fill="var(--bfo-icon-bg, #fff)"/><circle cx="16" cy="12" r="2" fill="var(--bfo-icon-bg, #fff)"/><circle cx="10" cy="18" r="2" fill="var(--bfo-icon-bg, #fff)"/>',
    "video": '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="m10 8 6 4-6 4Z"/>',
}


def line_icon(paths):
    return f'<g fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{paths}</g>'


def build():
    result = {}
    def add(name, value):
        result[ASSETS / name] = value
    for mode in ("gradient", "flat", "mono", "compact"):
        add(f"mark-{mode}.svg", svg(mark(mode, prefix=f"bfo-{mode}"), prefix=f"bfo-{mode}-root"))
    add("mark-inverse.svg", svg(mark("mono", color="#FFFFFF"), prefix="bfo-inverse"))
    for theme in ("light", "dark"):
        bg = "#F0FDFA" if theme == "light" else "#0F3D3A"
        dark_detail = f'<path d="{RIBBON}" fill="#5EEAD4"/><path d="{PLAY}" fill="#F0FDFA"/>' if theme == "dark" else ""
        add(f"app-icon-{theme}.svg", svg(f'<rect width="512" height="512" rx="112" fill="{bg}"/><g transform="translate(51.2 51.2) scale(.8)">{mark(prefix="bfo-app-"+theme)}{dark_detail}</g>', prefix="bfo-app-root-"+theme))
    add("favicon.svg", svg('<rect width="512" height="512" rx="112" fill="#F0FDFA"/>' + f'<g transform="translate(25.6 25.6) scale(.9)">{mark("compact")}</g>', prefix="bfo-favicon"))
    add("lockup-horizontal.svg", lockup())
    add("lockup-horizontal-inverse.svg", lockup(inverse=True))
    add("lockup-stacked.svg", lockup(stacked=True))
    add("lockup-stacked-inverse.svg", lockup(stacked=True, inverse=True))
    add("wordmark.svg", svg(f'<g transform="translate(10 66)">{wordmark()}</g>', 638, 100, prefix="bfo-wordmark"))
    for name, paths in ICON_PATHS.items():
        add(f"icons/{name}.svg", svg(line_icon(paths), 24, 24, title=f"BiliFav {name}", desc="24 单位网格，1.8 单位圆端线条图标。", prefix=f"bfo-icon-{name}"))
    symbols = [f'<symbol id="bfo-mark" viewBox="0 0 512 512">{mark("mono",color="currentColor")}</symbol>']
    symbols += [f'<symbol id="bfo-{name}" viewBox="0 0 24 24">{line_icon(paths)}</symbol>' for name, paths in ICON_PATHS.items()]
    add("sprite.svg", '<svg xmlns="http://www.w3.org/2000/svg">\n' + "\n".join(symbols) + '\n</svg>\n')
    tokens = {
        "version": "1.0.0", "brand": "BiliFav Organizer", "color": COLORS,
        "ui": {
            "light": {"background":"#F3F5F7", "surface":"#FFFFFF", "text":"#24303B", "muted":"#52656A", "border":"#CFD6DD", "action":"#087F6B", "onAction":"#FFFFFF", "success":"#238636", "warning":"#9A6900", "danger":"#C73535"},
            "dark": {"background":"#0F1419", "surface":"#1A212B", "text":"#D8E0EA", "muted":"#A1ADBA", "border":"#2A3441", "action":"#00D3A8", "onAction":"#052019", "success":"#3FB950", "warning":"#F5C518", "danger":"#FF5A5A"}},
        "spacing": [4,8,12,16,24,32,48,64], "radius": {"control":6, "card":12, "dialog":16},
        "typography": {"sans":"system-ui, 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif", "mono":"'Cascadia Code', Consolas, monospace", "size":[12,14,16,20,28,40]},
        "icon": {"viewBox":"0 0 24 24", "stroke":1.8, "linecap":"round", "linejoin":"round"},
        "motion": {"fastMs":120, "normalMs":180, "slowMs":240}}
    add("tokens.json", json.dumps(tokens, ensure_ascii=False, indent=2) + "\n")
    css = ["/* BiliFav Organizer brand v1.0.0. Opt-in tokens; no existing UI overrides. */", ":root {"]
    css += [f"  --bfo-{k}: {v};" for k,v in COLORS.items()]
    css += ["  --bfo-font-sans: " + tokens["typography"]["sans"] + ";", "  --bfo-font-mono: " + tokens["typography"]["mono"] + ";", "}"]
    for theme, values in tokens["ui"].items():
        css += [f'.bfo-theme-{theme}, :root[data-theme="{theme}"] {{']
        css += [f"  --bfo-ui-{k}: {v};" for k,v in values.items()]
        css += ["  --bfo-icon-bg: " + values["surface"] + ";", "}"]
    css += [".bfo-icon { width: 24px; height: 24px; flex: none; color: inherit; }", ".bfo-mark { display: block; width: 32px; height: 32px; }", ""]
    add("tokens.css", "\n".join(css))
    add("manifest.json", json.dumps({"version":"1.0.0", "masterViewBox":"0 0 512 512", "files":[p.relative_to(ASSETS).as_posix() for p in result if p.suffix == ".svg"]}, indent=2) + "\n")
    grid = ''.join(f'<path d="M{i} 0V512M0 {i}H512"/>' for i in range(0,513,32))
    construction = f'<rect width="720" height="640" fill="#F0FDFA"/><g transform="translate(104 32)"><g stroke="#CCFBF1" stroke-width="1">{grid}</g>{mark("flat",prefix="bfo-grid")}<rect x="64" y="48" width="388" height="416" fill="none" stroke="#0F3D3A" stroke-width="1" stroke-dasharray="6 4"/><path d="M188 48V464M0 254H512" fill="none" stroke="#0F3D3A" stroke-width="1" stroke-dasharray="6 4"/><circle cx="128" cy="382" r="5" fill="#0F3D3A"/></g><g fill="#0F3D3A" font-family="sans-serif" font-size="16"><text x="104" y="580">512 × 512 master · 32-unit grid</text><text x="104" y="608">Ink bounds: x 64–452 / y 48–464 · notch: (128, 382)</text></g>'
    result[DOCS / "construction.svg"] = svg(construction,720,640,title="BiliFav Organizer construction grid",prefix="bfo-grid-root")
    # Contact sheet is vector, and deliberately has no linked images or fonts.
    board = ['<rect width="1200" height="900" fill="#F7FBFA"/>', '<g fill="#0F3D3A" font-family="sans-serif"><text x="48" y="52" font-size="26" font-weight="700">BiliFav Organizer / Visual Identity v1.0</text><text x="48" y="78" font-size="14">BOOKMARK + B + PLAY · FOLD / FLOW / COLLECTION</text></g>']
    for i,(mode,label) in enumerate([("gradient","01 / PRIMARY"),("flat","02 / FLAT"),("mono","03 / MONO")]):
        x=48+i*384
        board += [f'<rect x="{x}" y="110" width="352" height="290" rx="20" fill="#FFFFFF"/>',f'<g transform="translate({x+71} 124) scale(.41)">{mark(mode,prefix="bfo-board-"+mode)}</g>', f'<text x="{x+24}" y="374" font-family="sans-serif" font-size="14" fill="#52656A">{label}</text>']
    board += ['<rect x="48" y="428" width="736" height="180" rx="20" fill="#FFFFFF"/>',f'<g transform="translate(58 446) scale(.82)">{lockup()[lockup().index("<g "):lockup().rindex("</svg>")]}</g>']
    for i,theme in enumerate(("light","dark")):
        x=824+i*164; bg="#F0FDFA" if theme=="light" else "#0F3D3A"
        dark_detail = f'<path d="{RIBBON}" fill="#5EEAD4"/><path d="{PLAY}" fill="#F0FDFA"/>' if theme == "dark" else ""
        board += [f'<rect x="{x}" y="438" width="148" height="148" rx="32" fill="{bg}"/>',f'<g transform="translate({x+14.8} 452.8) scale(.23125)">{mark(prefix="bfo-board-app-"+theme)}{dark_detail}</g>']
    for i,(name,c) in enumerate(COLORS.items()):
        x=48+i*184
        board += [f'<rect x="{x}" y="638" width="168" height="58" rx="12" fill="{c}" stroke="#CFD6DD"/>',f'<text x="{x}" y="722" fill="#52656A" font-family="sans-serif" font-size="14">{name.upper()} / {c}</text>']
    for i,(name,paths) in enumerate(ICON_PATHS.items()):
        x=48+i*92
        board += [f'<g transform="translate({x} 768) scale(1.5)" color="#0F766E">{line_icon(paths)}</g>',f'<text x="{x}" y="830" fill="#52656A" font-family="sans-serif" font-size="12">{name}</text>']
    result[DOCS / "overview.svg"] = svg("\n".join(board),1200,900,title="BiliFav Organizer visual identity overview",prefix="bfo-board-root")
    template = (Path(__file__).parent / "preview-template.html").read_text(encoding="utf-8")
    result[DOCS / "preview.html"] = template.replace("{{SPRITE}}", result[ASSETS / "sprite.svg"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if checked-in assets differ from the source")
    args = parser.parse_args()
    mismatch=[]
    for path, value in build().items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != value:
                mismatch.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(value,encoding="utf-8")
    if mismatch:
        raise SystemExit("Outdated assets: " + ", ".join(mismatch))
    print("Brand assets match source." if args.check else "Built brand assets.")


if __name__ == "__main__":
    main()
