"""Finding and running FFmpeg. winget installs it outside the PATH of shells that were already open."""
import json
import os
import shutil
import subprocess
from functools import cache
from pathlib import Path

WINGET_LINKS = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"


@cache
def tool(name: str) -> str:
    """Full path to ffmpeg or ffprobe."""
    found = shutil.which(name) or (WINGET_LINKS / f"{name}.exe" if (WINGET_LINKS / f"{name}.exe").exists() else None)
    if not found:
        raise SystemExit(f"{name} not found. Install it with: winget install --id Gyan.FFmpeg -e")
    return str(found)


def run(args: list[str], cwd: Path | None = None) -> None:
    """Run ffmpeg quietly; raise with its error output if it fails."""
    result = subprocess.run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *args], cwd=cwd,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {result.returncode}): {result.stderr.strip()[-800:]}\n"
                           f"command: ffmpeg {' '.join(args)[-1500:]}")


def loudness_stats(path: Path, target: str) -> dict:
    """First loudnorm pass: measure the file so the second pass can correct it exactly."""
    result = subprocess.run([tool("ffmpeg"), "-hide_banner", "-i", str(path), "-af", f"loudnorm={target}:print_format=json",
                             "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    report = result.stderr[result.stderr.rfind("{"):result.stderr.rfind("}") + 1]
    return json.loads(report)


def duration(path: Path) -> float:
    out = subprocess.run([tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)
