"""Shorts Factory: 3-5 vertical Shorts cut from each long video, for extra reach from work already done.

Shorts are re-rendered, not cropped: a slice of the long script and its voiceover goes through the
assembler again at 1080x1920, so text cards, chat windows and captions are laid out for a phone.
The first visual of each Short becomes a hook card, and the description points to the full video.

Segment choice: the LLM picks self-contained 30-58s passages with a strong opening; if no LLM is
available, a scoring rule picks them (chapter starts, numbers, graphics, no verdict/affiliate/cta lines).
"""
import json
import re
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from core.llm import LLM, LLMError
from core.log import get_logger
from core.media import run
from production.assembler import Assembler
from production.storyboard import recording_path, screen_references
from writing.models import Script
from writing.prompts import channel_context
from writing.quality_gate import words

log = get_logger("shorts")

SKIP_SECTIONS = {"verdict", "affiliate", "cta", "outro"}
NUMBER_RE = re.compile(r"\d")


class Segment(BaseModel):
    start_line: int
    end_line: int = Field(description="Inclusive")
    hook: str = Field(description="On-screen hook for the first seconds, 6 words or fewer")
    title: str = Field(description="Short title, 50 characters or fewer, no hashtags")


class ShortsPlan(BaseModel):
    segments: list[Segment]


def segment_seconds(timings: dict, start: int, end: int) -> float:
    by_line = {t["line"]: t for t in timings["lines"]}
    return by_line[end]["end"] - by_line[start]["start"]


def usable(script: Script, timings: dict, seg: Segment, lo: float, hi: float) -> bool:
    voiced = {t["line"] for t in timings["lines"]}
    lines = range(seg.start_line, seg.end_line + 1)
    return (0 <= seg.start_line <= seg.end_line < len(script.lines)
            and len(words(script.lines[seg.start_line].text)) <= 25
            and all(i in voiced and script.lines[i].section not in SKIP_SECTIONS for i in lines)
            and lo <= segment_seconds(timings, seg.start_line, seg.end_line) <= hi)


def heuristic_segments(script: Script, timings: dict, count: int, lo: float, hi: float) -> list[Segment]:
    """Grow a window from each candidate start until it lasts lo-hi seconds; score and keep the best non-overlapping."""
    voiced = [t["line"] for t in timings["lines"]]
    candidates = []
    for k, start in enumerate(voiced):
        line = script.lines[start]
        if line.section in SKIP_SECTIONS:
            continue
        if len(words(line.text)) > 25:
            continue
        for end in voiced[k:]:
            if script.lines[end].section in SKIP_SECTIONS:
                break
            secs = segment_seconds(timings, start, end)
            if secs > hi:
                break
            if secs >= lo:
                block = script.lines[start:end + 1]
                score = (2.0 * bool(line.chapter) + 1.0 * (line.section == "pattern_break")
                         + 0.5 * sum(bool(NUMBER_RE.search(b.text)) for b in block)
                         + 0.3 * sum(b.visual.split(":")[0].strip().upper() in ("CHAT", "CODE", "LIST") for b in block))
                hook = (line.chapter or re.sub(r"[^\w$%,' ]", "", line.text))
                candidates.append((score, Segment(start_line=start, end_line=end,
                                                  hook=" ".join(hook.split()[:6]), title=" ".join(hook.split()[:8]))))
                break
    chosen: list[Segment] = []
    for _, seg in sorted(candidates, key=lambda c: -c[0]):
        if all(seg.end_line < c.start_line or seg.start_line > c.end_line for c in chosen):
            chosen.append(seg)
        if len(chosen) == count:
            break
    return sorted(chosen, key=lambda s: s.start_line)


def llm_segments(cfg: dict, llm: LLM, script: Script, timings: dict, count: int, lo: float, hi: float) -> list[Segment]:
    by_line = {t["line"]: t for t in timings["lines"]}
    rows = [f"[{i}] ({l.section}, {by_line[i]['end'] - by_line[i]['start']:.1f}s) {l.text}"
            for i, l in enumerate(script.lines) if i in by_line]
    system = (f"{channel_context(cfg)}\n\nYou are a short-form editor cutting YouTube Shorts from a long video. "
              f"A good Short makes complete sense on its own, opens with its strongest line, and leaves the viewer "
              f"wanting the full video.")
    prompt = (f"Pick {count} non-overlapping segments of consecutive lines, each {lo:.0f}-{hi:.0f} seconds long "
              f"(add up the per-line seconds). Never include verdict, affiliate, cta or outro lines. For each, write "
              f"an on-screen hook (6 words or fewer) and a title (50 characters or fewer). The first spoken line "
              f"must be 25 words or fewer and make sense without earlier context. Never invent claims.\n\n"
              f"Script lines:\n" + "\n".join(rows))
    plan = llm.json(system, prompt, ShortsPlan)
    good = []
    for s in plan.segments:
        if usable(script, timings, s, lo, hi) and all(s.end_line < c.start_line or s.start_line > c.end_line for c in good):
            good.append(s)
    if len(good) < len(plan.segments):
        log.info("Dropped %d LLM segment(s) that broke the length or section rules", len(plan.segments) - len(good))
    return good[:count]


def pick_segments(cfg: dict, llm: LLM | None, script: Script, timings: dict) -> list[Segment]:
    s = cfg["shorts"]
    lo, hi, count = s["min_seconds"], s["max_seconds"], s["per_video"]
    segments: list[Segment] = []
    if llm:
        try:
            segments = llm_segments(cfg, llm, script, timings, count, lo, hi)
        except LLMError as exc:
            log.warning("LLM unavailable for Shorts selection, using the scoring rule: %s", exc)
    if len(segments) < count:
        extra = [h for h in heuristic_segments(script, timings, count, lo, hi)
                 if all(h.end_line < c.start_line or h.start_line > c.end_line for c in segments)]
        segments += extra[:count - len(segments)]
    return sorted(segments, key=lambda seg: seg.start_line)


def build_short(long_folder: Path, n: int, seg: Segment, script: Script, timings: dict, long_title: str,
                cfg: dict) -> Path:
    """Folder with the sliced script, voice and timings, ready for the assembler."""
    folder = long_folder / "shorts" / f"short_{n}"
    folder.mkdir(parents=True, exist_ok=True)
    by_line = {t["line"]: t for t in timings["lines"]}
    start = max(0.0, by_line[seg.start_line]["start"] - 0.05)
    end = by_line[seg.end_line]["end"] + 0.25
    run(["-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(long_folder / "voice.wav"), "-ar", "48000",
         str(folder / "voice.wav")])

    lines = [l.model_copy() for l in script.lines[seg.start_line:seg.end_line + 1]]
    # Keep each step tied to its original recording, even when its line number changes in a Short.
    for i, (_, name, _) in screen_references(script).items():
        if seg.start_line <= i <= seg.end_line:
            source = recording_path(long_folder, name)
            if source.is_file():
                (folder / "screen").mkdir(exist_ok=True)
                shutil.copy2(source, recording_path(folder, name))
            lines[i - seg.start_line] = lines[i - seg.start_line].model_copy(update={"recording": name})
    # Preserve a real demonstration at the opening; a title card must not replace the proof.
    if lines[0].visual.partition(":")[0].strip().upper() not in ("SCREEN", "FLOW", "COMPARE"):
        lines[0] = lines[0].model_copy(update={"visual": f"TEXT: {seg.hook}", "chapter": None})
    lines[0] = lines[0].model_copy(update={"section": "hook", "chapter": None})
    lines = [l if i == 0 else l.model_copy(update={"chapter": None}) for i, l in enumerate(lines)]
    short = Script(working_title=seg.title, angle=script.angle, presentation=script.presentation,
                   tools=script.tools, lines=lines)
    sliced = {"voice": timings["voice"], "duration": round(end - start, 3), "lines": [
        {**by_line[i], "line": i - seg.start_line, "start": round(by_line[i]["start"] - start, 3),
         "end": round(by_line[i]["end"] - start, 3)} for i in range(seg.start_line, seg.end_line + 1)]}
    (folder / "timings.json").write_text(json.dumps(sliced, indent=1, ensure_ascii=False), encoding="utf-8")
    framing_file = long_folder / "screen-framing.json"
    if framing_file.exists():
        framing = json.loads(framing_file.read_text(encoding="utf-8"))
        reframed = {str(i-seg.start_line): framing[str(i)] for i in range(seg.start_line, seg.end_line+1) if str(i) in framing}
        (folder / "screen-framing.json").write_text(json.dumps(reframed, indent=2), encoding="utf-8")

    tags = cfg["shorts"]["hashtags"]
    description = f"{seg.title}\n\nFull video: {long_title} (link in the related video above)\n\n{' '.join(tags)}"
    (folder / "pack.json").write_text(json.dumps({
        "format": "short", "from_long_video": long_folder.name, "lines": [seg.start_line, seg.end_line],
        "script": short.model_dump(), "seo": {"title": seg.title, "hook": seg.hook, "description": description,
                                              "hashtags": tags}}, indent=2, ensure_ascii=False), encoding="utf-8")
    (folder / "seo.md").write_text(f"# {seg.title}\n\n```text\n{description}\n```\n\nHook on screen: {seg.hook}\n"
                                   f"In YouTube Studio, set this Short's 'Related video' to the full video.\n",
                                   encoding="utf-8")
    return folder


def make_shorts(long_folder: Path, cfg: dict, llm: LLM | None, assembler: Assembler | None = None) -> list[Path]:
    pack = json.loads((long_folder / "pack.json").read_text(encoding="utf-8"))
    script = Script.model_validate(pack["script"])
    timings = json.loads((long_folder / "timings.json").read_text(encoding="utf-8"))
    long_title = pack["seo"]["titles"][0]["title"]
    segments = pick_segments(cfg, llm, script, timings)
    if not segments:
        log.warning("No segment of %s fits a Short", long_folder.name)
        return []
    assembler = assembler or Assembler(cfg)
    videos = []
    for n, seg in enumerate(segments, 1):
        folder = build_short(long_folder, n, seg, script, timings, long_title, cfg)
        short = Script.model_validate(json.loads((folder / "pack.json").read_text(encoding="utf-8"))["script"])
        log.info("Short %d: lines %d-%d, %.0fs, '%s'", n, seg.start_line, seg.end_line,
                 segment_seconds(timings, seg.start_line, seg.end_line), seg.title)
        videos.append(assembler.assemble(short, "short", folder))
    return videos
