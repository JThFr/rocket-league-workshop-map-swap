#!/usr/bin/env python3
"""Generate the rpmap app icon (``assets/rpmap.png``) - a dev-only helper.

This is **not** needed to run or build rpmap (the PNG is committed); it exists so
the icon can be regenerated and tweaked. It needs Pillow, which the app itself
does not use:

    python3 -m pip install pillow
    python3 scripts/make_icon.py            # -> assets/rpmap.png (256x256)

The design is drawn on a 64px grid and upscaled with nearest-neighbour, so the
pixel art stays crisp at the usual icon sizes (16/32/64/128/256 all divide 64).
It is deliberately a *generic* hex-panelled sphere - original artwork that only
evokes the look, not the Rocket League trademark.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw

GRID = 64                     # design resolution (before nearest upscale)
CENTER = GRID / 2.0

# Palette (matches the web UI's green accent on a dark sphere).
RING = (46, 160, 67, 255)     # #2ea043
EMBLEM = (46, 160, 67, 255)
SEAM = (26, 33, 40, 255)
# Light "ball" shading, outermost band last.
SHADES = [(214, 222, 230, 255),   # highlight
          (184, 193, 202, 255),
          (146, 156, 166, 255),
          (108, 117, 127, 255)]   # rim


def _hexagon(hx: float, hy: float, s: float) -> list[tuple[float, float]]:
    return [(hx + s * math.cos(math.radians(60 * k - 90)),
             hy + s * math.sin(math.radians(60 * k - 90))) for k in range(6)]


def _shade_sphere(img: Image.Image) -> None:
    """Band the sphere by distance from an upper-left light, hard-edged (pixel art)."""
    px = img.load()
    lx, ly = CENTER - 13, CENTER - 13
    for y in range(GRID):
        for x in range(GRID):
            if px[x, y][3] == 0:
                continue
            t = math.hypot(x - lx, y - ly)
            px[x, y] = SHADES[0] if t < 8 else SHADES[1] if t < 16 \
                else SHADES[2] if t < 24 else SHADES[3]


def _hex_panels(img: Image.Image, side: float, width: int) -> None:
    """Draw a honeycomb of seams, clipped to the sphere."""
    mask = Image.new("L", (GRID, GRID), 0)
    ImageDraw.Draw(mask).ellipse([CENTER - 25, CENTER - 25, CENTER + 25, CENTER + 25],
                                 fill=255)
    layer = Image.new("RGBA", (GRID, GRID), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    step_x, step_y = math.sqrt(3) * side, 1.5 * side
    for j in range(-3, 4):
        offset = (step_x / 2) if j % 2 else 0
        for i in range(-3, 4):
            pts = _hexagon(CENTER + i * step_x + offset, CENTER + j * step_y, side)
            draw.line(pts + [pts[0]], fill=SEAM, width=width, joint="curve")
    img.alpha_composite(Image.composite(
        layer, Image.new("RGBA", (GRID, GRID), (0, 0, 0, 0)), mask))


def _emblem(img: Image.Image, radius: int = 6) -> None:
    """The small green 'core' in the middle."""
    draw = ImageDraw.Draw(img)
    c = CENTER
    draw.ellipse([c - radius, c - radius, c + radius, c + radius], fill=EMBLEM)
    draw.ellipse([c - radius + 2, c - radius + 2, c + radius - 2, c + radius - 2],
                 fill=(14, 20, 26, 255))
    draw.ellipse([c - 1, c - 1, c + 1, c + 1], fill=EMBLEM)


def build() -> Image.Image:
    img = Image.new("RGBA", (GRID, GRID), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Green ring with a transparent gap, then the sphere.
    draw.ellipse([CENTER - 30, CENTER - 30, CENTER + 30, CENTER + 30], fill=RING)
    draw.ellipse([CENTER - 26, CENTER - 26, CENTER + 26, CENTER + 26], fill=(0, 0, 0, 0))
    draw.ellipse([CENTER - 25, CENTER - 25, CENTER + 25, CENTER + 25], fill=SHADES[1])
    _shade_sphere(img)
    _hex_panels(img, side=11.5, width=2)
    _emblem(img)
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", type=int, default=256, help="output size (default 256)")
    ap.add_argument("--out", default="assets/rpmap.png", help="output PNG path")
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    build().resize((args.size, args.size), Image.NEAREST).save(out)
    print(f"wrote {out} ({args.size}x{args.size})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
