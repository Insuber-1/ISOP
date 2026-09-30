"""Build the ISOP 80 x 120 cm poster from the supplied latest PDF.

The two small screenshots in the source are covered by one larger, full-width
view using the screenshot supplied with the request. The rest of the source
PDF stays in place.
"""
from pathlib import Path
import pymupdf

BASE = Path("/mnt/c/Users/InsuberPC/Downloads/ISOP_Vinyl_Poster_Portrait_80x120cmd.pdf")
SCREENSHOT = Path("/mnt/c/Users/InsuberPC/AppData/Local/Temp/codex-clipboard-c0908355-e581-4a48-88ad-079abfeb4319.png")
OUTPUT = Path(__file__).resolve().parent / "ISOP_Poster_80x120cm.pdf"

# Source poster dimensions are 80 x 120 cm (2267.72 x 3401.57 PDF points).
# The screenshot row in the source PDF is x=99..2169, y=1633..2145 pt.
PANEL = pymupdf.Rect(99.2, 1618.0, 2168.5, 2140.0)
SCREEN_RECT = pymupdf.Rect(124.7, 1626.0, 2143.0, 2133.0)
PANEL_FILL = (0.9608, 0.9725, 0.9569)
PANEL_LINE = (0.7922, 0.8471, 0.8000)

if not BASE.is_file():
    raise FileNotFoundError(f"Base PDF not found: {BASE}")
if not SCREENSHOT.is_file():
    raise FileNotFoundError(f"Screenshot not found: {SCREENSHOT}")

poster = pymupdf.open(BASE)
if len(poster) != 1:
    raise ValueError("Expected a one-page poster PDF")
page = poster[0]
expected = (80 / 2.54 * 72, 120 / 2.54 * 72)
if abs(page.rect.width - expected[0]) > 0.1 or abs(page.rect.height - expected[1]) > 0.1:
    raise ValueError(f"Unexpected page size: {page.rect}")

# Cover the former two-column image area and its captions, then insert the
# attached view at the same near-4:1 ratio so it fills the available width.
page.draw_rect(PANEL, color=PANEL_LINE, fill=PANEL_FILL, width=1.1, overlay=True)
page.insert_image(SCREEN_RECT, filename=str(SCREENSHOT), keep_proportion=True, overlay=True)

poster.set_metadata({**poster.metadata,
                     "title": "ISOP Project Poster - 80 x 120 cm Portrait",
                     "subject": "Latest supplied poster with enlarged full-width application view"})
poster.save(OUTPUT, garbage=4, deflate=True)

# Verify the exported file retains the original print dimensions and the new image.
check = pymupdf.open(OUTPUT)
if len(check) != 1 or abs(check[0].rect.width - expected[0]) > 0.1 or abs(check[0].rect.height - expected[1]) > 0.1:
    raise ValueError("Exported poster has incorrect page count or print dimensions")
if not any((round(r.width) > 1900 and 1600 < r.y0 < 1700) for xref in [item[0] for item in check[0].get_images(full=True)] for r in check[0].get_image_rects(xref)):
    raise ValueError("Full-width screenshot was not embedded in the expected position")
print(f"Created: {OUTPUT}")
print(f"Page: {check[0].rect.width:.2f} x {check[0].rect.height:.2f} pt (80 x 120 cm)")
print(f"Images: {len(check[0].get_images(full=True))}; screenshot area: {SCREEN_RECT}")
