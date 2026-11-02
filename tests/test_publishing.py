import json

import pytest

from publishing.registry import folder_of
from publishing.upload_pack import TAG_LIMIT, fit_tags, real_chapters
from writing.models import Line, Script


def test_fit_tags_dedupes_and_respects_youtube_limit():
    tags = fit_tags(["ai tools", "ai tools", "notion"] + [f"long tag number {i}" for i in range(60)])
    assert tags[:2] == ["ai tools", "notion"]
    assert sum(len(t) + (2 if " " in t else 0) for t in tags) + len(tags) - 1 <= TAG_LIMIT


def test_real_chapters_use_recorded_times(tmp_path):
    lines = [Line(section="hook", text="h", visual="TEXT: h"), Line(section="intro", text="i", visual="TEXT: i", chapter="Intro"),
             Line(section="point", text="a", visual="TEXT: a", chapter="First"),
             Line(section="point", text="b", visual="TEXT: b", chapter="Too soon"),
             Line(section="point", text="c", visual="TEXT: c", chapter="Second")]
    script = Script(working_title="t", angle="a", tools=[], lines=lines)
    starts = [0, 3, 40, 45, 95]
    (tmp_path / "timings.json").write_text(json.dumps({"lines": [{"line": i, "start": s, "end": s + 2} for i, s in enumerate(starts)]}))
    assert real_chapters(tmp_path, script) == [(0, "Intro"), (40, "First"), (95, "Second")]  # 45s is <10s after 40s


def test_item_ids_cannot_escape_the_videos_folder():
    with pytest.raises(ValueError):
        folder_of("../../core")
