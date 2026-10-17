#!/usr/bin/env python3
"""
Build app.icns, the macOS application icon, from app.ico.

app.ico tops out at 256px, and the Dock and Finder ask for 1024 on a Retina
display, so upscaling it would visibly blur. The aperture is two flat colours,
though, which makes it recoverable: the blue coverage of the 256 frame is
upscaled as a *mask*, re-thresholded on a 4x supersampled canvas to get a hard
edge back, and resampled down once - straight slits and a round rim survive
that cleanly, where resizing the RGB directly would smear them.

The tile is redrawn rather than scaled, on Apple's icon grid: an 824px rounded
square centred on a 1024 canvas with a soft shadow, instead of a square that
runs to the edge. A full-bleed tile reads larger than every other icon in the
Dock. Proportions inside the tile are the .ico's own: the aperture spans
168/256 of it.

    python make_icns.py          # writes app.icns next to this file

Needs Pillow, and `iconutil`, which ships with macOS.
"""

import os
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))

BLUE = (31, 32, 241)         # brand blue, #1f20f1 - taken from the artwork
TILE = (245, 244, 241)       # warm-50, the same tile the .ico uses
CANVAS = 1024
TILE_PX = 824                # Apple's grid: 100px margin on a 1024 canvas
TILE_RADIUS = 185            # ~22.5% of the tile, the macOS squircle radius
APERTURE = 168 / 256         # aperture diameter as a fraction of the tile
SS = 4                       # supersampling factor for the re-thresholded edge


def aperture_mask(size):
    """The aperture's coverage, `size` px square, as an 8-bit mask."""
    ico = Image.open(os.path.join(HERE, "app.ico"))
    ico.size = (256, 256)
    ico.load()
    rgb = np.asarray(ico.convert("RGB")).astype(np.float32)
    # Red channel runs 245 on the tile to 31 on blue, so it is the coverage.
    cover = np.clip((TILE[0] - rgb[..., 0]) / (TILE[0] - BLUE[0]), 0, 1)
    ys, xs = np.nonzero(cover > 0.5)
    # Pad the crop: cutting at the half-coverage bbox shaves the outermost
    # row of a 168px circle, and at 3x that row is a visible flat on all four
    # sides. The padding keeps the partial rows so the threshold finds the
    # real rim, and is scaled back out below.
    pad = 3
    d = xs.max() + 1 - xs.min()
    box = (xs.min() - pad, ys.min() - pad, xs.max() + 1 + pad, ys.max() + 1 + pad)
    m = Image.fromarray((cover * 255).astype(np.uint8)).crop(box)
    full = round(size * (d + 2 * pad) / d) * SS
    big = m.resize((full, full), Image.BICUBIC)
    # Hard threshold at half coverage: the bicubic ramp crosses 0.5 where the
    # true edge is, so this recovers the geometry. The downsample below puts
    # the antialiasing back, at the right width for the final size.
    hard = np.asarray(big.point(lambda v: 255 if v >= 128 else 0))
    # The rim is drawn rather than trusted: intersecting with a true circle
    # leaves the slits, which run out through the rim, exactly as they are.
    o = (full - size * SS) // 2
    hard = hard[o:o + size * SS, o:o + size * SS]
    disc = Image.new("L", (size * SS, size * SS), 0)
    ImageDraw.Draw(disc).ellipse((0, 0, size * SS - 1, size * SS - 1), fill=255)
    out = np.minimum(hard, np.asarray(disc))
    return Image.fromarray(out).resize((size, size), Image.LANCZOS)


def render(size=CANVAS):
    s = size / CANVAS
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    tile_px = round(TILE_PX * s)
    off = (size - tile_px) // 2
    radius = round(TILE_RADIUS * s)

    # Shadow first: soft, slightly low, the way system icons sit.
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (off, off + round(12 * s), off + tile_px, off + tile_px + round(12 * s)),
        radius, fill=(0, 0, 0, 70))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14 * s)))

    # The tile, supersampled so its corners are as clean as the aperture.
    t = Image.new("L", (size * SS, size * SS), 0)
    ImageDraw.Draw(t).rounded_rectangle(
        (off * SS, off * SS, (off + tile_px) * SS, (off + tile_px) * SS),
        radius * SS, fill=255)
    t = t.resize((size, size), Image.LANCZOS)
    canvas.alpha_composite(Image.merge(
        "RGBA", [Image.new("L", (size, size), c) for c in TILE] + [t]))

    ap = round(tile_px * APERTURE)
    mask = aperture_mask(ap)
    blue = Image.new("RGBA", (ap, ap), BLUE + (255,))
    blue.putalpha(mask)
    canvas.alpha_composite(blue, ((size - ap) // 2, (size - ap) // 2))
    return canvas


def main():
    master = render()
    work = tempfile.mkdtemp()
    iconset = os.path.join(work, "app.iconset")
    os.makedirs(iconset)
    try:
        for base in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = base * scale
                name = "icon_%dx%d%s.png" % (base, base,
                                             "@2x" if scale == 2 else "")
                # Small sizes are rendered, not shrunk from 1024: the slits
                # would otherwise blur shut below 32px.
                img = master if px == CANVAS else (
                    render(px) if px <= 64 else
                    master.resize((px, px), Image.LANCZOS))
                img.save(os.path.join(iconset, name))
        out = os.path.join(HERE, "app.icns")
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out],
                       check=True)
        print("wrote", out)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
