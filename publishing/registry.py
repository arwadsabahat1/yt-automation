"""Every publishable video in one list: long videos, standalone Shorts and Shorts cut from long videos.

Status lives next to each video in approval.json, so it survives re-renders and works the same for all three
kinds:  pending -> approved -> uploaded  (or rejected, with a reason).
"""
import json
import sqlite3
import subprocess
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core.media import tool
from core.config import ROOT
from publishing.checks import Check, run_checks, save_checks
from writing.pipeline import VIDEOS_DIR

STATUSES = ("pending", "approved", "rejected", "uploaded")


@dataclass
class Item:
    id: str  # folder path relative to VIDEOS_DIR, with forward slashes
    folder: Path
    fmt: str  # long | short
    kind: str  # long | short | cut (a Short cut from a long video)
    publish_date: date
    title: str
    status: str = "pending"
    note: str = ""
    youtube_url: str = ""
    checks: list[Check] = field(default_factory=list)
    seconds: float = 0.0
    image: str = ""  # path under VIDEOS_DIR for the list view: a thumbnail for long videos, a poster frame for Shorts

    @property
    def length(self) -> str:
        m, s = divmod(round(self.seconds), 60)
        return f"{m}:{s:02d}" if self.seconds else ""

    @property
    def fails(self) -> int:
        return sum(c.status == "fail" for c in self.checks)

    @property
    def warns(self) -> int:
        return sum(c.status == "warn" for c in self.checks)

    @property
    def blocked(self) -> bool:
        return any(c.status == "fail" for c in self.checks)

    @property
    def rendered(self) -> bool:
        return (self.folder / "video.mp4").exists()


def item_id(folder: Path) -> str:
    return folder.relative_to(VIDEOS_DIR).as_posix()


def folder_of(item: str) -> Path:
    path = (VIDEOS_DIR / item).resolve()
    if VIDEOS_DIR.resolve() not in path.parents:  # ids come from the browser: never leave the videos folder
        raise ValueError(f"bad item id: {item}")
    return path


def read_approval(folder: Path) -> dict:
    path = folder / "approval.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "pending"}


def content_fingerprint(folder: Path) -> str:
    """Bind approval to the finished media, metadata, subtitles and thumbnail choices."""
    digest = hashlib.sha256()
    files = [folder/n for n in ('video.mp4','pack.json','timings.json','captions.srt')]
    files += sorted((folder/'thumbnails').glob('thumb_*.jpg'))
    review = folder / 'upload-pack' / 'owner-review.json'
    if review.exists():
        # The review binds the exact external metadata/cover/caption files by SHA256.
        files.append(review)
    for path in files:
        if not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError('Publication assets must stay inside the video folder')
        digest.update(path.relative_to(folder).as_posix().encode())
        if path.exists():
            with path.open('rb') as f:
                for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
        else:digest.update(b'<missing>')
    # Description links and publication options are derived from this configuration.
    config = ROOT / 'config.yaml'
    if config.exists():
        digest.update(config.read_bytes())
    return digest.hexdigest()


def write_approval(folder: Path, status: str, note: str = "", youtube_url: str = "") -> dict:
    if status not in STATUSES:
        raise ValueError(status)
    data = {**read_approval(folder), "status": status, "note": note, "at": datetime.now(timezone.utc).isoformat()}
    if status == 'approved':
        data['content_fingerprint'] = content_fingerprint(folder)
    if youtube_url:
        data["youtube_url"] = youtube_url
    (folder / "approval.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    return data


def cached_checks(folder: Path, fmt: str, cfg: dict) -> list[Check]:
    """Re-run the checks only when the video, thumbnails or script changed since the last run."""
    saved = folder / "checks.json"
    inputs = [folder / n for n in ("video.mp4", "pack.json", "render.json", "screen-framing.json", "motion-scenes.json")] + [folder / "thumbnails" / "thumbnails.json"]
    inputs += [ROOT / n for n in ("config.yaml", "publishing/checks.py", "production/storyboard.py", "writing/quality_gate.py")]
    inputs += list((folder / "screen").glob("*.mp4"))
    inputs += list((folder / "motion").glob("*.mp4"))
    newest = max((p.stat().st_mtime for p in inputs if p.exists()), default=0)
    if saved.exists() and saved.stat().st_mtime >= newest:
        return [Check(**c) for c in json.loads(saved.read_text(encoding="utf-8"))]
    checks = run_checks(folder, fmt, cfg)
    save_checks(folder, checks)
    return checks


def poster(folder: Path) -> Path | None:
    """A still frame for the list view (cached), so the overview never has to load whole videos.
    Taken at 1s, when a Short's hook card is fully on screen."""
    video, out = folder / "video.mp4", folder / "poster.jpg"
    if not video.exists():
        return None
    if not out.exists() or out.stat().st_mtime < video.stat().st_mtime:
        subprocess.run([tool("ffmpeg"), "-v", "error", "-y", "-ss", "1", "-i", str(video), "-frames:v", "1",
                        "-vf", "scale=-2:360", "-q:v", "4", str(out)], capture_output=True)
    return out if out.exists() else None


def video_seconds(folder: Path) -> float:
    out = subprocess.run([tool("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(folder / "video.mp4")], capture_output=True, text=True).stdout.strip()
    return float(out) if out else 0.0


def title_of(folder: Path) -> str:
    pack = json.loads((folder / "pack.json").read_text(encoding="utf-8"))
    seo = pack.get("seo", {})
    if seo.get("titles"):
        return seo["titles"][0]["title"]
    return seo.get("title") or pack["script"]["working_title"]


def collect(conn: sqlite3.Connection, cfg: dict, with_checks: bool = True) -> list[Item]:
    items = []
    rows = conn.execute("""SELECT s.folder, p.slot_date, p.format FROM scripts s JOIN plan p ON p.id = s.plan_id
                           WHERE s.status IN ('ready', 'approved') ORDER BY p.slot_date, p.format""").fetchall()
    for row in rows:
        folder = Path(row["folder"])
        if not (folder / "pack.json").exists():
            continue
        slot = date.fromisoformat(row["slot_date"])
        items.append(Item(item_id(folder), folder, row["format"], row["format"], slot, title_of(folder)))
        # Shorts cut from a long video go out on the following days, one a day, to keep the channel active.
        for n, cut in enumerate(sorted((folder / "shorts").glob("short_*")), 1):
            if (cut / "pack.json").exists():
                items.append(Item(item_id(cut), cut, "short", "cut", slot + timedelta(days=n), title_of(cut)))
    for item in items:
        approval = read_approval(item.folder)
        item.status, item.note = approval["status"], approval.get("note", "")
        item.youtube_url = approval.get("youtube_url", "")
        if with_checks and item.rendered:
            item.checks = cached_checks(item.folder, item.fmt, cfg)
            item.seconds = video_seconds(item.folder)
            thumb = item.folder / "thumbnails" / "thumb_1_split.jpg"
            still = thumb if thumb.exists() else poster(item.folder)
            item.image = item_id(still.parent) + "/" + still.name if still else ""
    return sorted(items, key=lambda i: (i.publish_date, i.kind != "long", i.id))
