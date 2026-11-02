"""Visual Assembler: voiceover + script -> finished video.

Built for retention, which is what YouTube rewards and what earns ad money:
- a new shot every 2-5 seconds (faster during the hook), every stock shot slowly panning or zooming
- word-by-word captions with the spoken word highlighted, and chapter labels
- TEXT cues as bold cards over blurred footage, numbers in the accent colour
- music that ducks under the voice; final mix at YouTube's -14 LUFS
- 15 seconds of end-screen space on long videos

Each shot renders to its own small file (low memory on an 8 GB laptop), then one final pass joins them,
burns in captions and mixes the audio on the Intel GPU encoder.
"""
import hashlib
import json
import math
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from core.config import ROOT
from core.log import get_logger
from core.media import duration, loudness_stats, run
from production import captions
from production.cards import end_card, text_card
from production.graphics import render_graphic, studio_background
from production.stock import StockLibrary
from production.storyboard import ProductionBlocked, preflight, recording_path, screen_references
from writing.models import Script

log = get_logger("assembler")

FONTS_DIR = ROOT / "assets" / "fonts"
MUSIC_DIR = ROOT / "assets" / "music"
MUSIC_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".flac"}
SIZES = {"long": (1920, 1080), "short": (1080, 1920)}
MOTIONS = ("zoom_in", "pan_right", "zoom_out", "pan_left")
GRAPHICS = ("text", "chat", "code", "list", "flow", "compare")


@dataclass
class Shot:
    start: float
    end: float
    kind: str  # stock | text | screen | placeholder | end
    cue: str  # the visual cue text (query, card text or screen description)
    line: int = -1
    screen_no: int = 0
    source: str = ""  # file used, filled in when rendered
    offset: float = 0.0
    recording: str = ""
    focus: tuple[int, int, int, int] | None = None

    @property
    def length(self) -> float:
        return self.end - self.start


def cue_kind(visual: str) -> tuple[str, str]:
    prefix, _, rest = visual.partition(":")
    kind = {"SCREEN": "screen", "STOCK": "stock", "TEXT": "text", "CHAT": "chat", "CODE": "code",
            "LIST": "list", "FLOW": "flow", "COMPARE": "compare"}.get(prefix.strip().upper(), "stock")
    return kind, (rest if _ else visual).strip()


def plan_shots(script: Script, timings: dict, fmt: str, cfg: dict) -> list[Shot]:
    """One or more shots per spoken line, covering the timeline with no gaps.
    A line's shots run from its start to the next line's start, so pauses never show a black frame."""
    r = cfg["render"]
    lines = timings["lines"]
    shots: list[Shot] = []
    refs = screen_references(script)
    for n, t in enumerate(lines):
        start = 0.0 if n == 0 else t["start"]
        end = lines[n + 1]["start"] if n + 1 < len(lines) else t["end"] + r["tail_seconds"]
        kind, cue = cue_kind(script.lines[t["line"]].visual)
        if kind in GRAPHICS:  # one animated graphic per line: it builds up, then holds while the line finishes
            shots.append(Shot(start, end, kind, cue, t["line"]))
            continue
        # Stock and screen lines are cut into several shots to keep the pace up; faster during the hook.
        # Screen parts share one recording number, and `offset` makes a real recording play on continuously.
        max_len = (r.get("screen_shot_seconds", 12) if kind == "screen" else
                   r["hook_cut_seconds"] if start < r["hook_seconds"] or fmt == "short" else r["cut_seconds"])
        parts = max(1, math.ceil((end - start) / max_len))
        step = (end - start) / parts
        shots += [Shot(start + i * step, start + (i + 1) * step, kind, cue, t["line"],
                       refs[t["line"]][0] if kind == "screen" else 0,
                       offset=round(refs[t["line"]][2] + i * step, 3) if kind == "screen" else 0.0,
                       recording=refs[t["line"]][1] if kind == "screen" else "")
                  for i in range(parts)]
    if fmt == "long" and r["end_screen_seconds"]:
        last = shots[-1].end
        shots.append(Shot(last, last + r["end_screen_seconds"], "end", "end screen"))
    return shots


class Assembler:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.r = cfg["render"]
        self.stock = None  # graphics and real recordings can render without a stock API key

    # ---------- encoding ----------

    def _encode_args(self, final: bool) -> list[str]:
        if self.r["encoder"] == "h264_qsv":
            quality = ["-b:v", self.r["bitrate"], "-maxrate", self.r["maxrate"], "-bufsize", self.r["maxrate"]] if final \
                else ["-global_quality", "18"]
            return ["-c:v", "h264_qsv", "-preset", "medium" if final else "veryfast", *quality]
        return ["-c:v", "libx264", "-preset", "medium" if final else "veryfast", "-crf", "18" if final else "16",
                "-pix_fmt", "yuv420p"]

    def _render(self, inputs: list[str], graph: str, out: Path, frames: int) -> None:
        run([*inputs, "-filter_complex", graph, "-map", "[v]", "-an", "-r", str(self.r["fps"]),
             "-frames:v", str(frames), *self._encode_args(final=False), str(out)])

    # ---------- shots ----------

    def _cover(self, w: int, h: int, scale: float = 1.0) -> str:
        bw, bh = 2 * math.ceil(w * scale / 2), 2 * math.ceil(h * scale / 2)
        return f"scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},setsar=1"

    def _motion(self, kind: str, w: int, h: int, d: float) -> str:
        """Slow movement on every shot: still footage looks cheap and loses viewers."""
        z = self.r["motion_strength"]
        if kind in ("zoom_in", "zoom_out"):
            # Progress is clamped to 0-1: frame rounding can run a shot a frame past `d`, and a zoom below 100%
            # would leave the crop larger than the image, which crashes ffmpeg.
            progress = f"min(t/{d:.3f},1)"
            grow = f"(1+{z}*{progress})" if kind == "zoom_in" else f"(1+{z}-{z}*{progress})"
            return f"{self._cover(w, h)},scale=w='2*trunc({w}*{grow}/2)':h='2*trunc({h}*{grow}/2)':eval=frame,crop={w}:{h}"
        bw = 2 * math.ceil(w * (1 + z) / 2)
        progress = f"min(t/{d:.3f},1)"
        x = f"({bw}-{w})*{progress}" if kind == "pan_right" else f"({bw}-{w})*(1-{progress})"
        return f"{self._cover(w, h, 1 + z)},crop={w}:{h}:x='{x}':y='(ih-{h})/2'"

    def _stock_input(self, shot: Shot, strict: bool = True) -> list[str] | None:
        if self.stock is None:
            self.stock = StockLibrary(self.cfg)
        clip = self.stock.find(shot.cue, shot.length, strict)
        if not clip:
            return None
        shot.source = clip.name
        clip_len = duration(clip)
        shot.offset = round(max(0.0, min(clip_len - shot.length, clip_len * 0.25)), 2)  # skip slow clip intros
        return ["-ss", f"{shot.offset}", "-stream_loop", "-1", "-i", str(clip)]

    def render_shot(self, shot: Shot, n: int, out: Path, work: Path, size: tuple[int, int]) -> None:
        w, h = size
        fps = self.r["fps"]
        frames = max(1, round(shot.end * fps) - round(shot.start * fps))
        d = shot.length
        motion = MOTIONS[n % len(MOTIONS)]
        style = self.r["style"]

        if shot.kind in GRAPHICS:
            from production.motion import scene_source
            animated = scene_source(work.parent, shot.line, size)
            if animated:
                shot.source = animated.relative_to(work.parent.resolve()).as_posix()
                self._render(["-i", str(animated)],
                             f"[0:v]{self._cover(w, h)},tpad=stop_mode=clone:stop_duration={d + 1:.3f},fps={fps}[v]",
                             out, frames)
                return

        if shot.kind == "screen":
            recording = recording_path(work.parent, shot.recording or f"{shot.screen_no:02d}.mp4")
            if recording.exists():
                if shot.offset + d > duration(recording) + 0.05:
                    raise ProductionBlocked(f"{recording.name} is too short for this demo; record the full action instead of looping.")
                shot.source = f"screen/{recording.name}"
                framing_file = work.parent / "screen-framing.json"
                framing = json.loads(framing_file.read_text(encoding="utf-8")).get(str(shot.line)) if framing_file.exists() else None
                crop = ""
                if framing:
                    x, y, fw, fh = (int(framing[k]) for k in ("x", "y", "width", "height"))
                    if min(x, y) < 0 or min(fw, fh) <= 0:
                        raise ProductionBlocked(f"Invalid screen focus for line {shot.line}.")
                    shot.focus = (x, y, fw, fh)
                    crop = f"crop={fw}:{fh}:{x}:{y},"
                # Keep the demonstration readable above a separate narration-caption area.
                fh = round(h * (0.58 if h > w else 0.76))
                py = str(round(h * (0.07 if h > w else 0.05)))
                color = "0x" + style.get("panel", "#122335").lstrip("#")
                self._render(["-ss", f"{shot.offset:.3f}", "-i", str(recording)],
                             f"[0:v]{crop}scale={round(w * 0.9)}:{fh}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                             f"pad={w}:{h}:(ow-iw)/2:{py}:color={color},setsar=1,fps={fps}[v]", out, frames)
                return
            raise ProductionBlocked(f"Missing screen/{recording.name}: {shot.cue}. Add real footage before rendering.")

        if shot.kind in ("chat", "code", "list", "flow", "compare"):
            pattern = render_graphic(shot.kind, shot.cue, size, style, d, work / f"{n:04d}_frames")
            background = studio_background(work / "studio.png", size, style)
            shot.source = f"{shot.kind} graphic"
            self._render(["-loop", "1", "-i", str(background), "-framerate", "15", "-i", pattern],
                         f"[0:v]fps={fps}[bg];[1:v]format=rgba,fps={fps},"
                         f"tpad=stop_mode=clone:stop_duration={d + 1:.2f}[c];[bg][c]overlay=0:0[v]", out, frames)
            return

        if shot.kind in ("text", "end"):
            card = (text_card(shot.cue, work / f"{n:04d}.png", size, style) if shot.kind == "text"
                    else end_card(work / f"{n:04d}.png", size, style))
            background = studio_background(work / "studio.png", size, style)
            shot.source = card.name
            fade = "" if shot.start == 0 else ",fade=t=in:st=0:d=0.25:alpha=1"  # the opening frame shows the hook at once
            self._render(["-loop", "1", "-i", str(background), "-loop", "1", "-i", str(card)],
                         f"[0:v]fps={fps}[bg];[1:v]format=rgba{fade}[c];"
                         f"[bg][c]overlay=0:0:shortest=0[v]", out, frames)
            return

        source = self._stock_input(shot)
        if not source:
            raise ProductionBlocked(f"No relevant footage for '{shot.cue}'. Supply a real recording or change the visual; generic filler is disabled.")
        self._render(source, f"[0:v]{self._motion(motion, w, h, d)},fps={fps}[v]", out, frames)

    # ---------- whole video ----------

    def pick_music(self, folder: Path) -> Path | None:
        tracks = sorted(p for p in MUSIC_DIR.glob("*") if p.suffix.lower() in MUSIC_EXT)
        if not tracks:
            return None
        index = int(hashlib.md5(folder.name.encode()).hexdigest(), 16) % len(tracks)  # varies by video, stable on re-render
        return tracks[index]

    def mix_audio(self, folder: Path, total: float, music: Path | None) -> Path:
        """Voice plus music ducked under it, normalised to -14 LUFS: measure first, then apply the exact gain,
        because a single loudnorm pass undershoots on speech."""
        mix = folder / "mix_raw.wav"
        inputs = ["-i", "voice.wav"]
        graph = f"[0:a]apad=whole_dur={total:.3f}"
        if music:
            inputs += ["-stream_loop", "-1", "-i", str(music)]
            graph += (f",asplit=2[voice][key];[1:a]atrim=0:{total:.3f},volume={self.r['music_volume']},"
                      f"afade=t=in:st=0:d=2,afade=t=out:st={max(0, total - 4):.3f}:d=4[music];"
                      f"[music][key]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=400[ducked];"
                      f"[voice][ducked]amix=inputs=2:normalize=0")
        else:
            log.warning("No music in %s; voice only", MUSIC_DIR)
        run([*inputs, "-filter_complex", graph + "[a]", "-map", "[a]", "-t", f"{total:.3f}", "-ar", "48000", mix.name],
            cwd=folder)

        measured = loudness_stats(mix, "I=-14:TP=-1.5:LRA=11")
        gain = -14 - float(measured["input_i"])
        out = folder / "mix.wav"
        # Exact gain to -14 LUFS, then a limiter catches the peaks the gain pushes past -1.5 dBTP.
        # (loudnorm's own second pass stops short when speech peaks are already near the ceiling.)
        run(["-i", mix.name, "-af", f"volume={gain:.2f}dB,alimiter=limit=0.84:attack=5:release=50:level=disabled",
             "-ar", "48000", out.name], cwd=folder)
        mix.unlink()
        return out

    def assemble(self, script: Script, fmt: str, folder: Path) -> Path:
        timings = json.loads((folder / "timings.json").read_text(encoding="utf-8"))
        preflight(script, fmt, self.cfg, folder, timings)
        size = SIZES[fmt]
        work = folder / "_shots"
        work.mkdir(exist_ok=True)
        if self.stock is not None:
            self.stock.new_video()

        shots = plan_shots(script, timings, fmt, self.cfg)
        total = shots[-1].end
        log.info("Rendering %d shots, %.0fs, %dx%d", len(shots), total, *size)
        concat = []
        for n, shot in enumerate(shots):
            out = work / f"{n:04d}.mp4"
            self.render_shot(shot, n, out, work, size)
            concat.append(f"file '{out.name}'")
            if (n + 1) % 10 == 0 or n + 1 == len(shots):
                log.info("Rendered %d/%d shots", n + 1, len(shots))
        (work / "list.txt").write_text("\n".join(concat), encoding="utf-8")

        log.info("Timing captions with Whisper")
        heard = captions.heard_words(folder / "voice.wav", self.r["whisper_model"])
        lines = captions.timed_lines(timings, heard)
        chapters = [(s.start, script.lines[s.line].chapter) for s in shots
                    if s.line >= 0 and script.lines[s.line].chapter and s.start > 0.5
                    and s is next(x for x in shots if x.line == s.line)]
        cap_style = {**self.r["captions"][fmt], "accent": self.r["style"]["accent"]}
        (folder / "captions.ass").write_text(captions.ass_document(lines, chapters, cap_style, *size), encoding="utf-8")
        (folder / "captions.srt").write_text(captions.srt_document(lines), encoding="utf-8")

        log.info("Mixing audio")
        music = self.pick_music(folder)
        mixed = self.mix_audio(folder, total, music)

        log.info("Final render: captions burned in")
        fonts = Path(os.path.relpath(FONTS_DIR, folder)).as_posix()
        run(["-f", "concat", "-safe", "0", "-i", "_shots/list.txt", "-i", mixed.name,
             "-filter_complex", f"[0:v]ass=captions.ass:fontsdir='{fonts}'[v]",
             "-map", "[v]", "-map", "1:a", "-t", f"{total:.3f}", "-r", str(self.r["fps"]),
             *self._encode_args(final=True), "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
             "-movflags", "+faststart", "video.mp4"], cwd=folder)

        (folder / "render.json").write_text(json.dumps({
            "size": size, "seconds": round(total, 2), "music": music.name if music else None,
            "shots": [asdict(s) for s in shots]}, indent=1), encoding="utf-8")
        mixed.unlink()
        if not self.r["keep_shots"]:
            shutil.rmtree(work)
        missing = sorted({s.screen_no for s in shots if s.kind in ("placeholder", "stock_for_screen")})
        if missing:
            log.warning("Screen recordings still needed: %s (save as screen/NN.mp4 and render again)", missing)
        return folder / "video.mp4"
