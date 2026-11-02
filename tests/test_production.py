from production.assembler import cue_kind, plan_shots
from production.captions import align, ass_time, bgr, chunks
from production.stock import simpler_queries
from production.voice import spoken
from writing.models import Line, Script


def test_spoken_keeps_t_rex_together_without_a_sentence_break():
    assert spoken("T. rex could bite. T-rex and T rex.") == "Tee rex could bite. Tee rex and Tee rex."
    assert spoken("Meet Tyrannosaurus rex.") == "Meet Tyrannosaurus rex."


def test_spoken_reads_money_rates_and_symbols_aloud():
    assert spoken("Charge $1,000/month for 12 clips.") == "Charge 1,000 dollars per month for 12 clips."
    assert spoken("A $50k mistake") == "A 50 thousand dollars mistake"
    assert spoken("Views rose 30% and CTR jumped 2.8x") == "Views rose 30 percent and CTR jumped 2.8 times"
    assert spoken("Notion & Canva — both free") == "Notion and Canva - both free"


RENDER = {"render": {"tail_seconds": 0.5, "hook_cut_seconds": 2.5, "cut_seconds": 4.5, "hook_seconds": 30,
                     "end_screen_seconds": 15}}


def make_script(visuals):
    return Script(working_title="t", angle="a", tools=[],
                  lines=[Line(section="point", text="word " * 5, visual=v) for v in visuals])


def test_cue_kind():
    assert cue_kind("STOCK: laptop typing") == ("stock", "laptop typing")
    assert cue_kind("text: $1,000 a month") == ("text", "$1,000 a month")
    assert cue_kind("a sunset") == ("stock", "a sunset")


def test_plan_shots_covers_timeline_without_gaps_and_cuts_fast_in_hook():
    script = make_script(["STOCK: laptop", "TEXT: big number", "SCREEN: dashboard", "STOCK: office"])
    timings = {"lines": [{"line": 0, "start": 0.0, "end": 6.0}, {"line": 1, "start": 6.4, "end": 9.0},
                         {"line": 2, "start": 9.4, "end": 14.0}, {"line": 3, "start": 40.0, "end": 49.0}]}
    shots = plan_shots(script, timings, "long", RENDER)
    assert shots[0].start == 0 and all(a.end == b.start for a, b in zip(shots, shots[1:]))
    assert [s.kind for s in shots[:3]] == ["stock"] * 3  # 6.4s line in the hook: three 2.1s cuts
    assert shots[3].kind == "text" and shots[4].kind == "screen" and shots[4].screen_no == 1
    screen = [s for s in shots if s.kind == "screen"]
    assert len(screen) > 1 and {s.screen_no for s in screen} == {1}  # cut up, but one recording
    assert all(abs(s.offset - (s.start - screen[0].start)) < 0.01 for s in screen)  # plays on continuously
    assert shots[-1].kind == "end" and shots[-1].length == 15
    assert max(s.length for s in shots if s.kind == "stock" and s.start > 30) <= 4.5


def test_shorts_have_no_end_screen():
    shots = plan_shots(make_script(["STOCK: a"]), {"lines": [{"line": 0, "start": 0, "end": 5}]}, "short", RENDER)
    assert shots[-1].kind == "stock"


def test_align_keeps_script_spelling_and_fills_unheard_words():
    heard = [("Try", 0.0, 0.3), ("opus", 0.35, 0.6), ("clip", 0.6, 0.9), ("today", 1.6, 2.0)]
    words = align(["Try", "Opus", "Clip", "for", "$20", "today."], heard, 0.0, 2.0)
    assert [w[0] for w in words] == ["Try", "Opus", "Clip", "for", "$20", "today."]
    assert words[1][1] == 0.35 and words[5][1] == 1.6
    assert 0.9 <= words[3][1] < words[4][1] < 1.6  # unheard words spread between known times


def test_chunks_break_on_punctuation_and_size():
    words = [(w, i, i + 1) for i, w in enumerate("Stop. Posting random clips without a plan".split())]
    assert [[w[0] for w in g] for g in chunks(words, 3, 20)] == [["Stop."], ["Posting", "random", "clips"],
                                                                  ["without", "a", "plan"]]


def test_ass_helpers():
    assert ass_time(3725.456) == "1:02:05.46"
    assert bgr("#FFD400") == "&H0000D4FF"


def test_simpler_queries_end_with_short_terms():
    qs = simpler_queries("stressed programmer staring at computer screen")
    assert qs[0] == "stressed programmer staring at computer screen"
    assert all("programmer" in q for q in qs)  # every variant keeps the subject


def test_relevance_rejects_clips_matching_one_ambiguous_word():
    from production.stock import key_words, relevance
    wanted = key_words("pull request dashboard with automated AI test checks")
    car = {"tags": "car, dashboard, interior, driving"}
    tech = {"tags": "dashboard, analytics, test, software"}
    assert relevance(car, wanted) == 1
    assert relevance(tech, wanted) == 2


def test_tech_cues_reject_clips_without_tech_tags():
    from production.stock import key_words, relevance
    wanted = key_words("multi agent coding environment running tasks")
    domain = key_words("computer laptop code coding software screen ai")
    antelope = {"tags": "antelope, running, environment, wildlife"}
    assert relevance(antelope, wanted) >= 2 and relevance(antelope, domain) == 0
    assert "ai" in key_words("AI tools for work")


def test_parse_chat_and_parts():
    from production.graphics import parse_chat, parts
    assert parse_chat("user: Fix auth.py | ai: Which file?") == ("Fix auth.py", "Which file?")
    assert parse_chat("just a prompt") == ("just a prompt", "")
    assert parts("a | b |  | c") == ["a", "b", "c"]


def test_graphics_render_every_kind_in_both_formats():
    from production.graphics import RENDERERS, Canvas
    style = {"accent": "#FFD400", "panel": "#0E0E14"}
    cues = {"chat": "user: hi | ai: hello there", "code": "run | ✓ done | ✗ error: failed", "list": "one | two",
            "flow": "Record | Select | Review", "compare": "Manual | Assisted | Captions: type / review"}
    for size in ((1920, 1080), (1080, 1920)):
        for kind, cue in cues.items():
            frames = RENDERERS[kind](cue, Canvas(size, style), 1.0)
            assert len(frames) == 15 and frames[-1].size == size
            assert frames[-1].getbbox()  # something was drawn


def test_thumbnail_text_helpers():
    from production.thumbnails import key_word, phone_cap_height, split_lines
    assert split_lines(["CLIENTS", ">", "VIEWS"], 2) == [["CLIENTS"], [">", "VIEWS"]]
    assert split_lines(["ONE"], 3) == [["ONE"]]
    assert key_word(["$2", "PER", "1K?"]) == 0
    assert key_word(["AI", "CLIPPING", "TRUTH"]) == 2
    assert phone_cap_height(216) > phone_cap_height(100) > 0


def test_plausible_face_rejects_hands_shape_and_low_confidence():
    from production.thumbnails import plausible_face
    face = [100, 100, 100, 130, 125, 140, 175, 141, 150, 170, 130, 195, 170, 196, 0.95]
    assert plausible_face(face)
    assert not plausible_face(face[:14] + [0.8])                      # unsure
    assert not plausible_face([100, 100, 380, 200] + face[4:])          # wide blob, eyes misplaced
    assert not plausible_face(face[:5] + [200] + face[6:])              # eyes not level


def test_heuristic_shorts_are_in_range_non_overlapping_and_skip_cta():
    from production.shorts import heuristic_segments, segment_seconds
    lines = [Line(section="hook", text="Hook with 3 numbers", visual="TEXT: x")]
    lines += [Line(section="point", text=f"Point {i} costs $5", visual="LIST: a | b", chapter="Ch" if i % 6 == 0 else None)
              for i in range(1, 25)]
    lines += [Line(section="cta", text="Subscribe", visual="TEXT: sub")]
    script = Script(working_title="t", angle="a", tools=[], lines=lines)
    timings = {"lines": [{"line": i, "start": i * 8.0, "end": i * 8.0 + 7.5} for i in range(len(lines))]}
    segs = heuristic_segments(script, timings, 3, 30, 58)
    assert len(segs) == 3
    for a, b in zip(segs, segs[1:]):
        assert a.end_line < b.start_line
    for s in segs:
        assert 30 <= segment_seconds(timings, s.start_line, s.end_line) <= 58
        assert s.end_line < len(lines) - 1  # never the cta


def test_stock_subject_skips_describing_words_and_survives_simplification():
    from production.stock import simpler_queries, subject
    assert subject("hourglass sand running out") == "hourglass"
    assert subject("frustrated man head in hands desk") == "man"
    assert subject("smiling woman reading laptop") == "woman"
    assert all("hourglass" in q for q in simpler_queries("hourglass sand running out"))
