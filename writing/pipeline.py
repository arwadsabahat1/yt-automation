"""Runs Phase 2 for each planned calendar slot: script -> quality gate -> SEO -> files the owner can review."""
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

from core.config import OUTPUT_DIR
from core.llm import LLM, LLMError
from core.log import get_logger
from research.signals import autocomplete, search_terms
from research.youtube import YouTubeClient
from writing.models import Review, Script, SEOPack
from writing.quality_gate import gate, passes, words
from writing.script import load_notes, write_script
from writing.seo import build_description, write_seo

log = get_logger("writing")

VIDEOS_DIR = OUTPUT_DIR / "videos"
PLACEHOLDER_RE = re.compile(r"\[YOUR VERDICT:[^\]]*\]")


def pending_slots(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.id AS plan_id, p.slot_date, p.format, i.id AS idea_id, i.topic, i.score, i.reasons,
                  v.video_id, v.title, v.keyword
           FROM plan p JOIN ideas i ON i.id = p.idea_id JOIN videos v ON v.video_id = i.video_id
           WHERE p.id NOT IN (SELECT plan_id FROM scripts) AND p.slot_date >= ?
           ORDER BY p.slot_date, p.format LIMIT ?""",
        (date.today().isoformat(), limit),
    ).fetchall()


def searches_for(conn: sqlite3.Connection, topic: str, keyword: str, cfg: dict) -> list[str]:
    """Real YouTube searches near this topic: fresh autocomplete for its key terms plus the seed keyword's list."""
    found: list[str] = []
    try:
        found = autocomplete(search_terms(topic, max_words=3), cfg["language"], cfg["region"])
    except Exception as exc:  # bonus signal only
        log.warning("Autocomplete failed for '%s': %s", topic, exc)
    row = conn.execute("SELECT suggestions FROM signals WHERE keyword = ?", (keyword,)).fetchone()
    stored = json.loads(row["suggestions"]) if row else []
    return list(dict.fromkeys(found + stored))


def slug(text: str, max_len: int = 50) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_len].rstrip("-")


def write_slots(cfg: dict, conn: sqlite3.Connection, llm: LLM, yt: YouTubeClient, limit: int) -> list[dict]:
    notes = load_notes()
    past = [json.loads(r["script"]) for r in conn.execute("SELECT script FROM scripts")]
    past_texts = [Script.model_validate(p).text for p in past]
    results = []
    for slot in pending_slots(conn, limit):
        log.info("Writing %s %s: %s", slot["slot_date"], slot["format"], slot["topic"])
        source = next(iter(yt.videos([slot["video_id"]])), {"title": slot["title"], "description": ""})
        idea = {"topic": slot["topic"], "format": slot["format"], "reasons": json.loads(slot["reasons"]),
                "searches": searches_for(conn, slot["topic"], slot["keyword"], cfg)}
        from analytics.brain import prompt_feedback
        episode_cfg = {**cfg, '_analytics_feedback': prompt_feedback(conn, cfg, slot['format'])}
        try:
            script, brief = write_script(episode_cfg, llm, idea, source, notes)
            script, review, code_issues, rewrites = gate(episode_cfg, llm, script, slot["format"], brief, past_texts)
            seo = write_seo(episode_cfg, llm, script, slot["format"], idea["searches"], source["title"])
        except LLMError as exc:
            log.error("Stopping: %s", exc)
            break
        status = "ready" if passes(review, code_issues, cfg) else "blocked"
        folder = save(slot, script, review, code_issues, seo, cfg, status, rewrites, llm.last_used)
        conn.execute(
            """INSERT INTO scripts (plan_id, idea_id, status, quality, script, review, seo, folder, provider, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (slot["plan_id"], slot["idea_id"], status, review.score, script.model_dump_json(),
             review.model_dump_json(), seo.model_dump_json(), str(folder), llm.last_used,
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        past_texts.append(script.text)
        results.append({"slot": slot["slot_date"], "format": slot["format"], "title": seo.titles[0].title,
                        "status": status, "quality": review.score, "folder": folder})
        log.info("%s (%.1f/10) -> %s", status.upper(), review.score, folder)
    return results


def save(slot: sqlite3.Row, script: Script, review: Review, code_issues: list[str], seo: SEOPack,
         cfg: dict, status: str, rewrites: int, provider: str) -> Path:
    folder = VIDEOS_DIR / f"{slot['slot_date']}_{slot['format']}_{slug(script.working_title)}"
    folder.mkdir(parents=True, exist_ok=True)
    description = build_description(seo, script, cfg, slot["format"])
    (folder / "script.md").write_text(script_markdown(slot, script, review, code_issues, status, rewrites, provider),
                                      encoding="utf-8")
    (folder / "seo.md").write_text(seo_markdown(seo, description), encoding="utf-8")
    (folder / "pack.json").write_text(json.dumps({
        "slot_date": slot["slot_date"], "format": slot["format"], "status": status,
        "source_video": f"https://youtu.be/{slot['video_id']}",
        "script": script.model_dump(), "review": review.model_dump(), "automated_issues": code_issues,
        "seo": {**seo.model_dump(), "full_description": description},
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    from production.storyboard import build_storyboard, save_storyboard
    save_storyboard(build_storyboard(script, slot["format"], cfg, folder), folder)
    return folder


def dedupe(items: list[str], threshold: int = 70) -> list[str]:
    """Drop near-duplicates: the writer and the reviewer often flag the same fact in different words."""
    kept: list[str] = []
    for item in items:
        if not any(fuzz.token_set_ratio(item.lower(), k.lower()) >= threshold for k in kept):
            kept.append(item)
    return kept


def script_markdown(slot: sqlite3.Row, script: Script, review: Review, code_issues: list[str],
                    status: str, rewrites: int, provider: str) -> str:
    day = date.fromisoformat(slot["slot_date"])
    out = [f"# {script.working_title}", "",
           f"**{day:%a %d %b %Y} · {slot['format']} · {status.upper()}** · quality {review.score}/10 "
           f"(hook {review.hook}, retention {review.retention}, accuracy {review.accuracy}, originality "
           f"{review.originality}, policy {review.policy}, clarity {review.clarity}) · "
           f"{len(words(script.text))} words · {rewrites} rewrite(s) · written by {provider}", "",
           f"**Angle:** {script.angle}  ",
           f"**Idea source:** [{slot['title']}](https://youtu.be/{slot['video_id']}) (score {slot['score']})", ""]

    if status == "blocked":
        out += ["## Why it's blocked", *[f"- {i}" for i in code_issues],
                *[f"- Line {i.line}: {i.problem} → {i.fix}" for i in review.issues], ""]

    checks = dedupe([c for line in script.lines for c in line.checks] + review.unverifiable)
    placeholders = PLACEHOLDER_RE.findall(script.text)
    screens = [line.visual for line in script.lines if line.visual.strip().upper().startswith("SCREEN:")]
    out += ["## Before you approve"]
    out += [f"- [ ] Fill in: {p}" for p in placeholders]
    out += [f"- [ ] Verify: {c}" for c in checks]
    if screens:
        out += [f"- [ ] Record {len(screens)} screen clip(s), listed under the lines marked 🖥"]
    if not (placeholders or checks or screens):
        out.append("- Nothing flagged.")
    out.append("")

    out.append("## Script")
    for line in script.lines:
        if line.chapter:
            out += ["", f"### {line.chapter}"]
        icon = "🖥" if line.visual.strip().upper().startswith("SCREEN:") else "🎬"
        out += ["", f"`{line.section}` {line.text}", f"> {icon} {line.visual}"]
    return "\n".join(out) + "\n"


def seo_markdown(seo: SEOPack, description: str) -> str:
    out = ["# SEO pack", "", "## Title options"]
    out += [f"{n}. **{t.title}** ({len(t.title)} chars, "
            + (f"click {t.click_score}/10" if t.click_score is not None else "click appeal untested") + f"): {t.why}"
            for n, t in enumerate(seo.titles, 1)]
    out += ["", "## Description (copy-paste)", "```text", description, "```", "",
            "## Tags (copy-paste)", "```text", ", ".join(seo.tags), "```", "",
            "## Pinned comment", seo.pinned_comment, "",
            "## Thumbnail text options", *[f"- {t}" for t in seo.thumbnail_text], "",
            "## End screen", *[f"- {e}" for e in seo.end_screen]]
    return "\n".join(out) + "\n"
