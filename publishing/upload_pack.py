"""Upload pack: everything needed to publish an approved video in YouTube Studio in about two minutes.

Written to <video folder>/upload/: title, description (with real chapter times from the recorded voice),
tags, the publish time, and a Studio checklist. Uploading by hand is needed until Google audits the API
project; until then API uploads are locked to private.
"""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from publishing.registry import Item
from writing.models import Script, SEOPack
from writing.seo import MIN_CHAPTER_GAP_S, build_description

TAG_LIMIT = 500  # YouTube's limit for all tags together


def real_chapters(folder: Path, script: Script) -> list[tuple[int, str]]:
    """Chapter starts from timings.json. YouTube needs the first at 0:00, at least 3, each 10s+ long."""
    timings = json.loads((folder / "timings.json").read_text(encoding="utf-8"))
    marks: list[tuple[int, str]] = []
    for t in timings["lines"]:
        name = script.lines[t["line"]].chapter
        if not name:
            continue
        start = 0 if not marks else int(t["start"])
        if not marks or start - marks[-1][0] >= MIN_CHAPTER_GAP_S:
            marks.append((start, name))
    return marks if len(marks) >= 3 else []


def fit_tags(tags: list[str]) -> list[str]:
    kept, used = [], 0
    for tag in dict.fromkeys(t.strip() for t in tags if t.strip()):
        cost = len(tag) + (2 if " " in tag else 0) + (1 if kept else 0)  # YouTube counts quotes and commas
        if used + cost > TAG_LIMIT:
            break
        kept.append(tag)
        used += cost
    return kept


def publish_time(item: Item, cfg: dict) -> tuple[datetime, datetime]:
    """The slot's publish moment in the audience's time zone, and the same moment on this laptop's clock."""
    p = cfg["publishing"]
    hour, minute = (int(x) for x in p["time"].split(":"))
    audience = datetime(item.publish_date.year, item.publish_date.month, item.publish_date.day, hour, minute,
                        tzinfo=ZoneInfo(p["timezone"]))
    return audience, audience.astimezone()


def build(item: Item, cfg: dict) -> dict:
    pack = json.loads((item.folder / "pack.json").read_text(encoding="utf-8"))
    script = Script.model_validate(pack["script"])
    if item.kind == "cut":
        seo = pack["seo"]
        title = seo["title"][:95]
        description, tags = seo["description"], []
        thumbnails = []
    else:
        seo_pack = SEOPack.model_validate(pack["seo"])
        title = seo_pack.titles[0].title
        marks = real_chapters(item.folder, script) if item.fmt == "long" and (item.folder / "timings.json").exists() else None
        description = build_description(seo_pack, script, cfg, item.fmt, marks)
        tags = fit_tags(seo_pack.tags)
        thumbnails = sorted(p.name for p in (item.folder / "thumbnails").glob("thumb_*.jpg"))
    audience, local = publish_time(item, cfg)
    info = {
        "title": title, "description": description, "tags": ", ".join(tags), "format": item.fmt,
        "publish_audience": audience.strftime("%a %d %b %Y, %H:%M %Z"),
        "publish_local": local.strftime("%a %d %b %Y, %H:%M (your time)"),
        "video": str(item.folder / "video.mp4"), "captions": str(item.folder / "captions.srt"),
        "thumbnails": [str(item.folder / "thumbnails" / t) for t in thumbnails],
        "pinned_comment": pack.get("seo", {}).get("pinned_comment", ""),
        "steps": studio_steps(item, bool(thumbnails)),
    }
    out = item.folder / "upload"
    out.mkdir(exist_ok=True)
    (out / "title.txt").write_text(title, encoding="utf-8")
    (out / "description.txt").write_text(description, encoding="utf-8")
    (out / "tags.txt").write_text(info["tags"], encoding="utf-8")
    (out / "upload.json").write_text(json.dumps(info, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "README.md").write_text(readme(info), encoding="utf-8")
    return info


def studio_steps(item: Item, has_thumbnails: bool) -> list[str]:
    steps = ["In YouTube Studio click Create → Upload videos and choose video.mp4.",
             "Paste the title and description (copy buttons in the digest, or title.txt / description.txt)."]
    if item.fmt == "long":
        steps += ["Thumbnail: click Test & Compare and add all 3 thumbnails, so YouTube keeps the one people click most.",
                  "Show more → Tags: paste tags.txt.",
                  "Video elements: add an end screen (subscribe + best-for-viewer video) over the last 15 seconds."]
    else:
        steps += ["Shorts: add a related video. Point it to the full video if this Short was cut from one."]
    steps += ["Subtitles: upload captions.srt (English). It helps search and viewers watching with the sound off.",
              "Audience: 'No, it's not made for kids'.",
              "Altered content: 'No' (AI narration and stock footage of no real person saying or doing anything "
              "they didn't). Say 'Yes' if a video ever shows a realistic AI-made person or event.",
              "Visibility: Schedule, at the publish time shown here.",
              "After publishing: post the pinned comment and pin it, then click 'Mark as uploaded' in the digest "
              "and paste the video's link."]
    return steps


def readme(info: dict) -> str:
    out = [f"# Upload: {info['title']}", "", f"**Publish:** {info['publish_audience']} = {info['publish_local']}", "",
           "## Steps", *[f"{n}. {s}" for n, s in enumerate(info["steps"], 1)], "",
           "## Title", "```text", info["title"], "```", "", "## Description", "```text", info["description"], "```"]
    if info["tags"]:
        out += ["", "## Tags", "```text", info["tags"], "```"]
    if info["pinned_comment"]:
        out += ["", "## Pinned comment", info["pinned_comment"]]
    return "\n".join(out) + "\n"
