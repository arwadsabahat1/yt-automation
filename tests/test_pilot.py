from writing.models import SEOPack


def test_editorial_titles_can_prepare_an_upload_without_invented_click_scores():
    seo = SEOPack.model_validate({
        "titles": [{"title": f"Title {n}", "why": "Editorial option; untested."} for n in range(3)],
        "description": "An actual worked example.", "tags": [], "hashtags": [],
        "pinned_comment": "Which step helped?", "end_screen": [], "thumbnail_text": ["FIX YOUR CLIPS"]
    })
    assert all(title.click_score is None for title in seo.titles)


def test_derived_short_keeps_the_focus_of_its_selected_source_lines(tmp_path, monkeypatch):
    import json
    from production import shorts
    from core.config import load_config
    from writing.models import Line, Script
    (tmp_path / "screen").mkdir()
    (tmp_path / "screen" / "demo.mp4").write_bytes(b"source recording")
    focus = {"0": {"x": 0, "y": 0, "width": 1920, "height": 1080},
             "1": {"x": 1100, "y": 300, "width": 700, "height": 400}}
    (tmp_path / "screen-framing.json").write_text(json.dumps(focus))
    script = Script(working_title="Source", angle="A complete point", tools=[], lines=[
        Line(section="intro", text="Earlier setup.", visual="TEXT: Earlier"),
        Line(section="point", text="The actual useful opening.", visual="SCREEN: Inspect transcript", recording="demo.mp4", recording_start=12),
        Line(section="point", text="And its complete answer.", visual="FLOW: Check | Finish")])
    timing = {"voice": "am_adam", "lines": [{"line": i, "start": i*10, "end": i*10+9, "text": l.text} for i,l in enumerate(script.lines)]}
    monkeypatch.setattr(shorts, "run", lambda *args, **kwargs: None)
    folder = shorts.build_short(tmp_path, 1, shorts.Segment(start_line=1,end_line=2,hook="Keep the subject",title="A point"),script,timing,"Source",load_config())
    assert json.loads((folder / "screen-framing.json").read_text()) == {"0":focus["1"]}
    cut = json.loads((folder / "pack.json").read_text())["script"]["lines"][0]
    assert cut["recording_start"] == 12 and cut["visual"].startswith("SCREEN:")
