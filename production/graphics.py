"""Animated explainer graphics drawn from the script itself, for things stock footage can't show:

  CHAT: user: <prompt> | ai: <reply>   an AI chat window: the prompt types out, "..." then the reply appears
  CODE: <line> | <line> | ...          a terminal typing lines; "✓" lines turn green, "✗"/"error" lines red
  LIST: <item> | <item> | ...          a checklist ticking off one item at a time

Each graphic is a short PNG sequence (transparent, 15 fps) laid over blurred footage by the assembler.
The animation fills the first part of the line and then holds, so the result stays readable while the
narrator finishes the sentence.
"""
import re
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core.config import ROOT
from production.cards import fit, font, hex_rgba, wrap

MONO = ROOT / "assets" / "fonts" / "JetBrainsMono.ttf"
FPS = 15
GREEN, RED, GREY, WHITE = (63, 185, 80, 255), (248, 81, 73, 255), (139, 148, 158, 255), (240, 246, 252, 255)
WINDOW_BG, BAR_BG = (13, 17, 23, 245), (22, 27, 34, 255)


def mono(size: int, weight: str = "Medium") -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(MONO), size)
    f.set_variation_by_name(weight)
    return f


def parts(cue: str) -> list[str]:
    return [p.strip() for p in cue.split("|") if p.strip()]


def parse_chat(cue: str) -> tuple[str, str]:
    user = re.search(r"user\s*:\s*(.*?)(?:\|\s*ai\s*:|$)", cue, re.I | re.S)
    ai = re.search(r"\|\s*ai\s*:\s*(.*)$", cue, re.I | re.S)
    return (user.group(1).strip() if user else cue.strip()), (ai.group(1).strip() if ai else "")


def ease(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


class Canvas:
    """Geometry shared by all graphics: a centred window sized for the frame, above the captions."""

    def __init__(self, size: tuple[int, int], style: dict):
        self.w, self.h = size
        self.vertical = self.h > self.w
        self.m = min(size)
        self.style = style
        self.accent = hex_rgba(style["accent"])
        self.win_w = round(self.w * (0.88 if self.vertical else 0.70))
        self.cx = self.w // 2
        self.cy = round(self.h * (0.38 if self.vertical else 0.44))
        self.pad = round(self.m * 0.035)

    def window(self, draw: ImageDraw.ImageDraw, height: int, title: str, rise: float) -> tuple[int, int]:
        """Rounded app window with a title bar. `rise` (0-1) slides it up into place. Returns the content origin."""
        top = self.cy - height // 2 + round((1 - ease(rise)) * self.m * 0.06)
        left = self.cx - self.win_w // 2
        r = round(self.m * 0.022)
        bar = round(self.m * 0.055)
        draw.rounded_rectangle((left, top, left + self.win_w, top + height), radius=r, fill=WINDOW_BG,
                               outline=(48, 54, 61, 255), width=2)
        draw.rounded_rectangle((left, top, left + self.win_w, top + bar), radius=r, fill=BAR_BG)
        draw.rectangle((left, top + bar - r, left + self.win_w, top + bar), fill=BAR_BG)
        for i, color in enumerate(((255, 95, 86, 255), (255, 189, 46, 255), (39, 201, 63, 255))):
            x = left + self.pad + i * round(bar * 0.45)
            d = round(bar * 0.26)
            draw.ellipse((x, top + bar / 2 - d / 2, x + d, top + bar / 2 + d / 2), fill=color)
        label = font(round(bar * 0.36), "SemiBold")
        draw.text((self.cx - label.getlength(title) / 2, top + bar / 2 - label.size * 0.6), title, font=label, fill=GREY)
        return left + self.pad, top + bar + self.pad


def chat_frames(cue: str, canvas: Canvas, seconds: float) -> list[Image.Image]:
    user, reply = parse_chat(cue)
    c = canvas
    f = font(round(c.m * (0.062 if c.vertical else 0.051)), "SemiBold")
    bubble_w = round(c.win_w * 0.78)
    user_lines = wrap(user, f, bubble_w - 2 * c.pad)
    ai_lines = wrap(reply, f, bubble_w - 2 * c.pad) if reply else []
    line_h = round(f.size * 1.35)
    user_h = len(user_lines) * line_h + c.pad
    ai_h = (len(ai_lines) * line_h + c.pad) if ai_lines else 0
    height = round(c.m * 0.055) + 3 * c.pad + user_h + (c.pad + ai_h if ai_lines else 0)
    n = max(1, round(seconds * FPS))
    user_chars, ai_words = len(user), len(reply.split())
    frames = []
    for i in range(n):
        p = (i + 1) / n
        img = Image.new("RGBA", (c.w, c.h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        x0, y0 = c.window(d, height, "EXAMPLE · AI conversation", min(1, p / 0.12))
        right = x0 + c.win_w - 2 * c.pad
        # User prompt types out over the first 40%, then "..." while the AI "thinks", then the reply streams in.
        typed = user[:round(user_chars * min(1, max(0, (p - 0.05) / 0.35)))]
        shown = wrap(typed, f, bubble_w - 2 * c.pad) or [""]
        bw = max(f.getlength(t) for t in user_lines) + 2 * c.pad
        d.rounded_rectangle((right - bw, y0, right, y0 + user_h), radius=round(c.pad * 0.8), fill=c.accent)
        for k, text in enumerate(shown):
            d.text((right - bw + c.pad, y0 + c.pad / 2 + k * line_h), text, font=f, fill=(14, 14, 20, 255))
        if ai_lines and p > 0.45:
            ay = y0 + user_h + c.pad
            if p < 0.58:  # typing indicator
                for k in range(3):
                    r = round(f.size * 0.16)
                    on = int((i // 3) % 3 == k)
                    x = x0 + c.pad + k * r * 3
                    d.ellipse((x, ay + c.pad, x + 2 * r, ay + c.pad + 2 * r), fill=WHITE if on else GREY)
            else:
                count = round(ai_words * min(1, (p - 0.58) / 0.32))
                streamed = wrap(" ".join(reply.split()[:count]), f, bubble_w - 2 * c.pad)
                aw = max(f.getlength(t) for t in ai_lines) + 2 * c.pad
                d.rounded_rectangle((x0, ay, x0 + aw, ay + ai_h), radius=round(c.pad * 0.8), fill=(48, 54, 61, 255))
                for k, text in enumerate(streamed):
                    d.text((x0 + c.pad, ay + c.pad / 2 + k * line_h), text, font=f, fill=WHITE)
        frames.append(img)
    return frames


def code_frames(cue: str, canvas: Canvas, seconds: float) -> list[Image.Image]:
    c = canvas
    rows = parts(cue)[:6] or [cue]
    f = mono(round(c.m * (0.050 if c.vertical else 0.044)))
    max_chars = max(10, int((c.win_w - 2 * c.pad) / f.getlength("M")) - 4)
    shown_rows = []  # (text, color, prompt, mark); colour comes from the original line so wrapped parts keep it
    for row in rows:
        stripped = row.lstrip()
        color = GREEN if stripped.startswith("✓") else RED if stripped.startswith("✗") or "error" in row.lower() \
            else GREY if stripped.startswith("#") else WHITE
        mark = stripped[:1] if stripped[:1] in "✓✗" else ""
        body = (" " + stripped[1:].lstrip()) if mark else row
        for j, piece in enumerate(textwrap.wrap(body, max_chars, subsequent_indent="  ") or [""]):
            shown_rows.append(((" " + piece) if mark and j == 0 else piece, color,
                               "$ " if color == WHITE and j == 0 else "", mark if j == 0 else ""))
    line_h = round(f.size * 1.5)
    height = round(c.m * 0.055) + 2 * c.pad + len(shown_rows) * line_h + c.pad
    total = sum(len(r[0]) for r in shown_rows)
    n = max(1, round(seconds * FPS))
    frames = []
    for i in range(n):
        p = (i + 1) / n
        img = Image.new("RGBA", (c.w, c.h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        x0, y0 = c.window(d, height, "EXAMPLE · terminal", min(1, p / 0.12))
        budget = round(total * min(1, max(0, (p - 0.08) / 0.85)))
        cursor = None
        for k, (row, color, prompt, mark) in enumerate(shown_rows):
            if budget <= 0:
                break
            text = row[:budget]
            budget -= len(row)
            y = y0 + k * line_h
            if prompt:
                d.text((x0, y), prompt, font=f, fill=c.accent)
            if mark:  # drawn as shapes: the font's variable instances lack these glyphs
                s, mx, my = f.size * 0.7, x0 + f.size * 0.05, y + f.size * 0.35
                points = ([(mx, my + s * 0.5), (mx + s * 0.35, my + s * 0.85), (mx + s, my + s * 0.1)] if mark == "✓"
                          else [(mx, my), (mx + s, my + s)])
                d.line(points, fill=color, width=max(3, f.size // 9), joint="curve")
                if mark == "✗":
                    d.line([(mx + s, my), (mx, my + s)], fill=color, width=max(3, f.size // 9))
            d.text((x0 + f.getlength(prompt), y), text, font=f, fill=color)
            cursor = (x0 + f.getlength(prompt + text) + 4, y)
        typing_done = budget >= 0 and p > 0.93
        if cursor and (not typing_done or (i // 4) % 2 == 0):  # solid while typing, blinking when done
            d.rectangle((cursor[0], cursor[1] + f.size * 0.1, cursor[0] + f.size * 0.5, cursor[1] + f.size * 1.1), fill=WHITE)
        frames.append(img)
    return frames


def list_frames(cue: str, canvas: Canvas, seconds: float) -> list[Image.Image]:
    c = canvas
    items = parts(cue)[:5] or [cue]
    f = font(round(c.m * (0.068 if c.vertical else 0.064)), "Bold")
    box = round(f.size * 0.95)
    gap = round(f.size * 0.75)
    text_w = c.win_w - 3 * c.pad - box
    wrapped = [wrap(item, f, text_w) for item in items]
    line_h = round(f.size * 1.2)
    item_h = [max(box, len(w) * line_h) for w in wrapped]
    height = 2 * c.pad + sum(item_h) + gap * (len(items) - 1)
    n = max(1, round(seconds * FPS))
    frames = []
    for i in range(n):
        p = (i + 1) / n
        img = Image.new("RGBA", (c.w, c.h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        top = c.cy - height // 2
        left = c.cx - c.win_w // 2
        d.rounded_rectangle((left, top, left + c.win_w, top + height), radius=round(c.m * 0.022),
                            fill=hex_rgba(c.style["panel"], 225))
        y = top + c.pad
        for k, (lines, h) in enumerate(zip(wrapped, item_h)):
            start = 0.05 + k * 0.85 / len(items)  # each item gets its slot: appear, then tick
            local = (p - start) / (0.85 / len(items))
            if local <= 0:
                break
            shift = round((1 - ease(local * 2)) * c.m * 0.03)
            bx, by = left + c.pad + shift, y + (h - box) // 2
            done = local > 0.55
            d.rounded_rectangle((bx, by, bx + box, by + box), radius=round(box * 0.2),
                                outline=c.accent, width=max(3, box // 10), fill=c.accent if done else None)
            if done:  # tick drawn as a polyline so it works in any font
                d.line([(bx + box * 0.22, by + box * 0.52), (bx + box * 0.43, by + box * 0.72),
                        (bx + box * 0.78, by + box * 0.30)], fill=(14, 14, 20, 255), width=max(4, box // 7), joint="curve")
            for j, text in enumerate(lines):
                d.text((bx + box + c.pad, y + (h - len(lines) * line_h) // 2 + j * line_h - f.size * 0.1), text, font=f,
                       fill=WHITE if done else (200, 200, 200, 255))
            y += h + gap
        frames.append(img)
    return frames


def studio_background(out: Path, size: tuple[int, int], style: dict) -> Path:
    """A quiet, deterministic backdrop. Explanations need no unrelated stock footage."""
    w, h = size
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        p = y / max(1, h - 1)
        d.line((0, y, w, y), fill=(round(18 - p * 6), round(35 - p * 15), round(53 - p * 18)))
    grid = max(32, min(size) // 10)
    for x in range(0, w, grid):
        d.line((x, 0, x, h), fill=(26, 43, 60))
    for y in range(0, h, grid):
        d.line((0, y, w, y), fill=(26, 43, 60))
    accent = hex_rgba(style["accent"])[:3]
    d.rounded_rectangle((round(w * 0.07), round(h * 0.09), round(w * 0.12), round(h * 0.09) + 5),
                        radius=2, fill=accent)
    img.save(out)
    return out


def _center_text(d, text, box, sizes, color=WHITE, weight="Bold", max_lines=3):
    x0, y0, x1, y1 = box
    f, rows = fit(text, int(x1 - x0), max_lines, sizes, weight)
    # Also fit the available height; short portrait panels must never clip text.
    while len(rows) * f.size * 1.25 > y1 - y0 and f.size > 18:
        f = font(f.size - 2, weight)
        rows = wrap(text, f, int(x1 - x0))
    top = (y0 + y1 - len(rows) * f.size * 1.25) / 2
    for n, row in enumerate(rows):
        d.text(((x0 + x1 - f.getlength(row)) / 2, top + n * f.size * 1.25), row, font=f, fill=color)


def flow_frames(cue: str, canvas: Canvas, seconds: float) -> list[Image.Image]:
    """Progressive process diagram; layout changes to stacked steps for portrait video."""
    c = canvas
    steps = parts(cue)[:4]
    frames = []
    for n in range(max(1, round(seconds * FPS))):
        p = (n + 1) / max(1, round(seconds * FPS))
        active = min(len(steps) - 1, int(p * len(steps)))
        img = Image.new("RGBA", (c.w, c.h))
        d = ImageDraw.Draw(img)
        count = max(1, len(steps))
        gap = round(c.m * 0.04)
        bw = c.win_w if c.vertical else (c.win_w - gap * (count - 1)) / count
        bh = c.m * (0.15 if c.vertical else 0.22)
        height = count * bh + (count - 1) * gap if c.vertical else bh
        left, top = c.cx - c.win_w / 2, c.cy - height / 2
        for k, text in enumerate(steps):
            x = left if c.vertical else left + k * (bw + gap)
            y = top + k * (bh + gap) if c.vertical else top
            on = k <= active
            d.rounded_rectangle((x, y, x + bw, y + bh), radius=18,
                                fill=(22, 37, 53, 255), outline=c.accent if on else (60, 74, 88, 255), width=4)
            _center_text(d, text, (x + 18, y + 15, x + bw - 18, y + bh - 15),
                         range(round(c.m * 0.052), 24, -2), color=WHITE if on else GREY)
            if k < count - 1:
                if c.vertical:
                    ax, ay = c.cx, y + bh + gap / 2
                    d.line((ax, ay - 10, ax, ay + 8), fill=c.accent, width=4)
                    d.polygon([(ax - 7, ay + 2), (ax + 7, ay + 2), (ax, ay + 10)], fill=c.accent)
                else:
                    ax, ay = x + bw + gap / 2, y + bh / 2
                    d.line((ax - 10, ay, ax + 8, ay), fill=c.accent, width=4)
                    d.polygon([(ax + 2, ay - 7), (ax + 2, ay + 7), (ax + 10, ay)], fill=c.accent)
        frames.append(img)
    return frames


def comparison_parts(cue: str) -> tuple[list[str], list[tuple[str, str, str]]]:
    values = parts(cue)
    headers = (values[:2] + ["Option A", "Option B"])[:2]
    rows = []
    for value in values[2:5]:
        label, _, pair = value.partition(":")
        left, _, right = pair.partition("/")
        rows.append((label.strip(), left.strip(), right.strip()))
    return headers, rows


def compare_frames(cue: str, canvas: Canvas, seconds: float) -> list[Image.Image]:
    c = canvas
    headers, rows = comparison_parts(cue)
    frames = []
    for n in range(max(1, round(seconds * FPS))):
        p = (n + 1) / max(1, round(seconds * FPS))
        img = Image.new("RGBA", (c.w, c.h))
        d = ImageDraw.Draw(img)
        width = c.w * 0.88
        left = c.cx - width / 2
        label_h, row_h = c.m * 0.07, c.m * (0.15 if c.vertical else 0.14)
        header_h = c.m * 0.11
        height = header_h + len(rows) * (label_h + row_h)
        top = c.cy - height / 2
        d.rounded_rectangle((left, top, left + width, top + height), radius=22, fill=(17, 30, 44, 255),
                            outline=(58, 75, 92, 255), width=2)
        for k, title in enumerate(headers):
            _center_text(d, title, (left + k * width / 2 + 12, top + 6, left + (k + 1) * width / 2 - 12, top + header_h),
                         range(round(c.m * (0.065 if c.vertical else 0.07)), 24, -2), color=c.accent, max_lines=2)
        for k, (label, a, b) in enumerate(rows):
            if p < k / max(1, len(rows)):
                continue
            y = top + header_h + k * (label_h + row_h)
            d.rectangle((left + 2, y, left + width - 2, y + label_h), fill=(30, 47, 64, 255))
            _center_text(d, label, (left + 12, y, left + width - 12, y + label_h),
                         range(round(c.m * 0.06), 20, -2), color=GREY, max_lines=1)
            for j, text in enumerate((a, b)):
                _center_text(d, text, (left + j * width / 2 + 16, y + label_h + 8,
                                       left + (j + 1) * width / 2 - 16, y + label_h + row_h - 8),
                             range(round(c.m * (0.06 if c.vertical else 0.07)), 22, -2))
        frames.append(img)
    return frames


RENDERERS = {"chat": chat_frames, "code": code_frames, "list": list_frames,
             "flow": flow_frames, "compare": compare_frames}


def render_graphic(kind: str, cue: str, size: tuple[int, int], style: dict, shot_seconds: float, out_dir: Path) -> str:
    """Write the animation frames to `out_dir` and return the ffmpeg input pattern. The animation takes
    about 60% of the shot (at most 3.5s) so the finished graphic stays on screen to be read."""
    out_dir.mkdir(parents=True, exist_ok=True)
    seconds = min(3.5, max(1.2, shot_seconds * 0.6))
    for i, frame in enumerate(RENDERERS[kind](cue, Canvas(size, style), seconds)):
        frame.save(out_dir / f"{i:04d}.png", compress_level=1)
    return str(out_dir / "%04d.png")
