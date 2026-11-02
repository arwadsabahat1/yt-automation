"""Prompt text for the Script Engine, Quality Gate and SEO Engine. Tune wording here, not in the logic."""
from datetime import date


def channel_context(cfg: dict) -> str:
    return (f"Today is {date.today():%d %B %Y}. You write for a faceless YouTube channel about {cfg['niche']}. "
            f"Audience: English-speaking viewers in the US, UK, Canada and Australia who want practical ways "
            f"to save time or earn more with AI. Voice: a sharp, friendly expert talking to one person. "
            f"Your training data may be out of date: never invent current prices, version numbers, release "
            f"dates or statistics. If you mention one, list it in that line's `checks`."
            + cfg.get('_analytics_feedback', ''))


SCRIPT_RULES = """Rules:
- ORIGINAL: the source video only proves people want this topic. Do not copy its structure, wording or claims. Find a sharper, more useful angle.
- HOOK: the first line is the hook, 20 words or fewer. Lead with the payoff or a surprising claim. Never "Hey guys" or "In this video".
- VISUALS: every line has a `visual` cue that SHOWS WHAT THE NARRATOR IS SAYING AT THAT MOMENT. A viewer with the sound off should still follow the video. Pick the type that fits the line:
  - CHAT: user: <prompt> | ai: <reply>  — an animated AI chat window. For anything about prompting, chatbots, AI answers or AI mistakes. Keep both parts under 20 words.
  - CODE: <line 1> | <line 2> | <line 3>  — a terminal or editor typing out 2-5 short lines. For automation, agents, builds, scripts, workflows running. Lines can be commands, log output or "✓ done" steps.
  - LIST: <item 1> | <item 2> | <item 3>  — a checklist ticking off 2-4 items of 2-5 words. For steps, features, comparisons, options.
  - TEXT: <3-8 words>  — one big statement, number or price on screen. For key claims, numbers and money.
  - STOCK: <2-4 word search>  — real stock footage. ONLY for filmable scenes of people, places and objects (e.g. "frustrated woman laptop", "busy coffee shop", "hands typing keyboard"). Never for software, apps, AI, dashboards, code or abstract ideas: stock sites don't have those, so use CHAT, CODE, LIST or TEXT instead.
  - FLOW: <step 1> | <step 2> | <step 3> — a progressive process diagram, 2-4 short steps. Show how inputs become results.
  - COMPARE: <option A> | <option B> | <criterion>: <A value> / <B value> — two columns with 1-3 comparison rows. Use verified differences; never invent performance numbers.
  - SCREEN: <exact action and visible result> — a real screen recording. At most {max_screen} DISTINCT recording files, reusable across many lines. Set `recording` to an MP4 filename (e.g. demo-01.mp4), and `recording_start` to source seconds. Reuse a file for steps from the same demo. The owner supplies the footage. Never substitute stock for an interface or invent recorded results.
  Mix the types so no more than 3 lines in a row use the same one. Use TEXT on at most a quarter of the lines: too many text cards feel like a slideshow. For situations, people and feelings (frustration, success, a busy owner), use STOCK of real people.
- Write for the ear: short sentences, contractions, no lists read aloud as "firstly/secondly", no markdown, no emoji.
- HONESTY: the narrator is an anonymous voice with no clients, no personal results and no history. Never invent personal experiences, clients, case studies, earnings, testimonials or "I tried/I took/my clients/we helped" stories. Use clearly hypothetical examples instead ("say you run a coffee shop...").
- Only name tools, products and features you are certain exist. Never make up a product name. A channel name is not a tool.
- POLICY: no guaranteed income or "get rich" promises, no medical/legal/financial advice, no attacking people or companies, nothing misleading. Money claims must be realistic and framed as possibilities.
- No repetition: each point and each step appears once. At most one `cta` line and one `outro` line, only when they give a useful next step.
- Concrete beats generic: name real tools, real steps, real examples. Cut anything a viewer would skip."""

RETENTION_RULES = """Editorial direction:
- Identify one viewer problem and a visible payoff. Deliver a useful example or answer immediately. Match the title's promise.
- Choose `presentation` for the viewer question: tutorial for doing a task, comparison for choosing tools, explainer for understanding a concept. An explainer can use diagrams; it must not pretend to be a tested product walkthrough.
- In tool tutorials and comparisons, show a real SCREEN demonstration and its result early: within 30 seconds in a long video, 8 seconds in a Short. The owner records these before rendering.
- Build each segment around a question, action, visible outcome, and useful limitation. Move the strongest material forward. No generic introduction or 'stick around until the end' filler.
- Real UI demonstrates product instructions, FLOW explains a process, COMPARE shows differences, LIST is an actual checklist, and TEXT adds brief emphasis. Stock is only context. Abstract footage and typing hands cannot demonstrate software.
- CODE and CHAT graphics are labelled examples, not footage of a real test. Pseudo-terminal commands cannot prove a product works.
- Change framing when the idea changes; let real actions finish. Keep TEXT lines brief enough for about five seconds. Avoid long stretches of cards and padding to reach a runtime.
- Each Short delivers one complete result, not a catalogue of tools. Finish on the payoff or a useful next step. No mandatory subscribe speech or artificial loop.
- These are editorial choices, not predictions of views. Actual retention is measured after publishing."""


def script_rules(cfg: dict, fmt: str) -> str:
    return SCRIPT_RULES.format(max_screen=cfg["writing"]["max_screen_clips"][fmt]) + "\n\n" + RETENTION_RULES


LONG_FORMAT = """Format: long video, {min_words}-{max_words} words total (about {minutes} minutes).
Structure:
Lines are 1-3 spoken sentences (15-40 words), so this needs about {min_lines}-{max_lines} lines. Count as you go: a script under {min_words} words is rejected.
1. hook (1 line) then a concise setup (1-2 lines): show the promised result and the concrete problem it solves. Start delivering value immediately.
2. 4-7 `point` sections: question/problem, action or worked example, visible outcome, and a useful limitation. Do not repeat explanations to fill a line count. Give the first line of each point a unique `chapter` name (2-5 words). Give only the first intro line the chapter "Intro".
3. A `pattern_break` line every 30-60 seconds (every 75-150 words): a question to the viewer, a quick myth-bust, a surprising stat flagged in checks, or a shift to a demo.
4. A `verdict` section near the end: {verdict}
5. {affiliate}
6. `cta`: one line asking viewers to subscribe for a specific reason tied to the topic, then `outro` pointing to a related video ("watch this next")."""

SHORT_FORMAT = """Format: YouTube Short, {min_words}-{max_words} words total (35-60 seconds).
Structure: hook (1 line), 3-6 fast lines delivering ONE concrete idea, and a satisfying result. Demonstrate tools with SCREEN footage; use FLOW or COMPARE for explanations. No stock montage, generic intro, chapters, verdict section or filler. A `cta` is optional and must give a useful next step. {affiliate}"""

VERDICT_RULE = ("exactly one `verdict` line, about the video's main tool, based on the owner's own opinion. "
                "Owner's notes: {notes}. If the notes don't cover the main tool, the verdict line's text must be "
                'only "[YOUR VERDICT: 2-3 sentences on what you think of <tool>]" with nothing before or after it. '
                "The owner fills it in; never write an opinion for them.")
AFFILIATE_RULE = ("If the video naturally covers one of these tools, which have affiliate programs: {tools}, add one "
                  "`affiliate` line that recommends it as worth trying, says the link is in the description, and "
                  "makes no promises about results. Otherwise add no affiliate line. Never force a tool in.")


def format_brief(cfg: dict, fmt: str, notes: dict[str, str]) -> str:
    """Length and structure rules for a long video or a Short. Reused by the revise step."""
    w = cfg["writing"]
    note_text = "; ".join(f"{tool}: {opinion}" for tool, opinion in notes.items()) or "none yet"
    affiliate = AFFILIATE_RULE.format(tools=", ".join(cfg["affiliate_tools"]))
    if fmt == "long":
        lo, hi = w["long_words"]
        return LONG_FORMAT.format(min_words=lo, max_words=hi, minutes=round((lo + hi) / 2 / w["words_per_minute"]),
                                  min_lines=lo // 25, max_lines=hi // 22,
                                  verdict=VERDICT_RULE.format(notes=note_text), affiliate=affiliate)
    lo, hi = w["short_words"]
    return SHORT_FORMAT.format(min_words=lo, max_words=hi, affiliate=affiliate)


def script_prompt(cfg: dict, idea: dict, source: dict, notes: dict[str, str]) -> tuple[str, str]:
    fmt = format_brief(cfg, idea["format"], notes)
    system = f"{channel_context(cfg)}\n\nYou are the channel's head scriptwriter.\n\n{script_rules(cfg, idea['format'])}"
    prompt = f"""Write a script for this topic.

Topic idea: {idea['topic']}
Why it's in demand: {'; '.join(idea['reasons'])}
What people search on YouTube around it: {', '.join(idea['searches'][:15]) or 'n/a'}

Source video (evidence of demand only; do not copy):
Title: {source.get('title', '')}
Description: {source.get('description', '')[:1500]}

{fmt}"""
    return system, prompt


def review_prompt(cfg: dict, script_text: str, fmt: str, word_count: int, code_issues: list[str]) -> tuple[str, str]:
    system = (f"{channel_context(cfg)}\n\nYou are a strict YouTube script editor. You have seen thousands of "
              f"scripts fail. Score honestly: 7 means publishable, 9+ is rare. Every issue needs a concrete fix "
              f"that names the line.")
    prompt = f"""Review this {fmt} script ({word_count} words).

Check: hook strength in the first line; pacing, pattern breaks and open loops; factual accuracy and outdated claims; product or tool names that may not exist; invented personal experiences, clients, case studies or results (the narrator is anonymous and has none: score policy 5 or lower if any appear); repeated points or lines; consistent structure (numbered tiers, steps or "level one/two" labels count up without gaps or repeats, and every promise in the intro is delivered); income claims stronger than "possible" (for example "replace your job"); originality of the angle; YouTube policy risks (misleading money claims, harmful advice); clarity when heard once; whether each visual cue SHOWS what its line says (with the sound off, would the viewer still follow?). Flag generic visuals that could sit under any line, and STOCK cues for things stock footage can't show (software, AI, dashboards); say which CHAT/CODE/LIST/TEXT cue to use instead.
"[YOUR VERDICT: ...]" placeholders are intentional: the owner fills them in. Never ask for them to be filled, and don't lower scores for them.
{RETENTION_RULES}
Automated checks already found: {'; '.join(code_issues) or 'nothing'}.
List every time-sensitive claim (price, version, date, statistic, feature that may have changed) in `unverifiable`.

Script, one line per row as [index] (section) text || visual cue:
{script_text}"""
    return system, prompt


def revise_prompt(cfg: dict, script_text: str, fmt: str, issues: list[str], format_brief: str) -> tuple[str, str]:
    system = (f"{channel_context(cfg)}\n\nYou are the channel's head scriptwriter, revising after an edit.\n\n"
              f"{script_rules(cfg, fmt)}")
    issue_list = "\n".join(f"- {i}" for i in issues)
    prompt = f"""Fix every issue below with targeted edits. Leave lines that already work untouched.
Line numbers refer to the script as shown. To lengthen a script, insert new lines; to shorten it, delete or replace lines.
Every new line needs a section and a visual cue, and the same chapter/checks rules as the original.

Issues:
{issue_list}

Format the script must meet:
{format_brief}

Script, one line per row as [index] (section) text || visual cue:
{script_text}"""
    return system, prompt


def seo_prompt(cfg: dict, script: dict, fmt: str, searches: list[str], source_title: str) -> tuple[str, str]:
    system = (f"{channel_context(cfg)}\n\nYou are a YouTube SEO and packaging expert. Titles and thumbnails "
              f"decide whether a video gets clicked; descriptions and tags help search find it.")
    title_rule = ("50 characters or fewer, curiosity plus a clear benefit" if fmt == "short"
                  else "60 characters or fewer, main keyword in the first 3 words, curiosity plus a clear benefit")
    prompt = f"""Package this {fmt} video.

Working title: {script['working_title']}
Angle: {script['angle']}
Tools covered: {', '.join(script['tools']) or 'none'}
Chapters: {', '.join(c for c in script['chapters']) or 'none'}
Real YouTube searches around this topic (use their wording for keywords and tags): {', '.join(searches[:25]) or 'n/a'}
Competing video title (do not copy): {source_title}

Write:
- 3 different title options ({title_rule}). No clickbait the video doesn't deliver. No ALL CAPS sentences. click_score is your honest 0-10 prediction.
- description: the first 2 sentences (shown before "more") hook the viewer and include the main keyword; then 2-4 short paragraphs on what they'll learn. {'Keep it to 2-3 sentences total.' if fmt == 'short' else '120-250 words.'} No links, no chapters, no hashtags; those are added automatically.
- tags: 10-15, mixing broad and long-tail phrases people actually search.
- hashtags: 3, without spaces{', include #shorts' if fmt == 'short' else ''}.
- pinned_comment: a question that gets viewers to reply with their own experience.
- end_screen: 2 kinds of follow-up videos to point viewers to.
- thumbnail_text: 3 options, 4 words or fewer, different from the title."""
    return system, prompt
