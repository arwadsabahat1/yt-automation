"""Thumbnail Studio: three 1280x720 thumbnails per video for YouTube's Test & Compare.

Built for clicks: 2-4 huge words from the SEO pack, one highlighted in the accent colour, over the best frame
from the video's own footage (faces score highest: they lift click-through), or over one of the video's
chat/terminal graphics. Every design is checked at the size YouTube shows on phones (168x94).

Layouts:
  split    image on the right fading into a dark panel, text on the left
  bleed    full-bleed darkened image, key word on an accent block
  graphic  the video's own chat/terminal/checklist graphic, enlarged, with text
"""
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from core.config import DATA_DIR, ROOT
from core.log import get_logger
from core.media import duration, tool
from production.cards import hex_rgba
from production.graphics import RENDERERS, Canvas
from production.stock import CACHE_DIR

log = get_logger("thumbnails")

W, H = 1280, 720
PHONE = (168, 94)  # how big YouTube shows thumbnails in phone feeds
MIN_PHONE_CAP_PX = 11  # capital letters must stay at least this tall at phone size
FONT = ROOT / "assets" / "fonts" / "Anton-Regular.ttf"
FACE_MODEL = DATA_DIR / "models" / "face_detection_yunet_2023mar.onnx"
HIGHLIGHT_RE = re.compile(r"[\d$%£€]")


@dataclass
class Frame:
    image: Image.Image
    score: float
    face: tuple[int, int, int, int] | None  # x, y, w, h in the 1280x720 frame
    source: str


# ---------- picking frames ----------

def grab(clip: Path, at: float) -> Image.Image | None:
    raw = subprocess.run([tool("ffmpeg"), "-v", "error", "-ss", f"{at:.2f}", "-i", str(clip), "-frames:v", "1",
                          "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    if len(raw) != W * H * 3:
        return None
    return Image.frombuffer("RGB", (W, H), raw)


def colorfulness(rgb: np.ndarray) -> float:
    """Hasler-Suesstrunk colourfulness: dull grey frames make weak thumbnails."""
    r, g, b = (rgb[..., i].astype(float) for i in range(3))
    rg, yb = r - g, 0.5 * (r + g) - b
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def plausible_face(f) -> bool:
    """YuNet row: x, y, w, h, right eye, left eye, nose, mouth corners (x, y each), confidence.
    Rejects hands and objects: needs high confidence, a face-like shape and two level eyes inside the box."""
    x, y, w, h = f[:4]
    rex, rey, lex, ley = f[4:8]
    if f[14] < 0.9 or not 0.9 <= h / max(w, 1) <= 1.8:
        return False
    eyes_inside = all(x <= ex <= x + w and y <= ey <= y + h * 0.65 for ex, ey in ((rex, rey), (lex, ley)))
    return eyes_inside and abs(rey - ley) < h * 0.15 and abs(lex - rex) > w * 0.25


def score_frames(shots: list[dict], limit: int = 24) -> list[Frame]:
    """Score one frame from the middle of each stock shot: face first, then sharpness, colour and brightness."""
    import cv2  # only needed here
    detector = cv2.FaceDetectorYN_create(str(FACE_MODEL), "", (W, H), 0.75)
    frames, seen = [], set()
    for shot in shots:
        clip = CACHE_DIR / shot["source"]
        if shot["kind"] not in ("stock", "stock_for_screen") or shot["source"] in seen or not clip.exists():
            continue
        seen.add(shot["source"])
        at = (shot["offset"] + (shot["end"] - shot["start"]) / 2) % max(duration(clip) - 0.2, 0.2)
        img = grab(clip, at)
        if img is None:
            continue
        rgb = np.asarray(img)
        bgr = np.ascontiguousarray(rgb[..., ::-1])
        _, faces = detector.detect(bgr)
        real = [f for f in (faces if faces is not None else []) if plausible_face(f)]
        face = tuple(int(v) for v in max(real, key=lambda f: f[2] * f[3])[:4]) if real else None
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        sharp = min(1.0, cv2.Laplacian(gray, cv2.CV_64F).var() / 400)
        bright = 1 - abs(gray.mean() - 115) / 115
        face_bonus = 0.0
        if face:
            # Judge the face's own sharpness: good footage often has a deliberately blurred background.
            x, y, w, h = face
            face_sharp = min(1.0, cv2.Laplacian(gray[max(0, y):y + h, max(0, x):x + w], cv2.CV_64F).var() / 150)
            face_bonus = (2.0 + min(1.0, w * h / (W * H) * 12)) * (0.3 + 0.7 * face_sharp)
            sharp = max(sharp, face_sharp)
        score = face_bonus + sharp + min(1.0, colorfulness(rgb) / 60) + 0.5 * bright
        frames.append(Frame(img, score, face, shot["source"]))
        if len(frames) >= limit:
            break
    return sorted(frames, key=lambda f: f.score, reverse=True)


# ---------- drawing ----------

def anton(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT), size)


def fit_text(words: list[str], max_w: int, max_h: int, max_lines: int = 3):
    """Biggest Anton size that fits the words in the box, trying 1 to `max_lines` lines."""
    best = None
    for size in range(240, 70, -6):
        f = anton(size)
        for n_lines in range(1, max_lines + 1):
            lines = split_lines(words, n_lines)
            widths = [f.getlength(" ".join(line)) for line in lines]
            height = n_lines * size * 1.02
            if max(widths) <= max_w and height <= max_h:
                best = (f, lines)
                break
        if best:
            return best
    f = anton(72)
    return f, split_lines(words, max_lines)


def split_lines(words: list[str], n: int) -> list[list[str]]:
    """Split words into n lines as evenly as possible by character count."""
    n = max(1, min(n, len(words)))
    if n == 1:
        return [words]
    total = sum(len(w) for w in words)
    lines, current, target = [], [], total / n
    for word in words:
        if current and sum(len(w) for w in current) >= target and len(lines) < n - 1:
            lines.append(current)
            current = []
        current.append(word)
    return lines + [current]


def key_word(words: list[str]) -> int:
    """Index of the word to highlight: one with a number or $, else the last word."""
    for i, w in enumerate(words):
        if HIGHLIGHT_RE.search(w):
            return i
    return len(words) - 1


def draw_text(img: Image.Image, text: str, box: tuple[int, int, int, int], accent: tuple, mode: str) -> int:
    """Draw `text` inside box (x, y, w, h). mode 'color' paints the key word in the accent colour;
    'block' puts it on an accent block. Returns the font size used."""
    words = text.upper().split()
    x, y, bw, bh = box
    f, lines = fit_text(words, bw, bh)
    hi = key_word(words)
    draw = ImageDraw.Draw(img)
    line_h = round(f.size * 1.02)
    top = y + (bh - len(lines) * line_h) // 2
    stroke = max(4, f.size // 22)
    k = 0
    for row, line in enumerate(lines):
        cx = x
        ty = top + row * line_h
        for word in line:
            ww = f.getlength(word)
            if k == hi and mode == "block":
                pad = round(f.size * 0.08)
                draw.rectangle((cx - pad, ty + f.size * 0.08, cx + ww + pad, ty + f.size * 1.06), fill=accent)
                draw.text((cx, ty), word, font=f, fill=(12, 12, 16))
            else:
                # drop shadow, then the word with a dark outline so it reads on any background
                draw.text((cx + stroke, ty + stroke), word, font=f, fill=(0, 0, 0, 160))
                draw.text((cx, ty), word, font=f, fill=accent if k == hi else (255, 255, 255),
                          stroke_width=stroke, stroke_fill=(0, 0, 0))
            cx += ww + f.getlength(" ")
            k += 1
    return f.size


def phone_cap_height(font_size: int) -> float:
    """Height of capital letters at phone size, for the readability check."""
    top, bottom = anton(font_size).getbbox("H")[1::2]
    return (bottom - top) * PHONE[0] / W


def enhance(img: Image.Image) -> Image.Image:
    img = ImageEnhance.Color(img).enhance(1.25)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    return ImageEnhance.Sharpness(img).enhance(1.3)


def vignette(img: Image.Image, strength: float) -> Image.Image:
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).ellipse((-W * 0.25, -H * 0.35, W * 1.25, H * 1.35), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(120))
    dark = Image.new("RGB", (W, H), (0, 0, 0))
    return Image.composite(img, Image.blend(img, dark, strength), mask)


def crop_on(img: Image.Image, face, width: int) -> Image.Image:
    """A full-height slice `width` wide, centred on the face if there is one."""
    cx = face[0] + face[2] // 2 if face else W // 2
    left = min(max(0, cx - width // 2), W - width)
    return img.crop((left, 0, left + width, H))


def split_layout(frame: Frame, text: str, style: dict) -> tuple[Image.Image, int]:
    panel = hex_rgba(style["panel"])[:3]
    img = Image.new("RGB", (W, H), panel)
    photo = enhance(crop_on(frame.image, frame.face, 760))
    img.paste(photo, (W - 760, 0))
    # Opaque at the left, clear at the right, eased so the panel melts into the photo with no visible edge.
    fade = Image.linear_gradient("L").rotate(-90).resize((380, H)).point(lambda v: round(255 * (v / 255) ** 1.8))
    img.paste(Image.new("RGB", (380, H), panel), (W - 760, 0), fade)
    return img, draw_text(img, text, (48, 40, 560, H - 80), hex_rgba(style["accent"])[:3], "color")


def bleed_layout(frame: Frame, text: str, style: dict) -> tuple[Image.Image, int]:
    img = vignette(ImageEnhance.Brightness(enhance(frame.image)).enhance(0.62), 0.55)
    # Text goes on the side away from the face so the face stays visible.
    face_cx = frame.face[0] + frame.face[2] // 2 if frame.face else W // 2
    box = (48, 40, 640, H - 80) if face_cx > W // 2 else (W - 688, 40, 640, H - 80)
    if not frame.face:
        box = (90, 60, W - 180, H - 120)
    return img, draw_text(img, text, box, hex_rgba(style["accent"])[:3], "block")


def graphic_layout(kind: str, cue: str, background: Frame | None, text: str, style: dict) -> tuple[Image.Image, int]:
    base = background.image if background else Image.new("RGB", (W, H), hex_rgba(style["panel"])[:3])
    img = ImageEnhance.Brightness(base.filter(ImageFilter.GaussianBlur(18))).enhance(0.45)
    # Drawn on a narrow canvas so the window is compact, then enlarged: the graphic should read at phone size too.
    graphic = RENDERERS[kind](cue, Canvas((900, 1100), style), 2.0)[-1]
    graphic = graphic.crop(graphic.getbbox())
    scale = min(660 / graphic.width, (H - 100) / graphic.height)
    graphic = graphic.resize((round(graphic.width * scale), round(graphic.height * scale)), Image.LANCZOS)
    img.paste(graphic, (W - graphic.width - 36, (H - graphic.height) // 2), graphic)
    return img, draw_text(img, text, (44, 40, W - graphic.width - 110, H - 80), hex_rgba(style["accent"])[:3], "color")


# ---------- whole set ----------

def make_thumbnails(folder: Path, cfg: dict) -> list[Path]:
    style = cfg["render"]["style"]
    pack = json.loads((folder / "pack.json").read_text(encoding="utf-8"))
    shots = json.loads((folder / "render.json").read_text(encoding="utf-8"))["shots"]
    texts = [t for t in pack["seo"]["thumbnail_text"] if t.strip()][:3] or [pack["script"]["working_title"]]
    texts += [texts[0]] * (3 - len(texts))

    frames = score_frames(shots)
    if not frames:
        log.warning("No stock frames to use; thumbnails will use graphics and plain backgrounds")
    best = frames[0] if frames else None
    second = frames[1] if len(frames) > 1 else best
    # Prefer a chat window: for an AI-tools channel it says "AI" at a glance.
    graphic = next(((s["kind"], s["cue"]) for kind in ("chat", "code", "list") for s in shots if s["kind"] == kind), None)

    designs = []
    if best:
        designs.append(("split", lambda t: split_layout(best, t, style)))
        designs.append(("bleed", lambda t: bleed_layout(second, t, style)))
    if graphic:
        designs.append(("graphic", lambda t: graphic_layout(*graphic, second, t, style)))
    while len(designs) < 3 and best:  # not enough variety available: vary the frame instead
        alt = frames[min(len(designs), len(frames) - 1)]
        designs.append(("bleed", lambda t, a=alt: bleed_layout(a, t, style)))

    from analytics.brain import thumbnail_preference
    from core.db import connect
    preferred = []
    if cfg.get('niche', '').casefold() == cfg.get('analytics', {}).get('niche', '').casefold():
        with connect() as feedback_conn:
            preferred = thumbnail_preference(feedback_conn, cfg)
    if preferred:
        designs.sort(key=lambda design: preferred.index(design[0]) if design[0] in preferred else len(preferred))

    out_dir = folder / "thumbnails"
    out_dir.mkdir(exist_ok=True)
    paths, report = [], []
    for n, ((name, build), text) in enumerate(zip(designs, texts), 1):
        img, size = build(text)
        cap = phone_cap_height(size)
        path = out_dir / f"thumb_{n}_{name}.jpg"
        img.save(path, quality=92, optimize=True)
        paths.append(path)
        readable = cap >= MIN_PHONE_CAP_PX
        report.append({"file": path.name, "layout": name, "text": text, "font_px": size,
                       "phone_cap_px": round(cap, 1), "readable_on_phone": readable})
        if not readable:
            log.warning("%s: text is only %.1fpx tall on phones; shorten '%s'", path.name, cap, text)
    preview(paths, out_dir / "preview.jpg")
    (out_dir / "thumbnails.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    log.info("Thumbnails -> %s", out_dir)
    return paths


def preview(paths: list[Path], out: Path) -> None:
    """Sheet with each thumbnail at half size and at phone size, as viewers will see it in the feed."""
    sheet = Image.new("RGB", (640 + 40 + PHONE[0] + 40, len(paths) * (360 + 30) + 30), (24, 24, 28))
    for i, p in enumerate(paths):
        img = Image.open(p)
        y = 30 + i * 390
        sheet.paste(img.resize((640, 360), Image.LANCZOS), (20, y))
        sheet.paste(img.resize(PHONE, Image.LANCZOS), (700, y + 133))
    sheet.save(out, quality=90)
