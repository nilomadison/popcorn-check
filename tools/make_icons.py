"""Generate the Popcorn Check icon set (SVG master + PNG/ICO rasters).

The bucket geometry is defined once, in a 512x512 logical space, and emitted
both as SVG path data and as Pillow draw calls, so the vector and raster
icons can never drift apart.

Rasterizing needs Pillow, which is NOT a runtime dependency of the app --
this script is only run by hand when the artwork changes:

    uv run --with pillow tools/make_icons.py

Outputs land in static/ and are committed; the server just serves them.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static"

# Palette, lifted from the app's CSS custom properties in server.py.
BG_INNER = (36, 29, 23)      # #241d17  (--bg-grad inner, nudged up for tile life)
BG_OUTER = (14, 12, 10)      # #0e0c0a  (--bg)
TOMATO = (229, 72, 77)       # #e5484d  (--tomato)
TOMATO_DARK = (193, 57, 62)  # rim, for definition against the stripes
CREAM = (247, 239, 226)      # #f7efe2  stripe white
POP_LIGHT = (252, 237, 194)  # front kernels, warmed toward --butter
POP_SHADE = (228, 193, 128)  # back kernels, deeper butter for depth

S = 512  # logical coordinate space

# --- Bucket geometry -------------------------------------------------------
TOP_Y, BOT_Y = 206.0, 462.0
TOP_L, TOP_R = 92.0, 420.0
BOT_L, BOT_R = 146.0, 366.0
BOT_R_RADIUS = 24.0

RIM = (80.0, 186.0, 432.0, 232.0)  # x0, y0, x1, y1
RIM_RADIUS = 20.0

STRIPES = 7  # alternating tomato / cream, starting tomato at the left edge

# Popcorn puffs: (cx, cy, r, is_front). Back puffs are butter-shaded so the
# cluster reads as having depth instead of one flat blob.
PUFFS = [
    # Back: an unbroken row sitting on the rim, so no background can show
    # through the gaps between the front puffs...
    (118.0, 192.0, 31.0, False),
    (178.0, 190.0, 31.0, False),
    (238.0, 190.0, 31.0, False),
    (298.0, 188.0, 31.0, False),
    (356.0, 186.0, 31.0, False),
    (402.0, 192.0, 28.0, False),
    # ...plus two tucked into the upper notches, where the butter shading
    # peeks through and gives the cluster depth.
    (188.0, 122.0, 27.0, False),
    (352.0, 126.0, 25.0, False),
    # Front.
    (150.0, 166.0, 41.0, True),
    (232.0, 130.0, 47.0, True),
    (318.0, 158.0, 43.0, True),
    (388.0, 184.0, 35.0, True),
]

# One puff = a cluster of overlapping circles, as (dx, dy, dr) factors of r.
PUFF_LOBES = [
    (0.0, 0.0, 1.0),
    (-0.62, -0.42, 0.66),
    (0.60, -0.45, 0.62),
    (-0.60, 0.45, 0.60),
    (0.62, 0.44, 0.64),
    (0.0, -0.74, 0.55),
]


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def bucket_outline(steps: int = 8) -> list[tuple[float, float]]:
    """Tapered bucket silhouette, with the two bottom corners rounded."""
    r = BOT_R_RADIUS
    # Inset along the slanted sides, so the arc meets the side cleanly.
    lean_l = (BOT_L - TOP_L) / (BOT_Y - TOP_Y)
    lean_r = (TOP_R - BOT_R) / (BOT_Y - TOP_Y)

    pts: list[tuple[float, float]] = [(TOP_L, TOP_Y)]
    pts.append((BOT_L - lean_l * r, BOT_Y - r))
    cx, cy = BOT_L + r - lean_l * r, BOT_Y - r
    for i in range(steps + 1):  # bottom-left arc, 180deg -> 90deg
        a = math.pi - (math.pi / 2) * (i / steps)
        pts.append((cx + r * math.cos(a), cy - r * math.sin(a)))
    cx = BOT_R - r + lean_r * r
    for i in range(steps + 1):  # bottom-right arc, 90deg -> 0deg
        a = (math.pi / 2) * (1 - i / steps)
        pts.append((cx + r * math.cos(a), cy - r * math.sin(a)))
    pts.append((BOT_R + lean_r * r, BOT_Y - r))
    pts.append((TOP_R, TOP_Y))
    return pts


def stripe_quads() -> list[tuple[list[tuple[float, float]], bool]]:
    """Vertical stripes that taper with the bucket. True == tomato."""
    quads = []
    for i in range(STRIPES):
        t0, t1 = i / STRIPES, (i + 1) / STRIPES
        quads.append((
            [
                (_lerp(TOP_L, TOP_R, t0), TOP_Y - 4),
                (_lerp(TOP_L, TOP_R, t1), TOP_Y - 4),
                (_lerp(BOT_L, BOT_R, t1), BOT_Y + 4),
                (_lerp(BOT_L, BOT_R, t0), BOT_Y + 4),
            ],
            i % 2 == 0,
        ))
    return quads


def puff_circles(front: bool) -> list[tuple[float, float, float]]:
    out = []
    for cx, cy, r, is_front in PUFFS:
        if is_front != front:
            continue
        for dx, dy, dr in PUFF_LOBES:
            out.append((cx + dx * r, cy + dy * r, r * dr))
    return out


# --- Raster ----------------------------------------------------------------

def _hex(c: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % c


def render_art(px: int) -> Image.Image:
    """The bucket on transparency, supersampled then downsampled."""
    ss = 4
    n = px * ss
    k = n / S  # logical -> device

    def P(pts):
        return [(x * k, y * k) for x, y in pts]

    def ellipse(d, cx, cy, r, fill):
        d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=fill)

    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Popcorn, back layer then front, so the cluster overlaps the rim.
    for cx, cy, r in puff_circles(front=False):
        ellipse(d, cx, cy, r, POP_SHADE + (255,))
    for cx, cy, r in puff_circles(front=True):
        ellipse(d, cx, cy, r, POP_LIGHT + (255,))

    # Striped bucket body, drawn as full quads and clipped to the silhouette.
    body = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    for quad, is_tomato in stripe_quads():
        bd.polygon(P(quad), fill=(TOMATO if is_tomato else CREAM) + (255,))
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).polygon(P(bucket_outline()), fill=255)
    img.paste(body, (0, 0), mask)

    # Rim band, on top of both.
    x0, y0, x1, y1 = RIM
    d.rounded_rectangle(
        [x0 * k, y0 * k, x1 * k, y1 * k], radius=RIM_RADIUS * k, fill=TOMATO_DARK + (255,)
    )

    return img.resize((px, px), Image.LANCZOS)


def radial_bg(px: int, radius_frac: float = 0.95) -> Image.Image:
    """Warm radial falloff matching the app's --bg-grad."""
    lo = 64
    g = Image.new("RGB", (lo, lo))
    pix = g.load()
    c = (lo - 1) / 2
    span = radius_frac * lo
    for y in range(lo):
        for x in range(lo):
            t = min(1.0, math.hypot(x - c, y - c) / span)
            t = t * t * (3 - 2 * t)  # smoothstep
            pix[x, y] = tuple(
                round(_lerp(BG_INNER[i], BG_OUTER[i], t)) for i in range(3)
            )
    return g.resize((px, px), Image.BICUBIC)


def compose(px: int, content: float, rounded: bool) -> Image.Image:
    """Background tile + centered bucket occupying `content` of the width."""
    base = radial_bg(px).convert("RGBA")
    if rounded:
        m = Image.new("L", (px * 4, px * 4), 0)
        ImageDraw.Draw(m).rounded_rectangle(
            [0, 0, px * 4 - 1, px * 4 - 1], radius=int(px * 4 * 0.22), fill=255
        )
        base.putalpha(m.resize((px, px), Image.LANCZOS))

    art = render_art(px * 2)
    art = art.crop(art.getbbox())  # tight, so `content` is honestly the artwork
    w = max(1, round(px * content))
    h = max(1, round(w * art.height / art.width))
    art = art.resize((w, h), Image.LANCZOS)
    base.alpha_composite(art, ((px - w) // 2, (px - h) // 2))
    return base


# --- Vector ----------------------------------------------------------------

def art_bbox() -> tuple[float, float, float, float]:
    """Tight bounds of the bucket artwork, squared off around its centre."""
    xs, ys = [], []
    for cx, cy, r in puff_circles(True) + puff_circles(False):
        xs += [cx - r, cx + r]
        ys += [cy - r, cy + r]
    for x, y in bucket_outline():
        xs.append(x)
        ys.append(y)
    x0, y0, x1, y1 = RIM
    xs += [x0, x1]
    ys += [y0, y1]

    lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)
    side = max(hi_x - lo_x, hi_y - lo_y)
    cx, cy = (lo_x + hi_x) / 2, (lo_y + hi_y) / 2
    return cx - side / 2, cy - side / 2, side, side


def _artwork() -> str:
    """Popcorn + striped bucket + rim, in the shared 512-unit coordinates."""
    def path(pts):
        return "M %.1f %.1f " % pts[0] + " ".join(
            "L %.1f %.1f" % p for p in pts[1:]
        ) + " Z"

    puffs = []
    for colour, front in ((POP_SHADE, False), (POP_LIGHT, True)):
        for cx, cy, r in puff_circles(front):
            puffs.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{_hex(colour)}"/>'
            )

    stripes = ['<g clip-path="url(#bucket)">']
    for quad, is_tomato in stripe_quads():
        stripes.append(
            f'<path d="{path(quad)}" fill="{_hex(TOMATO if is_tomato else CREAM)}"/>'
        )
    stripes.append("</g>")

    x0, y0, x1, y1 = RIM
    rim = (
        f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" '
        f'rx="{RIM_RADIUS}" fill="{_hex(TOMATO_DARK)}"/>'
    )
    return "".join(puffs) + "".join(stripes) + rim


def _clip() -> str:
    d = "M %.1f %.1f " % bucket_outline()[0] + " ".join(
        "L %.1f %.1f" % p for p in bucket_outline()[1:]
    ) + " Z"
    return f'<clipPath id="bucket"><path d="{d}"/></clipPath>'


def build_mark_svg() -> str:
    """Tile-free crop of the same artwork, for the in-app header mark."""
    x, y, w, h = art_bbox()
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{x:.1f} {y:.1f} {w:.1f} {h:.1f}" width="64" height="64" '
        'role="img" aria-label="Popcorn Check">'
        f"<defs>{_clip()}</defs>{_artwork()}</svg>\n"
    )


def build_svg() -> str:
    bg = (
        '<defs><radialGradient id="bg" cx="50%" cy="34%" r="78%">'
        f'<stop offset="0" stop-color="{_hex(BG_INNER)}"/>'
        f'<stop offset="1" stop-color="{_hex(BG_OUTER)}"/>'
        "</radialGradient>"
        f"{_clip()}</defs>"
    )
    tile = f'<rect width="{S}" height="{S}" rx="{S * 0.22:.0f}" fill="url(#bg)"/>'

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {S} {S}" '
        'width="512" height="512" role="img" aria-label="Popcorn Check">'
        + bg + tile + _artwork() + "</svg>\n"
    )


def main() -> None:
    OUT.mkdir(exist_ok=True)

    (OUT / "icon.svg").write_text(build_svg(), encoding="utf-8")
    (OUT / "mark.svg").write_text(build_mark_svg(), encoding="utf-8")

    # Home screen / PWA. "any" gets rounded corners; maskable must bleed to
    # the edges and keep the artwork inside Android's 80% safe-zone circle.
    for px in (192, 512):
        compose(px, content=0.70, rounded=True).save(OUT / f"icon-{px}.png")
        compose(px, content=0.58, rounded=False).save(OUT / f"icon-maskable-{px}.png")

    # iOS composites onto its own rounded mask and dislikes transparency.
    compose(180, content=0.70, rounded=False).convert("RGB").save(
        OUT / "apple-touch-icon.png"
    )

    fav = compose(64, content=0.78, rounded=True)
    fav.resize((32, 32), Image.LANCZOS).save(OUT / "favicon-32.png")
    fav.save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])

    for f in sorted(OUT.iterdir()):
        print(f"{f.name:26} {f.stat().st_size:>7,} B")


if __name__ == "__main__":
    main()
