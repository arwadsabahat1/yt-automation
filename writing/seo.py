"""SEO Engine: titles, description (with chapters, tool links and affiliate disclosure), tags and extras."""
from core.llm import LLM
from research.scorer import contains_any
from writing.models import Script, SEOPack
from writing.prompts import seo_prompt
from writing.quality_gate import words

MIN_CHAPTER_GAP_S = 10  # YouTube ignores chapters shorter than 10 seconds
DISCLOSURE = "Some links above are affiliate links: if you sign up, I may earn a commission at no extra cost to you."


def timestamp(seconds: int) -> str:
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def chapters(script: Script, words_per_minute: int) -> list[tuple[int, str]]:
    """Chapter start times estimated from narration speed. The first chapter is forced to 0:00, as YouTube requires."""
    result: list[tuple[int, str]] = []
    spoken = 0
    for line in script.lines:
        if line.chapter:
            start = 0 if not result else round(spoken * 60 / words_per_minute)
            if not result or start - result[-1][0] >= MIN_CHAPTER_GAP_S:
                result.append((start, line.chapter))
        spoken += len(words(line.text))
    return result if len(result) >= 3 else []  # YouTube needs at least 3 chapters to show any


def affiliate_tools(script: Script, cfg: dict) -> list[str]:
    covered = " ".join(script.tools)
    return [tool for tool in cfg["affiliate_tools"] if contains_any(covered, [tool])]


def build_description(pack: SEOPack, script: Script, cfg: dict, fmt: str,
                      marks: list[tuple[int, str]] | None = None) -> str:
    """`marks` are real chapter start times from the recorded voice; without them they're estimated."""
    parts = [pack.description.strip()]
    if fmt == "long":
        marks = marks if marks is not None else chapters(script, cfg["writing"]["words_per_minute"])
        if marks:
            parts.append("Chapters\n" + "\n".join(f"{timestamp(t)} {name}" for t, name in marks))
    tools = affiliate_tools(script, cfg)
    if tools:
        links = cfg.get("affiliate_links") or {}
        parts.append("Tools mentioned\n" + "\n".join(
            f"- {tool.title()}: {links.get(tool) or f'[ADD AFFILIATE LINK: {tool}]'}" for tool in tools))
        parts.append(DISCLOSURE)
    parts.append(" ".join(h if h.startswith("#") else f"#{h}" for h in pack.hashtags))
    return "\n\n".join(parts)


def write_seo(cfg: dict, llm: LLM, script: Script, fmt: str, searches: list[str], source_title: str) -> SEOPack:
    summary = {"working_title": script.working_title, "angle": script.angle, "tools": script.tools,
               "chapters": [line.chapter for line in script.lines if line.chapter]}
    system, prompt = seo_prompt(cfg, summary, fmt, searches, source_title)
    return llm.json(system, prompt, SEOPack)
