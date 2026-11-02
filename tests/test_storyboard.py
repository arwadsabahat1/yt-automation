import json

import pytest
from pydantic import ValidationError

from production.assembler import plan_shots
from production.stock import StockLibrary
from production.storyboard import ProductionBlocked, build_storyboard, preflight, recording_path
from writing.models import Line, Script
from writing.quality_gate import code_checks


CFG = {"writing": {"long_words": [1, 300], "short_words": [1, 300], "words_per_minute": 120,
                   "long_min_minutes": 0, "max_screen_clips": {"long": 1, "short": 1}, "max_similarity": 0.15},
       "render": {"tail_seconds": 0.6, "end_screen_seconds": 8, "hook_seconds": 30,
                  "hook_cut_seconds": 2.5, "cut_seconds": 4.5, "screen_shot_seconds": 12},
       "editorial": {"require_demo_for_tools": True}}


def script(*lines, tools=None):
    return Script(working_title="A real example", angle="Show the result", tools=tools or [], lines=list(lines))


def test_tool_video_requires_a_real_demo_even_with_a_strong_script_score(tmp_path):
    s = script(Line(section="hook", text="Watch the result.", visual="COMPARE: Manual | Assisted | Work: type / review"),
               tools=["Notion"])
    report = build_storyboard(s, "short", CFG, tmp_path)
    assert report.blocked
    assert any("real SCREEN" in i.detail for i in report.issues)


def test_concept_explainer_can_use_diagrams_without_a_product_walkthrough(tmp_path):
    s = script(Line(section="hook", text="Here is how the process works.", visual="FLOW: Question | Context | Answer"),
               tools=["Notion"])
    s.presentation = "explainer"
    assert not build_storyboard(s, "short", CFG, tmp_path).blocked


def test_missing_footage_is_reported_before_any_render(tmp_path):
    s = script(Line(section="hook", text="Watch the result.", visual="SCREEN: export the final clip", recording="demo.mp4"))
    with pytest.raises(ProductionBlocked, match="Recording needed"):
        preflight(s, "short", CFG, tmp_path)
    assert "screen/demo.mp4" in (tmp_path / "storyboard.md").read_text(encoding="utf-8")


def test_recording_paths_cannot_escape_screen_directory(tmp_path):
    for name in ("../../private.mp4", str(tmp_path / "elsewhere.mp4"), "nested/demo.mp4"):
        with pytest.raises(ValueError):
            recording_path(tmp_path, name)
    with pytest.raises(ValidationError):
        Line(section="point", text="Example", visual="SCREEN: result", recording="../private.mp4")


def test_reused_demo_counts_as_one_recording_and_preserves_source_offsets():
    s = script(*[Line(section="hook" if i == 0 else "point", text="Show the real step.", visual="SCREEN: step",
                           chapter=f"Step {i}" if i else None, recording="demo.mp4", recording_start=i * 10)
                 for i in range(4)])
    assert not any("recordings, maximum" in i or "four SCREEN" in i for i in code_checks(s, "long", CFG, []))
    timing = {"lines": [{"line": i, "start": i * 4.0, "end": i * 4.0 + 3.5} for i in range(4)]}
    shots = plan_shots(s, timing, "short", CFG)
    assert len(shots) == 4  # the real action is not arbitrarily cut every 2.5 seconds
    assert {s.recording for s in shots} == {"demo.mp4"}
    assert [s.offset for s in shots] == [0, 10, 20, 30]


def test_edited_narration_is_not_silently_reused(tmp_path):
    s = script(Line(section="hook", text="The corrected explanation.", visual="TEXT: corrected"))
    timing = {"duration": 3, "lines": [{"line": 0, "start": 0, "end": 3, "text": "The old explanation."}]}
    assert any(i.status == "fail" and i.name == "Stale narration"
               for i in build_storyboard(s, "short", CFG, tmp_path, timing).issues)


def test_recordings_must_cover_the_tail_without_looping(tmp_path, monkeypatch):
    (tmp_path / "screen").mkdir()
    (tmp_path / "screen" / "demo.mp4").touch()
    monkeypatch.setattr("core.media.duration", lambda path: 10.0)
    s = script(Line(section="hook", text="Show the final export.", visual="SCREEN: result",
                    recording="demo.mp4", recording_start=8))
    timing = {"duration": 2, "lines": [{"line": 0, "start": 0, "end": 2, "text": s.lines[0].text}]}
    with pytest.raises(ProductionBlocked, match="Recording too short"):
        preflight(s, "short", CFG, tmp_path, timing)


def test_measured_short_length_overrides_word_count_estimate(tmp_path):
    cfg = {**CFG, "writing": {**CFG["writing"], "short_words": [90, 150]}}
    s = script(Line(section="hook", text="A complete example.", visual="FLOW: Record | Review"),
               Line(section="point", text="word " * 165, visual="LIST: one | two"))
    timing = {"duration": 45, "lines": [{"line": i, "start": i * 5, "end": 5 if i == 0 else 45,
                                        "text": line.text} for i, line in enumerate(s.lines)]}
    assert not build_storyboard(s, "short", cfg, tmp_path, timing).blocked


def test_irrelevant_fallback_is_never_used_for_subject_footage():
    stock = StockLibrary.__new__(StockLibrary)
    stock.fallbacks, stock.domain, stock.used = ["technology abstract"], {"computer"}, set()
    queries = []
    def search(query):
        queries.append(query)
        return [{"id": 1, "tags": "technology abstract", "duration": 10,
                 "videos": {"large": {"url": "https://example.test/clip", "width": 1920, "height": 1080}}}]
    stock._search = search
    assert stock.find("hourglass sand", 3, strict=True) is None
    assert "technology abstract" not in queries


def test_malformed_diagrams_are_caught_before_rendering():
    s = script(Line(section="hook", text="Compare these choices.", visual="COMPARE: A | B | price: one"),
               Line(section="point", text="Follow this process.", visual="FLOW: one"))
    issues = code_checks(s, "short", CFG, [])
    assert any("COMPARE needs" in i for i in issues)
    assert any("FLOW needs" in i for i in issues)


def test_cut_short_preserves_source_file_identity_and_offsets(tmp_path, monkeypatch):
    from production.shorts import Segment, build_short
    (tmp_path / "screen").mkdir()
    (tmp_path / "screen" / "02.mp4").write_bytes(b"second original recording")
    (tmp_path / "screen" / "demo.mp4").write_bytes(b"named recording")
    s = script(Line(section="hook", text="Start with the result.", visual="SCREEN: first recording"),
               Line(section="point", text="Explain the next step.", visual="FLOW: Start | Finish"),
               Line(section="point", text="Here is the export.", visual="SCREEN: second recording"),
               Line(section="point", text="Check the final result.", visual="SCREEN: named recording",
                    recording="demo.mp4", recording_start=12))
    s.presentation = "comparison"
    timing = {"voice": "am_adam", "duration": 40,
              "lines": [{"line": i, "start": i * 10, "end": i * 10 + 9, "text": l.text}
                        for i, l in enumerate(s.lines)]}
    monkeypatch.setattr("production.shorts.run", lambda *args, **kwargs: None)
    folder = build_short(tmp_path, 1, Segment(start_line=2, end_line=3, hook="Check the export", title="Real result"),
                         s, timing, "Full video", {"shorts": {"hashtags": ["#shorts"]}})
    pack = json.loads((folder / "pack.json").read_text(encoding="utf-8"))
    lines = pack["script"]["lines"]
    assert lines[0]["section"] == "hook" and lines[0]["visual"] == "SCREEN: second recording"
    assert [l["recording"] for l in lines] == ["02.mp4", "demo.mp4"]
    assert lines[1]["recording_start"] == 12
    assert (folder / "screen" / "02.mp4").read_bytes() == b"second original recording"
    assert pack["script"]["presentation"] == "comparison"


def test_llm_short_selection_rejects_overlapping_passages():
    from production.shorts import Segment, ShortsPlan, llm_segments
    s = script(*[Line(section="point", text="A complete opening sentence.", visual="LIST: one | two") for _ in range(10)])
    timing = {"lines": [{"line": i, "start": i * 10, "end": i * 10 + 9} for i in range(10)]}
    plan = ShortsPlan(segments=[Segment(start_line=a, end_line=b, hook="h", title="t")
                                for a, b in ((0, 3), (2, 5), (6, 9))])
    class Picker:
        def json(self, *args):
            return plan
    chosen = llm_segments({"niche": "AI tools"}, Picker(), s, timing, 3, 30, 58)
    assert [(p.start_line, p.end_line) for p in chosen] == [(0, 3), (6, 9)]


def test_publish_checks_block_stock_substituted_for_a_demo(tmp_path, monkeypatch):
    from publishing.checks import blocking, run_checks
    (tmp_path / "video.mp4").touch()
    s = script(Line(section="hook", text="Show the real result.", visual="SCREEN: product result", recording="demo.mp4"))
    (tmp_path / "pack.json").write_text(json.dumps({"script": s.model_dump()}), encoding="utf-8")
    (tmp_path / "render.json").write_text(json.dumps({"shots": [{"kind": "stock_for_screen", "screen_no": 1}]}), encoding="utf-8")
    monkeypatch.setattr("publishing.checks.probe", lambda path: {"streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920},
        {"codec_type": "audio", "codec_name": "aac"}], "format": {"duration": 35, "size": 1000}})
    monkeypatch.setattr("publishing.checks.scan_audio_video", lambda path: {"lufs": -14, "peak": -1,
                                                                          "black": [], "silence": []})
    checks = run_checks(tmp_path, "short", CFG)
    assert blocking(checks)
    assert any(c.name == "Screen recordings" and c.status == "fail" for c in checks)
