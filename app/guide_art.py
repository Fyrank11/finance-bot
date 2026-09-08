"""Render exact, public instruction copy as phone-friendly Telegram PNG cards.

This is a code-native UI renderer, not a generated illustration. Fonts ship
with the existing matplotlib dependency; no network or user data is involved.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path

import matplotlib
from PIL import Image, ImageDraw, ImageFont

from .guide_catalog import GUIDE_CATALOG


WIDTH, HEIGHT = 1080, 1440
NAVY = "#0C1625"
PANEL = "#142437"
MINT = "#8CF0CD"
WHITE = "#F4F8FC"
MUTED = "#B8C8D9"
LINE = "#284057"
FONTS = Path(matplotlib.get_data_path()) / "fonts" / "ttf"


@dataclass(frozen=True)
class TextRegion:
    name: str
    x: int
    y: int
    lines: tuple[str, ...]
    size: int
    line_height: int
    color: str = WHITE
    bold: bool = False


@dataclass(frozen=True)
class CardLayout:
    panels: tuple[tuple[int, int, int, int], ...]
    text: tuple[TextRegion, ...]
    note_top: int


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    # Keep FreeType instances local to a render so simultaneous worker threads
    # never share native drawing state.
    return ImageFont.truetype(str(FONTS / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)


def _wrap(text: str, size: int, width: int, *, bold: bool = False) -> tuple[str, ...]:
    font = _font(size, bold)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            if font.getlength(word) > width:
                raise ValueError(f"Guide contains a word too wide for its text box: {word!r}")
            candidate = f"{line} {word}" if line else word
            if font.getlength(candidate) <= width:
                line = candidate
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return tuple(lines)


def _layout(slug: str) -> CardLayout:
    guide = GUIDE_CATALOG[slug]
    title = _wrap(guide["title"], 58, 748, bold=True)
    if len(title) > 2:
        raise ValueError(f"Guide title needs more than two lines: {slug}")
    regions = [
        TextRegion("brand", 68, 50, ("МОЙ ФИНАНСОВЫЙ СОВЕТНИК",), 23, 30, MINT, True),
        TextRegion("title", 68, 115, title, 58, 70, WHITE, True),
        TextRegion("group", 68, 275, (guide["group"],), 26, 34, MUTED),
    ]
    panels = []
    top = 337
    for index, (label, key) in enumerate((("Зачем", "purpose"), ("Что указать", "inputs"), ("Что получится", "result")), 1):
        lines = _wrap(guide[key], 38, 792)
        height = 83 + len(lines) * 49
        panels.append((68, top, 1012, top + height))
        regions.append(TextRegion(f"number_{index}", 95, top + 24, (str(index),), 27, 34, NAVY, True))
        regions.append(TextRegion(f"{key}_label", 158, top + 22, (label,), 28, 36, MINT, True))
        regions.append(TextRegion(key, 158, top + 68, lines, 38, 49))
        top += height + 19
    note_top = top + 5
    note = _wrap(guide["note"], 36, 914)
    regions.append(TextRegion("note_label", 83, note_top + 9, ("ВАЖНО УЧЕСТЬ",), 24, 32, MINT, True))
    regions.append(TextRegion("note", 83, note_top + 52, note, 36, 46, MUTED))
    bottom = note_top + 52 + len(note) * 46
    if guide.get("example"):
        example = _wrap(guide["example"], 34, 914)
        regions.append(TextRegion("example", 83, bottom + 19, example, 34, 43, WHITE))
        bottom += 19 + len(example) * 43
    if bottom > HEIGHT - 84:
        raise ValueError(f"Guide copy overflows the phone card: {slug} ({bottom}px)")
    regions.append(TextRegion("footer", 68, HEIGHT - 49, ("Справка рядом · «Как это работает»",), 23, 29, MUTED))
    return CardLayout(tuple(panels), tuple(regions), note_top)


def guide_layout(slug: str) -> dict:
    """Expose actual text boxes for layout validation and accessible-copy QA."""
    layout = _layout(slug)
    probe = ImageDraw.Draw(Image.new("RGB", (WIDTH, HEIGHT)))
    boxes = []
    for region in layout.text:
        font = _font(region.size, region.bold)
        for index, line in enumerate(region.lines):
            xy = (region.x, region.y + index * region.line_height)
            box = probe.textbbox(xy, line, font=font, anchor="lt")
            boxes.append({"name": region.name, "text": line, "bounds": box, "font_size": region.size})
    return {"width": WIDTH, "height": HEIGHT, "panels": layout.panels, "text_regions": boxes}


def _icon(draw: ImageDraw.ImageDraw, slug: str) -> None:
    """Small topic-specific line symbols with no platform emoji dependence."""
    x, y = 864, 128
    color, stroke = MINT, 7

    def line(points, fill=color, width=stroke):
        draw.line([(x + px, y + py) for px, py in points], fill=fill, width=width, joint="curve")

    def rect(box, radius=12, fill=None):
        draw.rounded_rectangle(tuple(v + (x if i % 2 == 0 else y) for i, v in enumerate(box)),
                               radius=radius, fill=fill, outline=color, width=stroke)

    def circle(cx, cy, radius, fill=None):
        draw.ellipse((x + cx - radius, y + cy - radius, x + cx + radius, y + cy + radius),
                     fill=fill, outline=color, width=stroke)

    def arrow(a, b):
        line([a, b])
        dx, dy = b[0] - a[0], b[1] - a[1]
        norm = max((dx * dx + dy * dy) ** .5, 1)
        ux, uy = dx / norm, dy / norm
        line([(b[0] - ux * 19 - uy * 15, b[1] - uy * 19 + ux * 15), b,
              (b[0] - ux * 19 + uy * 15, b[1] - uy * 19 - ux * 15)])

    if slug in {"income", "expense"}:
        line([(8, 83), (8, 111), (124, 111), (124, 83)])
        arrow((66, 8), (66, 81)) if slug == "income" else arrow((66, 89), (66, 10))
    elif slug in {"budget", "opening"}:
        rect((6, 26, 126, 113))
        rect((84, 52, 127, 87), 8)
        if slug == "opening":
            line([(30, 60), (65, 60)])
            line([(48, 42), (48, 79)])
        else:
            line([(23, 10), (98, 10), (98, 24)])
            circle(105, 69, 3, color)
    elif slug == "history":
        circle(65, 65, 54)
        line([(65, 27), (65, 66), (94, 80)])
    elif slug == "limits":
        draw.arc((x + 7, y + 13, x + 123, y + 129), 180, 360, fill=color, width=stroke)
        line([(6, 73), (124, 73)])
        arrow((66, 73), (101, 32))
        line([(28, 109), (104, 109)])
    elif slug == "export":
        line([(82, 8), (22, 8), (22, 120), (112, 120), (112, 40), (82, 8), (82, 40), (112, 40)])
        arrow((66, 58), (66, 100))
    elif slug in {"payments", "month"}:
        rect((7, 24, 126, 120))
        line([(8, 51), (124, 51)])
        line([(36, 8), (36, 36)])
        line([(98, 8), (98, 36)])
        if slug == "payments":
            line([(38, 86), (58, 102), (95, 69)])
        else:
            for cx in (35, 68, 99):
                for cy in (76, 102):
                    circle(cx, cy, 4, color)
    elif slug == "analytics":
        line([(8, 13), (8, 121), (126, 121)])
        for left, top in ((26, 81), (63, 51), (100, 14)):
            rect((left, top, left + 16, 119), 3, PANEL)
    elif slug == "tips":
        draw.arc((x + 27, y + 7, x + 103, y + 91), 145, 395, fill=color, width=stroke)
        line([(34, 71), (47, 94), (85, 94), (97, 71)])
        line([(49, 108), (83, 108)])
        line([(54, 123), (78, 123)])
    elif slug == "savings":
        line([(67, 118), (67, 40)])
        draw.ellipse((x + 9, y + 35, x + 65, y + 70), outline=color, width=stroke)
        draw.ellipse((x + 68, y + 8, x + 124, y + 43), outline=color, width=stroke)
        line([(22, 119), (111, 119)])
    elif slug == "goals":
        circle(60, 72, 49)
        circle(60, 72, 27)
        arrow((122, 7), (60, 72))
    elif slug == "reserve":
        circle(65, 65, 57)
        circle(65, 65, 27)
        for a, b in (((24, 24), (46, 46)), ((85, 85), (106, 106)), ((24, 106), (46, 85)), ((85, 46), (106, 24))):
            line([a, b])
    elif slug == "savings_budget":
        rect((22, 6, 110, 126))
        rect((36, 20, 96, 50), 3)
        for cx in (42, 68, 94):
            for cy in (75, 102):
                circle(cx, cy, 4, color)
    elif slug == "forecast":
        line([(9, 10), (9, 119), (124, 119)])
        line([(22, 81), (49, 55), (76, 77)])
        arrow((76, 77), (118, 23))
    elif slug == "weekly":
        rect((7, 25, 125, 111))
        line([(9, 31), (66, 78), (123, 31)])
        line([(9, 107), (45, 68)])
        line([(123, 107), (89, 68)])
    elif slug == "search":
        circle(53, 51, 40)
        line([(82, 83), (124, 123)], width=9)
    elif slug == "family":
        circle(66, 30, 19)
        circle(21, 49, 14)
        circle(111, 49, 14)
        draw.arc((x + 35, y + 66, x + 97, y + 154), 180, 360, fill=color, width=stroke)
        draw.arc((x + 2, y + 82, x + 41, y + 143), 180, 290, fill=color, width=stroke)
        draw.arc((x + 91, y + 82, x + 130, y + 143), 250, 360, fill=color, width=stroke)
    elif slug == "settings":
        for cy, cx in ((27, 41), (66, 93), (105, 55)):
            line([(10, cy), (123, cy)])
            circle(cx, cy, 12, NAVY)
    elif slug == "debts":
        arrow((11, 37), (122, 37))
        arrow((122, 93), (11, 93))
    elif slug == "afford":
        line([(11, 14), (69, 14), (123, 69), (67, 125), (11, 69), (11, 14)])
        circle(34, 36, 6)
        line([(58, 58), (86, 86)])
        line([(58, 86), (86, 58)])
    elif slug == "credit":
        rect((6, 25, 126, 110))
        line([(8, 52), (124, 52)], width=14)
        line([(24, 89), (54, 89)])
    else:
        raise KeyError(f"No instruction icon for {slug!r}")


@lru_cache(maxsize=32)
def render_guide(slug: str) -> bytes:
    """Return a cached PNG of one catalog entry; reject arbitrary user text."""
    layout = _layout(slug)
    canvas = Image.new("RGB", (WIDTH, HEIGHT), NAVY)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((68, 98, 149, 103), radius=3, fill=MINT)
    _icon(draw, slug)
    for index, box in enumerate(layout.panels):
        draw.rounded_rectangle(box, radius=24, fill=PANEL)
        draw.rounded_rectangle((86, box[1] + 17, 127, box[1] + 59), radius=12, fill=MINT)
    draw.line((68, layout.note_top - 5, 1012, layout.note_top - 5), fill=LINE, width=2)
    for region in layout.text:
        font = _font(region.size, region.bold)
        for index, line in enumerate(region.lines):
            draw.text((region.x, region.y + index * region.line_height), line, fill=region.color,
                      font=font, anchor="lt")
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Render public guide cards for visual review")
    parser.add_argument("directory", type=Path, help="Local output directory (e.g. /tmp/finance-guide-review)")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    thumbs = []
    for topic in GUIDE_CATALOG:
        content = render_guide(topic)
        (args.directory / f"{topic}.png").write_bytes(content)
        with Image.open(BytesIO(content)) as card:
            thumbs.append(card.resize((270, 360), Image.Resampling.LANCZOS))
    columns = 4
    rows = (len(thumbs) + columns - 1) // columns
    gallery = Image.new("RGB", (columns * 270, rows * 360), NAVY)
    for index, thumb in enumerate(thumbs):
        gallery.paste(thumb, ((index % columns) * 270, (index // columns) * 360))
    gallery.save(args.directory / "gallery.jpg", quality=93)
    print(args.directory / "gallery.jpg")
