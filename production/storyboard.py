"""Editorial preflight and recording brief. Thresholds are house rules, not retention predictions."""
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from writing.models import Script
from writing.quality_gate import PLACEHOLDER_RE, code_checks, visual_kind, words


class ProductionBlocked(RuntimeError):
    """Missing evidence or source material must be fixed before an expensive render."""


def recording_path(folder: Path, name: str) -> Path:
    root = (folder / "screen").resolve()
    path = (root / name).resolve()
    if path.parent != root or path.suffix.lower() != ".mp4":
        raise ValueError(f"Recording must be an MP4 filename inside screen/: {name}")
    return path


def screen_references(script: Script) -> dict[int, tuple[int, str, float]]:
    refs = {}
    number = 0
    for i, line in enumerate(script.lines):
        if visual_kind(line.visual) == "SCREEN":
            number += 1
            refs[i] = (number, line.recording or f"{number:02d}.mp4", line.recording_start)
    return refs


@dataclass
class EditorialIssue:
    status: str
    name: str
    detail: str


@dataclass
class Storyboard:
    beats: list[dict]
    metrics: dict
    issues: list[EditorialIssue] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(i.status == "fail" for i in self.issues)


def build_storyboard(script: Script, fmt: str, cfg: dict, folder: Path,
                     timings: dict | None = None, validate_media: bool = False) -> Storyboard:
    settings = cfg.get("editorial", {})
    refs = screen_references(script)
    recorded = {t["line"]: t for t in (timings or {}).get("lines", [])}
    cursor, beats, issues = 0.0, [], []
    for issue in code_checks(script, fmt, cfg, []):
        # Measured speech duration is authoritative for repurposed Shorts; word-count estimates are not.
        if timings and fmt == "short" and issue.startswith(("Too short:", "Too long:")):
            seconds = float(timings.get("duration", 0)) + cfg.get("render", {}).get("tail_seconds", 0)
            if 0 < seconds <= cfg.get("shorts", {}).get("max_seconds", 58):
                continue
        issues.append(EditorialIssue("fail", "Script rules", issue))
    if PLACEHOLDER_RE.search(script.text):
        issues.append(EditorialIssue("fail", "Owner input", "Fill the verdict before generating narration."))
    lengths = {}
    for i, line in enumerate(script.lines):
        kind = visual_kind(line.visual).lower()
        if timings and i in recorded:
            t = recorded[i]
            start, end = t["start"], t["end"]
            if t.get("text") != line.text:
                issues.append(EditorialIssue("fail", "Stale narration", f"Line {i} changed; regenerate voice and timings."))
        else:
            start = cursor
            end = start + len(words(line.text)) * 60 / cfg["writing"]["words_per_minute"]
            if timings and line.text.strip() and not PLACEHOLDER_RE.search(line.text):
                issues.append(EditorialIssue("fail", "Stale narration", f"Line {i} is absent from the voiceover."))
        beat = {"line": i, "start": round(start, 3), "end": round(end, 3), "kind": kind,
                "text": line.text, "visual": line.visual}
        if i in refs:
            _, name, offset = refs[i]
            path = recording_path(folder, name)
            beat.update(recording=f"screen/{name}", recording_start=offset, available=path.is_file())
            if not path.is_file():
                issues.append(EditorialIssue("fail", "Recording needed", f"Line {i}: record {line.visual.partition(':')[2].strip()} → screen/{name}"))
            elif validate_media:
                from core.media import duration
                if name not in lengths:
                    lengths[name] = duration(path)
                next_start = next((recorded[j]["start"] for j in range(i + 1, len(script.lines)) if j in recorded), None)
                needed_end = next_start if timings and next_start is not None else end + cfg.get("render", {}).get("tail_seconds", 0)
                if offset + needed_end - start > lengths[name] + 0.05:
                    issues.append(EditorialIssue("fail", "Recording too short", f"Line {i}: {name} needs footage through {offset + needed_end - start:.1f}s. Demo footage cannot loop."))
        if kind == "text" and end - start > settings.get("max_text_seconds", 5):
            issues.append(EditorialIssue("warn", "Long text hold", f"Line {i}: {end - start:.1f}s on one text card; add a demonstration or a progressive diagram."))
        beats.append(beat)
        cursor = end + cfg.get("voice", {}).get("line_pause", 0.35)
    total = sum(b["end"] - b["start"] for b in beats)
    by_kind = {k: round(sum(b["end"] - b["start"] for b in beats if b["kind"] == k), 2)
               for k in dict.fromkeys(b["kind"] for b in beats)}
    demo = next((b["start"] for b in beats if b["kind"] == "screen"), None)
    graphics = sum(by_kind.get(k, 0) for k in ("text", "chat", "code", "list", "flow", "compare")) / max(total, 0.001)
    opening = settings.get("first_demo_seconds", {}).get(fmt, 30)
    if script.tools and demo is not None and demo > opening:
        issues.append(EditorialIssue("warn", "Opening payoff", f"First demonstration starts at {demo:.0f}s; aim to show the promised result within {opening}s."))
    if fmt == "long" and graphics > settings.get("max_graphics_share", 0.55):
        issues.append(EditorialIssue("warn", "Visual balance", f"{graphics:.0%} of spoken time uses graphics. Replace repeated cards with demonstrations where they explain more."))
    return Storyboard(beats, {"timing": "recorded" if timings else "estimated", "spoken_seconds": round(total, 2),
                             "seconds_by_kind": by_kind, "graphics_share": round(graphics, 3),
                             "first_demo_seconds": demo, "recording_count": len({v[1] for v in refs.values()})}, issues)


def save_storyboard(report: Storyboard, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "storyboard.json").write_text(json.dumps(asdict(report), indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# Storyboard and recording brief", "", "Editorial checks are house rules, not predictions of viewer retention.", "",
             f"Timing: {report.metrics['timing']}. Graphics: {report.metrics['graphics_share']:.0%} of spoken time.", ""]
    lines += [f"- {i.status.upper()} · {i.name}: {i.detail}" for i in report.issues]
    for b in report.beats:
        lines += ["", f"## {b['start']:.1f}–{b['end']:.1f}s · line {b['line']} · {b['kind'].upper()}",
                  b["text"], "", b["visual"]]
        if "recording" in b:
            lines.append(f"Source: {b['recording']} from {b['recording_start']:g}s · {'available' if b['available'] else 'RECORDING NEEDED'}")
    (folder / "storyboard.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def preflight(script: Script, fmt: str, cfg: dict, folder: Path, timings: dict | None = None) -> Storyboard:
    report = build_storyboard(script, fmt, cfg, folder, timings, validate_media=True)
    save_storyboard(report, folder)
    if report.blocked:
        raise ProductionBlocked("; ".join(f"{i.name}: {i.detail}" for i in report.issues if i.status == "fail"))
    return report
