"""On-screen graphics drawn with Pillow: text cards for TEXT: cues, placeholders for missing screen recordings,
and the end-screen card. Numbers and money stand out in the accent colour, because that's what makes
viewers stop and read."""
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core.config import ROOT

FONT_BODY = ROOT / "assets" / "fonts" / "Montserrat.ttf"
HIGHLIGHT_RE = re.compile(r"[\d$%£€]")


def font(size: int, weight: str = "ExtraBold") -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT_BODY), size)
    f.set_variation_by_name(weight)
    return f


def hex_rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    h = color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha


def wrap(text: str, f: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if current and f.getlength(trial) > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    return lines + ([current] if current else [])


def fit(text: str, max_width: int, max_lines: int, sizes: range, weight: str = "ExtraBold"):
    """Largest font size at which the text fits in `max_lines` lines."""
    for size in sizes:
        f = font(size, weight)
        lines = wrap(text, f, max_width)
        if len(lines) <= max_lines:
            return f, lines
    f = font(sizes[-1], weight)
    return f, wrap(text, f, max_width)


def draw_words(draw: ImageDraw.ImageDraw, lines: list[str], f, center_x: int, top: int, gap: int,
               color, accent) -> None:
    """Centered lines, drawing words with digits, $ or % in the accent colour."""
    space = f.getlength(" ")
    for row, line in enumerate(lines):
        words = line.split()
        x = center_x - f.getlength(line) / 2
        y = top + row * (f.size + gap)
        for word in words:
            draw.text((x, y), word, font=f, fill=accent if HIGHLIGHT_RE.search(word) else color)
            x += f.getlength(word) + space


def text_card(text: str, out: Path, size: tuple[int, int], style: dict) -> Path:
    """Transparent overlay: dark rounded panel with big text, laid over blurred footage by the assembler."""
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    text = text.strip().strip('"').upper()
    width_share = 0.72 if w > h else 0.78  # vertical Shorts need a wider share of the narrow frame
    f, lines = fit(text, int(w * width_share), 3 if w > h else 4, range(round(min(w, h) * 0.12), round(min(w, h) * 0.045), -4))
    gap = round(f.size * 0.18)
    block_h = len(lines) * f.size + (len(lines) - 1) * gap
    block_w = max(f.getlength(line) for line in lines)
    pad_x, pad_y = round(min(w, h) * 0.075), round(min(w, h) * 0.06)  # room for the accent bar on the left
    cy = h * (0.36 if h > w else 0.46)  # sit above the captions, which run along the lower part of the frame
    box = (w / 2 - block_w / 2 - pad_x, cy - block_h / 2 - pad_y, w / 2 + block_w / 2 + pad_x, cy + block_h / 2 + pad_y)
    radius = round(min(w, h) * 0.03)
    draw.rounded_rectangle(box, radius=radius, fill=hex_rgba(style["panel"], 215))
    bar = round(min(w, h) * 0.009)
    draw.rounded_rectangle((box[0] + radius, box[1] + pad_y * 0.7, box[0] + radius + bar, box[3] - pad_y * 0.7),
                           radius=bar // 2, fill=hex_rgba(style["accent"]))
    draw_words(draw, lines, f, w // 2, round(cy - block_h / 2 - f.size * 0.12), gap,
               (255, 255, 255, 255), hex_rgba(style["accent"]))
    img.save(out)
    return out


def placeholder_card(description: str, number: int, out: Path, size: tuple[int, int], style: dict) -> Path:
    """Opaque card shown where a screen recording is missing, so the draft video is still reviewable."""
    w, h = size
    img = Image.new("RGBA", size, hex_rgba(style["panel"]))
    draw = ImageDraw.Draw(img)
    m = min(w, h)
    label, title = fit(f"SCREEN RECORDING #{number} NEEDED", int(w * 0.85), 2, range(round(m * 0.06), round(m * 0.03), -2))
    draw_words(draw, title, label, w // 2, round(h * 0.28), 10, hex_rgba(style["accent"]), hex_rgba(style["accent"]))
    f, lines = fit(description, int(w * 0.8), 4, range(round(m * 0.055), round(m * 0.03), -2), "SemiBold")
    draw_words(draw, lines, f, w // 2, round(h * 0.45), round(f.size * 0.25), (230, 230, 230, 255), (230, 230, 230, 255))
    hint, note = fit(f"Save it as screen/{number:02d}.mp4 in this video's folder and render again",
                     int(w * 0.85), 2, range(round(m * 0.03), round(m * 0.02), -1), "Medium")
    draw_words(draw, note, hint, w // 2, round(h * 0.78), 6, (160, 160, 160, 255), (160, 160, 160, 255))
    img.save(out)
    return out


def end_card(out: Path, size: tuple[int, int], style: dict) -> Path:
    """Overlay for the end-screen seconds. The left two thirds stay clear for YouTube's end-screen elements."""
    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    f = font(round(h * 0.075))
    text = "WATCH THIS NEXT"
    draw.text(((w - f.getlength(text)) / 2, h * 0.12), text, font=f, fill=(255, 255, 255, 255))
    bar_w = round(f.getlength(text) * 0.4)
    draw.rectangle((w / 2 - bar_w / 2, h * 0.12 + f.size * 1.35, w / 2 + bar_w / 2, h * 0.12 + f.size * 1.35 + 8),
                   fill=hex_rgba(style["accent"]))
    img.save(out)
    return out
