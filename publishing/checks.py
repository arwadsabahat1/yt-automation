"""Pre-publish checks: everything a finished video must pass before it can be approved.

Blocking problems (fail) keep the Approve button disabled; warnings are shown but don't block.
Checks: file and format, length rules (8+ min long for mid-roll ads, under 60s for Shorts), loudness and
peaks, black frames, long silences, captions file, thumbnails readable on phones, and the script's own
to-dos (unfilled verdict, facts to verify, screen recordings still missing).
"""
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from core.media import tool
from production.storyboard import build_storyboard
from writing.models import Script

PLACEHOLDER_RE = re.compile(r"\[YOUR VERDICT:[^\]]*\]")


@dataclass
class Check:
    name: str
    status: str  # pass | warn | fail
    detail: str


def probe(video: Path) -> dict:
    out = subprocess.run([tool("ffprobe"), "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration,size",
                          "-of", "json", str(video)], capture_output=True, text=True)
    return json.loads(out.stdout or "{}")


def scan_audio_video(video: Path) -> dict:
    """One decoding pass: integrated loudness, true peak, black stretches and silent stretches."""
    out = subprocess.run([tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(video),
                          "-vf", "blackdetect=d=1.0:pix_th=0.08", "-af", "ebur128=peak=true,silencedetect=n=-45dB:d=3",
                          "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    loud = re.findall(r"I:\s+(-?[\d.]+) LUFS", out)
    peak = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", out)
    return {"lufs": float(loud[-1]) if loud else None, "peak": float(peak[-1]) if peak else None,
            "black": [(float(a), float(b)) for a, b in re.findall(r"black_start:([\d.]+) black_end:([\d.]+)", out)],
            "silence": [float(d) for d in re.findall(r"silence_duration: ([\d.]+)", out)]}


def run_checks(folder: Path, fmt: str, cfg: dict) -> list[Check]:
    video = folder / "video.mp4"
    if not video.exists():
        return [Check("Video file", "fail", "video.mp4 not rendered yet")]
    checks = []
    info = probe(video)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    seconds = float(info.get("format", {}).get("duration", 0))
    want = (1920, 1080) if fmt == "long" else (1080, 1920)
    size_ok = (v.get("width"), v.get("height")) == want and v.get("codec_name") == "h264" and a.get("codec_name") == "aac"
    checks.append(Check("Format", "pass" if size_ok else "fail",
                        f"{v.get('width')}x{v.get('height')} {v.get('codec_name')}/{a.get('codec_name')}, "
                        f"{int(info.get('format', {}).get('size', 0)) / 1e6:.0f} MB"))

    if fmt == "long":
        need = cfg["writing"]["long_min_minutes"] * 60
        checks.append(Check("Length", "pass" if seconds >= need else "fail",
                            f"{seconds / 60:.1f} min" + ("" if seconds >= need else
                                                         f": under {need / 60:.0f} min, no mid-roll ads")))
    else:
        checks.append(Check("Length", "pass" if seconds <= 60 else "fail",
                            f"{seconds:.0f}s" + ("" if seconds <= 60 else ": over 60s, YouTube won't treat it as a Short")))

    scan = scan_audio_video(video)
    lufs, peak = scan["lufs"], scan["peak"]
    if lufs is None:
        checks.append(Check("Loudness", "fail", "no audio measured"))
    else:
        status = "pass" if -16 <= lufs <= -12.5 and (peak or 0) <= -0.5 else "warn"
        checks.append(Check("Loudness", status, f"{lufs:.1f} LUFS (target -14), peak {peak:.1f} dB"))
    black = [b for b in scan["black"] if b[1] - b[0] >= 1.0]
    checks.append(Check("Black frames", "fail" if black else "pass",
                        "none" if not black else ", ".join(f"{s:.0f}-{e:.0f}s" for s, e in black[:5])))
    silence = scan["silence"]
    checks.append(Check("Silences", "warn" if any(d > 4 for d in silence) and fmt == "short" else "pass",
                        f"{len(silence)} gap(s) over 3s" if silence else "none"))  # end screens are quiet by design
    checks.append(Check("Captions file", "pass" if (folder / "captions.srt").exists() else "warn",
                        "captions.srt ready to upload" if (folder / "captions.srt").exists() else "missing"))

    if fmt == "long":
        report = folder / "thumbnails" / "thumbnails.json"
        if report.exists():
            thumbs = json.loads(report.read_text(encoding="utf-8"))
            weak = [t["file"] for t in thumbs if not t["readable_on_phone"]]
            checks.append(Check("Thumbnails", "warn" if weak else "pass",
                                f"{len(thumbs)} made" + (f"; hard to read on phones: {', '.join(weak)}" if weak else "")))
        else:
            checks.append(Check("Thumbnails", "fail", "none made yet"))

    pack = folder / "pack.json"
    if pack.exists():
        data = json.loads(pack.read_text(encoding="utf-8"))
        timing_path = folder / "timings.json"
        timing = json.loads(timing_path.read_text(encoding="utf-8")) if timing_path.exists() else None
        report = build_storyboard(Script.model_validate(data["script"]), fmt, cfg, folder, timing)
        checks += [Check(i.name, i.status, i.detail) for i in report.issues]
        text = " ".join(l["text"] for l in data["script"]["lines"])
        if PLACEHOLDER_RE.search(text):
            checks.append(Check("Your verdict", "fail", "fill in the [YOUR VERDICT] line, then re-render"))
        facts = sorted({c for l in data["script"]["lines"] for c in l.get("checks", [])}
                       | set(data.get("review", {}).get("unverifiable", [])))
        if facts:
            checks.append(Check("Facts to verify", "warn", f"{len(facts)} claim(s) listed in script.md"))
    render = folder / "render.json"
    if render.exists():
        shots = json.loads(render.read_text(encoding="utf-8"))["shots"]
        missing = sorted({s["screen_no"] for s in shots if s["kind"] in ("stock_for_screen", "placeholder")})
        if missing:
            checks.append(Check("Screen recordings", "fail",
                                f"#{', #'.join(map(str, missing))} shown with stock footage; record for a stronger video"))
    return checks


def save_checks(folder: Path, checks: list[Check]) -> Path:
    path = folder / "checks.json"
    path.write_text(json.dumps([asdict(c) for c in checks], indent=1), encoding="utf-8")
    return path


def blocking(checks: list[Check]) -> bool:
    return any(c.status == "fail" for c in checks)
