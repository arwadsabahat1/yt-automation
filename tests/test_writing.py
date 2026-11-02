from core.llm import LLM, LLMError, Unavailable, extract_json
from writing.models import Line, Review, Script, SEOPack, TitleOption
from writing.quality_gate import code_checks, passes, similarity
from writing.seo import build_description, chapters, timestamp

CFG = {
    "affiliate_tools": ["notion", "canva"],
    "affiliate_links": {"notion": "https://example.com/notion"},
    "writing": {"long_words": [20, 60], "short_words": [5, 30], "words_per_minute": 60, "long_min_minutes": 0.3,
                "min_quality": 7.5, "max_rewrites": 2, "max_similarity": 0.15,
                "max_screen_clips": {"long": 2, "short": 1}},
}


def line(section="point", text="one two three four five six seven eight nine ten", visual="STOCK: busy office", chapter=None):
    return Line(section=section, text=text, visual=visual, chapter=chapter)


def long_script(**overrides) -> Script:
    lines = [line("hook", "Stop wasting hours on email.", "TEXT: 3 hours a day"),
             line("intro", chapter="Intro"), line(chapter="First tool", visual="LIST: sort | reply | archive"),
             line(chapter="Second tool", visual="CHAT: user: summarise my inbox | ai: 3 urgent emails"), line("cta")]
    return Script(**{"working_title": "t", "angle": "a", "tools": ["Notion"], "lines": lines, **overrides})


def review(**scores) -> Review:
    base = dict(hook=8, retention=8, accuracy=8, originality=8, policy=9, clarity=8)
    return Review(**{**base, **scores})


def test_clean_long_script_has_no_code_issues():
    assert code_checks(long_script(), "long", CFG, []) == []


def test_code_checks_catch_length_hook_visuals_and_chapters():
    s = long_script(lines=[line("intro", "too short", "a cat")])
    issues = " ".join(code_checks(s, "long", CFG, []))
    assert "Too short" in issues
    assert "first line must be the hook" in issues
    assert "visual cue" in issues
    assert "3 chapters" in issues


def test_code_checks_flag_repeated_scripts():
    s = long_script()
    assert any("Too similar" in i for i in code_checks(s, "long", CFG, [s.text]))


def test_similarity():
    a = "the quick brown fox jumps over the lazy dog again and again"
    assert similarity(a, a) == 1.0
    assert similarity(a, "completely different words about making videos with ai tools today") == 0.0


def test_passes_needs_score_policy_and_no_code_issues():
    assert passes(review(), [], CFG)
    assert not passes(review(), ["Too short"], CFG)
    assert not passes(review(policy=6, hook=10, retention=10), [], CFG)
    assert not passes(review(hook=5, retention=5, accuracy=5), [], CFG)


def test_timestamps():
    assert timestamp(0) == "0:00"
    assert timestamp(75) == "1:15"
    assert timestamp(3725) == "1:02:05"


def test_chapters_start_at_zero_and_follow_word_count():
    # 60 words per minute: each 10-word line is 10 seconds
    assert chapters(long_script(), 60) == [(0, "Intro"), (15, "First tool"), (25, "Second tool")]


def test_chapters_need_three():
    s = long_script(lines=[line("hook"), line("intro", chapter="Intro"), line(chapter="Only one more")])
    assert chapters(s, 60) == []


def test_description_has_chapters_links_disclosure_and_hashtags():
    pack = SEOPack(titles=[TitleOption(title=f"t{i}", click_score=7, why="w") for i in range(3)],
                   description="Body.", tags=["a"], hashtags=["ai", "#productivity"], pinned_comment="?",
                   end_screen=["x"], thumbnail_text=["y"])
    desc = build_description(pack, long_script(tools=["Notion", "Canva", "ChatGPT"]), CFG, "long")
    assert "0:00 Intro" in desc
    assert "Notion: https://example.com/notion" in desc
    assert "[ADD AFFILIATE LINK: canva]" in desc
    assert "ChatGPT" not in desc  # no affiliate program listed for it
    assert "affiliate links" in desc
    assert desc.endswith("#ai #productivity")


def test_extract_json_strips_fences_and_chatter():
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json('Sure! {"a": {"b": 2}} Hope that helps') == '{"a": {"b": 2}}'


class FakeLLM(LLM):
    """Providers answer from a script of canned replies; 'DOWN' means unavailable."""

    def __init__(self, replies: dict[str, list[str]]):
        self.providers = [{"name": n, "model": "m"} for n in replies]
        self.replies = replies
        self.last_used = ""

    def _call(self, provider, system, prompt):
        reply = self.replies[provider["name"]].pop(0)
        if reply == "DOWN":
            raise Unavailable("down")
        return reply


def test_llm_falls_through_to_next_provider():
    llm = FakeLLM({"gemini": ["DOWN"], "groq": ['{"title": "x", "click_score": 5, "why": "y"}']})
    assert llm.json("s", "p", TitleOption).title == "x"
    assert llm.last_used == "groq/m"


def test_llm_repairs_invalid_json_once():
    llm = FakeLLM({"gemini": ['{"title": "x"}', '{"title": "x", "click_score": 5, "why": "y"}']})
    assert llm.json("s", "p", TitleOption).click_score == 5


def test_llm_raises_when_everything_fails():
    llm = FakeLLM({"gemini": ["DOWN"], "groq": ["not json", "still not json"]})
    try:
        llm.json("s", "p", TitleOption)
    except LLMError:
        return
    raise AssertionError("expected LLMError")


def test_llm_drops_provider_whose_daily_quota_is_gone():
    class QuotaLLM(FakeLLM):
        def _call(self, provider, system, prompt):
            if provider["name"] == "gemini":
                from core.llm import QuotaExhausted
                raise QuotaExhausted("quota")
            return super()._call(provider, system, prompt)

    llm = QuotaLLM({"gemini": [], "groq": ['{"title": "x", "click_score": 5, "why": "y"}'] * 2})
    llm.json("s", "p", TitleOption)
    assert [p["name"] for p in llm.providers] == ["groq"]
    llm.json("s", "p", TitleOption)  # second call goes straight to groq


def test_apply_edits_keeps_indexes_valid():
    from writing.models import LineEdit, Revision
    from writing.quality_gate import apply_edits
    s = long_script()  # lines: hook, intro, point, point, cta
    new = line("pattern_break", "quick question for you", "TEXT: ?")
    revised = apply_edits(s, Revision(edits=[
        LineEdit(action="insert_after", line=1, new_lines=[new]),
        LineEdit(action="replace", line=4, new_lines=[line("cta", "subscribe for more", "TEXT: subscribe")]),
        LineEdit(action="delete", line=3),
        LineEdit(action="delete", line=99),  # out of range: skipped
    ]))
    assert [l.section for l in revised.lines] == ["hook", "intro", "pattern_break", "point", "cta"]
    assert revised.lines[-1].text == "subscribe for more"
    assert revised.tools == s.tools


def test_code_checks_catch_screen_overuse_duplicates_placeholders_and_invented_stories():
    s = long_script()
    s.lines[2].visual = s.lines[3].visual = "SCREEN: tool"
    s.lines.append(line("verdict", "[YOUR VERDICT: 2-3 sentences on Notion] It is great.", "SCREEN: notion"))
    s.lines.append(line("cta", "I helped three clients double their views, so subscribe.", "TEXT: subscribe"))
    issues = " ".join(code_checks(s, "long", CFG, []))
    assert "3 separate SCREEN recordings, maximum 2" in issues
    assert "2 `cta` lines" in issues
    assert "placeholder must stand alone" in issues
    assert "'I helped' is an invented personal story" in issues


def test_code_checks_allow_clean_placeholder_and_hypotheticals():
    s = long_script()
    s.lines.insert(4, line("verdict", "[YOUR VERDICT: 2-3 sentences on what you think of Notion]", "TEXT: verdict"))
    s.lines[2].text = "Say you run a coffee shop."
    assert code_checks(s, "long", CFG, []) == []


def test_code_checks_catch_repeated_chapters_and_i_use():
    s = long_script()
    s.lines[2].chapter = "Intro"
    s.lines[3].text = "I use it every day."
    issues = " ".join(code_checks(s, "long", CFG, []))
    assert "Chapter names used more than once: ['intro']" in issues
    assert "'I use' is an invented personal story" in issues


def test_code_checks_flag_long_videos_under_minimum_minutes():
    cfg = {**CFG, "writing": {**CFG["writing"], "long_min_minutes": 1}}  # 60 wpm: 1 minute = 60 words
    issues = " ".join(code_checks(long_script(), "long", cfg, []))
    assert "needs 1+ for mid-roll ads" in issues


def test_code_checks_reject_stock_for_software_and_long_runs():
    s = long_script()
    s.lines[1].visual = "STOCK: AI dashboard with agents"
    issues = " ".join(code_checks(s, "long", CFG, []))
    assert "STOCK cues ask for software" in issues
    s = long_script(lines=[line("hook", "Stop wasting hours.", "TEXT: 3 hours")] + [line(visual="TEXT: x")] * 4)
    assert "four TEXT visuals in a row" in " ".join(code_checks(s, "long", CFG, []))


def test_code_checks_flag_too_many_text_cards():
    lines = [line("hook", "Stop wasting hours.", "TEXT: 3 hours")]
    lines += [line(visual=v) for v in ["TEXT: a", "STOCK: busy office", "TEXT: b", "LIST: x | y", "TEXT: c", "STOCK: man desk"]]
    issues = " ".join(code_checks(long_script(lines=lines), "long", CFG, []))
    assert "use TEXT cards" in issues
