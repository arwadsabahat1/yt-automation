"""Voice Studio: turns an approved script into narration with natural pauses and YouTube-standard loudness,
and records when each line starts so footage and captions can follow the voice."""
import json
import re
from pathlib import Path

import numpy as np
import soundfile as sf

from core.config import DATA_DIR
from core.log import get_logger
from core.media import run
from writing.models import Script

log = get_logger("voice")

MODEL = DATA_DIR / "models" / "kokoro-v1.0.int8.onnx"
VOICES = DATA_DIR / "models" / "voices-v1.0.bin"
PLACEHOLDER_RE = re.compile(r"\[YOUR VERDICT:[^\]]*\]")

# Written forms the speech model tends to misread. Applied in order.
SPOKEN = [
    # The abbreviation's period makes TTS pause after the letter T.
    # Keep the display spelling in scripts/captions; speak the two syllables.
    (re.compile(r"\bT(?:\.\s*|\s*[-\u2011]\s*|\s+)rex\b", re.I), lambda m: "Tee rex"),
    (re.compile(r"\$(\d[\d,]*)(?:\.(\d+))?\s*[kK]\b"), lambda m: f"{m.group(1)} thousand dollars"),
    (re.compile(r"\$(\d[\d,]*(?:\.\d+)?)"), lambda m: f"{m.group(1)} dollars"),
    (re.compile(r"/\s*(month|mo)\b", re.I), lambda m: " per month"),
    (re.compile(r"/\s*(year|yr)\b", re.I), lambda m: " per year"),
    (re.compile(r"/\s*(week|wk)\b", re.I), lambda m: " per week"),
    (re.compile(r"(\d)\s*%"), lambda m: f"{m.group(1)} percent"),
    (re.compile(r"(\d)\s*[xX]\b"), lambda m: f"{m.group(1)} times"),
    (re.compile(r"\s*&\s*"), lambda m: " and "),
    (re.compile(r"[‐-―]"), lambda m: "-"),  # unicode dashes
]


def spoken(text: str) -> str:
    for pattern, repl in SPOKEN:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()


class VoiceStudio:
    def __init__(self, cfg: dict):
        from kokoro_onnx import Kokoro  # slow import; only load when voicing
        self.cfg = cfg["voice"]
        self.lang = "en-gb" if self.cfg["voice"].startswith("b") else "en-us"
        self.tts = Kokoro(str(MODEL), str(VOICES))

    def narrate(self, script: Script, folder: Path) -> dict:
        """Writes voice.wav and timings.json in `folder`. Returns the timings."""
        c = self.cfg
        pieces, timings, cursor, sample_rate = [], [], 0.0, 24000
        for i, line in enumerate(script.lines):
            text = PLACEHOLDER_RE.sub("", line.text).strip()
            if not text:
                log.warning("Line %d has no text to speak (unfilled verdict?), skipping it", i)
                continue
            pause = c["chapter_pause"] if line.chapter and i > 0 else c["line_pause"]
            if timings:  # no silence before the hook
                pieces.append(np.zeros(int(pause * sample_rate), dtype=np.float32))
                cursor += pause
            samples, sample_rate = self.tts.create(spoken(text), voice=c["voice"], speed=c["speed"], lang=self.lang)
            length = len(samples) / sample_rate
            timings.append({"line": i, "start": round(cursor, 3), "end": round(cursor + length, 3), "text": line.text})
            pieces.append(samples.astype(np.float32))
            cursor += length

        raw = folder / "voice_raw.wav"
        sf.write(raw, np.concatenate(pieces), sample_rate)
        # Even out loudness to YouTube's target; 48 kHz matches the final video's audio track.
        run(["-i", str(raw), "-af", f"loudnorm=I={c['loudness']}:TP=-1.5:LRA=11", "-ar", "48000", str(folder / "voice.wav")])
        raw.unlink()
        result = {"voice": c["voice"], "duration": round(cursor, 3), "lines": timings}
        (folder / "timings.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Voiced %d lines, %.0fs -> %s", len(timings), cursor, folder / "voice.wav")
        return result
