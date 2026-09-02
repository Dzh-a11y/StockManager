"""Generate the StockManager application icon (master PNG, .icns, .ico).

Design: a deep navy rounded square with a soft vertical gradient, three
ascending semi-transparent bars and a bold rising trend line ending in an
arrowhead — "research moving up". Supersampled then downscaled for smooth
edges.

Run: python scripts/make_icon.py
Outputs under assets/icon/: icon.svg, master.png, icon.icns, icon.ico.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "icon"

SS = 2  # supersample factor
BASE = 1024
W = H = BASE * SS


def _lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def _scaled(points) -> list[tuple[int, int]]:
    return [(int(x * SS), int(y * SS)) for x, y in points]


def build() -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 1) rounded-square gradient background (navy -> steel blue)
    radius = int(W * 0.22)
    top = (9, 24, 46)
    bottom = (18, 58, 96)
    for y in range(H):
        t = y / (H - 1)
        color = tuple(_lerp(top[i], bottom[i], t) for i in range(3)) + (255,)
        draw.line([(0, y), (W, y)], fill=color)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 1, H - 1], radius=radius, fill=255)
    img.putalpha(mask)

    # 2) ascending bars (semi-transparent teal) near the bottom baseline
    bar_color = (94, 234, 212, 130)
    bars = [  # (x0, x1, top_y) — all bottom at 780
        (170, 270, 660),
        (360, 460, 590),
        (550, 650, 520),
    ]
    for x0, x1, top_y in bars:
        draw.rounded_rectangle(
            [int(x0 * SS), int(top_y * SS), int(x1 * SS), int(780 * SS)],
            radius=int(28 * SS),
            fill=bar_color,
        )

    # 3) bold rising trend line ending in an arrowhead
    line_points = _scaled([(200, 660), (330, 540), (470, 600), (650, 410), (850, 250)])
    draw.line(line_points, fill=(60, 224, 150, 255), width=int(66 * SS), joint="curve")
    # round the two ends
    for x, y in (line_points[0], line_points[-1]):
        r = int(33 * SS)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(60, 224, 150, 255))

    # arrowhead pointing up-right at the line end
    tip = (int(920 * SS), int(196 * SS))
    left = (int(838 * SS), int(180 * SS))
    right = (int(900 * SS), int(300 * SS))
    draw.polygon([tip, left, right], fill=(60, 224, 150, 255))

    # 4) downscale with antialiasing
    return img.resize((BASE, BASE), Image.LANCZOS)


def _write_png(img: Image.Image) -> Path:
    master = OUT / "master.png"
    img.save(master)
    return master


def _write_icns(master: Path) -> Path | None:
    """Build .icns via macOS ``iconutil`` from an iconset of PNGs."""
    if sys.platform != "darwin":
        return None
    iconset = OUT / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    sizes = [16, 32, 128, 256, 512]
    with Image.open(master) as base:
        for size in sizes:
            base.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
            base.resize((size * 2, size * 2), Image.LANCZOS).save(
                iconset / f"icon_{size}x{size}@2x.png"
            )
    icns = OUT / "icon.icns"
    result = subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=False
    )
    if result.returncode == 0 and icns.exists():
        return icns
    return None


def _write_ico(master: Path) -> Path | None:
    try:
        img = Image.open(master)
        ico = OUT / "icon.ico"
        img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return ico
    except Exception:
        return None


def _write_svg() -> Path:
    """Hand-authored vector source, kept as the canonical design."""
    svg = OUT / "icon.svg"
    svg.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#09182e"/>
      <stop offset="1" stop-color="#123a60"/>
    </linearGradient>
    <linearGradient id="line" x1="0" y1="1" x2="1" y2="0">
      <stop offset="0" stop-color="#3ce096"/>
      <stop offset="1" stop-color="#2dd4bf"/>
    </linearGradient>
  </defs>
  <rect x="16" y="16" width="992" height="992" rx="224" fill="url(#bg)"/>
  <g fill="rgba(94,234,212,0.5)">
    <rect x="170" y="660" width="100" height="120" rx="28"/>
    <rect x="360" y="590" width="100" height="190" rx="28"/>
    <rect x="550" y="520" width="100" height="260" rx="28"/>
  </g>
  <polyline points="200,660 330,540 470,600 650,410 850,250"
    fill="none" stroke="url(#line)" stroke-width="66" stroke-linecap="round" stroke-linejoin="round"/>
  <polygon points="920,196 838,180 900,300" fill="#3ce096"/>
</svg>
""",
        encoding="utf-8",
    )
    return svg


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    img = build()
    master = _write_png(img)
    icns = _write_icns(master)
    ico = _write_ico(master)
    svg = _write_svg()
    print(f"master.png  -> {master}")
    print(f"icon.icns   -> {icns if icns else '(跳过,仅 macOS 生成,或已失败)'}")
    print(f"icon.ico    -> {ico if ico else '(跳过,ICO 生成失败)'}")
    print(f"icon.svg    -> {svg}")


if __name__ == "__main__":
    main()
