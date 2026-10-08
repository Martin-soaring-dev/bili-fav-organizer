# -*- coding: utf-8 -*-
"""从品牌 SVG 生成 Windows 安装向导位图：packaging/wizard-image.png / wizard-small.png。

Inno 的向导大图是竖长条、比例固定 164:314；小图是方图。两张图都只放标志符号、
不放文字（头部只有 ~55 px，文字必糊）。产物提交进仓库，CI 只装 Inno Setup 即可，
不需要任何光栅器。

运行：python tools/brand/export_wizard_images.py
依赖：Pillow + Playwright Chromium（只在维护品牌资产时需要，不是应用依赖）。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SYMBOL = ROOT / "static" / "brand" / "mark-gradient.svg"
OUT_DIR = ROOT / "packaging"
HTTP_PORT = "8126"

# 画布宽度取 164 的整数倍，才能精确保持 Inno 要求的 164:314
WIZARD_WIDTH = 656
WIZARD_FILL = 0.78          # 符号占画布宽度的比例
WIZARD_VERTICAL_RATIO = 0.42  # 垂直中心落在画布 42% 高度处
SMALL_WIDTH = 318
SMALL_FILL = 0.86


def _svg_ratio(path: Path) -> float:
    """从 viewBox 取宽高比。"""
    import re
    text = path.read_text(encoding="utf-8")
    match = re.search(r'viewBox="([\d.\-\s]+)"', text)
    if not match:
        raise ValueError(f"无法从 {path.name} 读取 viewBox")
    parts = match.group(1).split()
    return float(parts[3]) / float(parts[2])


def _render(page, out_path: Path, canvas_w: int, canvas_h: int, box: tuple[int, int, int, int]):
    left, top, width, height = box
    # 走本地 HTTP：file:// 子资源会被 Chromium 拦掉，图片会渲染成空白
    symbol_url = f"http://127.0.0.1:{HTTP_PORT}/static/brand/{SYMBOL.name}"
    page.set_viewport_size({"width": canvas_w, "height": canvas_h})
    page.set_content(
        "<style>html,body{margin:0;padding:0;width:%dpx;height:%dpx;"
        "background:transparent;overflow:hidden}"
        "img{position:absolute;display:block;left:%dpx;top:%dpx;width:%dpx;height:%dpx}"
        "</style><img src=\"%s\" alt=\"\">"
        % (canvas_w, canvas_h, left, top, width, height, symbol_url))
    page.wait_for_timeout(300)
    page.screenshot(path=str(out_path), omit_background=True)

    image = Image.open(out_path)
    if image.size != (canvas_w, canvas_h):
        raise AssertionError(f"{out_path.name} 尺寸 {image.size}，期望 {(canvas_w, canvas_h)}")
    if image.mode != "RGBA":
        raise AssertionError(f"{out_path.name} 不是 RGBA（无透明通道）")
    for corner in ((0, 0), (canvas_w - 1, 0), (0, canvas_h - 1), (canvas_w - 1, canvas_h - 1)):
        if image.getpixel(corner)[3] != 0:
            raise AssertionError(f"{out_path.name} 四角不是透明像素：{corner}")
    alpha = image.getchannel("A").tobytes()
    ink = sum(1 for value in alpha if value > 40)
    if ink / (canvas_w * canvas_h) < 0.02:
        raise AssertionError(f"{out_path.name} 几乎空白（着墨像素 {ink}）")
    print(f"{out_path.name}: {canvas_w}x{canvas_h}，着墨 {ink / (canvas_w * canvas_h):.1%}，"
          f"内容区 {width}x{height} @({left},{top})，{out_path.stat().st_size} 字节")


def main() -> int:
    if not SYMBOL.is_file():
        print(f"缺少品牌 SVG：{SYMBOL}", file=sys.stderr)
        return 1
    ratio = _svg_ratio(SYMBOL)
    server = subprocess.Popen([sys.executable, "-m", "http.server", HTTP_PORT,
                               "--bind", "127.0.0.1", "--directory", str(ROOT)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.0)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            try:
                # 大图：欢迎页 / 完成页左侧竖条
                canvas_w = WIZARD_WIDTH
                canvas_h = round(canvas_w * 314 / 164)
                content_w = round(canvas_w * WIZARD_FILL)
                content_h = round(content_w * ratio)
                _render(page, OUT_DIR / "wizard-image.png", canvas_w, canvas_h,
                        (round((canvas_w - content_w) / 2),
                         round((canvas_h - content_h) * WIZARD_VERTICAL_RATIO),
                         content_w, content_h))
                # 小图：内页头部方图
                content_w = round(SMALL_WIDTH * SMALL_FILL)
                content_h = round(content_w * ratio)
                _render(page, OUT_DIR / "wizard-small.png", SMALL_WIDTH, SMALL_WIDTH,
                        (round((SMALL_WIDTH - content_w) / 2),
                         round((SMALL_WIDTH - content_h) / 2),
                         content_w, content_h))
            finally:
                browser.close()
    finally:
        server.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
