import subprocess

import pytest

from production import motion


def test_local_renderer_does_not_inherit_cloud_provider_credentials(monkeypatch):
    for name in ("GEMINI_API_KEY", "OPENAI_API_KEY", "HEYGEN_API_KEY", "PIXABAY_API_KEY"):
        monkeypatch.setenv(name, "must-not-reach-the-renderer")
    monkeypatch.setattr(motion, "tool", lambda name: "tools/ffmpeg.exe")
    env = motion.motion_environment()
    assert not any(env.get(k) == "must-not-reach-the-renderer" for k in env)
    assert env["DO_NOT_TRACK"] == "1"
    assert env["HYPERFRAMES_CONFIG_DIR"].startswith(str(motion.ROOT))
    assert env["HYPERFRAMES_HISTORY_ROOT"].startswith(str(motion.ROOT))


def test_checks_reject_a_skipped_browser_even_when_lint_passes(monkeypatch):
    monkeypatch.setattr(motion, "run_motion", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 0, '{"ok":true,"browserSkipped":true}', ""))
    with pytest.raises(RuntimeError, match="browser checks"):
        motion.check_motion()


def test_motion_sources_cannot_escape_the_video_folder(tmp_path):
    import json
    from production.storyboard import ProductionBlocked
    other = tmp_path / "outside.mp4"
    other.write_bytes(b"media")
    folder = tmp_path / "video"
    folder.mkdir()
    (folder / "motion-scenes.json").write_text(json.dumps({"2": {"1920x1080": {"file": "../outside.mp4"}}}))
    with pytest.raises(ProductionBlocked, match="motion source"):
        motion.scene_source(folder, 2, (1920, 1080))


def test_missing_mapped_motion_source_blocks_instead_of_becoming_filler(tmp_path):
    import json
    from production.storyboard import ProductionBlocked
    (tmp_path / "motion-scenes.json").write_text(json.dumps({"2": {"1920x1080": {"file": "motion/missing.mp4"}}}))
    with pytest.raises(ProductionBlocked, match="motion source"):
        motion.scene_source(tmp_path, 2, (1920, 1080))
    assert motion.scene_source(tmp_path, 2, (1080, 1920)) is None


def test_render_requires_completed_checks_and_uses_the_low_memory_profile(tmp_path, monkeypatch):
    out = tmp_path / "preview" / "video.mp4"
    monkeypatch.setattr(motion, "PREVIEW_VIDEO", out)
    monkeypatch.setattr(motion, "normalize_export_audio", lambda video: None)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        out.write_bytes(b"rendered output")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(motion, "run_motion", run)
    with pytest.raises(RuntimeError, match="browser check"):
        motion.render_motion({"ok": False})
    assert calls == []
    assert motion.render_motion({"ok": True, "browserSkipped": False}) == out
    assert calls[0][calls[0].index("--workers") + 1] == "1"
    assert "--low-memory-mode" in calls[0]
