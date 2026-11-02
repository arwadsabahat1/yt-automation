"""Project-local HyperFrames commands. The regular FFmpeg pipeline remains independent."""
import json
import math
import os
import shutil
import subprocess
from pathlib import Path

from core.config import ROOT, OUTPUT_DIR
from core.media import loudness_stats, run, tool

MOTION_DIR = ROOT / "motion"
STUDIO_DIR = MOTION_DIR / "studio"
CLI = MOTION_DIR / "node_modules" / "hyperframes" / "bin" / "hyperframes.mjs"
PREVIEW_VIDEO = OUTPUT_DIR / "previews" / "hyperframes-skills" / "video.mp4"


def scene_source(folder: Path, line: int, size: tuple[int, int]) -> Path | None:
    """An optional checked motion export for a script line, constrained to the video folder."""
    manifest = folder / "motion-scenes.json"
    if not manifest.is_file():
        return None
    entry = json.loads(manifest.read_text(encoding="utf-8")).get(str(line), {}).get(f"{size[0]}x{size[1]}")
    if not entry:
        return None
    from production.storyboard import ProductionBlocked
    root = folder.resolve()
    source = (root / entry["file"]).resolve()
    if not source.is_relative_to(root) or source.suffix.lower() != ".mp4" or not source.is_file():
        raise ProductionBlocked(f"Missing or invalid motion source for line {line}: {entry['file']}")
    return source


def motion_environment() -> dict[str, str]:
    env = os.environ.copy()
    # This local workflow does not use cloud media providers or inherit their credentials.
    for name in ("YOUTUBE_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "PIXABAY_API_KEY",
                 "PEXELS_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "HEYGEN_API_KEY"):
        env.pop(name, None)
    env.update(HYPERFRAMES_SKIP_SKILLS="1", HYPERFRAMES_NO_UPDATE_CHECK="1", DO_NOT_TRACK="1")
    env["HYPERFRAMES_CONFIG_DIR"] = str(ROOT / "data" / "cache" / "hyperframes" / "config")
    env["HYPERFRAMES_EXTRACT_CACHE_DIR"] = str(ROOT / "data" / "cache" / "hyperframes" / "frames")
    env["HYPERFRAMES_HISTORY_ROOT"] = str(ROOT / "data" / "cache" / "hyperframes" / "history")
    env["XDG_STATE_HOME"] = str(ROOT / "data" / "cache" / "hyperframes" / "state")
    env["PATH"] = str(Path(tool("ffmpeg")).parent) + os.pathsep + env.get("PATH", "")
    return env


def run_motion(args: list[str], project: Path = STUDIO_DIR, timeout: int = 600) -> subprocess.CompletedProcess:
    if not CLI.is_file():
        raise RuntimeError("HyperFrames is not installed. Run npm.cmd ci --prefix motion.")
    node = shutil.which("node")
    if not node:
        raise RuntimeError("HyperFrames requires Node.js 22 or newer.")
    return subprocess.run([node, str(CLI), *args], cwd=project, env=motion_environment(),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def require_success(result: subprocess.CompletedProcess) -> None:
    if result.returncode:
        raise RuntimeError((result.stdout + "\n" + result.stderr).strip()[-6000:])


def check_motion() -> dict:
    result = run_motion(["check", "--json"])
    require_success(result)
    try:
        report = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("HyperFrames returned no readable check report.") from exc
    if not report.get("ok") or report.get("browserSkipped"):
        raise RuntimeError("HyperFrames did not complete its browser checks: " + json.dumps(report)[:4000])
    return report


def preview_motion(port: int = 8766) -> str:
    result = run_motion(["preview", "--background", "--port", str(port)])
    require_success(result)
    return result.stdout.strip()


def normalize_export_audio(video: Path) -> None:
    """Match the channel's final loudness after the renderer has mixed its audio."""
    measured = loudness_stats(video, "I=-14:TP=-1.5:LRA=11")
    level = float(measured["input_i"])
    if not math.isfinite(level):
        raise RuntimeError("No measurable narration in the HyperFrames export.")
    gain = -14 - level
    staged = video.with_name(video.stem + ".audio-normalized.mp4")
    try:
        run(["-i", str(video), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
             "-af", f"volume={gain:.2f}dB,alimiter=limit=0.84:attack=5:release=50:level=disabled",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(staged)])
        staged.replace(video)
    finally:
        staged.unlink(missing_ok=True)
    (video.parent / "audio-normalization.json").write_text(json.dumps({
        "input_lufs": level, "target_lufs": -14, "gain_db": round(gain, 2),
        "video_stream": "copied without re-encoding"
    }, indent=2), encoding="utf-8")


def render_motion(check: dict | None = None) -> Path:
    check = check if check is not None else check_motion()
    if not check.get("ok") or check.get("browserSkipped"):
        raise RuntimeError("A completed HyperFrames browser check is required before rendering.")
    PREVIEW_VIDEO.parent.mkdir(parents=True, exist_ok=True)
    (PREVIEW_VIDEO.parent / "checks.json").write_text(json.dumps(check, indent=2), encoding="utf-8")
    result = run_motion(["render", "--quality", "looks", "--fps", "30", "--workers", "1",
                         "--low-memory-mode", "--output", str(PREVIEW_VIDEO)], timeout=1800)
    require_success(result)
    if not PREVIEW_VIDEO.is_file() or not PREVIEW_VIDEO.stat().st_size:
        raise RuntimeError("HyperFrames returned without a non-empty video.")
    normalize_export_audio(PREVIEW_VIDEO)
    return PREVIEW_VIDEO
