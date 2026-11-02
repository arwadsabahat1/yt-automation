"""Quality Gate: automated checks plus a second LLM acting as a strict editor. Weak scripts are rewritten,
and anything still below the bar is marked blocked so it never reaches production unseen."""
import re

from core.llm import LLM
from core.log import get_logger
from writing.models import Revision, Review, Script
from writing.prompts import review_prompt, revise_prompt

log = get_logger("quality_gate")

VISUAL_KINDS = ("CHAT", "CODE", "LIST", "FLOW", "COMPARE", "TEXT", "STOCK", "SCREEN")
MAX_TEXT_SHARE = 0.35  # the writer aims for 25%; above this a long video feels like a slideshow
# Things stock-footage libraries can't show; these belong in CHAT/CODE/LIST/TEXT graphics
SOFTWARE_RE = re.compile(r"\b(ai|chatbots?|chatgpt|gpt|claude|gemini|software|apps?|dashboards?|interfaces?|ui|"
                         r"code|coding|editor|terminal|website|web ?page|screen ?shots?|prompts?|agents?|"
                         r"workflows?|automations?|algorithms?|pull requests?|repos?|notifications?)\b", re.I)
HOOK_MAX_WORDS = 25
WORD_RE = re.compile(r"[a-z0-9']+")
PLACEHOLDER_RE = re.compile(r"\[YOUR VERDICT:[^\]]*\]")
# First-person stories the anonymous narrator can't truthfully tell
INVENTED_RE = re.compile(r"\b(my (clients?|customers?|students?|results?|channel made)|our clients?|"
                         r"i (took|helped|tested|tried|made|earned|landed|built|ran|use|used|personally)|"
                         r"i've (used|tried|tested|made|earned)|we (helped|used|built|ran))\b", re.I)
SINGLE_SECTIONS = ("cta", "outro", "verdict")


def words(text: str) -> list[str]:
    return WORD_RE.findall(text.lower())


def similarity(a: str, b: str, n: int = 5) -> float:
    """Share of 5-word phrases two scripts have in common (Jaccard). Unrelated scripts score near 0."""
    wa, wb = words(a), words(b)
    sa = {tuple(wa[i:i + n]) for i in range(len(wa) - n + 1)}
    sb = {tuple(wb[i:i + n]) for i in range(len(wb) - n + 1)}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def visual_kind(visual: str) -> str:
    """'CHAT: user: ... | ai: ...' -> 'CHAT'; '' if the cue has no known prefix."""
    prefix = visual.partition(":")[0].strip().upper()
    return prefix if prefix in VISUAL_KINDS else ""


def code_checks(script: Script, fmt: str, cfg: dict, past_scripts: list[str]) -> list[str]:
    w = cfg["writing"]
    lo, hi = w["long_words"] if fmt == "long" else w["short_words"]
    issues = []
    count = len(words(script.text))
    minutes = count / w["words_per_minute"]
    if count < lo:
        issues.append(f"Too short: {count} words, needs at least {lo}. Expand the weakest points with concrete examples.")
    elif fmt == "long" and minutes < w["long_min_minutes"]:
        issues.append(f"Runs about {minutes:.1f} minutes; needs {w['long_min_minutes']}+ for mid-roll ads. "
                      f"Add at least {round((w['long_min_minutes'] - minutes) * w['words_per_minute'])} words of real content.")
    elif count > hi:
        issues.append(f"Too long: {count} words, maximum {hi}. Cut filler and repetition.")
    if not script.lines or script.lines[0].section != "hook":
        issues.append("The first line must be the hook.")
    elif len(words(script.lines[0].text)) > HOOK_MAX_WORDS:
        issues.append(f"Line 0: hook is {len(words(script.lines[0].text))} words; keep it to {HOOK_MAX_WORDS} or fewer.")
    kinds = [visual_kind(line.visual) for line in script.lines]
    bad_visuals = [i for i, k in enumerate(kinds) if not k]
    if bad_visuals:
        issues.append(f"Lines {bad_visuals[:10]}: visual cue must start with {', '.join(k + ':' for k in VISUAL_KINDS)}.")
    screens = [i for i, k in enumerate(kinds) if k == "SCREEN"]
    max_screens = w["max_screen_clips"][fmt]
    recordings = {script.lines[i].recording or f"{n:02d}.mp4" for n, i in enumerate(screens, 1)}
    if len(recordings) > max_screens:
        issues.append(f"Lines {screens}: {len(recordings)} separate SCREEN recordings, maximum {max_screens}. "
                      "Reuse a named recording with recording_start for consecutive steps of the same demo.")
    if (cfg.get("editorial", {}).get("require_demo_for_tools", False) and script.tools
            and script.presentation in ("tutorial", "comparison") and not screens):
        issues.append("This video names tools but has no real SCREEN demonstration. Show the task and visible result.")
    for i, (kind, line) in enumerate(zip(kinds, script.lines)):
        cue = line.visual.partition(":")[2].strip()
        if kind == "FLOW" and not 2 <= len([p for p in cue.split("|") if p.strip()]) <= 4:
            issues.append(f"Line {i}: FLOW needs 2-4 short steps separated by |.")
        if kind == "COMPARE":
            parts = [p.strip() for p in cue.split("|") if p.strip()]
            if not 3 <= len(parts) <= 5 or any(":" not in p or "/" not in p.partition(":")[2] or
                                               not all(v.strip() for v in p.partition(":")[2].split("/", 1))
                                               for p in parts[2:]):
                issues.append(f"Line {i}: COMPARE needs two headers, then 1-3 rows: criterion: A value / B value.")
    unfilmable = [i for i, (k, line) in enumerate(zip(kinds, script.lines))
                  if k == "STOCK" and SOFTWARE_RE.search(line.visual.partition(":")[2])]
    if unfilmable:
        issues.append(f"Lines {unfilmable[:10]}: STOCK cues ask for software, AI or screens, which stock footage "
                      f"doesn't have. Use CHAT:, CODE:, LIST: or TEXT: to show what the line says.")
    text_share = kinds.count("TEXT") / max(1, len(kinds))
    if fmt == "long" and text_share > MAX_TEXT_SHARE:
        issues.append(f"{text_share:.0%} of lines use TEXT cards (max {MAX_TEXT_SHARE:.0%}): it feels like a slideshow. "
                      f"Change some to STOCK scenes of people, or CHAT/CODE/LIST where they fit.")
    for i in range(len(kinds) - 3):
        if kinds[i] and kinds[i] != "SCREEN" and len(set(kinds[i:i + 4])) == 1:
            issues.append(f"Lines {i}-{i + 3}: four {kinds[i]} visuals in a row. Mix the visual types.")
            break
    for section in SINGLE_SECTIONS:
        found = [i for i, line in enumerate(script.lines) if line.section == section]
        if len(found) > 1:
            issues.append(f"Lines {found}: {len(found)} `{section}` lines, only one allowed. Merge or delete the extras.")
    for i, line in enumerate(script.lines):
        if PLACEHOLDER_RE.search(line.text) and PLACEHOLDER_RE.sub("", line.text).strip():
            issues.append(f"Line {i}: a verdict placeholder must stand alone. Delete the opinion written around it.")
        invented = INVENTED_RE.search(line.text)
        if invented:
            issues.append(f"Line {i}: '{invented.group(0)}' is an invented personal story; the narrator has none. "
                          f"Make it a clearly hypothetical example.")
    names = [line.chapter.strip().lower() for line in script.lines if line.chapter]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        issues.append(f"Chapter names used more than once: {repeated}. Each chapter needs its own name; "
                      f"remove `chapter` from all but the first line of a section.")
    if fmt == "long" and sum(1 for line in script.lines if line.chapter) < 3:
        issues.append("Needs at least 3 chapters: set `chapter` on the first line of the intro and of each point.")
    closest = max((similarity(script.text, past) for past in past_scripts), default=0.0)
    if closest > w["max_similarity"]:
        issues.append(f"Too similar to a previous script ({closest:.0%} shared phrases). Change the angle and wording.")
    return issues


def apply_edits(script: Script, revision: Revision) -> Script:
    """Apply line edits from the bottom up, so earlier indexes stay valid. Out-of-range edits are skipped."""
    lines = list(script.lines)
    for edit in sorted(revision.edits, key=lambda e: e.line, reverse=True):
        if edit.action == "insert_after" and -1 <= edit.line < len(lines):
            lines[edit.line + 1:edit.line + 1] = edit.new_lines
        elif edit.action == "replace" and 0 <= edit.line < len(lines) and edit.new_lines:
            lines[edit.line:edit.line + 1] = edit.new_lines
        elif edit.action == "delete" and 0 <= edit.line < len(lines):
            del lines[edit.line]
        else:
            log.info("Skipped invalid edit: %s line %d", edit.action, edit.line)
    return script.model_copy(update={"lines": lines, "tools": revision.tools or script.tools})


def passes(review: Review, code_issues: list[str], cfg: dict) -> bool:
    return not code_issues and review.score >= cfg["writing"]["min_quality"] and review.policy >= 8


def gate(cfg: dict, llm: LLM, script: Script, fmt: str, brief: str,
         past_scripts: list[str]) -> tuple[Script, Review, list[str], int]:
    """Review, rewrite up to `max_rewrites` times, and return the best version with its final review."""
    rewrites = 0
    while True:
        code_issues = code_checks(script, fmt, cfg, past_scripts)
        system, prompt = review_prompt(cfg, script.numbered(), fmt, len(words(script.text)), code_issues)
        review = llm.json(system, prompt, Review)
        log.info("Quality %.1f/10 (policy %d), %d automated issue(s), rewrite %d",
                 review.score, review.policy, len(code_issues), rewrites)
        if passes(review, code_issues, cfg) or rewrites >= cfg["writing"]["max_rewrites"]:
            return script, review, code_issues, rewrites
        fixes = code_issues + [f"Line {i.line}: {i.problem} Fix: {i.fix}" for i in review.issues]
        system, prompt = revise_prompt(cfg, script.numbered(), fmt, fixes, brief)
        script = apply_edits(script, llm.json(system, prompt, Revision))
        rewrites += 1
