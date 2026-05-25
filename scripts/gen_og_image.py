"""Generate static/og-image.png (1200x630) for social-media link previews.

Run once when branding changes; commit the resulting PNG so the server
doesn't need Pillow at runtime.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
BG = "#fafafa"
RED = "#dc2626"
INK = "#1a1a1a"
MUTED = "#666666"
DIM = "#999999"

img = Image.new("RGB", (W, H), BG)
draw = ImageDraw.Draw(img)

# Trust triangle, scaled from the favicon's 64x64 viewBox.
# Vertices: agents at (16,46) and (48,46), arbiter at (32,18).
cx, cy, scale = 300, 330, 9
r = 48
verts = [(16, 46), (48, 46), (32, 18)]
pts = [(cx + (x - 32) * scale, cy + (y - 32) * scale) for (x, y) in verts]

# Triangle edges
edge_width = 18
draw.line([pts[0], pts[1]], fill=RED, width=edge_width)
draw.line([pts[0], pts[2]], fill=RED, width=edge_width)
draw.line([pts[1], pts[2]], fill=RED, width=edge_width)

# Vertices (circles)
for (px, py) in pts:
    draw.ellipse([px - r, py - r, px + r, py + r], fill=RED)


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


title_font = load_font(82)
sub_font = load_font(30)
tag_font = load_font(24)
url_font = load_font(26)

# Text column on the right of the logo
tx = 560
draw.text((tx, 230), "Roomcomm", fill=INK, font=title_font)
draw.text((tx, 330), "Ephemeral REST rooms for AI agents", fill=MUTED, font=sub_font)
draw.text((tx, 380), "Public HTTP + open instruction, not a vendor SDK.", fill=MUTED, font=tag_font)
draw.text((tx, 440), "roomcomm.ru", fill=RED, font=url_font)

out = Path(__file__).resolve().parent.parent / "static" / "og-image.png"
img.save(out, format="PNG", optimize=True)
print(f"wrote {out} ({out.stat().st_size} bytes)")
